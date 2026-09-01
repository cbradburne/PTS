/*
 * esp_mount_amoled175 — Mount-side ESP-NOW/Teensy bridge with circular LVGL display
 *
 * Hardware: Waveshare ESP32-S3-Touch-AMOLED-1.75
 *   Display : SH8601Z QSPI  466×466 circular AMOLED
 *             CS=12  CLK=38  D0=4  D1=5  D2=6  D3=7  RST=39
 *   Touch   : CST9217  I2C addr=0x5A  SDA=15  SCL=14  INT=11  RST=40
 *   Teensy  : Serial1  TX=43 RX=44  115200 baud
 *
 * Board settings (Arduino IDE):
 *   Board             : ESP32S3 Dev Module
 *   USB CDC On Boot   : Enabled
 *   Flash Size        : 16MB
 *   PSRAM             : OPI PSRAM
 *
 * Required libraries (Library Manager):
 *   lvgl          by kisvegabor  (v8.3.x)
 *   Arduino_GFX   by moononournation  (needs Arduino_SH8601 driver)
 *
 * lv_conf.h must be in this sketch folder.
 *
 * UI layout (466×466 circle, centre 233,233):
 *   Status ring  — full circle border, 8px wide, colour = connection state
 *   PT arc       — right side, 15 min→2 min  (rotation=12,  bg_angles 0,78, REVERSE)
 *   SL arc       — left side,  45 min→58 min (rotation=270, bg_angles 0,78, NORMAL)
 *   4 detent dots per arc, positioned on R=190 from centre
 *   Centre text  — "CAM" label, large mount number (48pt), state, RSSI
 *   10 slot circles — two rows near the bottom
 *   Touch zones  — left half cycles PT preset, right half cycles SL preset
 */

#define LV_CONF_INCLUDE_SIMPLE
#include <Arduino.h>
#include <lvgl.h>
#include <Arduino_GFX_Library.h>
#include <TouchDrv.hpp>
#include <SensorQMI8658.hpp>
#include <esp_now.h>
#include <WiFi.h>
#include <esp_wifi.h>
#include <esp_task_wdt.h>
#include <Wire.h>
#include "../shared/protocol.h"
#include "../shared/crash_report.h"
#include "ble_camera.h"   // Blackmagic camera control over BLE
#include "ui_types.h"   // ArcStrip struct — must be included before Arduino auto-prototypes

// ---------------------------------------------------------------------------
// Configuration — RUNTIME, set from the on-screen SETUP (no per-unit flashing)
// ---------------------------------------------------------------------------
// Mount ID and the paired hub(s) live in NVS; one binary serves every mount.
//
//   Enter setup : touch & hold the screen ~1.5 s (auto-entered when unpaired)
//   Flow        : tap CAM 1-5  →  scan lists hubs (SSID/MAC/signal)  →  tap
//                 your hub  →  SAVE  →  mount restarts paired.
//
// The hub needs no action: it learns this mount's MAC from the first packet
// (pairing rules in esp32_hub.ino).  Multiple hubs (e.g. home + work) can be
// paired; at boot — and whenever the active hub goes silent — the mount scans
// for any known hub and follows it, including onto a changed WiFi channel.

#include <Preferences.h>

// Scan filter.  BOTH prefixes are accepted, deliberately:
//
//   "PTS-"      the named hubs and satellites (esp32_hub_eth).  Four characters
//               so a location name ("Concert Hall", "Foyer") still fits the 16
//               usable characters of KnownHub::ssid and shows in full.
//   "CamMount"  the current production hub (esp32_hub), which is not renamed.
//
// Accepting both avoids a flag day: this mount firmware works against a rig
// that has not been touched AND against a test hub on the new board, so the
// two can be rolled out independently instead of everything having to change
// in one go.  Drop "CamMount" once no unnamed hub remains.
#define HUB_SSID_PREFIX      "PTS-"
#define HUB_SSID_PREFIX_OLD  "CamMount"
// Saved list, in NVS.  Mounts tour the building, so this is a HISTORY of every
// hub/satellite the mount has been paired to — not a snapshot of what is nearby.
// Requirement: at least 8, at most 16.  At 24 bytes an entry, 16 costs 388
// bytes of NVS, so there is no reason to sit below the ceiling.
#define MAX_KNOWN_HUBS   16
// Scan results and setup-screen rows.  Bounded by the 1.75" round panel, and
// four is plenty: the scan keeps the STRONGEST four and drops the rest, and
// there should never be more than that within range of one mount.
#define MAX_SCAN_ROWS    4

struct KnownHub {
    uint8_t mac[6];       // hub softAP BSSID == its ESP-NOW address
    uint8_t channel;      // last known channel (self-heals via rescan)
    char    ssid[17];
};

struct MountCfg {
    uint8_t  magic;       // CFG_MAGIC when valid
    uint8_t  mount_id;    // 1-5
    uint8_t  n_hubs;      // entries used in hubs[]
    uint8_t  last_hub;    // index of the hub currently in use
    KnownHub hubs[MAX_KNOWN_HUBS];
};
#define CFG_MAGIC 0xC3

static MountCfg    _cfg = {};
static Preferences _mount_prefs;
static bool        _cfg_valid   = false;
static uint8_t     _mount_id    = 0;      // runtime mount ID (0 = unpaired)
static uint8_t     _hub_mac[6]  = {};     // active hub ESP-NOW address
static uint8_t     _hub_channel = 1;
static bool        _setup_active = false; // SETUP screen is showing (declared
                                          // early: gates main/level touch zones)
// Set by CMD_RESCAN_BASES, acted on in hub_reacquire_poll().  A flag rather
// than reaching into the reacquire state directly, because that state is
// declared several hundred lines below the packet handler that sets this.
static volatile bool _reacq_requested = false;
static uint32_t      _reacq_hold_ms   = 0;   // stagger, so mounts scan one at a time

static bool        _pair_active  = false; // camera-pairing screen is showing —
                                          // same reason, and the touch zones
                                          // must not fire underneath it either

static void cfg_apply_active_hub() {
    const KnownHub &h = _cfg.hubs[_cfg.last_hub];
    memcpy(_hub_mac, h.mac, 6);
    _hub_channel = h.channel;
}

static void cfg_save() {
    _mount_prefs.begin("mcfg", false);
    _mount_prefs.putBytes("cfg", &_cfg, sizeof(_cfg));
    _mount_prefs.end();
}

// ---------------------------------------------------------------------------
// Deferred config save
// ---------------------------------------------------------------------------
// cfg_save() writes the whole MountCfg — 16 hub records, ~390 bytes — through
// NVS, and a flash erase/write of that size blocks for a good fraction of a
// second.  Calling it straight from hub_reacquire_poll(), which runs in loop(),
// put that stall in the path of every jog, look-at update and CV correction:
// one mount showed 213 ms loop peaks against 9-11 ms on mounts that were not
// roaming.  On a motion controller that is a visible hitch, not a statistic.
//
// What a roam actually changes is which hub we prefer and its channel — a
// CACHE.  It is rediscovered by scanning on the next boot regardless, so
// writing it the instant it changes buys almost nothing, and a mount flapping
// between two hubs would write flash every time it moved.
//
// So: mark it dirty and write once the choice has held still, and only while
// the mount is idle.  Explicit pairing does NOT use this — that is a
// deliberate act by an operator and must survive the power being pulled a
// second later.
#define CFG_SAVE_SETTLE_MS  30000UL
static bool     _cfg_dirty       = false;
static uint32_t _cfg_dirty_since = 0;

static void cfg_save_deferred() {
    _cfg_dirty       = true;
    _cfg_dirty_since = millis();
}


static void cfg_load() {
    // Zero first: a short read leaves the unwritten tail as-is, and we want
    // unused hub slots empty rather than stale.
    memset(&_cfg, 0, sizeof(_cfg));
    _mount_prefs.begin("mcfg", false);
    size_t n = _mount_prefs.getBytes("cfg", &_cfg, sizeof(_cfg));
    _mount_prefs.end();

    // Accept a SHORT read.  hubs[] is the last member, so a config written when
    // MAX_KNOWN_HUBS was smaller is a valid prefix of the current layout — the
    // saved entries land in exactly the right slots.  Insisting on an exact
    // size, as this used to, meant raising MAX_KNOWN_HUBS unpaired every mount
    // in the building and someone had to walk round with a stepladder.
    //
    // KnownHub's own layout must not change for this to hold: growing the ARRAY
    // is compatible, changing the ELEMENT is not.
    const size_t hdr      = offsetof(MountCfg, hubs);
    const size_t slots_in = (n > hdr) ? (n - hdr) / sizeof(KnownHub) : 0;
    _cfg_valid = (n >= hdr + sizeof(KnownHub) && n <= sizeof(_cfg) &&
                  _cfg.magic == CFG_MAGIC &&
                  _cfg.mount_id >= 1 && _cfg.mount_id <= 5 &&
                  _cfg.n_hubs >= 1 && _cfg.n_hubs <= MAX_KNOWN_HUBS &&
                  // don't trust an n_hubs claiming more entries than were read
                  _cfg.n_hubs <= slots_in &&
                  _cfg.last_hub < _cfg.n_hubs);
    if (_cfg_valid) {
        _mount_id = _cfg.mount_id;
        cfg_apply_active_hub();
    } else {
        memset(&_cfg, 0, sizeof(_cfg));
        _mount_id = 0;
    }
}


// ---------------------------------------------------------------------------
// Pin definitions
// ---------------------------------------------------------------------------

// QSPI display — CO5300 466×466
#define LCD_CS      12
#define LCD_SCLK    38
#define LCD_SDIO0    4
#define LCD_SDIO1    5
#define LCD_SDIO2    6
#define LCD_SDIO3    7
#define LCD_RESET   39
// No separate BL pin — brightness via gfx->setBrightness()

// Touch — CST9217  I2C addr 0x5A
#define TOUCH_SDA   15
#define TOUCH_SCL   14
#define TOUCH_INT   11
#define TOUCH_RST   40
#define TOUCH_ADDR  0x5A

#define TEENSY_TX    43
#define TEENSY_RX    44
#define TEENSY_BAUD  115200

// ---------------------------------------------------------------------------
// Timeouts / intervals
// ---------------------------------------------------------------------------

#define WATCHDOG_MS           10000
#define HUB_TIMEOUT_MS         5000
#define HW_WDT_TIMEOUT_MS     30000          // hardware watchdog — reset if loop stalls
#define ESPNOW_RESTART_MS     (2UL*60UL*1000UL) // restart after 2 min with no hub contact
// ...unless nothing is being attempted, in which case there is nothing to wait
// for and 2 min is 2 min of a dead mount.
//
// The two minutes above exist to let the cheaper remedy work first: 4 consecutive
// send failures refresh the peer, 3 refreshes rebuild the stack, and at a 15 s
// minimum gap that is ~8 rebuild attempts inside the window.  Every one of those
// rungs is driven by the ESP-NOW SEND CALLBACK.
//
// On 2026-08-18 cam1 wedged with "the stack's TX queue was full".  esp_now_send()
// is refused at the call in that state, so the callback never fires — and the
// counters preserved through the restart prove what followed: txfail still 248,
// reinits still 9, exactly where they had been before it went quiet.  In 137
// seconds of isolation the ladder did not run once.  The mount waited two
// minutes for a remedy that cannot start, then rebooted and came back fine.
//
// So the window is chosen by whether the ladder is actually running.  If send
// failures or reinits are still accruing, something is being tried and it gets
// the full two minutes.  If neither has moved since the silence began, nothing
// is being tried and waiting only extends the outage.
//
// This is also what keeps a hub reboot on the long window: with the hub off, the
// mount's sends FAIL and the callback fires, so the counters move and the full
// two minutes applies — which is the case the long window was written for.
#define ESPNOW_RESTART_STALLED_MS  (20UL*1000UL)
// ...but only a few times.  Restarting does not fix a one-way link — a
// fresh-booted mount still could not receive (confirmed 2026-06-16) — so past
// this count the restarts are pure cost: they blink the camera, throw away the
// reacquire scan in progress, and reset every counter that would have shown
// how long the mount had been isolated.  One rig logged 30 of them in an hour
// on two mounts, which is where ~1550 [ANOMALY] reports each came from.
// After giving up the mount stays awake and keeps scanning, which is the only
// thing that can actually find it a way back.
#define ESPNOW_RESTART_MAX      3
#define ISOLATION_RTC_MAGIC     0xC0FFEE10UL
// RTC_NOINIT survives esp_restart() but is undefined after a power-on or
// brownout, so a magic word tells a preserved count from uninitialised RAM.
// That is the behaviour we want: a power cycle is the operator intervening,
// and it should hand the mount its full quota of attempts back.
RTC_NOINIT_ATTR static uint32_t _iso_magic;
RTC_NOINIT_ATTR static uint32_t _iso_restarts;

// Why the last restart happened, carried across it in RTC_NOINIT and reported
// once the mount is back on the air.  See CMD_MOUNT_EVENT: while a mount is
// isolated it cannot transmit, so the event is invisible in comms.log, and the
// restart that rescues it clears every counter that would have explained it.
#define MOUNT_EVT_MAGIC 0x4D0E5701UL
RTC_NOINIT_ATTR static uint32_t _evt_magic;
RTC_NOINIT_ATTR static uint8_t  _evt_kind;
RTC_NOINIT_ATTR static uint16_t _evt_txfail, _evt_reinits, _evt_rx_s, _evt_tx_s;
RTC_NOINIT_ATTR static uint16_t _evt_refused, _evt_txerr;
static bool _evt_pending = false;   // set at boot, cleared once sent
#define STATUS_HEARTBEAT_MS    5000
#define TEENSY_PROBE_MS        2000
#define TOUCH_POLL_MS            50

// ---------------------------------------------------------------------------
// ESP-NOW receive queue
// ---------------------------------------------------------------------------

#define ESPNOW_RX_DEPTH  16
#define ESPNOW_MAX_LEN   250

struct EspNowMsg {
    uint8_t data[ESPNOW_MAX_LEN];
    uint8_t len;
};
static QueueHandle_t _espnow_rx_q;

// ---------------------------------------------------------------------------
// Display — Arduino_GFX SH8601 driver
// ---------------------------------------------------------------------------

// QSPI bus
static Arduino_ESP32QSPI *_bus = new Arduino_ESP32QSPI(
    LCD_CS, LCD_SCLK, LCD_SDIO0, LCD_SDIO1, LCD_SDIO2, LCD_SDIO3);

// CO5300 466×466 — same chip as the 1.64" board, square variant
// offsets: col_offset1=6, row_offset1=0, col_offset2=0, row_offset2=0
static Arduino_CO5300 *_gfx = new Arduino_CO5300(
    _bus, LCD_RESET, 0 /*rotation*/, 466, 466, 6, 0, 0, 0);

#define SCR_W  466
#define SCR_H  466

// Draw buffers are heap-allocated from PSRAM in setup() via ps_malloc.
// Keeping them as pointers (not static arrays) removes them from the DRAM BSS segment.
// UI_PROFILE=1 — a temporary diagnostic, off by default.
//
// The mount's loop hits ~220 ms during a move, on every mount including the two
// with no camera at all, so it is UI cost rather than anything added recently.
// Two candidates that want opposite fixes: LVGL RENDERING is slow (the draw
// buffers are 37 KB each in PSRAM, and rendering into PSRAM on an S3 is
// bandwidth-bound), or the FLUSH is slow (466x466 over QSPI, 12 chunks for a
// full redraw).  Timing them apart is the only way to know which.
//
// It borrows node_u32, which for a mount is the ESP-NOW reinit count — static
// at 1 on a healthy rig, and the brake it exists to prove is already confirmed.
// Packed (lvgl_ms << 16) | flush_ms so one health line carries both, with no
// protocol change and no serial monitor, which a mount on a rig does not have.
#ifndef UI_PROFILE
#define UI_PROFILE 0
#endif
#if UI_PROFILE
static uint16_t _ui_lvgl_max_ms  = 0;   // worst lv_timer_handler(), whole call
static uint16_t _ui_flush_max_ms = 0;   // ...of which, worst time inside flush
static uint32_t _ui_flush_accum_us = 0; // this lv_timer_handler()'s flush total
// UI_PROFILE=2 answers the question mode 1 raised.  Moving the draw buffer from
// PSRAM to internal RAM took rendering from ~160 ms to ~150 ms — near enough
// nothing — so it is not memory bandwidth.  The remaining suspect is how MUCH
// is being redrawn: this panel is a full-circle status ring, two arcs with
// detent dots and ten round slot indicators, all anti-aliased, and if a small
// arc change invalidates most of the screen then all of it re-renders.
//
// So count the pixels LVGL was asked to redraw, per pass, in kilopixels — the
// whole screen is 217 kpx, which fits a uint16 with room to spare.
//   area near 217 kpx  -> invalidation is the problem, not the drawing
//   area small, still slow -> the round anti-aliased shapes are the cost
static uint32_t _ui_inval_accum_px = 0;
static uint16_t _ui_inval_max_kpx  = 0;
// The total says how much, not what.  2.00 screens per pass could be one
// full-screen invalidation plus a half-screen arc twice over, or fifty small
// ones — and those want completely different fixes.  So also carry the LARGEST
// single invalidated area: near 217 kpx means something is dirtying the whole
// panel, and the only objects with a full-screen bounding box are the status
// ring and the transparent touch zone laid over everything.
static uint32_t _ui_inval_big_px   = 0;   // largest single area, this pass
static uint16_t _ui_inval_big_kpx  = 0;   // ...worst seen since the last report
// Which buffer the ladder actually got.  Printed on serial at boot, which a
// mount on a rig has no way to read — so it rides along here too, or "internal
// RAM did not help" cannot be told from "it never got internal RAM".
static uint8_t  _ui_buf_kind = 0;       // 1=int40  2=int20  3=PSRAM
#endif

#define LVGL_BUF_LINES 40
#define LVGL_BUF_BYTES (SCR_W * LVGL_BUF_LINES * sizeof(lv_color_t))
static lv_color_t *_lvgl_buf1 = nullptr;
static lv_display_t *_level_disp = nullptr;

static void lvgl_flush_cb(lv_display_t *disp, const lv_area_t *area,
                          uint8_t *px_map) {
    uint32_t w = (uint32_t)(area->x2 - area->x1 + 1);
    uint32_t h = (uint32_t)(area->y2 - area->y1 + 1);
#if UI_PROFILE
    uint32_t _t0 = micros();
#endif
    _gfx->draw16bitRGBBitmap(area->x1, area->y1, (uint16_t *)px_map, w, h);
#if UI_PROFILE
    // Summed across every chunk of one render pass, so it is comparable with
    // the lv_timer_handler() total rather than being one twelfth of it.
    _ui_flush_accum_us += micros() - _t0;
#endif
    lv_display_flush_ready(disp);
}

// The CO5300 AMOLED addresses columns in pairs, so a flush window starting on an
// odd column (or with an odd width) leaves the panel's write pointer misaligned:
// every row lands one pixel over and the region renders visibly SHEARED.
// Full-screen redraws (x=0, w=466) are naturally aligned, which is why only small
// partial updates showed it — notably the centred SETUP status line, whose x1 is
// (466 - text_width)/2 and so flips parity with the message being displayed.
// Round every invalidated area out to even bounds BEFORE LVGL renders it, so the
// rendered px_map always matches the widened, aligned window.
static void lvgl_rounder_cb(lv_event_t *e) {
    lv_area_t *a = lv_event_get_invalidated_area(e);
    a->x1 &= ~1;    // start on an even column
    a->x2 |= 1;     // end on an odd column  → even width
    a->y1 &= ~1;
    a->y2 |= 1;
#if UI_PROFILE >= 2
    // After rounding, so it counts what will actually be drawn.
    {
        uint32_t px = (uint32_t)(a->x2 - a->x1 + 1) * (a->y2 - a->y1 + 1);
        _ui_inval_accum_px += px;
        if (px > _ui_inval_big_px) _ui_inval_big_px = px;
    }
#endif
}

// ---------------------------------------------------------------------------
// Display brightness / dim
// ---------------------------------------------------------------------------

#define BRIGHT_NORMAL   200
#define BRIGHT_DIM       40
#define DIM_TIMEOUT_MS  60000UL

static bool     _dimmed        = false;
static uint32_t _last_touch_ms = 0;   // set to millis() at end of setup()

static void set_dim(bool dim) {
    if (_dimmed == dim) return;
    _dimmed = dim;
    _gfx->setBrightness(dim ? BRIGHT_DIM : BRIGHT_NORMAL);
}

// ---------------------------------------------------------------------------
// Touch — CST9217 (addr 0x5A)  via TouchDrvCSTXXX library (SensorsLib)
// Interrupt-driven: INT pin goes low on touch event.
// Library handles reset, I2C init, register parsing, and coordinate mirroring.
// ---------------------------------------------------------------------------

static volatile bool  _touch_irq = false;
static TouchDrvCSTXXX _touch;
static int16_t        _touch_x[5], _touch_y[5];

// Touch-hold detector (opens SETUP): stamped by the LVGL touch callback when a
// press begins, cleared on release; loop() opens setup after SETUP_HOLD_MS.
static volatile uint32_t _press_started_ms = 0;
#define SETUP_HOLD_MS  1500UL

// ---------------------------------------------------------------------------
// IMU — QMI8658 (shares I2C bus with touch, addr 0x6B)
// ---------------------------------------------------------------------------

static SensorQMI8658 _qmi;
static bool          _qmi_ok   = false;
static IMUdata       _acc      = {};   // last accelerometer reading

static void IRAM_ATTR touch_isr() { _touch_irq = true; }

static void lvgl_touch_cb(lv_indev_t *indev, lv_indev_data_t *data) {
    static lv_point_t last_pt      = {0, 0};
    static bool       last_pressed = false;

    if (!_touch_irq && !last_pressed) {
        data->state = LV_INDEV_STATE_RELEASED;
        return;
    }
    _touch_irq = false;

    uint8_t n = _touch.getPoint(_touch_x, _touch_y,
                                _touch.getSupportTouchPoint());
    if (n > 0) {
        _last_touch_ms = millis();
        if (!last_pressed) _press_started_ms = millis();   // press began — hold detector
        last_pt.x    = (lv_coord_t)_touch_x[0];
        last_pt.y    = (lv_coord_t)_touch_y[0];
        last_pressed = true;
        data->state  = LV_INDEV_STATE_PRESSED;
        data->point  = last_pt;
    } else {
        last_pressed = false;
        _press_started_ms = 0;                             // released
        data->state  = LV_INDEV_STATE_RELEASED;
    }
}

// ---------------------------------------------------------------------------
// Mount state
// ---------------------------------------------------------------------------

struct MsState {
    uint8_t  state;
    uint8_t  flags;
    uint8_t  pt_preset;          // 1-4
    uint8_t  sl_preset;          // 1-4
    uint16_t slot_occupied;
    uint16_t slot_at;
    uint8_t  target_slot;        // 0xFF = none
    uint8_t  active_la_subject;  // 0xFF = none; current look-at subject index
    bool     hub_connected;
};

static MsState _ms = {
    .state            = STATE_IDLE,
    .flags            = 0,
    .pt_preset        = 1,
    .sl_preset        = 1,
    .slot_occupied    = 0,
    .slot_at          = 0,
    .target_slot      = 0xFF,
    .active_la_subject = 0xFF,
    .hub_connected    = false,
};
// Called from loop().
static void cfg_save_poll() {
    if (!_cfg_dirty) return;
    if (millis() - _cfg_dirty_since < CFG_SAVE_SETTLE_MS) return;
    if (_ms.state != STATE_IDLE) return;      // never mid-move
    _cfg_dirty = false;
    cfg_save();
    Serial.println("[CFG] Roam settled — saved");
}

// ---------------------------------------------------------------------------
// Globals
// ---------------------------------------------------------------------------

static PacketParser _espnow_parser;
static PacketParser _teensy_parser;

static uint16_t _tx_seq              = 0;
static uint32_t _last_hub_rx_ms      = 0;
// Last ESP-NOW send to the hub that the radio ACKed.  If this stays fresh while
// _last_hub_rx_ms goes stale, the link is one-way (we can send, can't receive) —
// a HUB-side wedge that restarting THIS chip can't fix.  Gates the esp_restart
// below so the mount doesn't pointlessly reboot-loop (and blink the camera) for
// a fault that lives on the hub; the hub gets recovered from the PC instead.
static volatile uint32_t _last_espnow_tx_ok_ms = 0;
static bool     _watchdog_fired      = false;
static uint32_t _last_teensy_st_ms   = 0;
// When a client last asked for a position.  The unsolicited 5 Hz stream is
// filtered out; an explicit request still gets answered.
static uint32_t _last_getpos_ms      = 0;
// ── The look-at run, owned here ──────────────────────────────────────────
// See RUN_DEADMAN_MS in shared/protocol.h for why this moved and what replaced
// the deadman it removed.
static bool     _run_active  = false;
static uint8_t  _run_subj    = 0;
static uint8_t  _run_dir     = 0;
static uint8_t  _run_preset  = 2;
// Whether the current leg was running as of the last look-at status. Kept out
// here rather than static inside the handler so starting a run always begins
// from a known edge — a value left over from the previous run would otherwise
// fire a leg change off the first packet of the next one.
static bool     _run_leg_active = false;

static void run_send_leg() {
    uint8_t p[3] = { (uint8_t)(_run_subj & 0x07), (uint8_t)(_run_dir & 1), _run_preset };
    uint8_t buf[PKT_BUF_SIZE + 4];
    Serial1.write(buf, build_packet(buf, _mount_id, ++_tx_seq,
                                    CMD_START_LOOK_AT_MOVE, p, sizeof(p)));
}

static void run_stop(const char *why) {
    if (!_run_active) return;
    _run_active     = false;
    _run_leg_active = false;
    Serial.printf("[RUN] stopped: %s\n", why);
}
static uint32_t _last_heartbeat_ms   = 0;
static int8_t   _last_rssi           = 0;
static uint32_t _last_rssi_update_ms = 0;

static uint8_t _tx_buf[PKT_BUF_SIZE + 4];
static volatile uint8_t _espnow_consec_fails   = 0;   // consecutive send failures → peer refresh
static volatile uint8_t _espnow_refresh_count  = 0;   // peer del/add cycles since last full reinit
static volatile bool    _espnow_need_reinit    = false; // set in callback, actioned in loop()
static volatile bool    _espnow_need_refresh   = false; // peer del/add requested, actioned in loop()
                                                        // (esp_now_*() must not run in the WiFi-task
                                                        // send callback — it can corrupt the stack)
// Uniform health telemetry (bridge node)
static volatile uint32_t _espnow_fail_total = 0;  // cumulative send failures since boot
// esp_now_send() can refuse a frame outright rather than queue it — NO_MEM when
// the stack's transmit queue is full, NOT_FOUND for a peer that has gone, and a
// handful of others.  A refusal never reaches the send callback, so it moves
// NEITHER tx_fail NOR _last_espnow_tx_ok_ms, and the mount goes silent with every
// counter frozen.  That is exactly how mount 5 looked on 2026-08-11: txfail
// pinned at 70 across the whole outage while TX died, so the one number being
// watched said "healthy radio" throughout.  The satellite had the same wedge and
// the same blind spot until it started reading this return; the mount ignored it
// at all four send sites, which is why three minutes of downtime left no trace
// beyond "it went quiet".
static volatile uint32_t _espnow_tx_refused  = 0; // sends the stack would not accept
static volatile uint16_t _espnow_last_tx_err = 0; // esp_err_t of the most recent refusal
static uint32_t _reinit_count       = 0;          // completed full ESP-NOW reinits

// ---------------------------------------------------------------------------
// RF window: rssi and noise floor, accumulated per frame.
//
// A link fails on signal-to-NOISE, and only the signal half has ever been
// measured here.  -59 dBm on a -95 dBm floor has 36 dB of margin; the same
// -59 dBm on a -65 dBm floor has six and dies.  Those two are indistinguishable
// in the rssi column, want opposite remedies — leave the room alone and hunt an
// interferer, or move the hardware — and an afternoon went into guessing
// between them from where a mount happened to be standing.
//
// Written from the WiFi task's receive callback and read from loop(), so the
// accumulators are volatile and the read swaps them under a critical section:
// a torn min/max pair would read as a burst that never happened.
// ---------------------------------------------------------------------------
static volatile int32_t  _rf_rssi_sum = 0, _rf_nf_sum = 0;
static volatile int8_t   _rf_rssi_min = 0, _rf_rssi_max = 0;
static volatile int8_t   _rf_nf_min   = 0, _rf_nf_max   = 0;
static volatile uint16_t _rf_frames   = 0;
// Frames the receive queue would not take.  xQueueSend() was called with a zero
// timeout and its result discarded, so a full queue drops a command in the WiFi
// task before the application ever sees it — no ACK is generated, while the
// sender's frame WAS acknowledged at the MAC layer and looks entirely
// successful.  Both ends report success and the command simply vanishes, which
// is exactly what mount 1 looks like: 19% of its commands unanswered for twelve
// hours with every counter on both sides reading clean.
static volatile uint16_t _rf_rx_dropped = 0;
// Sends ATTEMPTED this window, against those that did not go out.  Only the
// failures were ever counted, which makes the number unreadable on a mount that
// transmits more than its neighbours — and the mount with the camera does.
static volatile uint16_t _rf_tx_attempts = 0;
static volatile uint16_t _rf_tx_failed   = 0;
static uint32_t          _rf_last_ms  = 0;
static portMUX_TYPE      _rf_mux      = portMUX_INITIALIZER_UNLOCKED;

static inline void rf_accumulate(int8_t rssi, int8_t nf) {
    portENTER_CRITICAL_ISR(&_rf_mux);
    if (!_rf_frames) {
        _rf_rssi_min = _rf_rssi_max = rssi;
        _rf_nf_min   = _rf_nf_max   = nf;
        _rf_rssi_sum = _rf_nf_sum  = 0;
    } else {
        if (rssi < _rf_rssi_min) _rf_rssi_min = rssi;
        if (rssi > _rf_rssi_max) _rf_rssi_max = rssi;
        if (nf   < _rf_nf_min)   _rf_nf_min   = nf;
        if (nf   > _rf_nf_max)   _rf_nf_max   = nf;
    }
    if (_rf_frames < 0xFFFF) {
        _rf_frames   = _rf_frames + 1;
        _rf_rssi_sum = _rf_rssi_sum + rssi;
        _rf_nf_sum   = _rf_nf_sum   + nf;
    }
    portEXIT_CRITICAL_ISR(&_rf_mux);
}

static uint32_t _espnow_last_reinit_ms = 0;      // for ESPNOW_REINIT_MIN_GAP_MS
static uint32_t _espnow_reinit_held    = 0;      // requests suppressed by the gap

// Minimum gap between full ESP-NOW reinits.
//
// The ladder — 4 consecutive failures refresh the peer, 3 refreshes rebuild the
// stack — had no brake, and the counters only reset on a SUCCESS.  So a run of
// failures rebuilt the stack every twelfth one, and since a rebuild is
// esp_now_deinit(), a 100 ms delay and a re-add, sends fail THROUGH it and count
// toward the next.  A burst sustains itself.
//
// Measured, not theorised: one 7-second burst produced 121 failures and ten full
// reinits, and an overnight log reached 753 of them — twelve times 753 is very
// nearly the 9,598 failures recorded, so essentially every failure that night
// was feeding this.
//
// 15 s because the isolation restart is at ESPNOW_RESTART_MS (2 min): that still
// allows ~8 genuine recovery attempts before the mount gives up and reboots,
// while turning a burst like the one above into a single rebuild.  Recovery from
// a genuinely dead stack is unaffected; only the repetition is.
#define ESPNOW_REINIT_MIN_GAP_MS  15000UL

// ── One-way transmit wedge ──────────────────────────────────────────────────
// The isolation restart needs no RX *and* no TX for two minutes.  Mount 4 spent
// an hour on 2026-08-11 failing 79% of its commands — 90 of 114 unacknowledged,
// txfail climbing ~40/s — while receiving perfectly well.  It was never
// isolated, so it was never eligible for the one remedy that works, and it sat
// there being told things and doing none of them until it was reflashed by hand.
// Its health packets kept arriving throughout, which is why it looked fine.
//
// A mount that cannot transmit is broken whether or not it can still hear. So
// the rate is watched too: sends failing this fast, for this long, while RX is
// healthy, is a wedge and gets the WiFi-level restart.
//
// 10/s is well clear of ordinary loss — a mount at the edge of range failed 40/s
// and a healthy one on the same rig failed 0 over ten minutes — and 30 s is far
// longer than any burst of interference or a base rebooting underneath it.
#define TXWEDGE_FAILS_PER_S   10UL
#define TXWEDGE_SUSTAIN_MS    30000UL
// Escalation, rewritten after watching it fail for two and a half hours.
//
// The first version tried the WiFi-level restart up to twice per wedge and then
// gave up and waited for the isolation path — which cannot fire during a TX
// wedge, because RX is healthy by definition.  Both halves were wrong.  The cap
// never applied at all: espnow_wifi_restart() sets _last_espnow_tx_ok_ms, and
// the quota-reset three lines below tested that same timestamp, so every
// restart satisfied its own recovery condition and handed the quota straight
// back.  Mount 4 ran 99 of them, thirty seconds apart, failing 12 sends/s
// before and after every single one.
//
// Then it rebooted itself and was clean for hours, at a byte-identical -76 dBm
// with the noise floor at -99.  99 restarts: no effect.  One reboot: fixed.
// So the WiFi restart is kept as a first try — it costs 300 ms against a boot's
// ten seconds and a camera re-pair — but it is now PROVED, not assumed: after
// it, the failure rate is watched for TXWEDGE_PROBATION_MS, and if the wedge is
// still there the mount reboots rather than repeating a remedy 99 attempts say
// does not work.
#define TXWEDGE_PROBATION_MS  10000UL   // prove the WiFi restart worked, or escalate
// Genuinely quiet, measured on the failure counter itself.  Deliberately NOT on
// _last_espnow_tx_ok_ms: that is what the restart touches, and testing a clock
// the remedy sets is how the old cap defeated itself.
#define TXWEDGE_CLEAR_MS      60000UL
#define TXWEDGE_MAX_REBOOTS   3         // then stay up rather than boot-loop
static uint32_t _wifi_restarts     = 0;   // WiFi-level restarts since boot
static uint32_t _txw_window_ms     = 0;   // when the current bad run started
static uint32_t _txw_window_fails  = 0;   // _espnow_fail_total when it started
static uint32_t _txw_probation_ms  = 0;   // watching a WiFi restart prove itself
static uint32_t _txw_probation_f   = 0;   // failure count when probation began
static uint32_t _txw_quiet_ms      = 0;   // last time the failure counter MOVED
static uint32_t _txw_quiet_val     = 0;
static bool     _txw_report_due    = false;
// Survives the reboot it counts, so a mount cannot boot-loop on a fault a boot
// does not fix.  Guarded by the same magic as _iso_restarts, and deliberately
// NOT cleared by hub_ok: RX is healthy throughout a TX wedge, so an RX-based
// reset would be no cap at all — the exact bug being fixed here.
RTC_NOINIT_ATTR static uint32_t _wedge_reboots;
static uint32_t _last_jog_fwd_ms    = 0;          // last CMD_JOG forwarded → defer health send
static uint32_t _health_last_ms     = 0;
static uint32_t _health_anom_ms     = 0;
static uint16_t _health_loop_max_ms = 0;
static uint32_t _health_last_txfail = 0;
static bool     _health_first_sent  = false;

// ---------------------------------------------------------------------------
// Colours
// ---------------------------------------------------------------------------

// Per-mount accent colours — matched to the PC app's btn_text hues (bright
// variants suit the black AMOLED background).  Index = MOUNT_ID - 1.
static const uint32_t MOUNT_ACCENT_HEX[5] = {
    0xA5D6A7,   // mount 1 — green
    0x90CAF9,   // mount 2 — blue
    0xD4B800,   // mount 3 — gold
    0x80CBC4,   // mount 4 — teal
    0xCE93D8,   // mount 5 — purple
};

// COL_ACCENT is a runtime variable so all existing code picks up the right
// mount colour without any per-site changes.
static lv_color_t _col_accent;
#define COL_ACCENT _col_accent

#define COL_BG          lv_color_hex(0x000000)
#define COL_TEXT        lv_color_hex(0xFFFFFF)
#define COL_DIM         lv_color_hex(0x888888)
#define COL_TRACK       lv_color_hex(0x2A2A2A)
#define COL_DOT_OFF     lv_color_hex(0x444444)

// Status ring colours
#define COL_RING_DISC   lv_color_hex(0x555555)   // disconnected — grey
#define COL_RING_IDLE   lv_color_hex(0x2E7D32)   // idle — green
#define COL_RING_JOG    lv_color_hex(0x0077CC)   // jogging — blue
#define COL_RING_MOVE   lv_color_hex(0xF57C00)   // moving — amber
#define COL_RING_HOME   lv_color_hex(0xE65100)   // homing — orange
#define COL_RING_ERR    lv_color_hex(0xC62828)   // error — red

// Slot colours
#define COL_SLOT_EMPTY  lv_color_hex(0x1A1A1A)
#define COL_SLOT_OCC    lv_color_hex(0x761100)
#define COL_SLOT_AT     lv_color_hex(0x2E7D32)
#define COL_SLOT_TGT    lv_color_hex(0xA2A500)

// ---------------------------------------------------------------------------
// UI objects — main screen
// ---------------------------------------------------------------------------

static lv_obj_t *_main_scr     = nullptr;
static lv_obj_t *_ring         = nullptr;   // status ring
static lv_obj_t *_lbl_num      = nullptr;   // "1"–"5" in 48pt
static lv_obj_t *_lbl_state    = nullptr;
static lv_obj_t *_lbl_rssi     = nullptr;
// Arc strips (not full dials — just arc + dots + label)
static ArcStrip _pt_strip = {};
static ArcStrip _sl_strip = {};

static lv_obj_t *_slot_obj[10] = {};

// ---------------------------------------------------------------------------
// UI objects — spirit level screen
// ---------------------------------------------------------------------------

static lv_obj_t *_level_scr       = nullptr;
static lv_obj_t *_level_dot       = nullptr;
static bool      _level_active    = false;
static bool      _level_centred   = false;

// ---------------------------------------------------------------------------
// Geometry constants (screen 466×466, centre 233,233)
// ---------------------------------------------------------------------------

// Arc container placed at (43,43), size 380×380
// Arc inner radius ≈ 165, outer ≈ 190  (arc_width=25, padding=6 → knob r≈165)
#define ARC_X    43
#define ARC_Y    43
#define ARC_SZ  380

// Dot positions on circle of R=190 from screen centre (233,233).
// θ measured clockwise from 12 o'clock.
// x = 233 + 190·sin(θ) − 4   (−4 to centre 8px dot)
// y = 233 − 190·cos(θ) − 4

// PT arc: rotation=282, bg_angles(0,78), LV_ARC_MODE_REVERSE — RIGHT side
// Arc track 12°→90° (CW); indicator fills from 90° back toward 12° as preset increases.
// 5 dots at 90°, 70.5°, 51°, 31.5°, 12° — dot[0]=arc start (slow), dot[4]=arc end (fast)
// x = 233 + 190·sin(θ) − 4,  y = 233 − 190·cos(θ) − 4
static const lv_coord_t PT_DOT_X[5] = { 419, 408, 377, 328, 269 };
static const lv_coord_t PT_DOT_Y[5] = { 229, 166, 109,  67,  43 };

// SL arc: rotation=180, bg_angles(0,78), LV_ARC_MODE_NORMAL — LEFT side
// Arc track 270°→348° (CW); indicator grows from 270° toward 348° as preset increases.
// 5 dots at 270°, 289.5°, 309°, 328.5°, 348° — dot[0]=arc start (slow), dot[4]=arc end (fast)
static const lv_coord_t SL_DOT_X[5] = {  39,  50,  81, 130, 190 };
static const lv_coord_t SL_DOT_Y[5] = { 229, 166, 109,  67,  43 };

// Slot rows — two rows near bottom of circle
// Both rows centred on x=233 (slots 3 and 8 on vertical centre line), spacing 46px
static const lv_coord_t SLOT1_CX[5] = { 141, 187, 233, 279, 325 };
static const lv_coord_t SLOT2_CX[5] = { 141, 187, 233, 279, 325 };
#define SLOT1_Y   336
#define SLOT2_Y   382
#define SLOT1_DIA  38
#define SLOT2_DIA  34

// ---------------------------------------------------------------------------
// Spirit level geometry (screen 466×466, centre 233,233)
// ---------------------------------------------------------------------------

#define LEVEL_CX       233     // screen centre x
#define LEVEL_CY       233     // screen centre y
#define LEVEL_OUTER_R  160     // outer guide ring radius (px)
#define LEVEL_CLOSE_R   50     // amber "close" ring radius
#define LEVEL_SNAP_R    20     // green snap-zone radius
#define LEVEL_DOT_R      8     // moving dot radius (16×16 px)
#define LEVEL_SCALE    400.0f  // px per g  (0.4g ≈ 23° ≈ outer ring)

// ---------------------------------------------------------------------------
// ESP-NOW / Teensy helpers
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// ESP-NOW link budget
// ---------------------------------------------------------------------------
// Peers default to a fast PHY rate chosen for throughput.  These packets are a
// few bytes, so throughput is irrelevant and range is everything: force 1 Mbps
// with a long preamble, worth roughly 6-10 dB of link budget over the default.
// Must be called for each peer, after esp_now_add_peer().
static void espnow_peer_long_range(const uint8_t *mac) {
    esp_now_rate_config_t rate = {};
    rate.phymode = WIFI_PHY_MODE_11B;
    rate.rate    = WIFI_PHY_RATE_1M_L;   // 1 Mbps, long preamble
    rate.ersu    = false;
    rate.dcm     = false;
    esp_now_set_peer_rate_config(mac, &rate);
}

// Every ESP-NOW send to the hub goes through here, so a refusal is counted
// exactly once and in one place.  Returning void keeps the call sites unchanged:
// none of them can do anything useful about a refusal in the moment — the point
// is that the refusal is now visible afterwards instead of invisible always.
static void espnow_tx(const uint8_t *buf, uint16_t n) {
    if (_rf_tx_attempts < 0xFFFF) _rf_tx_attempts = _rf_tx_attempts + 1;
    esp_err_t e = esp_now_send(_hub_mac, buf, n);
    if (e != ESP_OK) {
        // Refused outright: it did not go out, and it never reaches the send
        // callback, so it must be counted as a failure HERE or not at all.
        if (_rf_tx_failed < 0xFFFF) _rf_tx_failed = _rf_tx_failed + 1;
        // NB: ++ on a volatile is deprecated in C++20, so read-modify-write.
        _espnow_tx_refused  = _espnow_tx_refused + 1;
        _espnow_last_tx_err = (uint16_t)e;
    }
}

static void send_to_hub(CmdType cmd, const uint8_t *payload, uint8_t plen) {
    if (!_cfg_valid) return;   // unpaired — no hub to send to
    uint16_t n = build_packet(_tx_buf, _mount_id, ++_tx_seq, cmd, payload, plen);
    espnow_tx(_tx_buf, n);
}

// Drain the RF window and report it, then start a fresh one.  Swapped under the
// same lock the callback writes with: a min taken from this window against a
// max from the next would read as a burst that never happened.
static void send_rf_report() {
    int8_t   rmin, rmax, nmin, nmax;
    int32_t  rsum, nsum;
    uint16_t n;
    portENTER_CRITICAL(&_rf_mux);
    rmin = _rf_rssi_min; rmax = _rf_rssi_max; rsum = _rf_rssi_sum;
    nmin = _rf_nf_min;   nmax = _rf_nf_max;   nsum = _rf_nf_sum;
    n    = _rf_frames;
    _rf_frames = 0;
    portEXIT_CRITICAL(&_rf_mux);

    // Nothing heard.  Reported rather than skipped: silence is the strongest
    // reading there is, and a gap in the log would be indistinguishable from
    // the mount being off.
    int8_t rmean = n ? (int8_t)(rsum / (int32_t)n) : 0;
    int8_t nmean = n ? (int8_t)(nsum / (int32_t)n) : 0;
    uint16_t drop, txa, txf;
    portENTER_CRITICAL(&_rf_mux);
    drop = _rf_rx_dropped;  _rf_rx_dropped  = 0;
    txa  = _rf_tx_attempts; _rf_tx_attempts = 0;
    txf  = _rf_tx_failed;   _rf_tx_failed   = 0;
    portEXIT_CRITICAL(&_rf_mux);
    // Beside "frames heard", because "frames heard and thrown away" is the same
    // question asked one layer up.
    uint8_t p[RF_REPORT_PAYLOAD_LEN] = {
        (uint8_t)rmin, (uint8_t)rmean, (uint8_t)rmax,
        (uint8_t)nmin, (uint8_t)nmean, (uint8_t)nmax,
        (uint8_t)(n >> 8), (uint8_t)n,
        (uint8_t)(drop >> 8), (uint8_t)drop,
        (uint8_t)(txa >> 8),  (uint8_t)txa,
        (uint8_t)(txf >> 8),  (uint8_t)txf };
    send_to_hub(CMD_RF_REPORT, p, sizeof(p));
}

static void send_estop_to_teensy() {
    uint8_t buf[PKT_BUF_SIZE + 4];
    uint16_t n = build_packet(buf, _mount_id, ++_tx_seq, CMD_E_STOP, nullptr, 0);
    Serial1.write(buf, n);
}

static void send_preset_to_teensy(uint8_t group, uint8_t preset) {
    uint8_t payload[2] = { group, preset };
    uint8_t buf[PKT_BUF_SIZE + 4];
    uint16_t n = build_packet(buf, _mount_id, ++_tx_seq,
                              CMD_SET_ACTIVE_PRESET, payload, 2);
    Serial1.write(buf, n);
}


// STATUS is 10 bytes — see build_status() in shared/protocol.h, which is the
// canonical definition.  This hand-rolled copy emitted only 9 and so dropped
// byte [9], the active look-at subject.  Clients that read the missing byte as
// 0xFF ("no subject") had a stored location flicker red ten times a second
// while the camera sat steady on it, and masked a second bug besides: the red
// that should follow a manual move was arriving by accident from here rather
// than from the mount actually deselecting the subject.
//
// Keep this in step with shared/protocol.h if the payload ever grows again.
static void send_status_heartbeat() {
    uint8_t p[10];
    p[0] = _ms.state;
    p[1] = _ms.flags;
    p[2] = _ms.pt_preset;
    p[3] = _ms.sl_preset;
    p[4] = (_ms.slot_occupied >> 8) & 0xFF;
    p[5] =  _ms.slot_occupied & 0xFF;
    p[6] = (_ms.slot_at >> 8) & 0xFF;
    p[7] =  _ms.slot_at & 0xFF;
    p[8] =  _ms.target_slot;
    p[9] =  _ms.active_la_subject;   // 0-7, or 0xFF for none
    send_to_hub(CMD_STATUS, p, sizeof(p));
    _last_heartbeat_ms = millis();
}

// ---- Uniform health telemetry (bridge node) --------------------------------

static void send_health(bool anomaly) {
    PayloadHealth h = {};
    h.node_type     = HEALTH_NODE_BRIDGE;
    h.reset_reason  = (uint8_t)esp_reset_reason();
    h.uptime_s      = millis() / 1000UL;
    h.free_heap     = (uint32_t)esp_get_free_heap_size();
    h.min_free_heap = (uint32_t)esp_get_minimum_free_heap_size();
    h.loop_max_ms   = _health_loop_max_ms;
    h.tx_fail       = (uint16_t)_espnow_fail_total;
    h.rssi          = _last_rssi;
    h.flags         = (anomaly ? HEALTH_FLAG_ANOMALY : 0) | ble_cam_health_flags();
#if UI_PROFILE >= 2
    // lvgl_ms(12) | largest single invalidation kpx(9) | total kpx(11).
    // The buffer kind is dropped: it has already answered (internal, 40 lines),
    // and knowing WHICH invalidation is big now matters more than repeating it.
    h.node_u32      = ((uint32_t)(_ui_lvgl_max_ms  & 0xFFF) << 20)
                    | ((uint32_t)(_ui_inval_big_kpx & 0x1FF) << 11)
                    |  (uint32_t)(_ui_inval_max_kpx & 0x7FF);
#elif UI_PROFILE
    h.node_u32      = ((uint32_t)_ui_lvgl_max_ms << 16) | _ui_flush_max_ms;
#else
    // Packed: refusals in the high half, reinits in the low half.  The field is
    // defined as node-specific, and this keeps the wire format, the golden test
    // and every existing log line unchanged — a bridge's reinit count has only
    // ever been 0-3, so the low half still reads exactly as it always did.
    // Worth the packing: a refusal count climbing is the wedge STARTING, which
    // is two minutes of warning before the mount takes itself down, whereas the
    // event report only ever arrives after the fact.
    h.node_u32      = ((_espnow_tx_refused & 0xFFFFUL) << 16) |
                      (_reinit_count & 0xFFFFUL);
#endif
    uint8_t p[24];
    encode_health_payload(p, &h);
    send_to_hub(CMD_HEALTH, p, 24);   // no-op while unpaired (send_to_hub guards)
    _health_last_ms     = millis();
    _health_loop_max_ms = 0;
#if UI_PROFILE
    _ui_lvgl_max_ms = _ui_flush_max_ms = 0;
#if UI_PROFILE >= 2
    _ui_inval_max_kpx = _ui_inval_big_kpx = 0;
#endif
#endif
    _health_last_txfail = _espnow_fail_total;
    _health_first_sent  = true;
}

// Called each loop pass.  Jog-aware: the periodic send steps aside while jog
// traffic is flowing (ESP-NOW airtime matters mid-move), but never for more
// than 2 s past its slot — one 33-byte frame among a 50 Hz jog stream is noise.
static void health_check_bridge(uint32_t now) {
    if (!_cfg_valid) return;

    // On its OWN clock, above every early return below.  Hung off the end of
    // the health path first, which disabled it on exactly the mounts it was
    // built for: a mount whose txfail is jumping is in permanent anomaly, takes
    // the anomaly branch and its return on every pass, and never reaches the
    // tail.  Mount 4 was failing 40 sends a second, flagged ANOMALY on every
    // health line, and emitted not one RF report in the whole log.  A
    // diagnostic that switches itself off when the fault appears is worse than
    // none, because its silence reads as nothing to see.
    if (now - _rf_last_ms >= HEALTH_INTERVAL_MS) {
        _rf_last_ms = now;
        send_rf_report();
    }

    bool anomaly =
        (!_health_first_sent && now > 3000) ||
        (esp_get_free_heap_size() < HEALTH_LOW_HEAP_BYTES) ||
        (_health_loop_max_ms > HEALTH_LOOP_STALL_MS) ||
        (_espnow_fail_total - _health_last_txfail >= HEALTH_TXFAIL_JUMP);
    if (anomaly && (now - _health_anom_ms) >= HEALTH_ANOMALY_GAP_MS) {
        _health_anom_ms = now;
        send_health(true);
        return;
    }
    if (now - _health_last_ms < HEALTH_INTERVAL_MS) return;
    bool jog_busy = (now - _last_jog_fwd_ms) < HEALTH_JOG_DEFER_MS;
    bool overdue  = (now - _health_last_ms) > HEALTH_INTERVAL_MS + 2000;
    if (jog_busy && !overdue) return;
    send_health(false);
}

// ---------------------------------------------------------------------------
// ESP-NOW callbacks
// ---------------------------------------------------------------------------

static void on_espnow_recv(const esp_now_recv_info_t *recv_info,
                           const uint8_t *data, int len) {
    _last_hub_rx_ms = millis();
    _watchdog_fired = false;
    if (recv_info && recv_info->rx_ctrl) {
        _last_rssi = (int8_t)recv_info->rx_ctrl->rssi;
        // The noise floor arrives with every frame, beside the rssi, and has
        // never been read.  Accumulated rather than sampled: interference is
        // bursty, and a spot reading taken every 10 s misses the burst that
        // killed the frames in between.
        rf_accumulate((int8_t)recv_info->rx_ctrl->rssi,
                      (int8_t)recv_info->rx_ctrl->noise_floor);
    }
    if (len <= 0 || len > ESPNOW_MAX_LEN) return;
    EspNowMsg msg;
    msg.len = (uint8_t)len;
    memcpy(msg.data, data, len);
    if (xQueueSend(_espnow_rx_q, &msg, 0) != pdTRUE) {
        // NB: ++ on a volatile is deprecated in C++20, so read-modify-write.
        _rf_rx_dropped = _rf_rx_dropped + 1;
    }
}

static void on_espnow_sent(const wifi_tx_info_t *, esp_now_send_status_t s) {
    if (s == ESP_NOW_SEND_SUCCESS) {
        _espnow_consec_fails  = 0;
        _espnow_refresh_count = 0;
        _last_espnow_tx_ok_ms = millis();   // our send side is alive
    } else {
        // NB: ++ on a volatile is deprecated in C++20, so read-modify-write.
        _espnow_fail_total = _espnow_fail_total + 1;   // health telemetry (cumulative)
        if (_rf_tx_failed < 0xFFFF) _rf_tx_failed = _rf_tx_failed + 1;
        Serial.printf("ESP-NOW send failed (%d)\n", (int)s);
        // After several consecutive failures the ESP-NOW stack internally marks
        // the hub peer as stale.  Refresh it so that when the hub powers back on
        // the very next heartbeat gets through without needing a mount reboot.
        _espnow_consec_fails = _espnow_consec_fails + 1;
        if (_espnow_consec_fails >= 4) {
            _espnow_consec_fails = 0;
            _espnow_refresh_count = _espnow_refresh_count + 1;
            if (_espnow_refresh_count >= 3) {
                // Three peer refreshes with no recovery (~60 s) means the ESP-NOW
                // stack itself is degraded (e.g. after many hours without a hub).
                // A full deinit/reinit can't safely run in this WiFi-task callback,
                // so flag it and let loop() handle it.
                _espnow_need_reinit = true;
                Serial.println("ESP-NOW full reinit requested");
            } else {
                // Peer del/add must NOT run here either (WiFi-task context) —
                // flag it for loop(), same as the full reinit.
                _espnow_need_refresh = true;
            }
        }
    }
}

// ---------------------------------------------------------------------------
// ESP-NOW full reinit — called from loop() when _espnow_need_reinit is set.
// Tears down and rebuilds the entire ESP-NOW stack so a long hub absence
// (hundreds of failed sends) doesn't permanently corrupt the send side.
// ---------------------------------------------------------------------------

// Register (or re-register) the active hub as the sole ESP-NOW peer.
// Called from loop()/setup() only — never from a WiFi-task callback.
static void espnow_peer_refresh() {
    if (!_cfg_valid) return;
    esp_now_del_peer(_hub_mac);
    esp_now_peer_info_t peer = {};
    memcpy(peer.peer_addr, _hub_mac, 6);
    peer.channel = _hub_channel;
    peer.ifidx   = WIFI_IF_STA;
    peer.encrypt = false;
    esp_now_add_peer(&peer);
    espnow_peer_long_range(peer.peer_addr);
    Serial.printf("ESP-NOW hub peer refreshed (%d/3)\n", (int)_espnow_refresh_count);
}

static void espnow_full_reinit() {
    if (!_cfg_valid) return;   // nothing to rebuild toward while unpaired
    Serial.println("[ESP-NOW] Full stack reinit start");
    esp_now_deinit();
    delay(100);
    if (esp_now_init() != ESP_OK) {
        Serial.println("[ESP-NOW] reinit FAILED — will retry next cycle");
        // Leave _espnow_refresh_count > 0 so we try again shortly.
        _espnow_consec_fails  = 0;
        return;
    }
    esp_now_register_recv_cb(on_espnow_recv);
    esp_now_register_send_cb(on_espnow_sent);
    esp_now_peer_info_t peer = {};
    memcpy(peer.peer_addr, _hub_mac, 6);
    peer.channel = _hub_channel;
    peer.ifidx   = WIFI_IF_STA;
    peer.encrypt = false;
    esp_now_add_peer(&peer);
    espnow_peer_long_range(peer.peer_addr);
    _espnow_consec_fails  = 0;
    _espnow_refresh_count = 0;
    _espnow_need_refresh  = false;
    _reinit_count++;                                // health telemetry
    Serial.println("[ESP-NOW] Full stack reinit done");
}

// The rung above espnow_full_reinit(), and the reason it exists: that function
// is called a "full stack reinit" and is nothing of the sort.  It deinits and
// reinits ESP-NOW, and stops there — the WiFi driver and PHY underneath are
// never touched.  So when the damage is below ESP-NOW, the ladder can climb
// forever without reaching it, which is exactly what was measured on
// 2026-08-11: mount 4 ran up to six reinits while failing ~40 sends a second,
// and a chip reboot cleared it instantly at an unchanged -78 dBm with the noise
// floor at -99.  Nothing was wrong with the air.  Every rung that has ever
// demonstrably worked has been a full restart.
//
// This is that, minus the cost: no boot, so the camera stays paired, the screen
// stays up, RTC state survives and no isolation quota is spent.
//
// ESP-NOW must be down across the WiFi restart — it is a client of the driver
// being stopped — and the channel, power and protocol are all re-applied
// afterwards rather than assumed to survive.  esp_wifi_set_max_tx_power() in
// particular is documented as only taking effect after esp_wifi_start().
static bool espnow_wifi_restart() {
    if (!_cfg_valid) return false;
    Serial.println("[ESP-NOW] WiFi teardown start");
    esp_now_deinit();

    // WiFi.mode(WIFI_OFF), not esp_wifi_stop().
    //
    // The first version of this called esp_wifi_stop()/esp_wifi_start(), and it
    // never once worked: mount 4 ran 99 of them thirty seconds apart, failing
    // 12 sends/s before and after every single one, then rebooted itself and was
    // clean for fourteen hours at identical signal.
    //
    // esp_wifi_stop() leaves the driver INITIALISED, so its TX buffer pool stays
    // allocated — and espressif/esp-idf#18682 reports exactly this fault as a
    // leak of that pool: esp_now_send() returns NO_MEM once enough buffers are
    // never returned by a send-complete callback that does not fire.  A pool
    // that is leaking cannot be fixed by a remedy that does not free it, which
    // is why only the reboot worked.  That issue reports esp_now_deinit/init as
    // failing and a full WiFi teardown as recovering, which is what this is.
    //
    // Arduino's WiFi.mode(WIFI_OFF) reaches esp_wifi_deinit() via
    // espWiFiStop()->wifiLowLevelDeinit(), and going through the Arduino API
    // rather than calling esp_wifi_deinit() directly keeps WiFiGeneric's own
    // lowLevelInitDone bookkeeping in step — behind its back, the next
    // WiFi.mode() would not re-initialise.
    WiFi.mode(WIFI_OFF);
    delay(200);
    WiFi.mode(WIFI_STA);
    WiFi.disconnect();
    delay(200);

    // Re-applied rather than assumed to survive: the driver has been destroyed
    // and rebuilt, and esp_wifi_set_max_tx_power() only takes effect after the
    // interface is up at all.
    esp_wifi_set_ps(WIFI_PS_NONE);
    esp_wifi_set_max_tx_power(84);
    esp_wifi_set_protocol(WIFI_IF_STA,
        WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N);
    esp_wifi_set_channel(_hub_channel, WIFI_SECOND_CHAN_NONE);

    if (esp_now_init() != ESP_OK) {
        Serial.println("[ESP-NOW] re-init after WiFi teardown FAILED — only a reboot left");
        return false;
    }
    esp_now_register_recv_cb(on_espnow_recv);
    esp_now_register_send_cb(on_espnow_sent);
    esp_now_peer_info_t peer = {};
    memcpy(peer.peer_addr, _hub_mac, 6);
    peer.channel = _hub_channel;
    peer.ifidx   = WIFI_IF_STA;
    peer.encrypt = false;
    esp_now_add_peer(&peer);
    espnow_peer_long_range(peer.peer_addr);
    _espnow_consec_fails  = 0;
    _espnow_refresh_count = 0;
    _espnow_need_refresh  = false;
    _espnow_need_reinit   = false;
    // Give the recovered link the same grace a fresh boot gets, so the very
    // teardown that fixed things is not immediately counted as more silence.
    _last_espnow_tx_ok_ms = millis();
    _wifi_restarts++;
    Serial.printf("[ESP-NOW] WiFi teardown done (%lu since boot)\n",
                  (unsigned long)_wifi_restarts);
    return true;
}

// ---------------------------------------------------------------------------
// Packet handlers
// ---------------------------------------------------------------------------

static void ui_update();  // forward declaration

static void handle_hub_packet(const ParsedPacket &pkt) {
    if (pkt.mount_id != _mount_id && pkt.mount_id != MOUNT_BROADCAST) return;
    _last_hub_rx_ms = millis();
    if (pkt.cmd == CMD_JOG) _last_jog_fwd_ms = _last_hub_rx_ms;  // health-send deferral
    if (pkt.cmd == CMD_GET_POSITION) _last_getpos_ms = _last_hub_rx_ms;
    // Everything gets an ACK except JOG.
    //
    // Jog runs at 20 Hz to the active mount, and this replied to every frame —
    // so jogging put 20 packets a second back on the air that nothing reads.
    // The PC app has JOG in _TX_QUIET_CMDS and does not track its ACKs; the
    // 802.11 layer has already acknowledged the frame, which is what made the
    // hub's send succeed in the first place.  It was the single highest-rate
    // transmission on the rig and every one of them was discarded on arrival.
    //
    // Losing a jog frame costs 50 ms of a superseding stream.  Losing a
    // COMMAND matters, so those still answer.
    if (pkt.cmd != CMD_JOG) {
        uint8_t ack[PKT_BUF_SIZE + 4];
        espnow_tx(ack, build_ack(ack, _mount_id, ++_tx_seq, pkt.seq));
    }

    // "A satellite just came up — look again."  One scan, not a schedule.
    //
    // Staggered by mount id, because a scan takes the radio off-channel for a
    // second or two and five mounts doing that together would blind the whole
    // rig at once.  ~700 ms apart spreads it while still finishing inside four
    // seconds.  hub_reacquire_poll()'s safe_to_scan already refuses to scan a
    // mount that is MOVING, so a show in progress is not interrupted — it just
    // rescans at the next idle moment.
    if (pkt.cmd == CMD_RESCAN_BASES) {
        _reacq_requested = true;       // hub_reacquire_poll() acts on it
        _reacq_hold_ms   = millis() + (uint32_t)_mount_id * 700UL;
        Serial.printf("[REACQ] hub says a satellite is back — rescanning in %lu ms\n",
                      (unsigned long)((uint32_t)_mount_id * 700UL));
        return;
    }

    // Camera control stops here — it goes out over BLE, not down to the Teensy,
    // which has no idea what a Blackmagic command is and would log it as a bad
    // packet.  ACKed above either way: the ACK says the mount received the
    // command, and whether the camera link is up is reported separately in
    // CMD_HEALTH, so a missing camera does not look like a dead mount.
    if (pkt.cmd == CMD_CAM_CONTROL) {
        if (!ble_cam_send(pkt.payload, pkt.payload_len))
            Serial.println("[BLECAM] camera command dropped — no link");
        return;
    }

    // A run is a property of the mount now, not a sequence the PC drives.
    if (pkt.cmd == CMD_START_LOOK_AT_MOVE && pkt.payload_len >= 3) {
        _run_subj   = pkt.payload[0] & 0x07;
        _run_dir    = pkt.payload[1] & 1;
        _run_preset = pkt.payload[2];
        // Byte 3 is `repeat`.  Absent (an older client) means a single leg,
        // which is the behaviour that has always existed.
        bool rep = (pkt.payload_len >= 4) && pkt.payload[3];
        if (rep && !_run_active) {
            Serial.println("[RUN] started — mount owns it");
            _run_leg_active = false;   // this leg has not been seen running yet
        }
        if (!rep) run_stop("single leg requested");
        _run_active = rep;
    }
    // Anything that means "the operator has taken over" ends the run.  A jog is
    // a person moving the rig by hand; continuing to ping-pong underneath them
    // would be the mount arguing with the operator.
    if (pkt.cmd == CMD_E_STOP)  run_stop("E-STOP");
    if (pkt.cmd == CMD_JOG)     run_stop("operator jogged");
    if (pkt.cmd == CMD_GOTO || pkt.cmd == CMD_GOTO_SLOT || pkt.cmd == CMD_MOVE_REL)
        run_stop("a move was commanded");

    uint8_t fwd[PKT_BUF_SIZE + 4];
    Serial1.write(fwd, build_packet(fwd, pkt.mount_id, pkt.seq,
                                    pkt.cmd, pkt.payload, pkt.payload_len));
}

// ---------------------------------------------------------------------------
// What is worth putting on the air
// ---------------------------------------------------------------------------
// Every Teensy packet used to be forwarded, unconditionally: STATUS at 10 Hz,
// POSITION at 5 Hz while moving, LOOK_AT_STATUS at 10 Hz.  Most of it was read
// by nobody.
//
// POSITION had no subscriber at all — pc_app/comms/position_log.py says so in
// its own opening comment, and the logger there was written afterwards to give
// the stream a purpose.  LOOK_AT_STATUS carries three positional floats and the
// only consumer reads two fields, subject_id and look_at_active, ignoring the
// rest.  STATUS carries state, presets and slot masks, which change a few times
// a minute and were being sent six hundred times a minute.
//
// So: send state when it CHANGES, plus a slow refresh in case a change was
// lost, and stop streaming positions nothing reads.  A stream is self-healing —
// miss one and the truth arrives 100 ms later — and removing it removes that,
// which is what the refresh is for.  It is not belt and braces; it is the thing
// that stops a lost transition being wrong forever.
#ifndef TEENSY_FWD_FILTER
#define TEENSY_FWD_FILTER 1          // 0 restores the old forward-everything
#endif
// Long enough that it is not a stream, short enough that a lost change is a
// hitch rather than a fault.  5 s, not 10, because the look-at Run advances on
// a transition and stalls until it sees one.
#define STATE_REFRESH_MS   MOUNT_STATUS_REFRESH_MS   // see shared/protocol.h
// An explicit CMD_GET_POSITION is answered for this long afterwards, so the
// on-demand path still works while the unsolicited stream does not.
#define POS_ON_DEMAND_MS   2000UL

static uint32_t _fwd_status_ms = 0;
static uint8_t  _fwd_status[10] = {};
static uint8_t  _fwd_status_len = 0;
static uint32_t _fwd_la_ms      = 0;
static uint8_t  _fwd_la_subj    = 0xFE;   // not a valid subject or 0xFF
static uint8_t  _fwd_la_flags   = 0xFF;

static bool teensy_frame_worth_sending(const ParsedPacket &pkt) {
#if !TEENSY_FWD_FILTER
    return true;
#else
    uint32_t now = millis();
    switch (pkt.cmd) {

    case CMD_POSITION:
        // Only when someone actually asked.  _last_getpos_ms is set when a
        // CMD_GET_POSITION arrives from the hub, so a client that wants a
        // reading still gets one; the 5 Hz stream behind it stops.
        return (now - _last_getpos_ms) < POS_ON_DEMAND_MS;

    case CMD_STATUS: {
        // On change, or on the refresh.  Compared over the whole payload
        // because every byte of it is state someone acts on.
        uint8_t n = pkt.payload_len > sizeof(_fwd_status)
                  ? (uint8_t)sizeof(_fwd_status) : pkt.payload_len;
        bool changed = (n != _fwd_status_len) ||
                       memcmp(_fwd_status, pkt.payload, n) != 0;
        if (!changed && (now - _fwd_status_ms) < STATE_REFRESH_MS) return false;
        memcpy(_fwd_status, pkt.payload, n);
        _fwd_status_len = n;
        _fwd_status_ms  = now;
        return true;
    }

    case CMD_LOOK_AT_STATUS: {
        // Compared on subject_id and flags ONLY.  The three floats beside them
        // change every single frame, so comparing the payload would forward
        // every frame and change nothing — and those floats are exactly the
        // positional data nothing reads.
        if (pkt.payload_len < 14) return true;      // malformed; let it through
        uint8_t subj = pkt.payload[12], flags = pkt.payload[13];
        bool changed = (subj != _fwd_la_subj) || (flags != _fwd_la_flags);
        if (!changed && (now - _fwd_la_ms) < STATE_REFRESH_MS) return false;
        _fwd_la_subj  = subj;
        _fwd_la_flags = flags;
        _fwd_la_ms    = now;
        return true;
    }

    default:
        // Everything else is already an event: LIMITS_FOUND, HOME_COMPLETE,
        // SUBJECT_LIST, CALIB_PROMPT, REF_CONFIRMED, ACK, NACK.  Each is sent
        // once because something happened, and each is the only notice of it.
        return true;
    }
#endif
}

static void handle_teensy_packet(const ParsedPacket &pkt) {
    if (pkt.cmd == CMD_STATUS && pkt.payload_len >= 2) {
        _last_teensy_st_ms = millis();
        _last_heartbeat_ms = millis();
        bool changed = false;
        auto upd = [&](auto &f, auto v) {
            if (f != (decltype(f))v) { f = v; changed = true; }
        };
        upd(_ms.state, pkt.payload[0]);
        upd(_ms.flags, pkt.payload[1]);
        if (pkt.payload_len >= 9) {
            upd(_ms.pt_preset,     pkt.payload[2]);
            upd(_ms.sl_preset,     pkt.payload[3]);
            upd(_ms.slot_occupied, (uint16_t)((pkt.payload[4]<<8)|pkt.payload[5]));
            upd(_ms.slot_at,       (uint16_t)((pkt.payload[6]<<8)|pkt.payload[7]));
            upd(_ms.target_slot,   pkt.payload[8]);
        }
        // Byte [9] is the Teensy's authoritative look-at subject.  Track it
        // here as well as from CMD_LOOK_AT_STATUS: that one is event-driven, so
        // relying on it alone let this copy drift from the Teensy's truth, and
        // the heartbeat below then published the stale value.
        if (pkt.payload_len >= 10) {
            upd(_ms.active_la_subject, pkt.payload[9]);
        }
        if (changed) ui_update();
    }
    // Track active look-at subject for green dot display
    if (pkt.cmd == CMD_LOOK_AT_STATUS && pkt.payload_len >= 13) {
        uint8_t subj = pkt.payload[12];
        if (_ms.active_la_subject != subj) {
            _ms.active_la_subject = subj;
            ui_update();
        }
        // Leg finished.  The Teensy sends exactly one of these with the active
        // bit clear when a look-at sequence ends, so this is the transition —
        // and the whole decision now happens here rather than 400 km round the
        // houses and back.
        //
        // The bit is FLAG_LOOK_AT_ACTIVE, which is 0x40.  This read 0x01, a bit
        // the Teensy never sets: la_now was false on every packet, the rising
        // edge never happened, so the falling edge never did either.  The run
        // took its first leg — the one the PC sends — and then waited forever
        // for a transition that could not occur.  Named constant now, so the
        // two ends cannot drift apart again.
        if (pkt.payload_len >= 14 && _run_active) {
            bool la_now = (pkt.payload[13] & FLAG_LOOK_AT_ACTIVE) != 0;
            if (_run_leg_active && !la_now) {
                _run_dir ^= 1;
                Serial.printf("[RUN] leg done — next leg, direction %u\n", _run_dir);
                run_send_leg();
            }
            _run_leg_active = la_now;
        }
    }
    if (!_cfg_valid) return;   // unpaired — don't forward Teensy traffic anywhere
    if (!teensy_frame_worth_sending(pkt)) return;
    uint8_t fwd[PKT_BUF_SIZE + 4];
    espnow_tx(fwd, build_packet(fwd, _mount_id, pkt.seq,
                                pkt.cmd, pkt.payload,
                                pkt.payload_len));
}

// ---------------------------------------------------------------------------
// Touch zone callbacks
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// UI helper — build one arc strip
// ---------------------------------------------------------------------------

static void build_arc_strip(ArcStrip *s, lv_obj_t *scr,
                             int16_t rotation, bool is_pt) {
    // Arc widget (380×380, centred on screen)
    lv_obj_t *arc = lv_arc_create(scr);
    lv_obj_set_size(arc, ARC_SZ, ARC_SZ);
    lv_obj_set_pos(arc, ARC_X, ARC_Y);
    lv_arc_set_rotation(arc, rotation);
    lv_arc_set_bg_angles(arc, 0, 78);   // 78° = 13 minutes × 6°/min
    lv_arc_set_range(arc, 0, 4);        // 0=empty, 4=full; preset N fills N/4 of arc
    lv_arc_set_value(arc, 1);
    // PT uses REVERSE so the indicator fills from the 3 o'clock end upward toward 12
    lv_arc_set_mode(arc, is_pt ? LV_ARC_MODE_REVERSE : LV_ARC_MODE_NORMAL);
    lv_obj_remove_style(arc, nullptr, LV_PART_KNOB);
    lv_obj_remove_flag(arc, LV_OBJ_FLAG_CLICKABLE);

    // Track (background arc)
    lv_obj_set_style_arc_color(arc, COL_TRACK,  LV_PART_MAIN);
    lv_obj_set_style_arc_width(arc, 18, LV_PART_MAIN);
    lv_obj_set_style_arc_rounded(arc, true, LV_PART_MAIN);

    // Indicator (accent colour)
    lv_obj_set_style_arc_color(arc, COL_ACCENT, LV_PART_INDICATOR);
    lv_obj_set_style_arc_width(arc, 20, LV_PART_INDICATOR);
    lv_obj_set_style_arc_rounded(arc, true, LV_PART_INDICATOR);

    // Arc widget background — transparent so screen bg shows through
    lv_obj_set_style_bg_opa(arc, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(arc, 0, 0);
    lv_obj_set_style_pad_all(arc, 0, 0);
    lv_obj_remove_flag(arc, LV_OBJ_FLAG_SCROLLABLE);

    s->arc = arc;

    // 5 tick dots — arc-start + one at each segment boundary (4 segments)
    const lv_coord_t *dx = is_pt ? PT_DOT_X : SL_DOT_X;
    const lv_coord_t *dy = is_pt ? PT_DOT_Y : SL_DOT_Y;
    for (int i = 0; i < 5; i++) {
        lv_obj_t *dot = lv_obj_create(scr);
        lv_obj_set_size(dot, 8, 8);
        lv_obj_set_pos(dot, dx[i], dy[i]);
        lv_obj_set_style_radius(dot, LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_bg_color(dot, COL_DOT_OFF, 0);
        lv_obj_set_style_bg_opa(dot, LV_OPA_COVER, 0);
        lv_obj_set_style_border_width(dot, 0, 0);
        lv_obj_set_style_pad_all(dot, 0, 0);
        lv_obj_remove_flag(dot, (lv_obj_flag_t)(LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE));
        s->dots[i] = dot;
    }

}

static void update_arc_strip(ArcStrip *s, uint8_t preset) {
    // Nothing to do if the preset has not moved.  lv_arc_set_value() and every
    // set_style_bg_color() below mark their object dirty whether or not the
    // value differs, and these arcs are large — see ui_update().
    if (s->shown == preset) return;
    s->shown = preset;
    lv_arc_set_value(s->arc, preset);
    // Dots 0..preset lit (dot 0 = arc-start tick, always lit; dots 1-4 = segment ends)
    for (int i = 0; i < 5; i++) {
        lv_color_t col = (i <= preset) ? COL_ACCENT : COL_DOT_OFF;
        lv_obj_set_style_bg_color(s->dots[i], col, 0);
    }
}

// ---------------------------------------------------------------------------
// UI — ring colour from state
// ---------------------------------------------------------------------------

static lv_color_t ring_color() {
    if (!_ms.hub_connected) return COL_RING_DISC;
    switch (_ms.state) {
        case STATE_JOGGING:              return COL_RING_JOG;
        case STATE_MOVING_TO_POS:        return COL_RING_MOVE;
        case STATE_FINDING_LIMITS:       return COL_RING_HOME;
        case STATE_LOOK_AT_MOVE:         return lv_color_hex(0x00838F);  // teal — tracking
        case STATE_CALIBRATING_SUBJECT:  return lv_color_hex(0x7B1FA2);  // purple — calibrating
        case STATE_ERROR:                return COL_RING_ERR;
        default:                         return COL_RING_IDLE;
    }
}

static const char *state_str(uint8_t s) {
    switch (s) {
        case STATE_IDLE:                 return "IDLE";
        case STATE_JOGGING:              return "JOG";
        case STATE_MOVING_TO_POS:        return "MOVE";
        case STATE_FINDING_LIMITS:       return "HOME";
        case STATE_LOOK_AT_MOVE:         return "LOOK-AT";
        case STATE_CALIBRATING_SUBJECT:  return "CALIB";
        case STATE_ERROR:                return "ERR";
        default:                         return "---";
    }
}

// ---------------------------------------------------------------------------
// Spirit level screen — build
// ---------------------------------------------------------------------------

static void _ring_obj(lv_obj_t *parent, lv_coord_t cx, lv_coord_t cy,
                      lv_coord_t r, uint32_t col, lv_coord_t bw) {
    lv_obj_t *o = lv_obj_create(parent);
    lv_obj_set_size(o, r * 2, r * 2);
    lv_obj_set_pos(o, cx - r, cy - r);
    lv_obj_set_style_radius(o, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_opa(o, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_color(o, lv_color_hex(col), 0);
    lv_obj_set_style_border_width(o, bw, 0);
    lv_obj_set_style_border_opa(o, LV_OPA_COVER, 0);
    lv_obj_set_style_pad_all(o, 0, 0);
    lv_obj_remove_flag(o, (lv_obj_flag_t)(LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE));
}

static void build_level_screen() {
    lv_obj_t *scr = _level_scr;
    lv_obj_set_style_bg_color(scr, COL_BG, 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);

    // Concentric guide rings (outer → close → snap)
    _ring_obj(scr, LEVEL_CX, LEVEL_CY, LEVEL_OUTER_R, 0x2A2A2A, 2);
    _ring_obj(scr, LEVEL_CX, LEVEL_CY, LEVEL_CLOSE_R, 0x4A3A00, 1);
    _ring_obj(scr, LEVEL_CX, LEVEL_CY, LEVEL_SNAP_R,  0x1A4A1A, 2);

    // Centre crosshair (two 1px bars)
    auto make_bar = [&](lv_coord_t x, lv_coord_t y, lv_coord_t w, lv_coord_t h) {
        lv_obj_t *b = lv_obj_create(scr);
        lv_obj_set_size(b, w, h);
        lv_obj_set_pos(b, x, y);
        lv_obj_set_style_bg_color(b, lv_color_hex(0x2A2A2A), 0);
        lv_obj_set_style_bg_opa(b, LV_OPA_COVER, 0);
        lv_obj_set_style_border_width(b, 0, 0);
        lv_obj_set_style_pad_all(b, 0, 0);
        lv_obj_remove_flag(b, (lv_obj_flag_t)(LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE));
    };
    make_bar(LEVEL_CX - LEVEL_OUTER_R, LEVEL_CY,     LEVEL_OUTER_R * 2, 1);
    make_bar(LEVEL_CX,                 LEVEL_CY - LEVEL_OUTER_R, 1, LEVEL_OUTER_R * 2);

    // "LEVEL" label at top
    lv_obj_t *lbl = lv_label_create(scr);
    lv_label_set_text(lbl, "LEVEL");
    lv_obj_set_style_text_font(lbl, &lv_font_montserrat_16, 0);
    lv_obj_set_style_text_color(lbl, COL_DIM, 0);
    lv_obj_align(lbl, LV_ALIGN_TOP_MID, 0, 50);

    // Moving dot — positioned above everything, not clickable
    _level_dot = lv_obj_create(scr);
    lv_obj_set_size(_level_dot, LEVEL_DOT_R * 2, LEVEL_DOT_R * 2);
    lv_obj_set_pos(_level_dot, LEVEL_CX - LEVEL_DOT_R, LEVEL_CY - LEVEL_DOT_R);
    lv_obj_set_style_radius(_level_dot, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(_level_dot, lv_color_hex(0xF44336), 0);
    lv_obj_set_style_bg_opa(_level_dot, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(_level_dot, 0, 0);
    lv_obj_set_style_pad_all(_level_dot, 0, 0);
    lv_obj_remove_flag(_level_dot, (lv_obj_flag_t)(LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE));

    // Full-screen transparent touch zone — tap anywhere to return to main
    lv_obj_t *back = lv_obj_create(scr);
    lv_obj_set_size(back, SCR_W, SCR_H);
    lv_obj_set_pos(back, 0, 0);
    lv_obj_set_style_bg_opa(back, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(back, 0, 0);
    lv_obj_set_style_pad_all(back, 0, 0);
    lv_obj_remove_flag(back, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(back, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(back, [](lv_event_t *) {
        if (_setup_active || _pair_active) return;   // hold switched screen mid-press
        _level_active = false;
        lv_scr_load(_main_scr);
    }, LV_EVENT_CLICKED, nullptr);
}

// ---------------------------------------------------------------------------
// Spirit level screen — update (called from loop when level screen is active)
// ---------------------------------------------------------------------------

static void update_level_screen() {
    if (!_level_dot || !_qmi_ok) return;

    static uint32_t _last_level_ms = 0;
    const uint32_t  now_ms = millis();
    if (now_ms - _last_level_ms < 80) return;
    _last_level_ms = now_ms;

    if (_qmi.getDataReady())
        _qmi.getAccelerometer(_acc.x, _acc.y, _acc.z);

    // Remap accelerometer axes to screen axes.
    // Physical: tilt back  → acc.x positive → dot should rise   (screen -y)
    //           tilt left  → acc.y negative → dot should go right (screen +x)
    float dx   = -_acc.y * LEVEL_SCALE;
    float dy   = -_acc.x * LEVEL_SCALE;
    float dist = sqrtf(dx * dx + dy * dy);

    // Clamp dot to outer ring boundary
    if (dist > (float)LEVEL_OUTER_R) {
        float s = (float)LEVEL_OUTER_R / dist;
        dx *= s;  dy *= s;
    }

    int16_t new_x = (lv_coord_t)(LEVEL_CX + (int16_t)roundf(-dx) - LEVEL_DOT_R);
    int16_t new_y = (lv_coord_t)(LEVEL_CY + (int16_t)roundf(dy) - LEVEL_DOT_R);

    // Invalidate the bounding box covering old + new dot positions.
    // LVGL redraws the background in this area, clearing the trail,
    // then redraws the dot at its new position.
    static int16_t prev_x = new_x, prev_y = new_y;
    lv_coord_t x1 = LV_MIN(LV_MIN(prev_x, new_x) - LEVEL_DOT_R, (lv_coord_t)0);
    lv_coord_t y1 = LV_MIN(LV_MIN(prev_y, new_y) - LEVEL_DOT_R, (lv_coord_t)0);
    lv_coord_t x2 = LV_MAX(LV_MAX(prev_x, new_x) + LEVEL_DOT_R * 2, (lv_coord_t)SCR_W - 1);
    lv_coord_t y2 = LV_MAX(LV_MAX(prev_y, new_y) + LEVEL_DOT_R * 2, (lv_coord_t)SCR_H - 1);
    lv_area_t inv_area = { x1, y1, x2, y2 };
    lv_obj_invalidate_area(_level_scr, &inv_area);

    // Move dot after invalidating so LVGL redraws it at the new position
    lv_obj_set_pos(_level_dot, new_x, new_y);

    prev_x = new_x;
    prev_y = new_y;

    bool centred = (dist < (float)LEVEL_SNAP_R);
    if (centred != _level_centred) {
        _level_centred = centred;
        lv_obj_set_style_bg_color(_level_dot,
            centred ? lv_color_hex(0x4CAF50) : lv_color_hex(0xF44336), 0);
    }
}

// ---------------------------------------------------------------------------
// SETUP screen — pairing without recompiling
// ---------------------------------------------------------------------------
// Entered by touch & hold (~1.5 s) on any screen, or automatically at boot
// when unpaired.  Flow: tap CAM 1-5 → scan lists hubs (SSID + MAC + signal)
// → tap a hub (auto-selected when only one is found) → SAVE → restart paired.
// The WiFi scan gives everything ESP-NOW needs: the hub's AP BSSID is its
// ESP-NOW address and the scan result carries the channel.

static lv_obj_t *_setup_scr        = nullptr;
static lv_obj_t *_setup_id_btn[5]  = {};
static lv_obj_t *_setup_hub_btn[MAX_SCAN_ROWS] = {};
static lv_obj_t *_setup_hub_lbl[MAX_SCAN_ROWS] = {};
static lv_obj_t *_setup_scan_lbl   = nullptr;   // label inside the scan button
static lv_obj_t *_setup_save_btn   = nullptr;
static lv_obj_t *_setup_save_lbl   = nullptr;
static lv_obj_t *_setup_status     = nullptr;   // current pairing / result line
static uint8_t   _setup_sel_id     = 0;         // 1-5; 0 = not chosen yet
static int8_t    _setup_sel_hub    = -1;        // index into _scan_hub[]
static bool      _scan_running     = false;
static uint8_t   _scan_n           = 0;
static KnownHub  _scan_hub[MAX_SCAN_ROWS];
static int16_t   _scan_rssi[MAX_SCAN_ROWS];

#define COL_SETUP_SEL   lv_color_hex(0x2E7D32)   // selected button fill
#define COL_SETUP_BTN   lv_color_hex(0x1E1E1E)   // idle button fill

static void setup_refresh_widgets() {
    // Mount-ID buttons: selected one filled with accent-green
    for (int i = 0; i < 5; i++) {
        lv_obj_set_style_bg_color(_setup_id_btn[i],
            (_setup_sel_id == i + 1) ? COL_SETUP_SEL : COL_SETUP_BTN, 0);
    }
    // Hub rows: selected row green border
    for (int i = 0; i < MAX_SCAN_ROWS; i++) {
        if (i < _scan_n) {
            char row[48];
            snprintf(row, sizeof(row), "%s  %02X:%02X  %d dB%s",
                     _scan_hub[i].ssid,
                     _scan_hub[i].mac[4], _scan_hub[i].mac[5],
                     (int)_scan_rssi[i],
                     (_setup_sel_hub == i) ? "  <" : "");
            lv_label_set_text(_setup_hub_lbl[i], row);
            lv_obj_remove_flag(_setup_hub_btn[i], LV_OBJ_FLAG_HIDDEN);
            lv_obj_set_style_border_color(_setup_hub_btn[i],
                (_setup_sel_hub == i) ? COL_SETUP_SEL : lv_color_hex(0x383838), 0);
        } else {
            lv_obj_add_flag(_setup_hub_btn[i], LV_OBJ_FLAG_HIDDEN);
        }
    }
    // SAVE enabled only with both choices made
    bool ready = (_setup_sel_id >= 1) && (_setup_sel_hub >= 0);
    lv_obj_set_style_bg_color(_setup_save_btn,
        ready ? COL_SETUP_SEL : COL_SETUP_BTN, 0);
    lv_obj_set_style_text_color(_setup_save_lbl,
        ready ? COL_TEXT : COL_DIM, 0);
}

// Keep on-screen text ASCII-only.  LVGL's built-in Montserrat fonts cover
// ASCII plus the degree sign and LVGL's own symbols — nothing else.  A typographic
// dash (— U+2014) has no glyph and renders as a missing-glyph box, so use a plain
// '-'.  (Serial.print* strings are unaffected — a terminal renders UTF-8 fine.)
static void setup_show_status(const char *txt) {
    if (_setup_status) lv_label_set_text(_setup_status, txt);
}

static bool _reacq_scanning = false;   // background known-hub scan (see loop)

static void setup_start_scan() {
    if (_scan_running) return;
    _reacq_scanning = false;   // setup takes over the scan hardware
    _scan_running  = true;
    _scan_n        = 0;
    _setup_sel_hub = -1;
    lv_label_set_text(_setup_scan_lbl, "SCANNING...");
    setup_show_status("Searching for hubs...");
    setup_refresh_widgets();
    WiFi.scanDelete();
    WiFi.scanNetworks(true /*async*/, false /*no hidden*/);
}

// Poll the async scan from loop(); populate the hub rows when it completes.
static void setup_poll_scan() {
    if (!_scan_running) return;
    int n = WiFi.scanComplete();
    if (n == WIFI_SCAN_RUNNING) return;
    _scan_running = false;
    lv_label_set_text(_setup_scan_lbl, "SCAN AGAIN");

    _scan_n = 0;
    for (int i = 0; i < n; i++) {
        String ssid = WiFi.SSID(i);
        if (!ssid.startsWith(HUB_SSID_PREFIX) &&
            !ssid.startsWith(HUB_SSID_PREFIX_OLD)) continue;
        int16_t rssi = (int16_t)WiFi.RSSI(i);
        // Insert sorted by signal strength, strongest first
        if (_scan_n >= MAX_SCAN_ROWS && rssi <= _scan_rssi[MAX_SCAN_ROWS - 1])
            continue;   // list full and this one is weaker than everything held
        int pos = _scan_n < MAX_SCAN_ROWS ? _scan_n : MAX_SCAN_ROWS - 1;
        while (pos > 0 && rssi > _scan_rssi[pos - 1]) pos--;
        if (pos >= MAX_SCAN_ROWS) continue;
        for (int k = (int)((_scan_n < MAX_SCAN_ROWS ? _scan_n : MAX_SCAN_ROWS - 1)); k > pos; k--) {
            _scan_hub[k]  = _scan_hub[k - 1];
            _scan_rssi[k] = _scan_rssi[k - 1];
        }
        memcpy(_scan_hub[pos].mac, WiFi.BSSID(i), 6);
        _scan_hub[pos].channel = (uint8_t)WiFi.channel(i);
        strncpy(_scan_hub[pos].ssid, ssid.c_str(), 16);
        _scan_hub[pos].ssid[16] = '\0';
        _scan_rssi[pos] = rssi;
        if (_scan_n < MAX_SCAN_ROWS) _scan_n++;
    }
    WiFi.scanDelete();
    // The scan wanders across channels — go back to the active hub's channel
    // so a paired mount stays reachable while the user looks at the list.
    if (_cfg_valid)
        esp_wifi_set_channel(_hub_channel, WIFI_SECOND_CHAN_NONE);

    if (_scan_n == 0) {
        setup_show_status("No hubs found - is the hub powered?");
    } else {
        if (_scan_n == 1) _setup_sel_hub = 0;   // only one — preselect it
        char s[40];
        snprintf(s, sizeof(s), "%d hub%s found - tap to choose",
                 (int)_scan_n, _scan_n == 1 ? "" : "s");
        setup_show_status(s);
    }
    setup_refresh_widgets();
}

// SAVE: merge the chosen hub into the known-hubs list, persist, restart.
// Put a hub at the front of the known list, promoting it if already there.
// Shared by explicit pairing on the setup screen and by isolation adoption
// below, because the EVICTION rule matters as much as the insertion and two
// copies of it would drift.
//
// Move-to-front makes the array its own recency order, so a full list evicts
// the hub paired longest ago.  It used to always overwrite the last slot, which
// made the earlier slots permanent and the last one a revolving door: a mount
// touring the building silently forgot whichever satellite it had paired most
// recently before this one.
//
// Ordering is rewritten only when a hub is newly chosen, never on an ordinary
// roam — hub_reacquire_poll() just updates last_hub, so following a stronger
// hub costs no NVS write beyond the one it already does.
static void hub_remember(const KnownHub &h) {
    int8_t found = -1;
    for (uint8_t i = 0; i < _cfg.n_hubs; i++)
        if (memcmp(_cfg.hubs[i].mac, h.mac, 6) == 0) { found = (int8_t)i; break; }
    uint8_t at;
    if (found >= 0) {
        at = (uint8_t)found;                      // re-pair: promote it
    } else if (_cfg.n_hubs < MAX_KNOWN_HUBS) {
        at = _cfg.n_hubs;                         // room: grow by one
        _cfg.n_hubs++;
    } else {
        at = MAX_KNOWN_HUBS - 1;                  // full: drop the oldest
    }
    for (uint8_t i = at; i > 0; i--) _cfg.hubs[i] = _cfg.hubs[i - 1];
    _cfg.hubs[0]  = h;
    _cfg.last_hub = 0;
}

// Note what the hub row here does and does not do.  It sets the identity and
// PAIRS with that base — adding it to the known list and making it active — but
// it does not pin it.  The restart below runs the boot pick, which chooses the
// strongest KNOWN base, so if a stronger one is in range that is what the mount
// will come up on.  That is deliberate: picking the strongest at boot is the
// whole point, and a screen that silently outranked it would be worse than one
// that never offered the choice.  To force a particular base, it has to be the
// strongest the mount can hear — or the only one.
static void setup_apply_save() {
    if (_setup_sel_id < 1 || _setup_sel_hub < 0) return;
    const KnownHub &sel = _scan_hub[_setup_sel_hub];

    _cfg.magic    = CFG_MAGIC;
    _cfg.mount_id = _setup_sel_id;
    hub_remember(sel);
    cfg_save();

    setup_show_status("Saved - restarting...");
    lv_refr_now(NULL);          // force the message onto the panel
    delay(800);
    esp_restart();              // boot clean as the new identity
}

static void setup_enter() {
    if (_setup_active) return;
    _setup_active  = true;
    _level_active  = false;
    _setup_sel_id  = _mount_id;          // preselect current identity
    _setup_sel_hub = -1;
    _scan_n        = 0;
    set_dim(false);
    char cur[44];
    if (_cfg_valid)
        snprintf(cur, sizeof(cur), "Now: CAM %d > %s %02X:%02X",
                 _mount_id, _cfg.hubs[_cfg.last_hub].ssid,
                 _hub_mac[4], _hub_mac[5]);
    else
        snprintf(cur, sizeof(cur), "UNPAIRED - pick ID, then a hub");
    lv_scr_load(_setup_scr);
    setup_show_status(cur);
    setup_refresh_widgets();
    setup_start_scan();                  // user came here to pair — scan now
}

static void setup_exit() {
    _setup_active = false;
    lv_scr_load(_main_scr);
}

// ---------------------------------------------------------------------------
// Hub reacquire — roaming between known hubs + channel self-healing
// ---------------------------------------------------------------------------
// When paired but the hub has been silent for a while (moved site? hub changed
// channel? this hub off, the other one on?), scan for ANY known hub and follow
// the strongest one found.  Runs only outside SETUP; scanning is safe here
// because the hub is silent anyway.

// Base silence before scanning for another.  Was 20 s — ten missed heartbeats,
// during which a mount whose only base has died sits deaf and does nothing.
// That is survivable where a mount can fall back to the hub and is the whole of
// the outage where it cannot: a basement mount reached by one satellite has no
// second path, so this interval IS its downtime.
//
// Now three missed heartbeats, derived from the base's own interval in
// shared/protocol.h so the two cannot drift apart.
#define REACQ_SILENT_MS  BASE_SILENT_MS
// Gap between retries while still silent.  Was 30 s, which undid the faster
// detection whenever the base took a moment to come back — a satellite reboot
// is ~15 s, so the first scan found nothing and the mount then waited half a
// minute before trying again.  10 s covers a reboot in two attempts.
//
// Still not a standing rescan: this only applies WHILE the base is silent.  A
// mount with a working base never scans at all.
#define REACQ_PERIOD_MS  10000UL
// WHEN A MOUNT CHOOSES ITS BASE: once, at boot, and then not again.
//
// A mount does not move while it is powered.  It is bolted to a stand, the
// stand stays where it was put, and nothing about which base is nearest can
// change between power-on and power-off.  So the choice is made once, on the
// only occasion the answer can differ from last time — the mount may well have
// been carried somewhere else while it was off — and after that the radio is
// left alone to do its job.
//
// This replaces a periodic "look for something stronger" scan that ran every
// five minutes for the life of the mount.  It was not free.  WiFi.scanNetworks()
// takes the radio off-channel for one to three seconds, and for that whole time
// ESP-NOW fails in BOTH directions: the hub reads the run of failures as a TX
// wedge, refreshes the peer, and the mount blinks OFFLINE and back in the PC
// app.  An overnight bench log showed it exactly — 174 wedges, one every 300 s
// to the second, phase locked to the mount's boot, ~100-150 txfail in a single
// burst each time, while 97% of all other health samples logged zero.  Twice
// the link failed to come back, the mount sat isolated for the full two-minute
// timeout and restarted itself.  Paid every five minutes, all night, to answer
// a question whose answer cannot change.
//
// What the periodic scan was originally for is still covered, by the paths
// below rather than by repetition:
//
//   a satellite deployed to reach this mount   the boot scan adopts it, and so
//                                              does the silent scan once the
//                                              current base stops answering
//   fell back to a distant hub after an outage the silent scan (REACQ_SILENT_MS)
//                                              switches freely, no margin needed
//
// The one case genuinely given up is a base that gets WORSE while still
// answering — someone re-rigs a wall mid-show and a better base appears.  That
// needs a power cycle, or the SETUP screen, which is a fair price for a link
// that stops dropping on its own every five minutes.
//
// REACQ_UPGRADE_MARGIN_DB went with the periodic scan.  It existed so a mount
// sitting between two similar bases could not ping-pong every five minutes;
// with nothing repeating, there is nothing to oscillate.

// How long fully isolated before the mount may adopt a base it has never been
// paired with.  Deliberately shorter than ESPNOW_RESTART_MS, so adoption gets
// a chance BEFORE the isolation restart throws away the attempt.
#define REACQ_ADOPT_MS   60000UL
// How much stronger an unknown base must be to be adopted while a known one is
// still audible.  Adoption keeps a margin even though choosing between KNOWN
// bases no longer needs one: moving between bases we already trust is cheap and
// reversible, taking a stranger is neither — it is provisional, it costs a
// pairing, and getting it wrong puts the mount on someone else's rig.
#define REACQ_ADOPT_MARGIN_DB  20
// The same test at boot — see the note where it is used.  6 dB rather than 20:
// enough that scan-to-scan noise (a couple of dB at rest) cannot unseat a known
// base, small enough that a satellite in the same room as the mount actually
// wins.  Under it, a mount paired only with the hub would ignore the satellite
// beside it every single boot.
#define REACQ_BOOT_ADOPT_MARGIN_DB  6

// ---------------------------------------------------------------------------
// Provisional adoption
// ---------------------------------------------------------------------------
// A prefix match is NOT proof of a hub.  The only test a candidate passes is
// that its SSID starts with "PTS-", and a WiFi repeater rebroadcasting the hub's
// own SSID passes it too — with a different BSSID, so it is never in the known
// list and is therefore always a candidate.  Adopting one is silent death:
// there is no ESP-NOW peer behind it, and the far-end guard this design leans on
// (the hub refusing a device that claims a slot bound to another MAC) never
// engages, because there is no far end.
//
// That is not hypothetical.  A repeater named "PTS-Hub..." was installed near
// mount 5 while the satellite was being commissioned, and mount 5 spent the
// morning off the air.
//
// So adoption is provisional.  If the newly adopted hub has said nothing within
// ADOPT_TRIAL_MS, the mount puts back the hub it left, drops the impostor from
// the known list, and remembers it for ADOPT_DUD_HOLD_MS so the next scan does
// not walk straight back into it.  Cost of a wrong guess: one trial window.
#define ADOPT_TRIAL_MS      20000UL
#define ADOPT_DUD_HOLD_MS   (10UL * 60UL * 1000UL)
#define ADOPT_DUD_MAX       4

static KnownHub _adopt_prev;                    // hub we left, to go back to
static bool     _adopt_prev_ok   = false;
static uint32_t _adopt_start_ms  = 0;           // 0 = no trial running
static uint8_t  _dud_mac[ADOPT_DUD_MAX][6] = {};
static uint32_t _dud_at[ADOPT_DUD_MAX]     = {};
static uint8_t  _dud_next = 0;

static bool dud_known(const uint8_t *mac, uint32_t nowm) {
    for (int i = 0; i < ADOPT_DUD_MAX; i++)
        if (_dud_at[i] && (nowm - _dud_at[i]) < ADOPT_DUD_HOLD_MS &&
            memcmp(_dud_mac[i], mac, 6) == 0) return true;
    return false;
}

static void dud_record(const uint8_t *mac, uint32_t nowm) {
    memcpy(_dud_mac[_dud_next], mac, 6);
    _dud_at[_dud_next] = nowm ? nowm : 1;
    _dud_next = (uint8_t)((_dud_next + 1) % ADOPT_DUD_MAX);
}

// Drop a hub from the known list.  Needed on revert: hub_remember() has already
// stored the impostor, and leaving it there would let the ordinary "follow the
// strongest known hub" path walk into it on the very next scan.
static void hub_forget(const uint8_t *mac) {
    for (uint8_t i = 0; i < _cfg.n_hubs; i++) {
        if (memcmp(_cfg.hubs[i].mac, mac, 6) != 0) continue;
        for (uint8_t k = i; k + 1 < _cfg.n_hubs; k++) _cfg.hubs[k] = _cfg.hubs[k + 1];
        _cfg.n_hubs--;
        if (_cfg.last_hub >= _cfg.n_hubs) _cfg.last_hub = 0;
        return;
    }
}

static uint32_t _reacq_last_ms = 0;
static bool     _reacq_boot_done   = false;  // the one-time boot pick has run

// Confirm or undo a provisional adoption.  Runs on every poll, not only around
// a scan: the verdict is about whether traffic arrived, which has nothing to do
// with scanning.
static void adopt_trial_poll(uint32_t nowm) {
    if (!_adopt_start_ms) return;
    if ((int32_t)(_last_hub_rx_ms - _adopt_start_ms) > 0) {
        Serial.printf("[REACQ] Adoption confirmed — \"%s\" is talking to us\n",
                      _cfg.hubs[_cfg.last_hub].ssid);
        _adopt_start_ms = 0;
        cfg_save();                       // only now is it worth persisting
        return;
    }
    if ((uint32_t)(nowm - _adopt_start_ms) < ADOPT_TRIAL_MS) return;

    uint8_t bad[6];
    memcpy(bad, _cfg.hubs[_cfg.last_hub].mac, 6);
    Serial.printf("[REACQ] Adopted \"%s\" %02X:%02X said nothing in %lus — not a hub, "
                  "reverting\n", _cfg.hubs[_cfg.last_hub].ssid, bad[4], bad[5],
                  (unsigned long)(ADOPT_TRIAL_MS / 1000UL));
    dud_record(bad, nowm);
    hub_forget(bad);
    if (_adopt_prev_ok) hub_remember(_adopt_prev);
    cfg_apply_active_hub();
    _adopt_start_ms = 0;
}

static void hub_reacquire_poll() {
    if (!_cfg_valid || _setup_active || _scan_running) { _reacq_scanning = false; return; }

    uint32_t nowm = millis();
    adopt_trial_poll(nowm);
    if (!_reacq_scanning) {
        bool silent = (nowm - _last_hub_rx_ms) > REACQ_SILENT_MS;
        // A scan takes the radio off-channel for a second or two.  When the base
        // has already gone quiet that costs nothing — we are not hearing it
        // anyway — but a scan must never happen mid-move: on a camera rig those
        // seconds are when an operator might press stop, and deafness is not
        // something to schedule into a live shot.  In practice this only guards
        // the boot pick, since a silent base makes it true anyway, and a mount
        // that has just powered on is not moving.
        bool safe_to_scan = silent || (_ms.state == STATE_IDLE);
        // The boot pick.  Deliberately not conditioned on `silent`: for the
        // first REACQ_SILENT_MS after power-on _last_hub_rx_ms is still 0 and
        // millis() is small, so a freshly booted mount that has never heard
        // anything reads as NOT silent.  Waiting for that to age out would put
        // twenty seconds between power-on and choosing a base.
        bool boot_pick = !_reacq_boot_done;
        // Scan only to make the boot choice, or because the base we chose has
        // stopped answering.  Nothing periodic: see the note above the REACQ
        // defines for what that cost and what replaces it.
        // A satellite came back and the hub said so.  Re-arm the one-time pick,
        // after the per-mount stagger so five mounts do not deafen themselves
        // together.  safe_to_scan still applies, so a MOVING mount waits.
        if (_reacq_requested && (int32_t)(nowm - _reacq_hold_ms) >= 0) {
            _reacq_requested = false;
            _reacq_boot_done = false;
            _reacq_last_ms   = 0;
        }
        bool due = boot_pick || (silent && (nowm - _reacq_last_ms) > REACQ_PERIOD_MS);
        if (safe_to_scan && due) {
            _reacq_last_ms  = nowm;
            _reacq_scanning = true;
            Serial.println(boot_pick ? "[REACQ] Boot — choosing the strongest base"
                                     : "[REACQ] Base silent — scanning for known bases");
            WiFi.scanDelete();
            WiFi.scanNetworks(true /*async*/, false);
        }
        return;
    }

    int n = WiFi.scanComplete();
    if (n == WIFI_SCAN_RUNNING) return;
    _reacq_scanning = false;
    // Marked done on COMPLETION, not when the scan started, so a scan cut short
    // by SETUP or a config reload is retried rather than silently skipped —
    // which would leave the mount on whatever base NVS last remembered without
    // anything saying the choice never actually happened.  Set even when the
    // scan found nothing: the silent path owns it from here.
    bool was_boot_pick = !_reacq_boot_done;
    _reacq_boot_done   = true;

    int8_t  best   = -1;
    int16_t bestdb = -32768;
    int16_t curdb  = -32768;    // the hub we are on, measured in THIS scan
    uint8_t bestch = 0;
    for (int i = 0; i < n; i++) {
        const uint8_t *bssid = WiFi.BSSID(i);
        for (uint8_t k = 0; k < _cfg.n_hubs; k++) {
            if (memcmp(bssid, _cfg.hubs[k].mac, 6) != 0) continue;
            if (k == _cfg.last_hub) curdb = (int16_t)WiFi.RSSI(i);
            if ((int16_t)WiFi.RSSI(i) > bestdb) {
                bestdb = (int16_t)WiFi.RSSI(i);
                best   = (int8_t)k;
                bestch = (uint8_t)WiFi.channel(i);
            }
        }
    }
    // Nothing known is in range.  A mount in that state cannot rescue itself
    // today: the loop above only matches hubs it has already been paired with,
    // so a satellite deployed specifically to reach it is invisible until
    // somebody walks up to the mount and pairs it by hand.  On a rig where
    // mounts tour the building that is the difference between a satellite
    // fixing a mount and needing a ladder.
    //
    // So: if we have heard nothing at all for REACQ_ADOPT_MS, adopt the
    // strongest AP whose SSID carries our prefix.  The prefix is the first
    // guard; the second is at the far end, where the hub's pairing rules
    // reject a device claiming a slot bound to a different MAC — so wandering
    // onto a neighbouring rig's hub is refused there rather than trusted here.
    // Strongest prefixed AP we do NOT already know, and have not already tried
    // and found silent.  Known ones are the `best` path's business.
    KnownHub adopt = {};
    bool     adopt_seen = false;
    int16_t  adopt_db   = -32768;
    for (int i = 0; i < n; i++) {
        String ssid = WiFi.SSID(i);
        if (!ssid.startsWith(HUB_SSID_PREFIX) &&
            !ssid.startsWith(HUB_SSID_PREFIX_OLD)) continue;
        const uint8_t *bssid = WiFi.BSSID(i);
        bool known = false;
        for (uint8_t k = 0; k < _cfg.n_hubs; k++)
            if (memcmp(bssid, _cfg.hubs[k].mac, 6) == 0) { known = true; break; }
        if (known || dud_known(bssid, nowm)) continue;
        if ((int16_t)WiFi.RSSI(i) <= adopt_db) continue;
        adopt_db = (int16_t)WiFi.RSSI(i);
        memcpy(adopt.mac, bssid, 6);
        adopt.channel = (uint8_t)WiFi.channel(i);
        snprintf(adopt.ssid, sizeof(adopt.ssid), "%s", ssid.c_str());
        adopt_seen = true;
    }

    // Two ways in.
    //
    // ISOLATED    nothing known in range, nothing heard for REACQ_ADOPT_MS.
    // OUTCLASSED  a known hub IS audible, but a stranger beats every known
    //             option by REACQ_ADOPT_MARGIN_DB.  Without this a mount that
    //             could still hear its old hub at -63 dB clung to it and never
    //             looked at a satellite in the same room 20 dB stronger — and
    //             the known-hub upgrade path could not rescue it either, because
    //             a satellite is by definition not in the known list.
    //
    // Compared against the BEST known reading, not merely the current one, so a
    // stranger cannot win a contest a known hub would have won.  Either way the
    // adoption is provisional — see adopt_trial_poll().
    // The margin is smaller at boot, because what it is protecting is smaller.
    //
    // Mid-run, taking a stranger means dropping a link that is working and
    // betting twenty seconds of trial on the replacement answering — with a
    // rig live, that has to be nearly certain, hence 20 dB.  At boot nothing is
    // depending on this mount yet: a trial that fails reverts to the base we
    // would otherwise have chosen, and costs only startup time nobody is
    // watching.  So the boot pick can afford to back a merely clear winner.
    //
    // Without this, "choose the strongest at boot" quietly means "choose the
    // strongest base you have already been paired with".  A mount set up
    // against the hub alone would keep passing over the foyer satellite sitting
    // beside it, because a satellite is by definition not in the known list —
    // which is exactly the trap the OUTCLASSED path was written for, reopened
    // by making the scan happen once instead of continuously.
    int16_t adopt_margin = was_boot_pick ? REACQ_BOOT_ADOPT_MARGIN_DB
                                         : REACQ_ADOPT_MARGIN_DB;
    int16_t known_db = (bestdb > curdb) ? bestdb : curdb;
    // REACQ_ADOPT_MS is a "ride out a momentary dropout" timer, and at boot
    // there is no dropout to ride out: nothing known answered the scan, so the
    // minute would be spent re-learning what we already know.  A mount carried
    // to a room served only by a satellite it has never met should come up on
    // it, not sit dark for a minute first.
    bool isolated    = (best < 0) &&
                       (was_boot_pick || (nowm - _last_hub_rx_ms) > REACQ_ADOPT_MS);
    bool outclassed  = (known_db > -32768) && (adopt_db > known_db + adopt_margin);
    bool adopt_ok    = adopt_seen && !_adopt_start_ms && (isolated || outclassed);

    if (adopt_ok && isolated && was_boot_pick)
        Serial.printf("[REACQ] Boot — no known base in range, trying \"%s\" %02X:%02X "
                      "on ch %d (%d dB)\n", adopt.ssid, adopt.mac[4], adopt.mac[5],
                      (int)adopt.channel, (int)adopt_db);
    else if (adopt_ok && isolated)
        Serial.printf("[REACQ] Isolated %lus — trying \"%s\" %02X:%02X on ch %d (%d dB)\n",
                      (unsigned long)((nowm - _last_hub_rx_ms) / 1000UL),
                      adopt.ssid, adopt.mac[4], adopt.mac[5],
                      (int)adopt.channel, (int)adopt_db);
    else if (adopt_ok)
        Serial.printf("[REACQ] Best known hub %d dB, \"%s\" %d dB (+%d) — trying "
                      "%02X:%02X on ch %d\n", (int)known_db, adopt.ssid,
                      (int)adopt_db, (int)(adopt_db - known_db),
                      adopt.mac[4], adopt.mac[5], (int)adopt.channel);

    WiFi.scanDelete();

    if (adopt_ok) {
        // Provisional: remember where to go back to, and do NOT persist yet —
        // an impostor must not survive a power cycle in the stored config.
        _adopt_prev    = _cfg.hubs[_cfg.last_hub];
        _adopt_prev_ok = true;
        _adopt_start_ms = nowm ? nowm : 1;
        hub_remember(adopt);
        cfg_apply_active_hub();
    } else if (best >= 0) {
        // Switching hub is free when the current one has gone quiet — we have
        // nothing to lose.  While it is still answering it must be a clear
        // upgrade, measured in the same scan so the two numbers are comparable,
        // or a mount sitting between two similar hubs would ping-pong and spend
        // its life re-pairing instead of working.
        // Straight to the strongest, no margin test.  A margin existed to stop a
        // mount between two similar bases ping-ponging on a scan it repeated
        // every five minutes — with the repetition gone there is nothing to
        // oscillate: this runs once at boot, and otherwise only when the base
        // we are on has stopped answering, where there is nothing to lose.
        bool hub_changed = (best != (int8_t)_cfg.last_hub);
        bool ch_changed  = (bestch != _cfg.hubs[best].channel);
        if (hub_changed || ch_changed) {
            _cfg.hubs[best].channel = bestch;
            _cfg.last_hub           = (uint8_t)best;
            cfg_save_deferred();      // a roam is a cache update, not a commitment
            cfg_apply_active_hub();
            Serial.printf("[REACQ] %s base \"%s\" %02X:%02X on ch %d (%d dB)%s\n",
                          was_boot_pick ? "Chose" : "Following",
                          _cfg.hubs[best].ssid, _hub_mac[4], _hub_mac[5],
                          (int)bestch, (int)bestdb,
                          hub_changed ? " — switched" : " — channel changed");
        } else if (was_boot_pick) {
            // Say the choice was made even when it changed nothing, so "stayed
            // on the base it already had" and "the boot scan never ran" are
            // distinguishable from a serial log.
            Serial.printf("[REACQ] Chose base \"%s\" %02X:%02X (%d dB) — already active\n",
                          _cfg.hubs[best].ssid, _hub_mac[4], _hub_mac[5], (int)bestdb);
        } else {
            Serial.println("[REACQ] Known base visible on stored channel — waiting");
        }
    }
    // Scanning wandered off-channel — always come back to the active hub.
    esp_wifi_set_channel(_hub_channel, WIFI_SECOND_CHAN_NONE);
    espnow_peer_refresh();
}

// ---------------------------------------------------------------------------
// Camera pairing screen
// ---------------------------------------------------------------------------
// Reached by holding the screen again from SETUP.  A second hidden gesture
// rather than another button, because SETUP is already full and this is a
// commissioning job done once per mount, by whoever built the rig.
//
// The mount goes OFF THE AIR while this is open — pairing stops WiFi so BLE has
// the radio — so it must not be possible to wander off and leave it here.
// Hence CAMPAIR_IDLE_MS: a mount silently out of service is the kind of fault
// that is discovered during a show.
#define CAMPAIR_IDLE_MS  (2UL * 60UL * 1000UL)

static lv_obj_t *_pair_scr    = nullptr;
static lv_obj_t *_pair_status = nullptr;
static lv_obj_t *_pair_code   = nullptr;
static char      _pair_buf[7] = "";
static uint8_t   _pair_n      = 0;
static uint32_t  _pair_idle_ms = 0;
// File-scope, not a function static, so re-entering the screen starts from a
// known state instead of inheriting the verdict from the last visit.
static BcPairState _pair_last_state = BCP_OFF;

static void campair_refresh() {
    if (!_pair_active) return;
    // Six slots always drawn, so the number of digits still owed is visible at
    // a glance rather than being counted.
    char shown[16] = "";
    for (uint8_t i = 0; i < 6; i++) {
        shown[i * 2]     = (i < _pair_n) ? _pair_buf[i] : '-';
        shown[i * 2 + 1] = ' ';
    }
    shown[12] = 0;
    lv_label_set_text(_pair_code, shown);

    const char *msg;
    switch (ble_cam_pair_state()) {
        case BCP_SEARCHING:  msg = "Searching for a camera..."; break;
        case BCP_NO_CAMERA:  msg = "No camera in range";        break;
        case BCP_WANT_CODE:  msg = "Enter the code on the camera"; break;
        case BCP_PAIRED:     msg = "PAIRED";                    break;
        case BCP_FAILED:     msg = "Pairing FAILED - try again"; break;
        default:             msg = "";                          break;
    }
    lv_label_set_text(_pair_status, msg);
    lv_obj_set_style_text_color(_pair_status,
        ble_cam_pair_state() == BCP_PAIRED ? COL_SETUP_SEL : COL_DIM, 0);
}

static void campair_exit() {
    _pair_active = false;
    ble_cam_pair_end();          // restarts the chip unless WiFi was left up
    lv_scr_load(_main_scr);
}

static void campair_enter() {
    if (_pair_active) return;
    _pair_active   = true;
    _setup_active  = false;      // we came from SETUP; it is no longer showing
    _pair_n        = 0;
    _pair_buf[0]   = 0;
    _pair_idle_ms    = millis();
    _pair_last_state = BCP_OFF;
    ble_cam_pair_begin();
    lv_scr_load(_pair_scr);
    campair_refresh();
}

static void campair_key(char c) {
    _pair_idle_ms = millis();
    if (c == '<') {                       // backspace
        if (_pair_n) _pair_buf[--_pair_n] = 0;
    } else if (c == '#') {                // confirm — the green lobe tab
        if (_pair_n == 6) ble_cam_pair_submit((uint32_t)strtoul(_pair_buf, nullptr, 10));
    } else if (c == 'B') {                // leave, and put the mount back on air
        campair_exit();
    } else if (_pair_n < 6) {
        _pair_buf[_pair_n++] = c;
        _pair_buf[_pair_n]   = 0;
    }
    campair_refresh();
}

static void campair_poll(uint32_t now) {
    if (!_pair_active) return;
    // Redraw only when something changed.  This runs every loop pass, and
    // rewriting the labels at ~1 kHz would spend the whole frame budget
    // repainting text that has not moved.  Key presses refresh themselves.
    BcPairState st = ble_cam_pair_state();
    if (st != _pair_last_state) {
        // A fresh attempt means a fresh code: clear whatever was half-typed so
        // the operator is not appending to an abandoned entry.
        if (st == BCP_WANT_CODE || st == BCP_FAILED) { _pair_n = 0; _pair_buf[0] = 0; }
        _pair_last_state = st;
        _pair_idle_ms    = now;
        campair_refresh();
    }
    if ((now - _pair_idle_ms) > CAMPAIR_IDLE_MS) {
        Serial.println("[CAM] pairing screen idle — leaving, mount back on the air");
        campair_exit();
    }
}

static void campair_build() {
    _pair_scr = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(_pair_scr, COL_BG, 0);
    lv_obj_set_style_bg_opa(_pair_scr, LV_OPA_COVER, 0);
    lv_obj_remove_flag(_pair_scr, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *t = lv_label_create(_pair_scr);
    lv_label_set_text(t, "CAMERA PAIRING");
    lv_obj_set_style_text_font(t, &lv_font_montserrat_14, 0);
    lv_obj_set_style_text_color(t, COL_TEXT, 0);
    lv_obj_align(t, LV_ALIGN_TOP_MID, 0, 45);

    _pair_status = lv_label_create(_pair_scr);
    lv_label_set_text(_pair_status, "");
    lv_obj_set_style_text_font(_pair_status, &lv_font_montserrat_12, 0);
    lv_obj_set_style_text_color(_pair_status, COL_DIM, 0);
    lv_obj_align(_pair_status, LV_ALIGN_TOP_MID, 0, 72);

    _pair_code = lv_label_create(_pair_scr);
    lv_label_set_text(_pair_code, "- - - - - -");
    lv_obj_set_style_text_font(_pair_code, &lv_font_montserrat_24, 0);
    lv_obj_set_style_text_color(_pair_code, COL_TEXT, 0);
    lv_obj_align(_pair_code, LV_ALIGN_TOP_MID, 0, 100);

    // Keypad 3x4, with FORGET and OK moved out to the circle's side lobes.
    //
    // Squaring a keypad inside a round panel wastes the widest part of it, and
    // the first version spent a whole row on FORGET | BACK along the bottom.
    // Moving those two into the lobes buys that row back and gives every key
    // ~30% more height, which is the dimension that was short: at ~266 DPI the
    // old 46 px rows were about 4.4 mm, well under a fingertip.
    //
    // The keypad is deliberately NARROWER than the space allows.  It could run
    // to the lobes, but then its edge keys would sit hard against FORGET and OK
    // — and of the two, FORGET is the one you least want caught by a thumb that
    // missed 1 or 7.  32 px of clearance each side is cheap insurance.
    //
    // Geometry, so the next person does not re-derive it: the panel is 466 across
    // with centre 233, and the usable width at a given y is 2*sqrt(233^2 - dy^2).
    // The bottom row at y=388 has x=60..406, so 98..366 clears it; the side tabs
    // at y=168..298 have x=9..457, so 12 and 400 clear too.
    static const char *KEYS[12] = { "1","2","3", "4","5","6", "7","8","9",
                                    "B","0","<" };
    for (int i = 0; i < 12; i++) {
        lv_obj_t *b = lv_obj_create(_pair_scr);
        lv_obj_set_size(b, 86, 60);
        lv_obj_set_pos(b, 98 + (i % 3) * 91, 136 + (i / 3) * 64);
        lv_obj_set_style_radius(b, 8, 0);
        lv_obj_set_style_bg_color(b, COL_SETUP_BTN, 0);
        lv_obj_set_style_bg_opa(b, LV_OPA_COVER, 0);
        lv_obj_set_style_border_color(b, lv_color_hex(0x555555), 0);
        lv_obj_set_style_border_width(b, 1, 0);
        lv_obj_set_style_pad_all(b, 0, 0);
        lv_obj_remove_flag(b, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(b, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_t *l = lv_label_create(b);
        const char *k = KEYS[i];
        lv_label_set_text(l, k[0] == '<' ? LV_SYMBOL_BACKSPACE
                           : k[0] == 'B' ? "BACK" : k);
        lv_obj_set_style_text_font(l, k[0] == 'B' ? &lv_font_montserrat_12
                                                  : &lv_font_montserrat_16, 0);
        lv_obj_set_style_text_color(l, COL_TEXT, 0);
        lv_obj_center(l);
        lv_obj_add_event_cb(b, [](lv_event_t *e) {
            campair_key((char)(intptr_t)lv_event_get_user_data(e));
        }, LV_EVENT_CLICKED, (void *)(intptr_t)k[0]);
    }

    // The two lobe tabs.  Coloured because they are the only irreversible and
    // the only committing action on the screen, and on a round panel colour
    // reads faster than position.
    auto tab = [&](lv_coord_t x, uint32_t bg, const char *txt,
                   const lv_font_t *font, lv_event_cb_t cb) {
        lv_obj_t *b = lv_obj_create(_pair_scr);
        lv_obj_set_size(b, 54, 130);
        lv_obj_set_pos(b, x, 168);
        lv_obj_set_style_radius(b, 26, 0);
        lv_obj_set_style_bg_color(b, lv_color_hex(bg), 0);
        lv_obj_set_style_bg_opa(b, LV_OPA_COVER, 0);
        lv_obj_set_style_border_width(b, 0, 0);
        lv_obj_set_style_pad_all(b, 0, 0);
        lv_obj_remove_flag(b, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(b, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_t *l = lv_label_create(b);
        lv_label_set_text(l, txt);
        lv_obj_set_style_text_font(l, font, 0);
        lv_obj_set_style_text_color(l, COL_TEXT, 0);
        lv_obj_center(l);
        lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, nullptr);
    };
    tab(12,  0xB71C1C, "FORGET", &lv_font_montserrat_12,
        [](lv_event_t *) { ble_cam_forget(); campair_refresh(); });
    tab(400, 0x2E7D32, LV_SYMBOL_OK, &lv_font_montserrat_24,
        [](lv_event_t *) { if (_pair_n == 6) campair_key('#'); });
}

static void setup_build() {
    _setup_scr = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(_setup_scr, COL_BG, 0);
    lv_obj_set_style_bg_opa(_setup_scr, LV_OPA_COVER, 0);

    lv_obj_t *title = lv_label_create(_setup_scr);
    lv_label_set_text(title, "SETUP");
    lv_obj_set_style_text_font(title, &lv_font_montserrat_16, 0);
    lv_obj_set_style_text_color(title, COL_ACCENT, 0);
    lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 34);

    // This mount's own MAC — needed by nobody (pairing is automatic) but
    // invaluable when supporting someone else's build over the phone.
    lv_obj_t *own = lv_label_create(_setup_scr);
    char ownbuf[24];
    uint8_t mymac[6]; WiFi.macAddress(mymac);
    snprintf(ownbuf, sizeof(ownbuf), "this: %02X:%02X:%02X:%02X:%02X:%02X",
             mymac[0], mymac[1], mymac[2], mymac[3], mymac[4], mymac[5]);
    lv_label_set_text(own, ownbuf);
    lv_obj_set_style_text_font(own, &lv_font_montserrat_12, 0);
    lv_obj_set_style_text_color(own, COL_DIM, 0);
    lv_obj_align(own, LV_ALIGN_TOP_MID, 0, 58);

    // ── Mount-ID row: five 56 px round buttons ──────────────────────────
    lv_obj_t *idlbl = lv_label_create(_setup_scr);
    lv_label_set_text(idlbl, "CAMERA");
    lv_obj_set_style_text_font(idlbl, &lv_font_montserrat_12, 0);
    lv_obj_set_style_text_color(idlbl, COL_DIM, 0);
    lv_obj_set_pos(idlbl, 60, 86);
    for (int i = 0; i < 5; i++) {
        lv_obj_t *b = lv_obj_create(_setup_scr);
        lv_obj_set_size(b, 56, 56);
        lv_obj_set_pos(b, 60 + i * 70, 104);
        lv_obj_set_style_radius(b, LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_bg_color(b, COL_SETUP_BTN, 0);
        lv_obj_set_style_bg_opa(b, LV_OPA_COVER, 0);
        lv_obj_set_style_border_color(b, lv_color_hex(MOUNT_ACCENT_HEX[i]), 0);
        lv_obj_set_style_border_width(b, 2, 0);
        lv_obj_set_style_pad_all(b, 0, 0);
        lv_obj_remove_flag(b, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(b, LV_OBJ_FLAG_CLICKABLE);
        char nb[2] = { (char)('1' + i), 0 };
        lv_obj_t *nl = lv_label_create(b);
        lv_label_set_text(nl, nb);
        lv_obj_set_style_text_font(nl, &lv_font_montserrat_16, 0);
        lv_obj_set_style_text_color(nl, COL_TEXT, 0);
        lv_obj_center(nl);
        lv_obj_add_event_cb(b, [](lv_event_t *e) {
            _setup_sel_id = (uint8_t)(uintptr_t)lv_event_get_user_data(e);
            setup_refresh_widgets();
        }, LV_EVENT_CLICKED, (void *)(uintptr_t)(i + 1));
        _setup_id_btn[i] = b;
    }

    // ── Hub result rows ──────────────────────────────────────────────────
    for (int i = 0; i < MAX_SCAN_ROWS; i++) {
        lv_obj_t *r = lv_obj_create(_setup_scr);
        lv_obj_set_size(r, 320, 36);
        lv_obj_set_pos(r, 73, 176 + i * 42);
        lv_obj_set_style_radius(r, 8, 0);
        lv_obj_set_style_bg_color(r, COL_SETUP_BTN, 0);
        lv_obj_set_style_bg_opa(r, LV_OPA_COVER, 0);
        lv_obj_set_style_border_color(r, lv_color_hex(0x383838), 0);
        lv_obj_set_style_border_width(r, 2, 0);
        lv_obj_set_style_pad_all(r, 0, 0);
        lv_obj_remove_flag(r, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(r, (lv_obj_flag_t)(LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_HIDDEN));
        lv_obj_t *l = lv_label_create(r);
        lv_label_set_text(l, "");
        lv_obj_set_style_text_font(l, &lv_font_montserrat_12, 0);
        lv_obj_set_style_text_color(l, COL_TEXT, 0);
        lv_obj_center(l);
        lv_obj_add_event_cb(r, [](lv_event_t *e) {
            _setup_sel_hub = (int8_t)(intptr_t)lv_event_get_user_data(e);
            setup_refresh_widgets();
        }, LV_EVENT_CLICKED, (void *)(intptr_t)i);
        _setup_hub_btn[i] = r;
        _setup_hub_lbl[i] = l;
    }

    // ── Status line ──────────────────────────────────────────────────────
    _setup_status = lv_label_create(_setup_scr);
    lv_label_set_text(_setup_status, "");
    lv_obj_set_style_text_font(_setup_status, &lv_font_montserrat_12, 0);
    lv_obj_set_style_text_color(_setup_status, COL_DIM, 0);
    lv_obj_align(_setup_status, LV_ALIGN_TOP_MID, 0, 350);

    // ── Bottom row: SCAN | SAVE | BACK ──────────────────────────────────
    auto make_btn = [&](lv_coord_t x, lv_coord_t w, const char *txt,
                        lv_event_cb_t cb) -> lv_obj_t * {
        lv_obj_t *b = lv_obj_create(_setup_scr);
        lv_obj_set_size(b, w, 44);
        lv_obj_set_pos(b, x, 370);
        lv_obj_set_style_radius(b, 10, 0);
        lv_obj_set_style_bg_color(b, COL_SETUP_BTN, 0);
        lv_obj_set_style_bg_opa(b, LV_OPA_COVER, 0);
        lv_obj_set_style_border_color(b, lv_color_hex(0x555555), 0);
        lv_obj_set_style_border_width(b, 1, 0);
        lv_obj_set_style_pad_all(b, 0, 0);
        lv_obj_remove_flag(b, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(b, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_t *l = lv_label_create(b);
        lv_label_set_text(l, txt);
        lv_obj_set_style_text_font(l, &lv_font_montserrat_12, 0);
        lv_obj_set_style_text_color(l, COL_TEXT, 0);
        lv_obj_center(l);
        lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, nullptr);
        return b;
    };
    lv_obj_t *scan_btn = make_btn(83, 104, "SCAN", [](lv_event_t *) {
        setup_start_scan();
    });
    _setup_scan_lbl = lv_obj_get_child(scan_btn, 0);
    _setup_save_btn = make_btn(195, 104, "SAVE", [](lv_event_t *) {
        setup_apply_save();
    });
    _setup_save_lbl = lv_obj_get_child(_setup_save_btn, 0);
    make_btn(307, 76, "BACK", [](lv_event_t *) {
        if (_cfg_valid) setup_exit();   // unpaired: nowhere to go back to
    });
}

// ---------------------------------------------------------------------------
// UI build
// ---------------------------------------------------------------------------

static void ui_build() {
    _main_scr  = lv_scr_act();
    _level_scr = lv_obj_create(NULL);
    lv_obj_t *scr = _main_scr;
    lv_obj_set_style_bg_color(scr, COL_BG, 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);

    /* ── Status ring ─────────────────────────────────────────────────────── */
    // Full-circle border (3px inset so border falls inside the visible circle)
    _ring = lv_obj_create(scr);
    lv_obj_set_size(_ring, 460, 460);
    lv_obj_set_pos(_ring, 3, 3);
    lv_obj_set_style_radius(_ring, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_opa(_ring, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_color(_ring, COL_RING_DISC, 0);
    lv_obj_set_style_border_width(_ring, 8, 0);
    lv_obj_set_style_border_opa(_ring, LV_OPA_COVER, 0);
    lv_obj_set_style_pad_all(_ring, 0, 0);
    lv_obj_remove_flag(_ring, (lv_obj_flag_t)(LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE));

    /* ── SL arc (left, 270°→348°, 45 min→58 min) ────────────────────────── */
    build_arc_strip(&_sl_strip, scr, 180, false);

    /* ── PT arc (right, 12°→90°, 2 min→15 min, reverse fill) ────────────── */
    build_arc_strip(&_pt_strip, scr, 282, true);

    /* ── Centre labels ───────────────────────────────────────────────────── */

    // Large mount number — 48pt scaled 2× via transform (effective ~96pt)
    // Fixed-size box ensures the pivot is always at the label's visual centre
    // regardless of which digit (1-5) is displayed.
    _lbl_num = lv_label_create(scr);
    char num[3];
    if (_mount_id >= 1) snprintf(num, sizeof(num), "%d", _mount_id);
    else                snprintf(num, sizeof(num), "?");   // unpaired
    lv_label_set_text(_lbl_num, num);
    lv_obj_set_style_text_font(_lbl_num, &lv_font_montserrat_48, 0);
    lv_obj_set_style_text_color(_lbl_num, COL_ACCENT, 0);
    lv_obj_set_style_text_align(_lbl_num, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_size(_lbl_num, 44, 58);   // wider than any single digit at 48pt
    lv_obj_set_style_transform_scale(_lbl_num, 768, 0);      // 3× (LV_SCALE_NONE=256)
    lv_obj_set_style_transform_pivot_x(_lbl_num, 22, 0);     // centre of 44px box
    lv_obj_set_style_transform_pivot_y(_lbl_num, 29, 0);     // centre of 58px box
    lv_obj_align(_lbl_num, LV_ALIGN_CENTER, 0, -20);

    // State text
    _lbl_state = lv_label_create(scr);
    lv_label_set_text(_lbl_state, "---");
    lv_obj_set_style_text_font(_lbl_state, &lv_font_montserrat_16, 0);
    lv_obj_set_style_text_color(_lbl_state, COL_DIM, 0);
    lv_obj_align(_lbl_state, LV_ALIGN_CENTER, 0, 36);

    // RSSI text
    _lbl_rssi = lv_label_create(scr);
    lv_label_set_text(_lbl_rssi, "");
    lv_obj_set_style_text_font(_lbl_rssi, &lv_font_montserrat_12, 0);
    lv_obj_set_style_text_color(_lbl_rssi, COL_DIM, 0);
    lv_obj_align(_lbl_rssi, LV_ALIGN_CENTER, 0, 58);

    /* ── Slot circles ────────────────────────────────────────────────────── */
    for (int i = 0; i < 10; i++) {
        int col  = i % 5;
        int row  = i / 5;
        lv_coord_t cx  = (row == 0) ? SLOT1_CX[col] : SLOT2_CX[col];
        lv_coord_t cy  = (row == 0) ? SLOT1_Y : SLOT2_Y;
        lv_coord_t dia = (row == 0) ? SLOT1_DIA : SLOT2_DIA;

        lv_obj_t *circle = lv_obj_create(scr);
        lv_obj_set_size(circle, dia, dia);
        lv_obj_set_pos(circle, cx - dia / 2, cy - dia / 2);
        lv_obj_set_style_radius(circle, LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_bg_color(circle, COL_SLOT_EMPTY, 0);
        lv_obj_set_style_bg_opa(circle, LV_OPA_COVER, 0);
        lv_obj_set_style_border_color(circle, lv_color_hex(0x383838), 0);
        lv_obj_set_style_border_width(circle, 1, 0);
        lv_obj_set_style_pad_all(circle, 0, 0);
        lv_obj_remove_flag(circle, (lv_obj_flag_t)(LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE));

        // Slot number
        char nbuf[3];
        snprintf(nbuf, sizeof(nbuf), "%d", i + 1);
        lv_obj_t *nlbl = lv_label_create(circle);
        lv_label_set_text(nlbl, nbuf);
        lv_obj_set_style_text_font(nlbl, &lv_font_montserrat_12, 0);
        lv_obj_set_style_text_color(nlbl, COL_DIM, 0);
        lv_obj_center(nlbl);

        _slot_obj[i] = circle;
    }

    /* ── Touch zones (transparent, full-height half-screens) ────────────── */
    // Full-screen touch zone — opens spirit level screen.
    lv_obj_t *z_level = lv_obj_create(scr);
    lv_obj_set_size(z_level, SCR_W, SCR_H);
    lv_obj_set_pos(z_level, 0, 0);
    lv_obj_set_style_bg_opa(z_level, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(z_level, 0, 0);
    lv_obj_set_style_pad_all(z_level, 0, 0);
    lv_obj_remove_flag(z_level, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(z_level, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(z_level, [](lv_event_t *) {
        if (_setup_active || _pair_active) return;   // hold switched screen mid-press
        if (_dimmed) {
            set_dim(false);   // first touch just wakes the screen
        } else {
            _level_active = true;
            lv_scr_load(_level_scr);
        }
    }, LV_EVENT_CLICKED, nullptr);

    lv_obj_update_layout(scr);

    build_level_screen();
    setup_build();
    campair_build();
}

// ---------------------------------------------------------------------------
// UI update — called whenever _ms changes
// ---------------------------------------------------------------------------

// Repaint only what actually changed.
//
// Measured before touching it: during a move this was redrawing 474 kpx per
// render pass — 2.18 whole screens — for 150 ms of LVGL time, on a mount whose
// loop then could not service a stop command for that long.
//
// The cause is that every lv_obj_set_style_*() marks its object dirty whether
// the value differs or not, and ui_update() ran on EVERY status change with a
// STATUS packet arriving many times a second during a move.  The first line was
// the worst of it: _ring is a full-circle border, so its bounding box is the
// entire 466x466 panel and recolouring it to the colour it already was cost a
// whole screen.
//
// Two earlier guesses at this missed — the QSPI flush (a quarter of the cost)
// and PSRAM draw buffers (no measurable difference, and confirmed to have been
// in internal RAM when it did not help). The area counter is what found it.
static void ui_update() {
    if (!_ring) return;

    /* Status ring — full-screen bounding box, so this guard is the big one */
    static lv_color_t s_ring;
    static bool       s_ring_valid = false;
    lv_color_t rc = ring_color();
    if (!s_ring_valid || !lv_color_eq(rc, s_ring)) {
        lv_obj_set_style_border_color(_ring, rc, 0);
        s_ring = rc; s_ring_valid = true;
    }

    /* State label */
    static const char *s_state_txt = nullptr;
    static bool        s_state_dim = true;
    const char *st = state_str(_ms.state);
    if (st != s_state_txt) { lv_label_set_text(_lbl_state, st); s_state_txt = st; }
    bool dim = !_ms.hub_connected;
    if (dim != s_state_dim) {
        lv_obj_set_style_text_color(_lbl_state, dim ? COL_DIM : COL_TEXT, 0);
        s_state_dim = dim;
    }

    /* Arc strips */
    update_arc_strip(&_pt_strip, _ms.pt_preset);
    update_arc_strip(&_sl_strip, _ms.sl_preset);

    /* Slot circles */
    bool la_mode = (_ms.flags & FLAG_LOOK_AT_MODE) != 0;
    for (int i = 0; i < 10; i++) {
        uint16_t bit = (uint16_t)(1 << i);
        lv_color_t col;
        bool is_active = la_mode ? (i < 8 && i == (int)_ms.active_la_subject)
                                 : !!(_ms.slot_at & bit);
        if ((_ms.target_slot == i) && (_ms.state == STATE_MOVING_TO_POS))
            col = COL_SLOT_TGT;
        else if (is_active)
            col = COL_SLOT_AT;
        else if ((_ms.slot_occupied & bit) && !(la_mode && i >= 8))
            col = COL_SLOT_OCC;
        else
            col = COL_SLOT_EMPTY;
        static lv_color_t s_slot[10];
        static bool       s_slot_valid = false;
        static uint16_t   s_slot_occ   = 0xFFFF;
        if (!s_slot_valid || !lv_color_eq(col, s_slot[i])) {
            lv_obj_set_style_bg_color(_slot_obj[i], col, 0);
            s_slot[i] = col;
        }
        if (((s_slot_occ ^ _ms.slot_occupied) & bit) || !s_slot_valid) {
            lv_obj_t *nlbl = lv_obj_get_child(_slot_obj[i], 0);
            if (nlbl) lv_obj_set_style_text_color(nlbl,
                (_ms.slot_occupied & bit) ? COL_TEXT : COL_DIM, 0);
        }
        if (i == 9) { s_slot_valid = true; s_slot_occ = _ms.slot_occupied; }
    }
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

void setup() {
    Serial.begin(115200);
    delay(200);
    crash_report_print();   // report the previous panic, if any

    // Validate the isolation-restart quota before anything can read it.
    // RTC_NOINIT is not cleared by the bootloader, so after a power-on or
    // brownout this holds whatever was in RAM — which could be a huge value
    // that silently disables the restarts entirely, or a small one that spends
    // a quota the operator has just reset by pulling the power.
    // A note from the restart that just happened, if there was one.  Checked
    // before anything can overwrite it, and consumed once so a power-cycle
    // later does not re-report an old event.
    if (_evt_magic == MOUNT_EVT_MAGIC) {
        _evt_magic   = 0;
        _evt_pending = true;
        if (_wedge_reboots)
            Serial.printf("[ESPNOW] wedge reboot %lu of %d\n",
                          (unsigned long)_wedge_reboots, TXWEDGE_MAX_REBOOTS);
        Serial.printf("[ESPNOW] last restart: kind %u, txfail %u, reinits %u, "
                      "RX stale %us, TX stale %us, refused %u (last err 0x%04X)\n",
                      _evt_kind, _evt_txfail, _evt_reinits, _evt_rx_s, _evt_tx_s,
                      _evt_refused, _evt_txerr);
    }
    if (_iso_magic != ISOLATION_RTC_MAGIC) {
        _iso_magic     = ISOLATION_RTC_MAGIC;
        _iso_restarts  = 0;
        // Same magic, same reasoning: RTC_NOINIT holds whatever was in RAM
        // after a power-on, and an unvalidated value here either disables the
        // wedge reboots or spends the quota a human just reset by pulling the
        // power.
        _wedge_reboots = 0;
    } else if (_iso_restarts) {
        Serial.printf("[ESPNOW] Resumed after isolation restart %lu of %d\n",
                      (unsigned long)_iso_restarts, ESPNOW_RESTART_MAX);
    }

    // Load runtime identity from NVS, then set the accent colour before any
    // UI calls.  Unpaired units get neutral grey and boot into SETUP.
    cfg_load();
    _col_accent = lv_color_hex(_cfg_valid ? MOUNT_ACCENT_HEX[_mount_id - 1]
                                          : 0x9E9E9E);
    if (_cfg_valid)
        Serial.printf("\n=== esp_mount_amoled175  CAM %d (NVS) ===\n", _mount_id);
    else
        Serial.println("\n=== esp_mount_amoled175  UNPAIRED — SETUP will open ===");

    // Teensy UART
    Serial1.begin(TEENSY_BAUD, SERIAL_8N1, TEENSY_RX, TEENSY_TX);

    // Touch — library init (handles reset sequence + I2C)
    _touch.setPins(TOUCH_RST, TOUCH_INT);
    if (!_touch.begin(Wire, TOUCH_ADDR, TOUCH_SDA, TOUCH_SCL)) {
        Serial.println("WARNING: Touch init failed — check wiring/address");
    }
    _touch.setMaxCoordinates(SCR_W, SCR_H);
    _touch.setMirrorXY(true, true);
    attachInterrupt(digitalPinToInterrupt(TOUCH_INT), touch_isr, FALLING);

    // IMU — QMI8658 (shares I2C bus, addr 0x6B)
    _qmi_ok = _qmi.begin(Wire, QMI8658_L_SLAVE_ADDRESS, TOUCH_SDA, TOUCH_SCL);
    if (!_qmi_ok) {
        Serial.println("WARNING: QMI8658 not found — spirit level disabled");
    } else {
        _qmi.configAccelerometer(SensorQMI8658::ACC_RANGE_4G,
                                 SensorQMI8658::ACC_ODR_125Hz,
                                 SensorQMI8658::LPF_MODE_0);
        _qmi.enableAccelerometer();
        Serial.println("QMI8658 accelerometer ready");
    }

    // ── Arduino_GFX + LVGL ───────────────────────────────────────────────
    _gfx->begin();
    _gfx->setBrightness(200);
    _gfx->fillScreen(0x0000);

    lv_init();

    // ONE draw buffer, in PSRAM, and both of those are measured rather than
    // assumed.
    //
    // UI_PROFILE on a moving mount said the loop stall is 220 ms, of which
    // lv_timer_handler() is 215 and the QSPI flush only 57.  So ~160 ms is LVGL
    // RENDERING rather than the bus.
    //
    // The obvious next move was internal RAM, since PSRAM writes on an S3 go
    // through the cache at a fraction of SRAM bandwidth.  It was tried, and the
    // profile confirmed the buffer really did land in internal RAM: rendering
    // went 160 ms to 150 ms.  Nothing.  So the cost is the drawing itself —
    // anti-aliased arcs, a full-circle ring and ten round slot indicators — and
    // not where the pixels live.
    //
    // Back in PSRAM for that reason.  Internal RAM is the scarce pool on this
    // board, wanted by WiFi, NimBLE and a 48 KB LVGL heap, and spending 37 KB of
    // it on a change measured at zero is a poor trade.  The remaining stall is
    // accepted deliberately: it only happens while a mount is moving, and ACK
    // round-trip measured across 214 move windows was unchanged at the median
    // (63 ms moving, 63 ms idle) — so the screen redraws slowly and nothing
    // else waits on it.
    //
    // The second buffer bought nothing and cost 37 KB.  Double buffering only
    // pays when a flush is asynchronous, so rendering can overlap it — but
    // draw16bitRGBBitmap() blocks and lvgl_flush_cb() calls
    // lv_display_flush_ready() the instant it returns.  There was never
    // anything to overlap; LVGL just alternated between two buffers.
    //
    // Ladder rather than one attempt: internal DMA-capable RAM is the scarce
    // thing on this board, with WiFi, NimBLE and a 48 KB LVGL pool already in
    // it.  Take the big buffer if it fits, half of it if not, and PSRAM as a
    // last resort so a mount still boots and draws either way — saying which,
    // because "the UI is slow again" is otherwise unattributable.
    struct { size_t bytes; uint32_t caps; const char *what; } tries[] = {
        { LVGL_BUF_BYTES,     MALLOC_CAP_SPIRAM,                    "PSRAM, 40 lines" },
        { LVGL_BUF_BYTES,     MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA, "internal, 40 lines" },
        { LVGL_BUF_BYTES / 2, MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA, "internal, 20 lines" },
    };
    size_t lvgl_buf_bytes = 0;
    for (auto &t : tries) {
        _lvgl_buf1 = (lv_color_t *)heap_caps_malloc(t.bytes, t.caps);
        if (_lvgl_buf1) {
            lvgl_buf_bytes = t.bytes;
#if UI_PROFILE >= 2
            _ui_buf_kind = (uint8_t)(&t - tries) + 1;
#endif
            Serial.printf("LVGL draw buffer: %s (%u bytes), free internal %u\n",
                          t.what, (unsigned)t.bytes,
                          (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
            break;
        }
    }
    if (!_lvgl_buf1) {
        Serial.println("FATAL: no memory anywhere for an LVGL draw buffer");
        while (true) delay(1000);
    }

    lv_display_t *disp = lv_display_create(SCR_W, SCR_H);
    _level_disp = disp;
    lv_display_set_flush_cb(disp, lvgl_flush_cb);
    lv_display_add_event_cb(disp, lvgl_rounder_cb, LV_EVENT_INVALIDATE_AREA, NULL);
    lv_display_set_buffers(disp, _lvgl_buf1, nullptr,
                           lvgl_buf_bytes, LV_DISPLAY_RENDER_MODE_PARTIAL);

    lv_indev_t *indev = lv_indev_create();
    lv_indev_set_type(indev, LV_INDEV_TYPE_POINTER);
    lv_indev_set_read_cb(indev, lvgl_touch_cb);

    lv_theme_t *th = lv_theme_default_init(disp,
        COL_ACCENT, COL_ACCENT, true, &lv_font_montserrat_16);
    lv_display_set_theme(disp, th);

    ui_build();
    ui_update();

    // ── WiFi / ESP-NOW ───────────────────────────────────────────────────
    WiFi.mode(WIFI_STA);
    WiFi.disconnect();
    esp_wifi_set_ps(WIFI_PS_NONE);
    esp_wifi_set_max_tx_power(84);   // max (~20.5 dBm) — the mount may be 50 m out
    esp_wifi_set_protocol(WIFI_IF_STA,
        WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N);
    delay(200);

    _espnow_rx_q = xQueueCreate(ESPNOW_RX_DEPTH, sizeof(EspNowMsg));

    if (esp_now_init() != ESP_OK) {
        Serial.println("ESP-NOW init failed");
        while (true) delay(1000);
    }
    esp_now_register_recv_cb(on_espnow_recv);
    esp_now_register_send_cb(on_espnow_sent);

    if (_cfg_valid) {
        // Paired: lock the stored channel and register the hub peer.  If the
        // hub has moved channel, the reacquire scan in loop() self-heals.
        esp_err_t chres = esp_wifi_set_channel(_hub_channel, WIFI_SECOND_CHAN_NONE);
        Serial.printf("Channel %d: %s\n", _hub_channel, esp_err_to_name(chres));
        espnow_peer_refresh();
        Serial.printf("Hub   MAC : %02X:%02X:%02X:%02X:%02X:%02X  (\"%s\")\n",
            _hub_mac[0], _hub_mac[1], _hub_mac[2],
            _hub_mac[3], _hub_mac[4], _hub_mac[5],
            _cfg.hubs[_cfg.last_hub].ssid);
    } else {
        Serial.println("Unpaired — no channel lock, no hub peer (SETUP will scan)");
    }

    pkt_parser_init(&_espnow_parser);
    pkt_parser_init(&_teensy_parser);
    _last_hub_rx_ms       = millis();
    _last_espnow_tx_ok_ms = millis();

    Serial.printf("Mount MAC : %s  (hub pairs to this automatically)\n",
                  WiFi.macAddress().c_str());

    // Hardware watchdog — resets the chip if loop() stalls for > 30 s
    // (e.g. QSPI deadlock, ESP-NOW stack hang, heap corruption).
    // trigger_panic = true so the timeout actually RESETS the chip; with false
    // it only logs and never recovers — defeating the point of the watchdog.
    esp_task_wdt_config_t twdt_cfg = {
        .timeout_ms     = HW_WDT_TIMEOUT_MS,
        .idle_core_mask = 0,
        .trigger_panic  = true,
    };
    esp_task_wdt_reconfigure(&twdt_cfg);
    esp_task_wdt_add(NULL);

    delay(100);
    send_status_heartbeat();
    _last_touch_ms = millis();   // start the 60 s dim countdown from here

    // Unpaired unit: nothing useful to do on the main screen — open SETUP
    // (which immediately starts a hub scan).
    if (!_cfg_valid) setup_enter();

    // Last, deliberately: ESP-NOW is up and settled before the BLE radio is
    // brought in, so anything the spike costs is visible as a change to a
    // working link rather than confused with a bad start.
    // Camera status goes straight back to the hub as CMD_CAM_STATUS, unread.
    // The mount is a pipe in both directions; only the PC app knows what a
    // Blackmagic parameter means, and keeping it that way means a new camera
    // feature never needs a mount reflash.
    ble_cam_on_status([](const uint8_t *d, uint16_t n) {
        if (!n || n > CAM_CONTROL_MAX_LEN) return;
        uint8_t buf[PKT_BUF_SIZE + 4];
        espnow_tx(buf,
                  build_packet(buf, _mount_id, ++_tx_seq, CMD_CAM_STATUS, d, n));
    });
    ble_cam_setup();
}

// ---------------------------------------------------------------------------
// Loop
// ---------------------------------------------------------------------------

// Drain all pending Teensy serial bytes.  Called before AND after
// lv_timer_handler() so bytes that arrive during LVGL rendering are
// never left in the FIFO long enough to overflow it (~11 ms at 115200).
static inline void drain_teensy_serial() {
    while (Serial1.available()) {
        ParsedPacket pkt;
        if (pkt_feed(&_teensy_parser, (uint8_t)Serial1.read(), &pkt))
            handle_teensy_packet(pkt);
    }
}

void loop() {
    esp_task_wdt_reset();
    ble_cam_poll();

    // Health telemetry: worst gap between loop iterations ≈ worst iteration.
    {
        static uint32_t _prev_loop_ms2 = 0;
        uint32_t nowh = millis();
        if (_prev_loop_ms2) {
            uint32_t gap = nowh - _prev_loop_ms2;
            if (gap > _health_loop_max_ms)
                _health_loop_max_ms = (gap > 65535) ? 65535 : (uint16_t)gap;
        }
        _prev_loop_ms2 = nowh;
    }

    // ── Teensy serial — drain BEFORE rendering so commands are never stale
    drain_teensy_serial();

    // ── LVGL tick + render (may block up to ~50 ms on full redraws) ──────
    static uint32_t _prev_ms = 0;
    const uint32_t now = millis();
    lv_tick_inc(now - _prev_ms);
    _prev_ms = now;
#if UI_PROFILE
    _ui_flush_accum_us = 0;
    uint32_t _ui_t0 = micros();
#endif
    lv_timer_handler();
#if UI_PROFILE
    {
        uint16_t total = (uint16_t)((micros() - _ui_t0) / 1000UL);
        uint16_t flush = (uint16_t)(_ui_flush_accum_us / 1000UL);
        if (total > _ui_lvgl_max_ms)  _ui_lvgl_max_ms  = total;
        if (flush > _ui_flush_max_ms) _ui_flush_max_ms = flush;
#if UI_PROFILE >= 2
        uint16_t kpx = (uint16_t)(_ui_inval_accum_px / 1000UL);
        if (kpx > _ui_inval_max_kpx) _ui_inval_max_kpx = kpx;
        uint16_t big = (uint16_t)(_ui_inval_big_px / 1000UL);
        if (big > _ui_inval_big_kpx) _ui_inval_big_kpx = big;
        _ui_inval_accum_px = 0;
        _ui_inval_big_px   = 0;
#endif
    }
#endif

    // ── Teensy serial — drain AFTER rendering to catch bytes that arrived
    //    while LVGL was flushing to the display
    drain_teensy_serial();

    // ── Touch & hold (~1.5 s): main -> SETUP -> CAMERA PAIRING ───────────
    // Chained rather than given a button of its own: SETUP has no room left,
    // and pairing is a once-per-mount commissioning job for whoever built the
    // rig, not something an operator needs to find.
    if (_press_started_ms && (millis() - _press_started_ms >= SETUP_HOLD_MS)) {
        _press_started_ms = 0;
        if (!_setup_active && !_pair_active)      setup_enter();
        else if (_setup_active && !_pair_active)  campair_enter();
    }

    // ── SETUP scan poller + hub reacquire ────────────────────────────────
    setup_poll_scan();
    campair_poll(millis());
    hub_reacquire_poll();
    cfg_save_poll();

    // ── Display dim timeout (never while SETUP is open) ──────────────────
    if (!_dimmed && !_setup_active &&
            (millis() - _last_touch_ms >= DIM_TIMEOUT_MS)) {
        if (_level_active) {
            _level_active = false;
            lv_scr_load(_main_scr);
        }
        set_dim(true);
    }

    // ── Spirit level update ──────────────────────────────────────────────
    if (_level_active) {
        update_level_screen();
    }

    // ── ESP-NOW peer refresh / full reinit (flagged from the send callback,
    //    actioned here — esp_now_*() must never run in the WiFi task)
    if (_espnow_need_refresh) {
        _espnow_need_refresh = false;
        espnow_peer_refresh();
    }
    if (_espnow_need_reinit) {
        _espnow_need_reinit = false;
        uint32_t rn = millis();
        if (_espnow_last_reinit_ms &&
                (rn - _espnow_last_reinit_ms) < ESPNOW_REINIT_MIN_GAP_MS) {
            // Held off — see ESPNOW_REINIT_MIN_GAP_MS.  Reset the ladder so it
            // has to climb the full twelve failures again rather than
            // re-requesting on the very next four.
            _espnow_refresh_count = 0;
            _espnow_reinit_held++;
            if (_espnow_reinit_held == 1 || (_espnow_reinit_held % 10) == 0)
                Serial.printf("[ESP-NOW] reinit held off (%lu since boot) — "
                              "%lus since the last one\n",
                              (unsigned long)_espnow_reinit_held,
                              (unsigned long)((rn - _espnow_last_reinit_ms) / 1000UL));
        } else {
            _espnow_last_reinit_ms = rn ? rn : 1;
            espnow_full_reinit();
        }
    }

    // ── One-way transmit wedge ───────────────────────────────────────────
    // Watched on the RATE of send failures, not on silence, so a mount that is
    // still receiving is not excused.  See TXWEDGE_FAILS_PER_S.
    {
        uint32_t nw    = millis();
        // Failures AND refusals.  This watched _espnow_fail_total alone, which
        // is incremented from the send callback — and a refusal never reaches
        // that callback, which is stated three functions up in this same file.
        // So the detector was blind to the exact fault it exists to catch.
        //
        // Measured 2026-08-15: mount 5 isolated twice, both times with the
        // queue full — 1770 refusals against txfail 680, then 1673 refusals
        // against txfail 15. The second is unambiguous: the radio was fine and
        // nothing was failing, the stack simply stopped accepting frames. TX
        // died a full 59 s before RX did, so there was a minute of exactly the
        // conditions this was written for, and it saw fifteen failures and did
        // nothing. Both ran on to a two-minute isolation and a full reboot.
        uint32_t fails = _espnow_fail_total + _espnow_tx_refused;
        bool rx_alive  = (nw - _last_hub_rx_ms) < HUB_TIMEOUT_MS;

        // Track when the failure counter last MOVED.  This is the only honest
        // measure of "recovered" available: nothing a remedy does can fake it.
        if (fails != _txw_quiet_val) { _txw_quiet_val = fails; _txw_quiet_ms = nw; }
        if (!_txw_quiet_ms) _txw_quiet_ms = nw;

        // A genuinely quiet spell hands the reboot quota back, so a wedge hours
        // ago does not leave the mount unable to rescue itself from the next.
        if (_wedge_reboots && (nw - _txw_quiet_ms) > TXWEDGE_CLEAR_MS)
            _wedge_reboots = 0;

        // Probation: did the WiFi restart actually work?  Assumed before,
        // measured now.
        if (_txw_probation_ms && (nw - _txw_probation_ms) >= TXWEDGE_PROBATION_MS) {
            uint32_t held = nw - _txw_probation_ms;
            uint32_t got  = fails - _txw_probation_f;
            _txw_probation_ms = 0;
            if ((got * 1000UL / held) >= TXWEDGE_FAILS_PER_S) {
                Serial.printf("[ESP-NOW] WiFi restart did NOT hold (%lu more fails "
                              "in %lus) — rebooting\n",
                              (unsigned long)got, (unsigned long)(held / 1000UL));
                if (_wedge_reboots < TXWEDGE_MAX_REBOOTS) {
                    _wedge_reboots++;
                    // Stash it before the reboot; this is the only chance.
                    _evt_magic   = MOUNT_EVT_MAGIC;
                    _evt_kind    = MOUNT_EVENT_TX_WEDGE_REBOOT;
                    _evt_txfail  = (uint16_t)fails;
                    _evt_reinits = (uint16_t)_reinit_count;
                    _evt_rx_s    = 0;              // RX was fine; that is the point
                    _evt_tx_s    = (uint16_t)(held / 1000UL);
                    _evt_refused = (uint16_t)_espnow_tx_refused;
                    _evt_txerr   = _espnow_last_tx_err;
                    esp_restart();
                } else {
                    // A boot has not fixed it either.  Stay up and keep working
                    // as best we can rather than cycling: a mount that reboots
                    // forever is worse than one that limps, and the log now says
                    // which this is.
                    static uint32_t moaned = 0;
                    if (!moaned || (nw - moaned) > 60000UL) {
                        moaned = nw ? nw : 1;
                        Serial.printf("[ESP-NOW] TX still wedged after %d reboots — "
                                      "staying up; this needs a human\n",
                                      TXWEDGE_MAX_REBOOTS);
                    }
                }
            } else {
                Serial.println("[ESP-NOW] WiFi restart held — TX recovered");
            }
        }

        if (!_cfg_valid || _setup_active || !rx_alive || _txw_probation_ms) {
            _txw_window_ms = 0;                  // not our case, or already acting
        } else if (!_txw_window_ms) {
            _txw_window_ms    = nw ? nw : 1;
            _txw_window_fails = fails;
        } else {
            uint32_t held = nw - _txw_window_ms;
            uint32_t got  = fails - _txw_window_fails;
            // Rate below threshold at any point: this is not a wedge, it is a
            // link having a bad moment.  Start the window again from here.
            if (held >= 1000 && (got * 1000UL / held) < TXWEDGE_FAILS_PER_S) {
                _txw_window_ms    = nw ? nw : 1;
                _txw_window_fails = fails;
            } else if (held >= TXWEDGE_SUSTAIN_MS) {
                Serial.printf("[ESP-NOW] TX wedged: %lu sends failed in %lus "
                              "while RX is healthy\n",
                              (unsigned long)got, (unsigned long)(held / 1000UL));
                _txw_window_ms = 0;
                if (espnow_wifi_restart()) {
                    // Reported once TX works again — it cannot be reported now,
                    // which is the whole problem being fixed.
                    _txw_report_due   = true;
                    _evt_txfail       = (uint16_t)fails;
                    _evt_reinits      = (uint16_t)_reinit_count;
                    _evt_rx_s         = 0;
                    _evt_tx_s         = (uint16_t)(held / 1000UL);
                    _evt_refused      = (uint16_t)_espnow_tx_refused;
                    _evt_txerr        = _espnow_last_tx_err;
                    // ...and now prove it worked, instead of assuming.
                    _txw_probation_ms = millis();
                    _txw_probation_f  = _espnow_fail_total;
                }
            }
        }
    }

    // ── Hub connection state ─────────────────────────────────────────────
    bool hub_ok = (millis() - _last_hub_rx_ms) < HUB_TIMEOUT_MS;
    // Contact restored — hand back the full quota, so a mount that recovers
    // and is later isolated again gets to retry rather than staying passive
    // because of an outage hours ago.
    if (hub_ok && _iso_restarts) _iso_restarts = 0;

    // Report the last restart, once, as soon as there is somewhere to report it.
    // Deliberately gated on hub_ok rather than sent at boot: a mount that comes
    // back still isolated would otherwise shout into the same void that hid the
    // event in the first place.
    if (_evt_pending && hub_ok) {
        _evt_pending = false;
        uint8_t p[MOUNT_EVENT_PAYLOAD_LEN] = {
            _evt_kind,
            (uint8_t)(_evt_txfail  >> 8), (uint8_t)_evt_txfail,
            (uint8_t)(_evt_reinits >> 8), (uint8_t)_evt_reinits,
            (uint8_t)(_evt_rx_s    >> 8), (uint8_t)_evt_rx_s,
            (uint8_t)(_evt_tx_s    >> 8), (uint8_t)_evt_tx_s,
            (uint8_t)(_evt_refused >> 8), (uint8_t)_evt_refused,
            (uint8_t)(_evt_txerr   >> 8), (uint8_t)_evt_txerr,
            (uint8_t)(_wifi_restarts > 255 ? 255 : _wifi_restarts) };
        send_to_hub(CMD_MOUNT_EVENT, p, sizeof(p));
    }

    // A one-way TX wedge that the WiFi-level restart cleared.  Gated on TX
    // actually working again, not on hub_ok: hub_ok is an RX test, and RX was
    // never the problem — sending this the instant the restart returned would
    // fire it straight back into the wedge it is reporting.
    if (_txw_report_due && (millis() - _last_espnow_tx_ok_ms) < 2000) {
        _txw_report_due = false;
        uint8_t p[MOUNT_EVENT_PAYLOAD_LEN] = {
            MOUNT_EVENT_TX_WEDGE,
            (uint8_t)(_evt_txfail  >> 8), (uint8_t)_evt_txfail,
            (uint8_t)(_evt_reinits >> 8), (uint8_t)_evt_reinits,
            (uint8_t)(_evt_rx_s    >> 8), (uint8_t)_evt_rx_s,
            (uint8_t)(_evt_tx_s    >> 8), (uint8_t)_evt_tx_s,
            (uint8_t)(_evt_refused >> 8), (uint8_t)_evt_refused,
            (uint8_t)(_evt_txerr   >> 8), (uint8_t)_evt_txerr,
            (uint8_t)(_wifi_restarts > 255 ? 255 : _wifi_restarts) };
        send_to_hub(CMD_MOUNT_EVENT, p, sizeof(p));
    }
    if (hub_ok != _ms.hub_connected) {
        _ms.hub_connected = hub_ok;
        ui_update();
        if (hub_ok) _watchdog_fired = false;
    }

    // Restart the chip only when the mount is TRULY isolated — i.e. we can
    // neither receive from the hub NOR successfully send to it for the full
    // window.  If our sends are still being ACKed (TX fresh) while RX is stale,
    // the link is one-way: a HUB-side send wedge that restarting THIS chip does
    // not fix (confirmed 2026-06-16 — a fresh-booted mount still couldn't
    // receive).  Restarting then only reboot-loops and blinks the camera, so we
    // hold off and let the hub be recovered from the PC (CMD_HUB_RESTART).
    uint32_t rx_age = millis() - _last_hub_rx_ms;
    uint32_t tx_age = millis() - _last_espnow_tx_ok_ms;

    // Mark when the silence began, and what the recovery ladder's counters read
    // at that moment.  See ESPNOW_RESTART_STALLED_MS: if neither has moved since,
    // no rung of the ladder is running and the long window buys nothing.
    static uint32_t _iso_since_ms = 0, _iso_fail_mark = 0, _iso_reinit_mark = 0;
    if (rx_age <= HUB_TIMEOUT_MS || tx_age <= HUB_TIMEOUT_MS) {
        _iso_since_ms = 0;                       // still in contact one way or another
    } else if (!_iso_since_ms) {
        _iso_since_ms    = millis() ? millis() : 1;
        _iso_fail_mark   = _espnow_fail_total;
        _iso_reinit_mark = _reinit_count;
    }
    bool ladder_running = _iso_since_ms &&
                          (_espnow_fail_total != _iso_fail_mark ||
                           _reinit_count      != _iso_reinit_mark);
    uint32_t iso_window = ladder_running ? ESPNOW_RESTART_MS
                                         : ESPNOW_RESTART_STALLED_MS;

    bool rx_stale = (rx_age > iso_window);
    bool tx_stale = (tx_age > iso_window);
    // _pair_active suspends this for the same reason _setup_active does, and it
    // matters more.  Camera pairing STOPS WIFI so BLE can have the radio, so
    // there is no RX and no TX by design — and because no send is being
    // attempted, the failure counters do not move, ladder_running is false and
    // the SHORT stalled window applies.  Twenty seconds, while somebody is
    // typing a six-digit code and confirming it on the camera.  The mount
    // rebooted mid-entry, every time, and the screen coming back looked like the
    // pairing screen timing out.
    //
    // Silence during pairing is not isolation.  It is the one case where the
    // mount is deliberately off the air and knows it.  The screen has its own
    // bound — CAMPAIR_IDLE_MS, two minutes, restarted by every keypress — and it
    // EXITS rather than restarting the chip, which is the right remedy for
    // "wandered off and left it open".
    if (_cfg_valid && !_setup_active && !_pair_active &&
            !hub_ok && rx_stale && tx_stale) {
        if (_iso_restarts < ESPNOW_RESTART_MAX) {
            _iso_restarts++;
            Serial.printf("[ESPNOW] Mount isolated (no RX or TX) after %lus — %s — "
                          "restarting chip to recover stack (attempt %lu of %d)\n",
                          (unsigned long)(iso_window / 1000UL),
                          ladder_running ? "recovery was running and did not help"
                                         : "nothing was being attempted (TX queue wedged)",
                          (unsigned long)_iso_restarts, ESPNOW_RESTART_MAX);
            // Leave a note for after the reboot — this is the only chance.
            _evt_magic   = MOUNT_EVT_MAGIC;
            _evt_kind    = MOUNT_EVENT_ISOLATED;
            _evt_txfail  = (uint16_t)_espnow_fail_total;
            _evt_reinits = (uint16_t)_reinit_count;
            // _evt_rx_s is pinned near ESPNOW_RESTART_MS by construction — the
            // restart fires the moment the LATER of the two crosses it, and RX
            // is the later one whenever TX died first.  So it is the gap between
            // the two that carries the information, not either on its own.
            _evt_rx_s    = (uint16_t)((millis() - _last_hub_rx_ms) / 1000UL);
            _evt_tx_s    = (uint16_t)((millis() - _last_espnow_tx_ok_ms) / 1000UL);
            _evt_refused = (uint16_t)_espnow_tx_refused;
            _evt_txerr   = _espnow_last_tx_err;
            esp_restart();
        } else {
            // Restarting has been tried and did not help.  Stay up and keep
            // scanning: hub_reacquire_poll() can adopt a satellite that was
            // deployed to reach us, and it cannot do that from a boot loop.
            static uint32_t last_note = 0;
            if (millis() - last_note > 60000UL) {
                last_note = millis();
                Serial.printf("[ESPNOW] Still isolated after %d restarts — staying up "
                              "and scanning instead of rebooting\n", ESPNOW_RESTART_MAX);
            }
        }
    }

    // ── Watchdog ─────────────────────────────────────────────────────────
    // The run's own deadman, ahead of the blunt one below.  The round trip this
    // replaced was accidentally a deadman: a run advanced only while the PC
    // could reach the mount, so unplugging the hub stopped it.  Owning the run
    // locally would otherwise have a mount ping-ponging a camera indefinitely
    // with nobody able to tell it to stop.
    //
    // Stops repeating rather than halting: the current leg finishes under its
    // own deceleration, and WATCHDOG_MS below still hard-stops everything two
    // seconds later if contact does not come back.
    if (_run_active && (millis() - _last_hub_rx_ms) > RUN_DEADMAN_MS)
        run_stop("no contact with any base");

    if (!_watchdog_fired && (millis() - _last_hub_rx_ms > WATCHDOG_MS)) {
        _watchdog_fired = true;
        send_estop_to_teensy();
    }

    // ── Drain ESP-NOW RX queue ───────────────────────────────────────────
    EspNowMsg en_msg;
    while (xQueueReceive(_espnow_rx_q, &en_msg, 0) == pdTRUE) {
        for (int i = 0; i < en_msg.len; i++) {
            ParsedPacket pkt;
            if (pkt_feed(&_espnow_parser, en_msg.data[i], &pkt))
                handle_hub_packet(pkt);
        }
    }

    // ── RSSI label refresh (every 2 s) ───────────────────────────────────
    if (_lbl_rssi && (now - _last_rssi_update_ms >= 2000)) {
        _last_rssi_update_ms = now;
        if (_ms.hub_connected) {
            // Camera state rides on the RSSI line rather than a line of its
            // own: the gap between here and the first row of slot circles is
            // ~19 px, too tight for another label, and there is 450 px of width
            // going spare.
            //
            // It is here at all because a mount that has just rebooted takes
            // some seconds to find its camera again, and with nothing on screen
            // saying so, the honest reading is "did that work?".  The wait was
            // never the complaint — the silence was.
            const char *cam = "";
            switch (ble_cam_ui_state()) {
                case BCU_LINKING:  cam = "  CAM...";        break;
                case BCU_READY:    cam = "  CAM " LV_SYMBOL_OK; break;
                case BCU_UNPAIRED: cam = "  CAM --";        break;
                default:           cam = "";                break;
            }
            char rssi_buf[28];
            snprintf(rssi_buf, sizeof(rssi_buf), "%d dBm%s", (int)_last_rssi, cam);
            lv_label_set_text(_lbl_rssi, rssi_buf);
            lv_color_t rssi_col;
            if      (_last_rssi >= -65) rssi_col = lv_color_hex(0x4CAF50);
            else if (_last_rssi >= -75) rssi_col = lv_color_hex(0xFFA726);
            else                        rssi_col = lv_color_hex(0xF44336);
            lv_obj_set_style_text_color(_lbl_rssi, rssi_col, 0);
        } else {
            lv_label_set_text(_lbl_rssi, "");
        }
    }

    // ── Heartbeat ────────────────────────────────────────────────────────
    if (now - _last_heartbeat_ms >= STATUS_HEARTBEAT_MS)
        send_status_heartbeat();

    // ── Health telemetry (10 s cadence, jog-deferred, anomaly-triggered) ─
    health_check_bridge(now);

    // ── Teensy probe ─────────────────────────────────────────────────────
    if (now - _last_teensy_st_ms >= TEENSY_PROBE_MS) {
        uint8_t probe[PKT_BUF_SIZE + 4];
        uint16_t plen = build_packet(probe, _mount_id, ++_tx_seq,
                                     CMD_GET_STATUS, nullptr, 0);
        Serial1.write(probe, plen);
        _last_teensy_st_ms = now;
    }
    // No delay() — lv_timer_handler() self-limits; removing the 5 ms dead
    // time keeps Serial1 latency well below the FIFO fill time (~11 ms).
}
