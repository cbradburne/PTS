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

#define HUB_SSID_PREFIX  "CamMount"   // scan filter — must match the hub's AP_SSID
#define MAX_KNOWN_HUBS   4

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

static void cfg_load() {
    _mount_prefs.begin("mcfg", false);
    size_t n = _mount_prefs.getBytes("cfg", &_cfg, sizeof(_cfg));
    _mount_prefs.end();
    _cfg_valid = (n == sizeof(_cfg) && _cfg.magic == CFG_MAGIC &&
                  _cfg.mount_id >= 1 && _cfg.mount_id <= 5 &&
                  _cfg.n_hubs >= 1 && _cfg.n_hubs <= MAX_KNOWN_HUBS &&
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
#define LVGL_BUF_LINES 40
#define LVGL_BUF_BYTES (SCR_W * LVGL_BUF_LINES * sizeof(lv_color_t))
static lv_color_t *_lvgl_buf1 = nullptr;
static lv_color_t *_lvgl_buf2 = nullptr;
static lv_display_t *_level_disp = nullptr;

static void lvgl_flush_cb(lv_display_t *disp, const lv_area_t *area,
                          uint8_t *px_map) {
    uint32_t w = (uint32_t)(area->x2 - area->x1 + 1);
    uint32_t h = (uint32_t)(area->y2 - area->y1 + 1);
    _gfx->draw16bitRGBBitmap(area->x1, area->y1, (uint16_t *)px_map, w, h);
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
static uint32_t _reinit_count       = 0;          // completed full ESP-NOW reinits
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

static void send_to_hub(CmdType cmd, const uint8_t *payload, uint8_t plen) {
    if (!_cfg_valid) return;   // unpaired — no hub to send to
    uint16_t n = build_packet(_tx_buf, _mount_id, ++_tx_seq, cmd, payload, plen);
    esp_now_send(_hub_mac, _tx_buf, n);
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


static void send_status_heartbeat() {
    uint8_t p[9];
    p[0] = _ms.state;
    p[1] = _ms.flags;
    p[2] = _ms.pt_preset;
    p[3] = _ms.sl_preset;
    p[4] = (_ms.slot_occupied >> 8) & 0xFF;
    p[5] =  _ms.slot_occupied & 0xFF;
    p[6] = (_ms.slot_at >> 8) & 0xFF;
    p[7] =  _ms.slot_at & 0xFF;
    p[8] =  _ms.target_slot;
    send_to_hub(CMD_STATUS, p, 9);
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
    h.flags         = anomaly ? 0x01 : 0x00;
    h.node_u32      = _reinit_count;
    uint8_t p[24];
    encode_health_payload(p, &h);
    send_to_hub(CMD_HEALTH, p, 24);   // no-op while unpaired (send_to_hub guards)
    _health_last_ms     = millis();
    _health_loop_max_ms = 0;
    _health_last_txfail = _espnow_fail_total;
    _health_first_sent  = true;
}

// Called each loop pass.  Jog-aware: the periodic send steps aside while jog
// traffic is flowing (ESP-NOW airtime matters mid-move), but never for more
// than 2 s past its slot — one 33-byte frame among a 50 Hz jog stream is noise.
static void health_check_bridge(uint32_t now) {
    if (!_cfg_valid) return;
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
    if (recv_info && recv_info->rx_ctrl)
        _last_rssi = (int8_t)recv_info->rx_ctrl->rssi;
    if (len <= 0 || len > ESPNOW_MAX_LEN) return;
    EspNowMsg msg;
    msg.len = (uint8_t)len;
    memcpy(msg.data, data, len);
    xQueueSend(_espnow_rx_q, &msg, 0);
}

static void on_espnow_sent(const wifi_tx_info_t *, esp_now_send_status_t s) {
    if (s == ESP_NOW_SEND_SUCCESS) {
        _espnow_consec_fails  = 0;
        _espnow_refresh_count = 0;
        _last_espnow_tx_ok_ms = millis();   // our send side is alive
    } else {
        // NB: ++ on a volatile is deprecated in C++20, so read-modify-write.
        _espnow_fail_total = _espnow_fail_total + 1;   // health telemetry (cumulative)
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

// ---------------------------------------------------------------------------
// Packet handlers
// ---------------------------------------------------------------------------

static void ui_update();  // forward declaration

static void handle_hub_packet(const ParsedPacket &pkt) {
    if (pkt.mount_id != _mount_id && pkt.mount_id != MOUNT_BROADCAST) return;
    _last_hub_rx_ms = millis();
    if (pkt.cmd == CMD_JOG) _last_jog_fwd_ms = _last_hub_rx_ms;  // health-send deferral
    uint8_t ack[PKT_BUF_SIZE + 4];
    esp_now_send(_hub_mac, ack, build_ack(ack, _mount_id, ++_tx_seq, pkt.seq));
    uint8_t fwd[PKT_BUF_SIZE + 4];
    Serial1.write(fwd, build_packet(fwd, pkt.mount_id, pkt.seq,
                                    pkt.cmd, pkt.payload, pkt.payload_len));
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
        if (changed) ui_update();
    }
    // Track active look-at subject for green dot display
    if (pkt.cmd == CMD_LOOK_AT_STATUS && pkt.payload_len >= 13) {
        uint8_t subj = pkt.payload[12];
        if (_ms.active_la_subject != subj) {
            _ms.active_la_subject = subj;
            ui_update();
        }
    }
    if (!_cfg_valid) return;   // unpaired — don't forward Teensy traffic anywhere
    uint8_t fwd[PKT_BUF_SIZE + 4];
    esp_now_send(_hub_mac, fwd, build_packet(fwd, _mount_id, pkt.seq,
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
        if (_setup_active) return;   // hold-gesture switched to SETUP mid-press
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
static lv_obj_t *_setup_hub_btn[MAX_KNOWN_HUBS] = {};
static lv_obj_t *_setup_hub_lbl[MAX_KNOWN_HUBS] = {};
static lv_obj_t *_setup_scan_lbl   = nullptr;   // label inside the scan button
static lv_obj_t *_setup_save_btn   = nullptr;
static lv_obj_t *_setup_save_lbl   = nullptr;
static lv_obj_t *_setup_status     = nullptr;   // current pairing / result line
static uint8_t   _setup_sel_id     = 0;         // 1-5; 0 = not chosen yet
static int8_t    _setup_sel_hub    = -1;        // index into _scan_hub[]
static bool      _scan_running     = false;
static uint8_t   _scan_n           = 0;
static KnownHub  _scan_hub[MAX_KNOWN_HUBS];
static int16_t   _scan_rssi[MAX_KNOWN_HUBS];

#define COL_SETUP_SEL   lv_color_hex(0x2E7D32)   // selected button fill
#define COL_SETUP_BTN   lv_color_hex(0x1E1E1E)   // idle button fill

static void setup_refresh_widgets() {
    // Mount-ID buttons: selected one filled with accent-green
    for (int i = 0; i < 5; i++) {
        lv_obj_set_style_bg_color(_setup_id_btn[i],
            (_setup_sel_id == i + 1) ? COL_SETUP_SEL : COL_SETUP_BTN, 0);
    }
    // Hub rows: selected row green border
    for (int i = 0; i < MAX_KNOWN_HUBS; i++) {
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
        if (!ssid.startsWith(HUB_SSID_PREFIX)) continue;
        int16_t rssi = (int16_t)WiFi.RSSI(i);
        // Insert sorted by signal strength, strongest first
        if (_scan_n >= MAX_KNOWN_HUBS && rssi <= _scan_rssi[MAX_KNOWN_HUBS - 1])
            continue;   // list full and this one is weaker than everything held
        int pos = _scan_n < MAX_KNOWN_HUBS ? _scan_n : MAX_KNOWN_HUBS - 1;
        while (pos > 0 && rssi > _scan_rssi[pos - 1]) pos--;
        if (pos >= MAX_KNOWN_HUBS) continue;
        for (int k = (int)((_scan_n < MAX_KNOWN_HUBS ? _scan_n : MAX_KNOWN_HUBS - 1)); k > pos; k--) {
            _scan_hub[k]  = _scan_hub[k - 1];
            _scan_rssi[k] = _scan_rssi[k - 1];
        }
        memcpy(_scan_hub[pos].mac, WiFi.BSSID(i), 6);
        _scan_hub[pos].channel = (uint8_t)WiFi.channel(i);
        strncpy(_scan_hub[pos].ssid, ssid.c_str(), 16);
        _scan_hub[pos].ssid[16] = '\0';
        _scan_rssi[pos] = rssi;
        if (_scan_n < MAX_KNOWN_HUBS) _scan_n++;
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
static void setup_apply_save() {
    if (_setup_sel_id < 1 || _setup_sel_hub < 0) return;
    const KnownHub &sel = _scan_hub[_setup_sel_hub];

    _cfg.magic    = CFG_MAGIC;
    _cfg.mount_id = _setup_sel_id;
    int8_t found = -1;
    for (uint8_t i = 0; i < _cfg.n_hubs; i++)
        if (memcmp(_cfg.hubs[i].mac, sel.mac, 6) == 0) { found = (int8_t)i; break; }
    if (found >= 0) {
        _cfg.hubs[found] = sel;              // refresh channel/ssid
        _cfg.last_hub    = (uint8_t)found;
    } else if (_cfg.n_hubs < MAX_KNOWN_HUBS) {
        _cfg.hubs[_cfg.n_hubs] = sel;
        _cfg.last_hub = _cfg.n_hubs;
        _cfg.n_hubs++;
    } else {
        _cfg.hubs[MAX_KNOWN_HUBS - 1] = sel; // full — replace the last entry
        _cfg.last_hub = MAX_KNOWN_HUBS - 1;
    }
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

#define REACQ_SILENT_MS  20000UL   // hub silence before we start scanning
#define REACQ_PERIOD_MS  30000UL   // min gap between reacquire scans

static uint32_t _reacq_last_ms = 0;

static void hub_reacquire_poll() {
    if (!_cfg_valid || _setup_active || _scan_running) { _reacq_scanning = false; return; }

    uint32_t nowm = millis();
    if (!_reacq_scanning) {
        if ((nowm - _last_hub_rx_ms) > REACQ_SILENT_MS &&
                (nowm - _reacq_last_ms) > REACQ_PERIOD_MS) {
            _reacq_last_ms  = nowm;
            _reacq_scanning = true;
            Serial.println("[REACQ] Hub silent — scanning for known hubs");
            WiFi.scanDelete();
            WiFi.scanNetworks(true /*async*/, false);
        }
        return;
    }

    int n = WiFi.scanComplete();
    if (n == WIFI_SCAN_RUNNING) return;
    _reacq_scanning = false;

    int8_t  best   = -1;
    int16_t bestdb = -32768;
    uint8_t bestch = 0;
    for (int i = 0; i < n; i++) {
        const uint8_t *bssid = WiFi.BSSID(i);
        for (uint8_t k = 0; k < _cfg.n_hubs; k++) {
            if (memcmp(bssid, _cfg.hubs[k].mac, 6) != 0) continue;
            if ((int16_t)WiFi.RSSI(i) > bestdb) {
                bestdb = (int16_t)WiFi.RSSI(i);
                best   = (int8_t)k;
                bestch = (uint8_t)WiFi.channel(i);
            }
        }
    }
    WiFi.scanDelete();

    if (best >= 0) {
        bool hub_changed = (best != (int8_t)_cfg.last_hub);
        bool ch_changed  = (bestch != _cfg.hubs[best].channel);
        if (hub_changed || ch_changed) {
            _cfg.hubs[best].channel = bestch;
            _cfg.last_hub           = (uint8_t)best;
            cfg_save();
            cfg_apply_active_hub();
            Serial.printf("[REACQ] Following hub \"%s\" %02X:%02X on ch %d (%d dB)%s\n",
                          _cfg.hubs[best].ssid, _hub_mac[4], _hub_mac[5],
                          (int)bestch, (int)bestdb,
                          hub_changed ? " — switched hub" : " — channel changed");
        } else {
            Serial.println("[REACQ] Known hub visible on stored channel — waiting");
        }
    }
    // Scanning wandered off-channel — always come back to the active hub.
    esp_wifi_set_channel(_hub_channel, WIFI_SECOND_CHAN_NONE);
    espnow_peer_refresh();
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
    for (int i = 0; i < MAX_KNOWN_HUBS; i++) {
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
        if (_setup_active) return;   // hold-gesture switched to SETUP mid-press
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
}

// ---------------------------------------------------------------------------
// UI update — called whenever _ms changes
// ---------------------------------------------------------------------------

static void ui_update() {
    if (!_ring) return;

    /* Status ring */
    lv_obj_set_style_border_color(_ring, ring_color(), 0);

    /* State label */
    lv_label_set_text(_lbl_state, state_str(_ms.state));
    lv_obj_set_style_text_color(_lbl_state,
        _ms.hub_connected ? COL_TEXT : COL_DIM, 0);

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
        lv_obj_set_style_bg_color(_slot_obj[i], col, 0);

        lv_obj_t *nlbl = lv_obj_get_child(_slot_obj[i], 0);
        if (nlbl) lv_obj_set_style_text_color(nlbl,
            (_ms.slot_occupied & bit) ? COL_TEXT : COL_DIM, 0);
    }
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

void setup() {
    Serial.begin(115200);
    delay(200);
    crash_report_print();   // report the previous panic, if any

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

    // Allocate LVGL draw buffers from PSRAM
    _lvgl_buf1 = (lv_color_t *)ps_malloc(LVGL_BUF_BYTES);
    _lvgl_buf2 = (lv_color_t *)ps_malloc(LVGL_BUF_BYTES);
    if (!_lvgl_buf1 || !_lvgl_buf2) {
        Serial.println("FATAL: PSRAM alloc failed for LVGL buffers");
        while (true) delay(1000);
    }

    lv_display_t *disp = lv_display_create(SCR_W, SCR_H);
    _level_disp = disp;
    lv_display_set_flush_cb(disp, lvgl_flush_cb);
    lv_display_add_event_cb(disp, lvgl_rounder_cb, LV_EVENT_INVALIDATE_AREA, NULL);
    lv_display_set_buffers(disp, _lvgl_buf1, _lvgl_buf2,
                           LVGL_BUF_BYTES, LV_DISPLAY_RENDER_MODE_PARTIAL);

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
    lv_timer_handler();

    // ── Teensy serial — drain AFTER rendering to catch bytes that arrived
    //    while LVGL was flushing to the display
    drain_teensy_serial();

    // ── Touch & hold (~1.5 s) opens SETUP from any screen ────────────────
    if (!_setup_active && _press_started_ms &&
            (millis() - _press_started_ms >= SETUP_HOLD_MS)) {
        _press_started_ms = 0;
        setup_enter();
    }

    // ── SETUP scan poller + hub reacquire ────────────────────────────────
    setup_poll_scan();
    hub_reacquire_poll();

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
        espnow_full_reinit();
    }

    // ── Hub connection state ─────────────────────────────────────────────
    bool hub_ok = (millis() - _last_hub_rx_ms) < HUB_TIMEOUT_MS;
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
    bool rx_stale = (millis() - _last_hub_rx_ms       > ESPNOW_RESTART_MS);
    bool tx_stale = (millis() - _last_espnow_tx_ok_ms > ESPNOW_RESTART_MS);
    if (_cfg_valid && !_setup_active && !hub_ok && rx_stale && tx_stale) {
        Serial.println("[ESPNOW] Mount isolated (no RX or TX) — restarting chip to recover stack");
        esp_restart();
    }

    // ── Watchdog ─────────────────────────────────────────────────────────
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
            char rssi_buf[12];
            snprintf(rssi_buf, sizeof(rssi_buf), "%d dBm", (int)_last_rssi);
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
