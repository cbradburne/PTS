/*
 * esp32_hub.ino — WiFi AP + ESP-NOW hub  (Seeed Studio XIAO ESP32S3)
 *
 * This board handles all networking: WiFi AP, ESP-NOW to mounts, TCP and
 * WebSocket to PC/phone clients.  Display output is forwarded to a separate
 * Waveshare ESP32-S3-Touch-LCD-7 board over UART using the disp_uart protocol.
 *
 * Architecture:
 *   [PC 1]    ── TCP (port 7777) ──┐
 *   [PC 2]    ── TCP (port 7777) ──┤
 *   [PC USB]  ── Serial 921600   ──┤── XIAO ESP32S3 Hub ──┬── Mount 1 ESP32
 *   [Phone]   ── WS  (port 80)   ──┘   (WiFi AP+ESP-NOW)  ├── Mount 2 ESP32
 *   [Tablet]  ── WS  (port 80)              │             ├── Mount 3 ESP32
 *                                           │ UART        ├── Mount 4 ESP32
 *                                     Waveshare display   └── Mount 5 ESP32
 *
 * UART wiring to display (3 wires):
 *   XIAO D6 / GPIO43 (TX)  →  Waveshare GPIO13 (RX)
 *   XIAO D7 / GPIO44 (RX)  ←  Waveshare GPIO12 (TX)
 *   XIAO GND               —  Waveshare GND
 *
 * Arduino IDE board settings for THIS board (XIAO ESP32S3):
 *   Board            : XIAO_ESP32S3
 *   USB CDC On Boot  : Enabled
 *   USB Mode         : USB-OTG (TinyUSB)   ← REQUIRED for Serial.enableReboot(false),
 *                      which stops the PC from resetting the hub when it reopens the
 *                      port.  The default "Hardware CDC and JTAG" mode CANNOT do this.
 *                      NOTE: TinyUSB mode disables esptool auto-reset — to reflash, hold
 *                      BOOT, tap RESET, release, then upload.  The USB VID/PID also
 *                      changes, so Windows may assign a new COM port (update the PC config).
 *
 * Requires libraries:
 *   mathieucarbou/ESPAsyncWebServer  (ESP-IDF v5 compatible fork)
 *   mathieucarbou/AsyncTCP
 */

#include <WiFi.h>
#include <esp_wifi.h>
#include <esp_now.h>
#include <freertos/queue.h>
#include <ESPAsyncWebServer.h>
#include <esp_task_wdt.h>
#include <esp_system.h>   // esp_reset_reason()
#include <sys/socket.h>   // ::send/MSG_DONTWAIT — see broadcast_to_all()
#include "../shared/disp_uart.h"
#include "web_app.h"
#include "hub_types.h"
#include "../shared/crash_report.h"   // RelayMsg — must be last so it follows all other includes

// ---------------------------------------------------------------------------
// Required Arduino board settings — enforced at compile time
// ---------------------------------------------------------------------------
// 'Serial' must be the native USB CDC (so the PC↔hub USB link works at all) AND
// in TinyUSB mode (so Serial.enableReboot(false) exists — the fix that stops the
// PC from resetting the hub when it reopens the port).  Without these, the build
// either fails cryptically ("HardwareSerial has no member setTxTimeoutMs",
// because Serial falls back to UART0) or silently flashes the DTR-reset bug back.
#if !ARDUINO_USB_CDC_ON_BOOT
  #error "Tools -> 'USB CDC On Boot' must be ENABLED (otherwise Serial is UART0, not USB)."
#endif
#if ARDUINO_USB_MODE != 0
  #error "Tools -> 'USB Mode' must be 'USB-OTG (TinyUSB)' (required for Serial.enableReboot(false))."
#endif

// ---------------------------------------------------------------------------
// Configuration — edit before flashing
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// DEMO MODE — five simulated mounts, for photography and UI work with no rig
// ---------------------------------------------------------------------------
// Set to 1, flash the hub, and it invents five mounts with stored positions.
// Because the hub is the switchboard, the 7" display, the PC app and the web
// app all light up identically with NO changes to any of them — recalls flash
// amber while "travelling", then settle green on arrival.
//
// Set back to 0 and reflash for a real rig.  Demo builds never write the
// pairing table (mount_table_save() is a no-op), so nothing persists.
#ifndef DEMO_MODE                // -DDEMO_MODE=1 also works, without editing this
#define DEMO_MODE        0
#endif
#define DEMO_TRAVEL_MS   15000   // simulated travel time for a recall

#define AP_SSID      "CamMount"
// The AP password is NOT a secret in this repository and must not be one.
//
// This is a public tree, so anything committed here is published: changing the
// literal only publishes a different password. The real one is baked at build
// time instead —
//
//     AP_PASSWORD=yourpassword tools/build.sh flash hub
//
// — and never committed. What is left here is a placeholder that gets a rig on
// the air out of the box and is meant to be replaced. WPA2 needs at least 8
// characters: shorter and softAP() refuses the password, which does not fail
// loudly, it just brings the AP up OPEN.
#ifndef AP_PASSWORD
#define AP_PASSWORD  "changeme123"
#endif
#define AP_CHANNEL   1

// Static soft-AP network config.  Clients (phone/tablet/laptop over WS/TCP)
// connect to AP_IP — this is the hub's address on its own WiFi network.
// (The directly-wired PC uses USB serial, so it's unaffected by this.)
//
// The standard SoftAP address, and the one every doc, the manual and the PC
// app's defaults have always quoted.  It used to be 169.254.22.22/16 for Dante
// audio-network compatibility; that moved to the Ethernet interface on the
// ESP32-S3-ETH hub, where it belongs, and both hubs now raise the same AP so a
// client does not have to know which one it is talking to.
static const IPAddress AP_IP     (192, 168, 4, 1);
static const IPAddress AP_GATEWAY(192, 168, 4, 1);     // the AP is its own gateway
static const IPAddress AP_SUBNET (255, 255, 255, 0);
#define TCP_PORT     7777
#define MAX_CLIENTS  4

// ---------------------------------------------------------------------------
// Paired-mount table — RUNTIME, no MAC addresses in source
// ---------------------------------------------------------------------------
// Learned by first contact and persisted in NVS.  Every packet a mount sends
// carries the mount ID chosen on its own touchscreen; the hub binds
// {MAC → slot} the first time it hears each device (see mount_table_observe
// for the three binding rules).  An all-zero entry means the slot is unbound.
//
// Migration from the old hard-coded MOUNT_MACS[]: automatic — on the first
// boot with an empty table, every powered mount heartbeats within ~5 s and
// binds itself to the slot it already claims.

#include <Preferences.h>

static uint8_t     _mount_mac[NUM_MOUNTS][6] = {};   // all-zero = unbound
static Preferences _prefs;

static bool mount_mac_valid(uint8_t idx) {
    if (idx >= NUM_MOUNTS) return false;
    const uint8_t *m = _mount_mac[idx];
    return (m[0] | m[1] | m[2] | m[3] | m[4] | m[5]) != 0;
}

// Slot index this MAC is bound to, or -1.
static int8_t mount_table_find(const uint8_t mac[6]) {
    for (int i = 0; i < NUM_MOUNTS; i++)
        if (mount_mac_valid(i) && memcmp(mac, _mount_mac[i], 6) == 0)
            return (int8_t)i;
    return -1;
}

static void mount_table_save() {
#if DEMO_MODE
    return;   // demo builds must never persist their fake mounts (see DEMO_MODE)
#else
    _prefs.begin("mounts", false);
    _prefs.putBytes("table", _mount_mac, sizeof(_mount_mac));
    _prefs.end();
#endif
}

static void mount_table_load() {
    _prefs.begin("mounts", false);   // r/w so a missing namespace is created quietly
    size_t n = _prefs.getBytes("table", _mount_mac, sizeof(_mount_mac));
    _prefs.end();
    if (n != sizeof(_mount_mac))
        memset(_mount_mac, 0, sizeof(_mount_mac));   // absent/short — start unbound
}

// ---------------------------------------------------------------------------
// UART link to display board
// ---------------------------------------------------------------------------

// XIAO ESP32S3: D6=GPIO43 (TX), D7=GPIO44 (RX)
// On XIAO with native USB, Serial (USB CDC) is separate from UART hardware,
// so GPIO43/44 are free for Serial1.
#define DISP_TX_PIN  43
#define DISP_RX_PIN  44


// Mutex protecting Serial1 (display UART) — both loop() and forward_to_mounts
// (called from loop) write to it, so no real concurrency issue, but explicit
// for clarity if this ever moves to a separate task.
// A MUTEX, not a spinlock — see the identical note in esp32_hub_eth.ino.
// Every one of these sections wraps disp_uart_send(), whose Serial1.write()
// blocks when the TX buffer is full, waiting for the UART interrupt to drain
// it.  portENTER_CRITICAL() disables interrupts, so that ISR could not run and
// the write waited for space only an interrupt could free — the interrupt
// watchdog then reset the hub at 300 ms (reset reason INT_WDT).
//
// Every caller is reached from loop(), never an ISR, which is what makes a
// blocking primitive legal here.
static SemaphoreHandle_t _disp_mux = nullptr;

static inline void disp_lock()   { if (_disp_mux) xSemaphoreTake(_disp_mux, portMAX_DELAY); }
static inline void disp_unlock() { if (_disp_mux) xSemaphoreGive(_disp_mux); }

// ---------------------------------------------------------------------------
// Display send helpers
// ---------------------------------------------------------------------------

static void disp_update_cam(uint8_t mount_id, uint8_t state, uint8_t flags, int8_t rssi) {
    uint8_t buf[4] = { mount_id, state, flags, (uint8_t)rssi };
    disp_lock();
    disp_uart_send(Serial1, DISP_MSG_UPDATE_CAM, buf, sizeof(buf));
    disp_unlock();
}

static void disp_set_disconnected(uint8_t mount_id) {
    disp_lock();
    disp_uart_send(Serial1, DISP_MSG_SET_DISCONNECTED, &mount_id, 1);
    disp_unlock();
}

// Network-client mirrors of the two display pushes below (defined later, after
// the client-broadcast helpers).  Every table change and every conflict already
// funnels through disp_send_mount_table() / disp_send_pair_conflict() for the
// console, so notifying TCP/WS/USB clients from the same two spots keeps the web
// app and PC app in lock-step with the display — all reading the hub's one table.
// Both run in loop() context (on_espnow_recv only queues), so inline sends are safe.
static void bcast_mount_table();
static void bcast_pair_conflict(uint8_t cam, const uint8_t *new_mac,
                                const uint8_t *old_mac);

// Push the paired-mount table (5 × MAC, zero = unbound) to the display board.
// Sent at boot, on every table change, and on DISP_MSG_GET_MOUNT_TABLE.
static void disp_send_mount_table() {
    uint8_t buf[NUM_MOUNTS * 6];
    memcpy(buf, _mount_mac, sizeof(buf));
    disp_lock();
    disp_uart_send(Serial1, DISP_MSG_MOUNT_TABLE, buf, sizeof(buf));
    disp_unlock();
    bcast_mount_table();                        // ...and to TCP / WS / USB clients
}

// Show (cam 1-5) or dismiss (cam 0) the pairing-conflict prompt on the display.
static void disp_send_pair_conflict(uint8_t cam, const uint8_t *new_mac,
                                    const uint8_t *old_mac) {
    uint8_t buf[13] = {};
    buf[0] = cam;
    if (new_mac) memcpy(buf + 1, new_mac, 6);
    if (old_mac) memcpy(buf + 7, old_mac, 6);
    disp_lock();
    disp_uart_send(Serial1, DISP_MSG_PAIR_CONFLICT, buf, sizeof(buf));
    disp_unlock();
    bcast_pair_conflict(cam, new_mac, old_mac);   // ...and to TCP / WS / USB clients
}

static void disp_update_preset(uint8_t mount_id, uint8_t pt, uint8_t sz) {
    uint8_t buf[3] = { mount_id, pt, sz };
    disp_lock();
    disp_uart_send(Serial1, DISP_MSG_UPDATE_PRESET, buf, sizeof(buf));
    disp_unlock();
}

static void disp_update_clients(uint8_t tcp_count, uint8_t ws_count) {
    uint8_t buf[2] = { tcp_count, ws_count };
    disp_lock();
    disp_uart_send(Serial1, DISP_MSG_UPDATE_CLIENTS, buf, sizeof(buf));
    disp_unlock();
}

static void disp_update_slots(uint8_t mount_id, uint16_t slot_occupied,
                               uint16_t slot_at, uint8_t target_slot, uint8_t state) {
    uint8_t buf[7] = {
        mount_id,
        (uint8_t)(slot_occupied >> 8), (uint8_t)(slot_occupied & 0xFF),
        (uint8_t)(slot_at      >> 8), (uint8_t)(slot_at      & 0xFF),
        target_slot,
        state
    };
    disp_lock();
    disp_uart_send(Serial1, DISP_MSG_UPDATE_SLOTS, buf, sizeof(buf));
    disp_unlock();
}

static void refresh_espnow_peer(uint8_t idx);

// ---------------------------------------------------------------------------
// UART receive parser — display sends SEND_CMD when user taps "Apply"
// ---------------------------------------------------------------------------

static enum { RX_MAGIC, RX_TYPE, RX_LEN, RX_PAYLOAD, RX_CHECKSUM }
    _rx_state = RX_MAGIC;
static uint8_t _rx_type = 0, _rx_len = 0, _rx_pos = 0;
static uint8_t _rx_buf[DISP_UART_MAX_PAYLOAD];

static void process_disp_byte(uint8_t b);
static void dispatch_disp_msg(uint8_t type, uint8_t len, const uint8_t *data);

// ---------------------------------------------------------------------------
// Relay queue  (ESP-NOW callback → main loop, thread-safe)
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

static void refresh_espnow_peer(uint8_t idx) {
    // Delete and re-add the peer to clear any stale internal send state that
    // accumulates while the mount is offline (failed-send state in the ESP-NOW
    // stack causes subsequent sends to silently fail even after the mount reboots).
    if (!mount_mac_valid(idx)) return;   // unbound slot — no peer to refresh
    esp_now_del_peer(_mount_mac[idx]);
    esp_now_peer_info_t peer = {};
    memcpy(peer.peer_addr, _mount_mac[idx], 6);
    peer.channel = AP_CHANNEL;
    peer.ifidx   = WIFI_IF_AP;
    peer.encrypt = false;
    esp_now_add_peer(&peer);
    espnow_peer_long_range(peer.peer_addr);
    Serial.printf("ESP-NOW peer %d refreshed\n", idx + 1);
}

// ---------------------------------------------------------------------------
// ESP-NOW send callback — detect silent delivery failures
// ---------------------------------------------------------------------------
// esp_now_send() returns ESP_OK when the packet is *accepted by the stack*,
// NOT when it is delivered.  The send callback is the only way to know whether
// the peer actually ACKed the packet.  If we see consecutive failures we
// refresh the peer, which clears any stale internal state in the ESP-NOW stack
// (the most common cause of "hub display fine, commands not reaching mount").

#define ESPNOW_MAX_CONSEC_FAILS 4   // refresh after this many consecutive failures

static volatile uint8_t _espnow_fails[NUM_MOUNTS]          = {};
static volatile bool    _espnow_need_refresh[NUM_MOUNTS]    = {};
// Set when the PC sends CMD_HUB_REINIT_ESPNOW; actioned in loop().  A full
// esp_now deinit/init clears a hub→mount send wedge that peer-refresh can't
// (what a power cycle does), without rebooting the hub.
static volatile bool    _espnow_need_full_reinit           = false;
// Set when the PC sends CMD_HUB_RESTART; actioned in loop().  Full esp_restart()
// — the escalation when the esp_now reinit doesn't clear the wedge (the wedge
// lives below the ESP-NOW layer; only a full reboot has ever cleared it).
static volatile bool    _need_full_restart                 = false;

// Uninterrupted send-FAIL run per mount (0 = last send succeeded).  Unlike
// _espnow_fails (which resets each time it triggers a peer refresh), this one
// only resets on SUCCESS — it is the raw signal the autonomous wedge detector
// reads in loop().  Capped at 255.
static volatile uint8_t  _espnow_fail_run[NUM_MOUNTS] = {};
// Cumulative send failures across all mounts since boot — health telemetry.
static volatile uint32_t _espnow_fail_total = 0;
// Per-mount cumulative send failures.  health_check() sums only the mounts it
// can currently hear: a mount that isn't powered fails every send forever, and
// counting that as a fault flags a rig running fewer than five mounts unhealthy
// for as long as it runs.
static volatile uint32_t _espnow_fail_cum[NUM_MOUNTS] = {};

static void on_espnow_sent(const wifi_tx_info_t *info, esp_now_send_status_t status) {
    const uint8_t *mac_addr = info->des_addr;
    for (int i = 0; i < NUM_MOUNTS; i++) {
        if (!mount_mac_valid(i) || memcmp(mac_addr, _mount_mac[i], 6) != 0) continue;
        if (status == ESP_NOW_SEND_SUCCESS) {
            _espnow_fails[i]    = 0;
            _espnow_fail_run[i] = 0;
        } else {
            // NB: ++ on a volatile is deprecated in C++20, so read-modify-write.
            _espnow_fail_total = _espnow_fail_total + 1;   // health telemetry (cumulative)
            _espnow_fail_cum[i] = _espnow_fail_cum[i] + 1;   // per-mount, for health_check()
            if (_espnow_fail_run[i] < 255) _espnow_fail_run[i] = _espnow_fail_run[i] + 1;
            _espnow_fails[i] = _espnow_fails[i] + 1;
            if (_espnow_fails[i] >= ESPNOW_MAX_CONSEC_FAILS) {
                _espnow_fails[i]          = 0;
                _espnow_need_refresh[i]   = true;  // handled safely in loop()
            }
        }
        break;
    }
}

static void process_status_for_display(const RelayMsg &msg, const ParsedPacket &pkt);
static void send_hub_event(uint8_t kind, uint8_t mount_id, int8_t rssi,
                           uint8_t state, uint8_t flags);

#define RELAY_QUEUE_DEPTH 32
static QueueHandle_t _relay_queue;

// WS receive queue — AsyncTCP task enqueues raw bytes; main loop processes them.
// Keeps all AsyncWebSocket state access (send + receive) on the same task and
// prevents the race between binaryAll() (main loop) and on_ws_event (AsyncTCP).
#define WS_RX_QUEUE_DEPTH 16
static QueueHandle_t _ws_rx_queue;

static int8_t    _mount_rssi[NUM_MOUNTS]      = {};
static uint32_t  _mount_last_seen[NUM_MOUNTS] = {};
// Paces the rig-wide refresh a client connection triggers — see the accept path.
#define ACCEPT_BURST_MIN_MS  5000UL
// Seeded one interval in the past (unsigned wrap) so the FIRST client of a
// session is never the one made to wait.  Plain 0 would suppress the burst for
// any connection inside the hub's first 5 seconds — and mounts beacon on their
// own, so they can already be known by then.
static uint32_t  _last_accept_burst_ms = 0 - ACCEPT_BURST_MIN_MS;

// ---- Ghost STATUS-frame guard ----
// After long uptime the WiFi RX path can deliver corrupt/"ghost" frames whose
// source matches a mount MAC but whose RSSI reads exactly 0 — the cause of the
// phantom "camera N connected, 0 dBm, jogging" on the display.  A real ESP-NOW
// reception is essentially always negative, so rssi==0 is a safe ghost signature.
// Toggle to 0 to A/B test the guard (the capture still logs ghosts either way).
// Declared here (before process_status_for_display) so both the #if and the
// counter are in scope at the use site.
#define HUB_DROP_GHOST_RSSI0  1
static uint32_t  _ghost_rx_drops = 0;   // STATUS frames dropped as ghosts (rssi==0)

// Sequence counter for hub-originated PC-diagnostic packets (HUB_DIAG /
// HUB_EVENT / HEALTH / display-health relay).  Declared early: used by
// dispatch_disp_msg() well before the diag block that owns the other counters.
static uint16_t _usb_diag_seq = 0;

// Last known look-at move direction per mount (-1=none, 0=min/◀, 1=max/▶).
// Set when any client sends CMD_START_LOOK_AT_MOVE; broadcast to all clients.
static int8_t    _la_dir[NUM_MOUNTS];   // initialised to -1 in setup()
static uint16_t  _la_dir_seq = 0;       // sequence counter for hub-injected CMD_LA_MOVE_DIR packets
#define MOUNT_TIMEOUT_MS  3000

// ---------------------------------------------------------------------------
// Autonomous self-recovery — no PC required
// ---------------------------------------------------------------------------
// The hub→mount send wedge (WiFi-driver TX path dies for one mount after long
// uptime) was previously detected and recovered ONLY by the PC app via
// CMD_HUB_REINIT_ESPNOW / CMD_HUB_RESTART.  A hub running with just the hub
// display stayed wedged until someone power-cycled it.  The hub can see the
// wedge itself: sends to a mount FAIL continuously (send callback) while that
// mount's STATUS keeps ARRIVING (RX fine, so the mount is powered and in
// range).  Ladder: reinit ESP-NOW, then esp_restart() — field data (Jun 2026
// logs) shows reinit alone cures ~15% and a restart cures the rest.
//
// The PC app's escalation is unchanged and acts as a backstop: with a PC
// attached its reinit typically fires first (~3 s); the hub's own restart at
// 25 s beats the PC's 35 s escalation.  Both share the reinit cooldown stamp.

#include "esp_attr.h"

#define SELF_WEDGE_ALIVE_MS      7000UL   // "mount is alive" = STATUS within this (AMOLED heartbeats every 5 s)
#define SELF_WEDGE_MIN_FAILS     2        // uninterrupted send fails before the wedge clock starts
#define SELF_REINIT_AFTER_MS     6000UL   // wedge age → full ESP-NOW reinit
#define SELF_WIFI_REINIT_AFTER_MS 14000UL // wedge age → bounce WiFi (below ESP-NOW)
// Own cooldown: stage 1's 30 s gap must not suppress this rung, or the 25 s
// restart would always beat it and the WiFi bounce would never run at all.
#define SELF_WIFI_REINIT_COOLDOWN_MS 90000UL
#define SELF_REINIT_COOLDOWN_MS  30000UL  // min gap between reinits (PC- or self-triggered)
#define SELF_RESTART_AFTER_MS    25000UL  // wedge age → esp_restart()
#define SELF_RESTART_MAX_STREAK  3        // boot-loop guard: max consecutive self-restarts
#define HEALTHY_CLEAR_MS         600000UL // 10 min wedge-free clears the restart streak

// Proactive maintenance restart: every long-uptime pathology seen so far
// (TX wedge, ghost RX frames) is cured by a reboot and develops after ~12 h.
// Restart deliberately at 8 h uptime — but only when nothing is happening:
// no client command for 20 min AND every connected mount reports IDLE.
#define MAINT_RESTART_UPTIME_MS  (8UL * 3600UL * 1000UL)
#define MAINT_RESTART_IDLE_MS    (20UL * 60UL * 1000UL)

// Survives esp_restart() (not power-on/brownout): consecutive self-restart
// count, so a wedge that reappears instantly after every reboot can't create
// an infinite ~40 s boot loop.  Cleared after 10 min wedge-free, or on any
// non-software reset.
RTC_NOINIT_ATTR static uint32_t _self_restart_streak;

static uint32_t _tx_wedge_since_ms[NUM_MOUNTS] = {};  // 0 = no wedge clock running
static uint32_t _last_reinit_ms       = 0;   // stamped by hub_espnow_full_reinit()
static uint32_t _last_wifi_reinit_ms  = 0;   // stamped by hub_wifi_full_reinit()
static uint32_t _last_wedge_ms        = 0;   // last time any wedge clock was active
static uint32_t _last_client_cmd_ms   = 0;   // last command from any client (TCP/WS/serial/display)
static uint8_t  _mount_last_state[NUM_MOUNTS];  // last STATUS state byte (0xFF = unknown)
static bool     _restart_block_logged = false;  // rate-limits the streak-exceeded log line

// Per-mount live context cached from STATUS — consumed by the OSC server.
static uint8_t  _mount_pt_preset[NUM_MOUNTS]  = {2, 2, 2, 2, 2};
static uint8_t  _mount_sl_preset[NUM_MOUNTS]  = {2, 2, 2, 2, 2};
static uint8_t  _mount_la_subject[NUM_MOUNTS] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

// ---------------------------------------------------------------------------
// Pairing rules  (called from loop() for the first parsed packet per message)
// ---------------------------------------------------------------------------
// Every packet a mount sends carries the mount ID chosen on its touchscreen.
// Rules:
//   1. new MAC   → empty slot            : bind + persist   (first contact)
//   2. known MAC → different EMPTY slot  : move the binding (renumbered on its
//      own screen) — old slot freed, display told it disconnected
//   3. any MAC   → slot owned by another : reject; log + HUB_EVENT, remembered
//      in _conflict_* for the hub-display prompt (stage 3).  The claimant
//      never turns green, which sends the operator to its setup screen.

static uint8_t  _conflict_mac[6] = {};
static uint8_t  _conflict_slot   = 0xFF;   // 0-based claimed slot; 0xFF = none
static uint32_t _conflict_log_ms = 0;
// Display-prompt bookkeeping: what the hub display is currently showing, what
// the operator chose to ignore (suppressed until reboot), and when the
// conflicting device last claimed (for staleness dismissal).
static uint8_t  _conflict_shown_slot   = 0xFF;   // prompt on display for this slot
static uint8_t  _conflict_ignored_mac[6] = {};
static uint8_t  _conflict_ignored_slot = 0xFF;
static uint32_t _conflict_last_claim_ms = 0;

// Returns the slot this MAC is bound to after applying the rules, 0xFF if the
// claim was rejected.  Cheap in steady state (one table lookup + compare).
static uint8_t mount_table_observe(const uint8_t mac[6], uint8_t claimed_id,
                                   int8_t rssi) {
    int8_t cur = mount_table_find(mac);
    if (claimed_id < 1 || claimed_id > NUM_MOUNTS)
        return (cur >= 0) ? (uint8_t)cur : 0xFF;   // no claim in this packet
    uint8_t slot = (uint8_t)(claimed_id - 1);
    if (cur == (int8_t)slot) return slot;          // steady state

    if (mount_mac_valid(slot)) {
        // Rule 3 — claimed slot belongs to a different device.
        memcpy(_conflict_mac, mac, 6);
        _conflict_slot          = slot;
        _conflict_last_claim_ms = millis();
        uint32_t nowm = millis();
        if (_conflict_log_ms == 0 || nowm - _conflict_log_ms > 30000) {
            _conflict_log_ms = nowm;
            Serial.printf("[PAIR] CONFLICT: %02X:%02X:%02X:%02X:%02X:%02X claims CAM %u, "
                          "already bound to %02X:%02X:%02X:%02X:%02X:%02X — ignoring. "
                          "Re-number one of them (setup screen on the mount).\n",
                          mac[0], mac[1], mac[2], mac[3], mac[4], mac[5], claimed_id,
                          _mount_mac[slot][0], _mount_mac[slot][1], _mount_mac[slot][2],
                          _mount_mac[slot][3], _mount_mac[slot][4], _mount_mac[slot][5]);
            send_hub_event(7, claimed_id, rssi, mac[4], mac[5]);
        }
        // Drive the hub-display prompt: skip if the operator already chose
        // IGNORE for this exact device+slot; otherwise show it once per new
        // conflict (a fresh conflict replaces the shown one).
        bool ignored = (slot == _conflict_ignored_slot &&
                        memcmp(mac, _conflict_ignored_mac, 6) == 0);
        if (!ignored && _conflict_shown_slot != slot) {
            _conflict_shown_slot = slot;
            disp_send_pair_conflict((uint8_t)(slot + 1), mac, _mount_mac[slot]);
        }
        return 0xFF;
    }

    if (cur >= 0) {
        // Rule 2 — known device renumbered to an empty slot: move the binding.
        Serial.printf("[PAIR] CAM %d renumbered to CAM %u "
                      "(%02X:%02X:%02X:%02X:%02X:%02X)\n",
                      cur + 1, claimed_id,
                      mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
        memset(_mount_mac[cur], 0, 6);
        _mount_last_seen[cur]   = 0;
        _mount_last_state[cur]  = 0xFF;
        _mount_rssi[cur]        = 0;
        _espnow_fails[cur]      = 0;
        _espnow_fail_run[cur]   = 0;
        _tx_wedge_since_ms[cur] = 0;
        disp_set_disconnected((uint8_t)(cur + 1));
        send_hub_event(6, claimed_id, rssi, (uint8_t)(cur + 1), mac[5]);
    } else {
        // Rule 1 — first contact: bind.
        Serial.printf("[PAIR] CAM %u paired: %02X:%02X:%02X:%02X:%02X:%02X\n",
                      claimed_id,
                      mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
        send_hub_event(5, claimed_id, rssi, mac[4], mac[5]);
    }

    memcpy(_mount_mac[slot], mac, 6);
    mount_table_save();
    refresh_espnow_peer(slot);        // registers the ESP-NOW peer for TX
    _espnow_fails[slot]      = 0;
    _espnow_fail_run[slot]   = 0;
    _tx_wedge_since_ms[slot] = 0;
    _mount_last_state[slot]  = 0xFF;
    disp_send_mount_table();          // keep the display's Mounts panel current
    return slot;
}

// Dismiss the display's conflict prompt when the conflicting device has gone
// quiet (renumbered on its own screen, or powered off).  Called from loop().
static void conflict_staleness_check(uint32_t now) {
    if (_conflict_shown_slot == 0xFF) return;
    if (now - _conflict_last_claim_ms > 10000) {
        Serial.println("[PAIR] Conflict claimant went quiet — dismissing display prompt");
        _conflict_shown_slot = 0xFF;
        disp_send_pair_conflict(0, nullptr, nullptr);
    }
}

// Operator decision from the hub display (REPLACE / IGNORE on the prompt).
static void pair_decide(uint8_t cam, uint8_t decision, const uint8_t *mac) {
    if (cam < 1 || cam > NUM_MOUNTS) return;
    uint8_t slot = (uint8_t)(cam - 1);

    if (decision == 1) {
        // REPLACE: bind the new device to this slot.  If it was bound
        // elsewhere (renumber onto an occupied slot), free its old home.
        int8_t cur = mount_table_find(mac);
        if (cur >= 0 && cur != (int8_t)slot) {
            memset(_mount_mac[cur], 0, 6);
            _mount_last_seen[cur]  = 0;
            _mount_last_state[cur] = 0xFF;
            disp_set_disconnected((uint8_t)(cur + 1));
        }
        if (mount_mac_valid(slot))
            esp_now_del_peer(_mount_mac[slot]);   // evict the previous owner's peer
        Serial.printf("[PAIR] REPLACE: CAM %u now %02X:%02X:%02X:%02X:%02X:%02X "
                      "(operator approved on display)\n", cam,
                      mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
        memcpy(_mount_mac[slot], mac, 6);
        mount_table_save();
        refresh_espnow_peer(slot);
        _mount_last_seen[slot]   = 0;
        _mount_last_state[slot]  = 0xFF;
        _espnow_fails[slot]      = 0;
        _espnow_fail_run[slot]   = 0;
        _tx_wedge_since_ms[slot] = 0;
        send_hub_event(5, cam, 0, mac[4], mac[5]);
        disp_send_mount_table();
    } else {
        // IGNORE: suppress prompts for this exact device+slot until reboot.
        Serial.printf("[PAIR] IGNORE: CAM %u claim from %02X:%02X:%02X:%02X:%02X:%02X "
                      "suppressed until hub reboot\n", cam,
                      mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
        memcpy(_conflict_ignored_mac, mac, 6);
        _conflict_ignored_slot = slot;
    }
    _conflict_shown_slot = 0xFF;
    disp_send_pair_conflict(0, nullptr, nullptr);   // dismiss the prompt
}

// Forget a slot from the display's Mounts panel.  NOTE: a live mount that
// still claims this cam number simply re-pairs itself within ~5 s (rule 1) —
// Forget exists for retiring dead/replaced hardware.
static void pair_forget(uint8_t cam) {
    if (cam < 1 || cam > NUM_MOUNTS) return;
    uint8_t slot = (uint8_t)(cam - 1);
    if (!mount_mac_valid(slot)) return;
    Serial.printf("[PAIR] FORGET: CAM %u (%02X:%02X:%02X:%02X:%02X:%02X) "
                  "unbound from display\n", cam,
                  _mount_mac[slot][0], _mount_mac[slot][1], _mount_mac[slot][2],
                  _mount_mac[slot][3], _mount_mac[slot][4], _mount_mac[slot][5]);
    esp_now_del_peer(_mount_mac[slot]);
    memset(_mount_mac[slot], 0, 6);
    mount_table_save();
    _mount_last_seen[slot]   = 0;
    _mount_last_state[slot]  = 0xFF;
    _espnow_fails[slot]      = 0;
    _espnow_fail_run[slot]   = 0;
    _tx_wedge_since_ms[slot] = 0;
    disp_set_disconnected(cam);
    disp_send_mount_table();
}

// Rate-limit CMD_STATUS broadcasts to WebSocket clients.
// The XIAO ESP32S3 shares one radio between WiFi AP and ESP-NOW.  While the
// hub is firing esp_now_send (driven by incoming jog packets), the AP radio is
// intermittently unavailable, delaying TCP ACKs from the phone.  When enough
// ACKs are missed, lwIP fires _onTimeout → _client->close(true).
//
// Fix: suppress ALL WS STATUS during active jogging (last jog < 200 ms ago).
// When idle, limit to 5 Hz per mount — well within what the phone UI needs.
static uint32_t _ws_status_ms[NUM_MOUNTS] = {};
static uint32_t _ws_last_jog_ms           = 0;
#define WS_STATUS_INTERVAL_MS  200   // 5 Hz idle rate
#define WS_JOG_SUPPRESS_MS     200   // suppress STATUS for 200 ms after last jog

// ---------------------------------------------------------------------------
// TCP / WebSocket / Serial
// ---------------------------------------------------------------------------

static WiFiServer _tcp_server(TCP_PORT);

struct ClientSlot {
    WiFiClient   client;
    PacketParser parser;
    bool         active = false;
};
static ClientSlot _slots[MAX_CLIENTS];

static AsyncWebServer _http_server(80);
static AsyncWebSocket _ws("/ws");

static uint8_t   _tcp_count = 0, _ws_count = 0;
static uint32_t  _last_client_update_ms = 0;
#define CLIENT_UPDATE_INTERVAL_MS 500

static PacketParser _serial_parser;

// ---------------------------------------------------------------------------
// Forward declarations
// ---------------------------------------------------------------------------

static void forward_to_mounts(const ParsedPacket &pkt);
#if DEMO_MODE
// Defined further down (needs _relay_queue); ui_send_to_mount() calls it above.
static bool demo_consume_cmd(uint8_t mount_id, uint8_t cmd,
                             const uint8_t *payload, uint8_t plen);
static void demo_init();
static void demo_tick();
#endif
static void intercept_la_move_dir(uint8_t mount_id, uint8_t dir);
static void ui_send_to_mount(uint8_t mount_id, CmdType cmd,
                              const uint8_t *payload, uint8_t plen);

// ---------------------------------------------------------------------------

static void ui_send_to_mount(uint8_t mount_id, CmdType cmd,
                              const uint8_t *payload, uint8_t plen) {
#if DEMO_MODE
    if (demo_consume_cmd(mount_id, cmd, payload, plen)) return;   // display presses
#endif
    static uint16_t ui_seq = 0;
    uint8_t  raw[PKT_BUF_SIZE + 4];
    uint16_t raw_len = build_packet(raw, mount_id, ++ui_seq, cmd, payload, plen);
    if (mount_id == MOUNT_BROADCAST) {
        for (int i = 0; i < NUM_MOUNTS; i++)
            if (mount_mac_valid(i))
                esp_now_send(_mount_mac[i], raw, raw_len);
    } else if (mount_id >= 1 && mount_id <= NUM_MOUNTS) {
        if (mount_mac_valid(mount_id - 1))
            esp_now_send(_mount_mac[mount_id - 1], raw, raw_len);
    }
}

// ---------------------------------------------------------------------------
// ESP-NOW receive callback
// ---------------------------------------------------------------------------

static void on_espnow_recv(const esp_now_recv_info_t *recv_info,
                           const uint8_t *data, int len) {
    // Belt and braces for the ordering in setup(): this is live from the moment
    // the callback is registered, and a NULL queue is an assert rather than a
    // dropped frame.  One compare against a rig that boot-loops.
    if (!_relay_queue) return;
    if (len <= 0 || len > (int)sizeof(RelayMsg::data)) return;
    // Table lookup from the WiFi task while loop() may be binding a slot is a
    // benign race: worst case one message classifies as unknown and the
    // pairing logic in loop() re-resolves it from src_mac.
    int8_t idx = mount_table_find(recv_info->src_addr);
    RelayMsg msg;
    msg.len     = (uint16_t)len;
    msg.rssi    = (int8_t)recv_info->rx_ctrl->rssi;
    msg.src_idx = (idx >= 0) ? (uint8_t)idx : 0xFF;
    memcpy(msg.src_mac, recv_info->src_addr, 6);
    memcpy(msg.data, data, len);

    xQueueSend(_relay_queue, &msg, 0);
}

// ---------------------------------------------------------------------------
// Broadcast to all clients
// ---------------------------------------------------------------------------

// Sends to USB serial and TCP clients.  WebSocket is handled separately in the
// relay loop with per-mount rate limiting to avoid overflowing the WS send queue.
// ---------------------------------------------------------------------------
// Diagnostic telemetry (PTS_DIAG)
// ---------------------------------------------------------------------------
// Scaffolding, not instrumentation the firmware needs to run.  These lines were
// added to answer specific questions — why phones lost their WebSocket, whether
// a client was drowning or being dropped — and they answered them.  They print
// unconditionally on a port someone may be watching for something else, so they
// are compiled out unless asked for:
//
//     DIAG=1 ./tools/build.sh hub
//
// OFF by default: a shipped rig should not be narrating itself, and these lines
// are noise to anyone watching the port for something else.  Turn them back on
// for a debugging session with DIAG=1.
//
// What makes that safe is that FAULT reports are never gated — [DOWN], ESP-NOW
// errors, link up/down all still print.  A quiet build still says when
// something breaks; it just stops saying when nothing does.
#ifndef PTS_DIAG
#define PTS_DIAG 0
#endif
#if PTS_DIAG
  #define DIAG_PRINTF(...)  Serial.printf(__VA_ARGS__)
#else
  #define DIAG_PRINTF(...)  ((void)0)
#endif

// Dropped frames per client, reported below.  A slow client losing status
// frames is not interesting; a client losing them steadily is.
static uint32_t _bcast_dropped = 0, _bcast_sent = 0;

static void broadcast_to_all(const uint8_t *data, uint16_t len) {
    Serial.write(data, len);
    for (int i = 0; i < MAX_CLIENTS; i++) {
        if (_slots[i].active && _slots[i].client.connected()) {
            // Raw non-blocking send, NOT client.write().  NetworkClient::write()
            // cannot be bounded: its send() already passes MSG_DONTWAIT, so
            // SO_SNDTIMEO is never consulted, and it instead retries around a
            // select() with a 1-second timeout up to ten times — and a partial
            // write RESETS that retry count.  One client that stops reading
            // therefore stalls this loop for ten seconds, and two for twenty.
            //
            // This is the hottest path in the firmware: every relayed frame
            // passes through it.  Blocking here does not merely delay one
            // client, it stops the hub reading ANYTHING — mounts, satellites,
            // the PC app — for the duration.  The rig showed exactly that:
            // "hub last received PC bytes 19.9s ago | last diag 19.9s ago",
            // mounts declared unreachable, and phones on a satellite's AP
            // losing their WebSocket while the hub was wedged inside write().
            //
            // Dropping a frame for a client that cannot keep up is the correct
            // trade: status is superseded within 200 ms, and the alternative is
            // to punish every other node for one slow reader.  The satellite
            // uplink was converted to this first and its drops went to zero.
            int fd = _slots[i].client.fd();
            int w  = (fd >= 0) ? ::send(fd, data, len, MSG_DONTWAIT) : -1;
            _bcast_sent++;
            if (w != (int)len) _bcast_dropped++;
        }
    }
}

// ---------------------------------------------------------------------------
// Pairing management — mirror the hub's table / conflict to network clients
// ---------------------------------------------------------------------------
// Hub-injected packets (mount_id 0xFE = hub sentinel), not part of the relay
// queue, so — exactly like intercept_la_move_dir() — they go to TCP/serial via
// broadcast_to_all() and to WebSocket via _ws.binaryAll().  Called only from
// disp_send_mount_table()/disp_send_pair_conflict(), always in loop() context.
static uint16_t _pair_seq = 0;

static void bcast_mount_table() {
    uint8_t buf[MOUNT_TABLE_PAYLOAD_LEN];
    memcpy(buf, _mount_mac, sizeof(buf));
    uint8_t raw[PKT_BUF_SIZE + 4];
    uint16_t n = build_packet(raw, 0xFE, ++_pair_seq, CMD_MOUNT_TABLE, buf, sizeof(buf));
    broadcast_to_all(raw, n);
    _ws.binaryAll(raw, (size_t)n);
}

static void bcast_pair_conflict(uint8_t cam, const uint8_t *new_mac,
                                const uint8_t *old_mac) {
    uint8_t buf[PAIR_CONFLICT_PAYLOAD_LEN] = {};
    buf[0] = cam;
    if (new_mac) memcpy(buf + 1, new_mac, 6);
    if (old_mac) memcpy(buf + 7, old_mac, 6);
    uint8_t raw[PKT_BUF_SIZE + 4];
    uint16_t n = build_packet(raw, 0xFE, ++_pair_seq, CMD_PAIR_CONFLICT, buf, sizeof(buf));
    broadcast_to_all(raw, n);
    _ws.binaryAll(raw, (size_t)n);
}

// Hub-scoped pairing commands from any client (TCP / WebSocket / USB).  The hub
// owns the mount table; these view / set / clear it and must NOT reach a mount.
// Returns true if consumed (the caller then stops — does not forward).
static bool handle_pairing_cmd(const ParsedPacket &pkt) {
    switch (pkt.cmd) {
    case CMD_GET_MOUNT_TABLE:
        bcast_mount_table();                       // refresh every client's view
        return true;
    case CMD_PAIR_DECIDE:                           // set: replace (1) / ignore (0)
        if (pkt.payload_len >= PAIR_DECIDE_PAYLOAD_LEN)
            pair_decide(pkt.payload[0], pkt.payload[1], pkt.payload + 2);
        return true;
    case CMD_PAIR_FORGET:                           // clear: unbind a slot
        if (pkt.payload_len >= 1)
            pair_forget(pkt.payload[0]);
        return true;
    default:
        return false;
    }
}

// ---------------------------------------------------------------------------
// Forward packet to mount(s)
// ---------------------------------------------------------------------------

// Shared interception for CMD_START_LOOK_AT_MOVE regardless of which client
// sent it (TCP, WebSocket, or display UART).  Broadcasts CMD_LA_MOVE_DIR to
// all TCP/serial clients and DISP_MSG_LA_MOVE_DIR to the display board so
// every device can show the correct arrow flash without knowing who started it.
static void intercept_la_move_dir(uint8_t mount_id, uint8_t dir) {
    if (mount_id < 1 || mount_id > NUM_MOUNTS) return;
    _la_dir[mount_id - 1] = (int8_t)dir;
    // Display board (UART)
    uint8_t dbuf[2] = { mount_id, dir };
    disp_lock();
    disp_uart_send(Serial1, DISP_MSG_LA_MOVE_DIR, dbuf, 2);
    disp_unlock();
    // TCP / serial clients + WebSocket clients
    uint8_t raw[PKT_BUF_SIZE + 4];
    uint16_t rlen = build_packet(raw, mount_id, ++_la_dir_seq,
                                 CMD_LA_MOVE_DIR, &dir, 1);
    broadcast_to_all(raw, rlen);
    _ws.binaryAll(raw, (size_t)rlen);   // hub-injected — not in relay queue, must send explicitly
}

#if DEMO_MODE
// ---------------------------------------------------------------------------
// Demo engine
// ---------------------------------------------------------------------------
// Synthetic STATUS frames are pushed into _relay_queue — the same queue real
// ESP-NOW traffic arrives on — so they travel the normal relay path out to TCP,
// WebSocket, USB serial and the display.  Nothing downstream can tell the
// difference, which is why all three surfaces agree without touching them.

struct DemoCam {
    uint16_t occupied;    // bitmask of stored slots
    uint16_t at;          // slot the camera is parked on (0 while travelling)
    uint8_t  state;       // STATE_IDLE / STATE_MOVING_TO_POS
    uint8_t  target;      // slot being travelled to, 0xFF when idle
    uint32_t arrive_ms;   // millis() at which the move completes
    uint8_t  pt_preset;   // 1-4, cycled by tapping the PT dial
    uint8_t  sl_preset;   // 1-4, cycled by tapping the SL dial
    bool     has_slider;  // false → slider dial renders dormant everywhere
    bool     look_at;     // true  → row shows subjects 1-8 plus ◀/▶ arrows
    uint8_t  subject;     // active look-at subject 0-7, 0xFF = none
    bool     at_max;      // look-at: slider parked at max (▶) end vs min (◀)
};
static DemoCam  _demo[NUM_MOUNTS];
static uint32_t _demo_status_ms = 0;
static uint16_t _demo_seq       = 0;

static void demo_init() {
    // 4-6 stored positions each, deliberately uneven so screenshots look real
    // rather than synthetic.  Bit 0 = slot 1.
    // Bit 0 = slot 1.  On the look-at camera these bits are stored SUBJECTS
    // (1-8) instead of positions, because that is what the row shows.
    static const uint16_t MASKS[NUM_MOUNTS] = {
        0x001F,   // cam 1: slots 1-5            (5)
        0x000F,   // cam 2: LOOK-AT — subjects 1-4 calibrated
        0x003F,   // cam 3: slots 1-6            (6)
        0x0117,   // cam 4: slots 1,2,3,5,9      (5)
        0x00E3,   // cam 5: slots 1,2,6,7,8      (5)
    };
    // Cam 2 runs in look-at mode: its row becomes 8 subject buttons plus ◀/▶,
    // so the look-at UI can be photographed too.  Needs a slider by definition.
    static const bool LOOKAT[NUM_MOUNTS] = { false, true, false, false, false };
    // Mixed speeds — five identical dials look obviously synthetic in a photo.
    // Tapping a dial on any surface cycles these for real (CMD_SET_ACTIVE_PRESET).
    static const uint8_t PT[NUM_MOUNTS] = { 2, 3, 1, 4, 2 };
    static const uint8_t SL[NUM_MOUNTS] = { 3, 1, 2, 1, 4 };
    // Cam 4 has no slider, so its slider dial shows dormant on every surface —
    // a realistic mixed rig, and it lets that state be photographed.
    static const bool HAS_SL[NUM_MOUNTS] = { true, true, true, false, true };
    for (int i = 0; i < NUM_MOUNTS; i++) {
        _demo[i].occupied   = MASKS[i];
        _demo[i].state      = STATE_IDLE;
        _demo[i].target     = 0xFF;
        _demo[i].arrive_ms  = 0;
        _demo[i].at         = 0;
        _demo[i].pt_preset  = PT[i];
        _demo[i].sl_preset  = SL[i];
        _demo[i].has_slider = HAS_SL[i];
        _demo[i].look_at    = LOOKAT[i];
        _demo[i].subject    = LOOKAT[i] ? 1 : 0xFF;   // subject 2 selected
        _demo[i].at_max     = false;                  // parked at the min (◀) end
        for (int s = 0; s < NUM_POSITIONS; s++)     // park on the lowest slot
            if (MASKS[i] & (1u << s)) { _demo[i].at = (uint16_t)(1u << s); break; }
        // Fake MAC so the Mounts pairing panel shows five bound mounts too.
        // RAM only — mount_table_save() is disabled in demo builds.
        uint8_t mac[6] = { 0x02, 0x00, 0x00, 0x00, 0xDE, (uint8_t)(i + 1) };
        memcpy(_mount_mac[i], mac, 6);
    }
    Serial.println("[DEMO] five simulated mounts active — this is NOT a real rig");
}

// Handle a command aimed at a simulated mount.  Returns true if consumed, so
// the caller must not try to send it over ESP-NOW (there is nothing out there).
static bool demo_consume_cmd(uint8_t mount_id, uint8_t cmd,
                             const uint8_t *payload, uint8_t plen) {
    int lo = 0, hi = NUM_MOUNTS - 1;
    if (mount_id >= 1 && mount_id <= NUM_MOUNTS) lo = hi = mount_id - 1;
    else if (mount_id != MOUNT_BROADCAST)        return false;

    for (int i = lo; i <= hi; i++) {
        DemoCam &d = _demo[i];
        switch (cmd) {
        case CMD_GOTO_SLOT:
            if (plen >= 1 && payload[0] < NUM_POSITIONS
                    && (d.occupied & (1u << payload[0]))) {
                d.target    = payload[0];
                d.state     = STATE_MOVING_TO_POS;   // UIs flash the target amber
                d.at        = 0;                     // no longer at the old slot
                d.arrive_ms = millis() + DEMO_TRAVEL_MS;
            }
            break;
        case CMD_STORE_POS:
            if (plen >= 1 && payload[0] < NUM_POSITIONS) {
                d.occupied |= (uint16_t)(1u << payload[0]);
                d.at        = (uint16_t)(1u << payload[0]);   // stored = you are there
                d.state     = STATE_IDLE;
                d.target    = 0xFF;
            }
            break;
        case CMD_CLEAR_POS:
            if (plen >= 1 && payload[0] < NUM_POSITIONS) {
                d.occupied &= (uint16_t)~(1u << payload[0]);
                d.at       &= (uint16_t)~(1u << payload[0]);
            }
            break;
        case CMD_SET_ACTIVE_PRESET:
            // Tapping a speed dial on any surface cycles the preset. The UIs
            // deliberately do NOT update locally — they wait for the STATUS
            // echo — so the demo has to carry this or the dials never move.
            if (plen >= 2 && payload[1] >= 1 && payload[1] <= 4) {
                if      (payload[0] == GROUP_PAN_TILT)    d.pt_preset = payload[1];
                else if (payload[0] == GROUP_SLIDER_ZOOM && d.has_slider)
                                                          d.sl_preset = payload[1];
            }
            break;
        case CMD_SWITCH_SUBJECT:                 // subject tap on a look-at row
            if (plen >= 1 && payload[0] < 8) d.subject = payload[0];
            break;
        case CMD_START_LOOK_AT_MOVE:             // ◀ / ▶ on a look-at row
            // payload: subject_id, direction (0=min/◀, 1=max/▶), speed_preset
            if (plen >= 2 && d.look_at) {
                if (payload[0] < 8) d.subject = payload[0];
                d.state     = STATE_LOOK_AT_MOVE;
                d.at_max    = (payload[1] == 1);   // destination end (▶ = max)
                d.arrive_ms = millis() + DEMO_TRAVEL_MS;
                // forward_to_mounts() only reaches its own intercept AFTER the
                // send, which we skipped — so fire the arrow-flash broadcast
                // here or the ◀/▶ indicator never animates for TCP/WS clients.
                intercept_la_move_dir((uint8_t)(i + 1), payload[1]);
            }
            break;
        case CMD_E_STOP:
            d.state = STATE_IDLE; d.target = 0xFF;   // stops mid-travel, parked nowhere
            break;
        default:
            break;    // everything else is simply swallowed
        }
    }
    return true;
}

static void demo_tick() {
    uint32_t now = millis();

    for (int i = 0; i < NUM_MOUNTS; i++) {
        DemoCam &d = _demo[i];
        if (d.state == STATE_MOVING_TO_POS && (int32_t)(now - d.arrive_ms) >= 0) {
            d.at     = (uint16_t)(1u << d.target);   // arrived — UIs turn it green
            d.state  = STATE_IDLE;
            d.target = 0xFF;
        } else if (d.state == STATE_LOOK_AT_MOVE && (int32_t)(now - d.arrive_ms) >= 0) {
            // Leaving STATE_LOOK_AT_MOVE is what promotes the ◀/▶ arrow from
            // flashing amber to solid green — the UIs key off that transition.
            d.state = STATE_IDLE;
        }
    }

    if (now - _demo_status_ms < 100) return;         // STATUS at 10 Hz
    _demo_status_ms = now;

    for (int i = 0; i < NUM_MOUNTS; i++) {
        DemoCam &d = _demo[i];
        uint8_t p[10] = {
            d.state,
            (uint8_t)((d.has_slider ? FLAG_HAS_SLIDER  : 0)
                      | (d.look_at  ? FLAG_LOOK_AT_MODE : 0)
                      // Idle look-at mount reports which slider end it's parked
                      // at, so the ◀/▶ arrow shows green there (like a real one).
                      | ((d.look_at && d.state == STATE_IDLE)
                             ? (d.at_max ? FLAG_AT_MAX_LIMIT : FLAG_AT_MIN_LIMIT) : 0)
                      | FLAG_LIMITS_SET | FLAG_REF_SET),
            d.pt_preset, d.sl_preset,
            (uint8_t)(d.occupied >> 8), (uint8_t)(d.occupied & 0xFF),
            (uint8_t)(d.at >> 8),       (uint8_t)(d.at & 0xFF),
            d.target,
            d.subject,                                         // active look-at subject
        };
        RelayMsg msg;
        msg.len     = build_packet(msg.data, (uint8_t)(i + 1), ++_demo_seq,
                                   CMD_STATUS, p, sizeof(p));
        msg.rssi    = -52;          // MUST be non-zero: rssi==0 is the ghost guard
        msg.src_idx = (uint8_t)i;
        memcpy(msg.src_mac, _mount_mac[i], 6);
        xQueueSend(_relay_queue, &msg, 0);
    }
}
#endif  // DEMO_MODE


static void forward_to_mounts(const ParsedPacket &pkt) {
    _last_client_cmd_ms = millis();   // any client traffic defers the maintenance restart
    if (handle_pairing_cmd(pkt)) return;   // hub-scoped pairing — handled here, not sent to mounts
#if DEMO_MODE
    if (demo_consume_cmd(pkt.mount_id, pkt.cmd, pkt.payload, pkt.payload_len)) return;
#endif
    uint8_t  raw[PKT_BUF_SIZE + 4];
    uint16_t raw_len = build_packet(raw, pkt.mount_id, pkt.seq, pkt.cmd,
                                    pkt.payload, pkt.payload_len);
    if (pkt.mount_id == MOUNT_BROADCAST) {
        for (int i = 0; i < NUM_MOUNTS; i++)
            if (mount_mac_valid(i))
                esp_now_send(_mount_mac[i], raw, raw_len);
    } else if (pkt.mount_id >= 1 && pkt.mount_id <= NUM_MOUNTS) {
        if (mount_mac_valid(pkt.mount_id - 1))
            esp_now_send(_mount_mac[pkt.mount_id - 1], raw, raw_len);
    }
    // Intercept JOG to update display preset bars
    if (pkt.cmd == CMD_JOG && pkt.payload_len >= 10) {
        uint8_t pt = pkt.payload[8], sz = pkt.payload[9];
        if (pkt.mount_id >= 1 && pkt.mount_id <= NUM_MOUNTS) {
            disp_update_preset(pkt.mount_id, pt, sz);
        } else if (pkt.mount_id == MOUNT_BROADCAST) {
            for (int i = 1; i <= NUM_MOUNTS; i++)
                disp_update_preset(i, pt, sz);
        }
    }
    // Intercept SWITCH_SUBJECT so the display updates immediately rather than
    // waiting for the mount to send CMD_LOOK_AT_STATUS (which only arrives when
    // a look-at move actually starts).
    if (pkt.cmd == CMD_SWITCH_SUBJECT && pkt.payload_len >= 1) {
        if (pkt.mount_id >= 1 && pkt.mount_id <= NUM_MOUNTS) {
            disp_look_at_status(pkt.mount_id, pkt.payload[0]);
        }
    }
    // Intercept START_LOOK_AT_MOVE — notify all clients of the direction.
    if (pkt.cmd == CMD_START_LOOK_AT_MOVE && pkt.payload_len >= 2) {
        intercept_la_move_dir(pkt.mount_id, pkt.payload[1]);
    }
}

// ---------------------------------------------------------------------------
// STATUS packet → display
// ---------------------------------------------------------------------------

// Forward CMD_LIMITS_FOUND from a mount to the display board via DISP_UART.
// payload: 9-byte LIMITS_FOUND body (axis + min + max, big-endian).
static void disp_limits_found(uint8_t mount_id, const uint8_t *payload9) {
    uint8_t buf[10];
    buf[0] = mount_id;
    memcpy(buf + 1, payload9, 9);
    disp_lock();
    disp_uart_send(Serial1, DISP_MSG_LIMITS_FOUND, buf, 10);
    disp_unlock();
}

// Forward CMD_HOME_COMPLETE from a mount to the display board via DISP_UART.
// payload: 1-byte axis.
static void disp_home_complete(uint8_t mount_id, uint8_t axis) {
    uint8_t buf[2] = { mount_id, axis };
    disp_lock();
    disp_uart_send(Serial1, DISP_MSG_HOME_COMPLETE, buf, 2);
    disp_unlock();
}

// Forward CMD_CONFIG_REPORT from a mount to the display board via DISP_UART.
// payload: 75-byte config report from the mount (1 orientation + 72 speeds + 2 stall thresholds).
static void disp_config_report(uint8_t mount_id, const uint8_t *payload75) {
    uint8_t buf[76];
    buf[0] = mount_id;
    memcpy(buf + 1, payload75, 75);
    disp_lock();
    disp_uart_send(Serial1, DISP_MSG_CONFIG_REPORT, buf, 76);
    disp_unlock();
}

// Extract 8-bit subject validity mask from a SUBJECT_LIST payload and send
// it to the display as the compact DISP_MSG_SUBJECT_MASK (2 bytes).
// Each subject record is SUBJECT_RECORD_LEN bytes; byte 0 of each record is
// the 'valid' flag.  Full list is too large for DISP_UART_MAX_PAYLOAD, so
// we only forward the mask.
static void disp_subject_mask(uint8_t mount_id, const uint8_t *payload232) {
    uint8_t mask = 0;
    for (uint8_t i = 0; i < MAX_SUBJECTS; i++) {
        if (payload232[i * SUBJECT_RECORD_LEN]) mask |= (1u << i);
    }
    uint8_t buf[2] = { mount_id, mask };
    disp_lock();
    disp_uart_send(Serial1, DISP_MSG_SUBJECT_MASK, buf, 2);
    disp_unlock();
}

// Forward CMD_LOOK_AT_STATUS subject_id to the display board via DISP_UART.
// payload: 14-byte LOOK_AT_STATUS; subject_id is at payload[12].
static void disp_look_at_status(uint8_t mount_id, uint8_t subject_id) {
    uint8_t buf[2] = { mount_id, subject_id };
    disp_lock();
    disp_uart_send(Serial1, DISP_MSG_LOOK_AT_STATUS, buf, 2);
    disp_unlock();
}

// Forward CMD_CALIB_PROMPT sub_state to the display board via DISP_UART.
static void disp_calib_prompt(uint8_t mount_id, uint8_t sub_state) {
    uint8_t buf[2] = { mount_id, sub_state };
    disp_lock();
    disp_uart_send(Serial1, DISP_MSG_CALIB_PROMPT, buf, 2);
    disp_unlock();
}

static void process_status_for_display(const RelayMsg &msg, const ParsedPacket &pkt) {
    if (pkt.cmd != CMD_STATUS)     return;
    if (pkt.payload_len < 2)       return;   // need at least state + flags
    if (msg.src_idx >= NUM_MOUNTS) return;

    uint8_t mount_id = msg.src_idx + 1;

    // ── Ghost-frame guard ────────────────────────────────────────────────
    // After long uptime the WiFi RX path can deliver corrupt frames whose
    // source matches a mount MAC but whose RSSI reads exactly 0 — the cause of
    // the phantom "camera N connected, 0 dBm, jogging" on the display.  Capture
    // every occurrence (rate-limited, per mount) for the PC log, then drop it so
    // it can never mark a mount connected.  Guard is a toggle for A/B testing;
    // the capture fires either way.
    if (msg.rssi == 0) {
        _ghost_rx_drops++;
        static uint32_t _last_ghost_evt_ms[NUM_MOUNTS] = {};
        uint32_t nowm = millis();
        if (nowm - _last_ghost_evt_ms[msg.src_idx] > 2000) {
            _last_ghost_evt_ms[msg.src_idx] = nowm;
            send_hub_event(1 /*ghost*/, mount_id, msg.rssi,
                           pkt.payload[0], pkt.payload[1]);
        }
#if HUB_DROP_GHOST_RSSI0
        return;   // drop — never reaches the display
#endif
    }

    bool was_offline = (_mount_last_seen[msg.src_idx] == 0);

    _mount_rssi[msg.src_idx]      = msg.rssi;
    _mount_last_seen[msg.src_idx] = millis();

    if (was_offline) {
        // Real connect transition — log it so the PC can compare a genuine
        // connect (real negative RSSI, IDLE state) against a ghost.
        send_hub_event(0 /*connect*/, mount_id, msg.rssi,
                       pkt.payload[0], pkt.payload[1]);
        refresh_espnow_peer(msg.src_idx);
        ui_send_to_mount(mount_id, CMD_GET_STATE, nullptr, 0);
    }

    // Teensy STATUS payload (10 bytes):
    //   [0] state  [1] flags  [2] pt_preset  [3] sl_preset
    //   [4..5] slot_occupied  [6..7] slot_at  [8] target_slot  [9] active_la_subject
    uint8_t state = pkt.payload[0];
    uint8_t flags = pkt.payload[1];

    _mount_last_state[msg.src_idx] = state;   // read by the maintenance-restart idle check

    // Cache per-mount presets + look-at subject — the OSC control server
    // builds preset-aware GOTO_SLOT / JOG / START_LOOK_AT_MOVE from these.
    if (pkt.payload_len >= 4) {
        _mount_pt_preset[msg.src_idx] = pkt.payload[2];
        _mount_sl_preset[msg.src_idx] = pkt.payload[3];
    }
    if (pkt.payload_len >= 10)
        _mount_la_subject[msg.src_idx] = pkt.payload[9];

    disp_update_cam(mount_id, state, flags, msg.rssi);

    if (pkt.payload_len >= 4 && state != STATE_JOGGING) {
        disp_update_preset(mount_id, pkt.payload[2], pkt.payload[3]);
    }

    if (pkt.payload_len >= 9) {
        uint16_t slot_occ = ((uint16_t)pkt.payload[4] << 8) | pkt.payload[5];
        uint16_t slot_at  = ((uint16_t)pkt.payload[6] << 8) | pkt.payload[7];
        uint8_t  tgt      = pkt.payload[8];
        disp_update_slots(mount_id, slot_occ, slot_at, tgt, state);
    }

    // Byte [9]: active look-at subject (0-7, 0xFF = none).
    // Forward to display on every STATUS so late-connecting displays always
    // show the correct selected subject without waiting for a move to start.
    if (pkt.payload_len >= 10) {
        disp_look_at_status(mount_id, pkt.payload[9]);
    }
}

// ---------------------------------------------------------------------------
// Display → hub: config commands from touchscreen
// ---------------------------------------------------------------------------

static void process_disp_byte(uint8_t b) {
    switch (_rx_state) {
        case RX_MAGIC:   if (b == DISP_UART_MAGIC) _rx_state = RX_TYPE; break;
        case RX_TYPE:    _rx_type = b; _rx_state = RX_LEN; break;
        case RX_LEN:
            _rx_len = b; _rx_pos = 0;
            _rx_state = (b == 0) ? RX_CHECKSUM : RX_PAYLOAD;
            break;
        case RX_PAYLOAD:
            if (_rx_pos < DISP_UART_MAX_PAYLOAD) _rx_buf[_rx_pos] = b;
            if (++_rx_pos >= _rx_len) _rx_state = RX_CHECKSUM;
            break;
        case RX_CHECKSUM: {
            uint8_t expected = disp_uart_checksum(_rx_type, _rx_len, _rx_buf);
            if (b == expected) dispatch_disp_msg(_rx_type, _rx_len, _rx_buf);
            _rx_state = RX_MAGIC;
            break;
        }
    }
}

static void dispatch_disp_msg(uint8_t type, uint8_t len, const uint8_t *d) {
    // ── Pairing (stage 3): decisions and table requests from the display ──
    if (type == DISP_MSG_PAIR_DECIDE && len >= 8) {
        _last_client_cmd_ms = millis();
        pair_decide(d[0], d[1], d + 2);
        return;
    }
    if (type == DISP_MSG_PAIR_FORGET && len >= 1) {
        _last_client_cmd_ms = millis();
        pair_forget(d[0]);
        return;
    }
    if (type == DISP_MSG_GET_MOUNT_TABLE) {
        disp_send_mount_table();
        return;
    }
    if (type == DISP_MSG_HEALTH && len >= 24) {
        // Display's health record — wrap into a CMD_HEALTH packet (sender
        // sentinel 0xFD) and forward to the PC over Serial only.
        uint8_t buf[PKT_BUF_SIZE + 4];
        uint16_t n = build_packet(buf, 0xFD /*display sentinel*/, ++_usb_diag_seq,
                                  CMD_HEALTH, d, 24);
        Serial.write(buf, n);
        return;
    }

    if (type == DISP_MSG_SEND_CMD && len >= 3) {
        uint8_t mount_id = d[0];
        CmdType cmd      = (CmdType)d[1];
        uint8_t plen     = d[2];
        if (len >= 3 + plen) {
            _last_client_cmd_ms = millis();   // display touch counts as client activity
            ui_send_to_mount(mount_id, cmd, d + 3, plen);
            // Intercept START_LOOK_AT_MOVE from the display board — same
            // notification path as TCP/WebSocket clients (see forward_to_mounts).
            if (cmd == CMD_START_LOOK_AT_MOVE && plen >= 2) {
                intercept_la_move_dir(mount_id, d[3 + 1]);  // d[3]=subject_id, d[4]=direction
            }
        }
    }
}

// ---------------------------------------------------------------------------
// WebSocket event handler
// ---------------------------------------------------------------------------

static void on_ws_event(AsyncWebSocket *server, AsyncWebSocketClient *client,
                        AwsEventType type, void *arg, uint8_t *data, size_t len) {
    if (type == WS_EVT_CONNECT) {
        // Drop STATUS packets silently when the send queue is full rather than
        // closing the connection.  Queue pressure comes from mount STATUS bursts;
        // the phone UI only needs ~10 Hz updates so dropped frames are fine.
        client->setCloseClientOnQueueFull(false);
        Serial.printf("WS client %u connected from %s\n",
                      client->id(), client->remoteIP().toString().c_str());
        // Trigger all known-online mounts to resend STATUS + full state so
        // the new WS client gets up-to-date data immediately (same as TCP connect).
        for (int i = 0; i < NUM_MOUNTS; i++) {
            if (_mount_last_seen[i] > 0) {
                ui_send_to_mount(i + 1, CMD_GET_STATUS, nullptr, 0);
                ui_send_to_mount(i + 1, CMD_GET_STATE,  nullptr, 0);
            }
        }
    } else if (type == WS_EVT_DISCONNECT) {
        Serial.printf("WS client %u disconnected\n", client->id());
    } else if (type == WS_EVT_DATA) {
        AwsFrameInfo *info = (AwsFrameInfo *)arg;
        if (info->final && info->index == 0 && info->len == len
                        && info->opcode == WS_BINARY
                        && len <= PKT_BUF_SIZE) {
            // Do NOT call forward_to_mounts here — we are in the AsyncTCP task.
            // Enqueue raw bytes; main loop drains and forwards, keeping all WS
            // state access (binaryAll sends + this receive) on one task.
            WsRxMsg msg;
            msg.len = (uint16_t)len;
            memcpy(msg.data, data, len);
            xQueueSend(_ws_rx_queue, &msg, 0);   // non-blocking; drop if full
        }
    }
}

#define HW_WDT_TIMEOUT_MS  30000   // hardware watchdog — reset if loop stalls

const uint32_t HEARTBEAT_MS = 2000;   // 2 s — well within mount's 5 s timeout
uint32_t last_hb = 0;

// ---- USB wedge diagnostic ----
// Monotonic counts of bytes / complete packets the hub has read from the PC
// over USB Serial.  Reported to the PC once a second so it can tell host-side
// from hub-side stalls (see send_usb_diag / the read loop).
static uint32_t _usb_rx_bytes      = 0;
static uint32_t _usb_rx_pkts       = 0;
static uint32_t _last_usb_diag_ms  = 0;
#define USB_DIAG_INTERVAL_MS  1000
// (_usb_diag_seq is declared early, next to the ghost guard — see above)
// Captured once at boot — tells the PC WHY the hub last reset (poweron / panic /
// brownout / watchdog).  Reported in every diag; the PC reads it when it sees
// the rx counters reset (i.e. the hub rebooted).
static uint8_t  _reset_reason      = 0;

// Send the USB-RX counters to the PC over Serial ONLY.  Deliberately bypasses
// broadcast_to_all()/_ws so TCP and WebSocket clients are untouched — this is a
// point-to-point diagnostic for the directly-wired PC.
static void send_usb_diag() {
    // 13 bytes: rx_bytes(u32) + rx_pkts(u32) + reset_reason(1) + ghost_drops(u32).
    // The trailing ghost-drops field is backward-compatible — older PC builds
    // read only the first 9 bytes.
    uint8_t payload[13];
    payload[0] = (_usb_rx_bytes >> 24) & 0xFF;
    payload[1] = (_usb_rx_bytes >> 16) & 0xFF;
    payload[2] = (_usb_rx_bytes >>  8) & 0xFF;
    payload[3] =  _usb_rx_bytes        & 0xFF;
    payload[4] = (_usb_rx_pkts  >> 24) & 0xFF;
    payload[5] = (_usb_rx_pkts  >> 16) & 0xFF;
    payload[6] = (_usb_rx_pkts  >>  8) & 0xFF;
    payload[7] =  _usb_rx_pkts         & 0xFF;
    payload[8] = _reset_reason;        // why the hub last booted (esp_reset_reason)
    payload[9]  = (_ghost_rx_drops >> 24) & 0xFF;
    payload[10] = (_ghost_rx_drops >> 16) & 0xFF;
    payload[11] = (_ghost_rx_drops >>  8) & 0xFF;
    payload[12] =  _ghost_rx_drops        & 0xFF;
    uint8_t buf[PKT_BUF_SIZE + 4];
    uint16_t n = build_packet(buf, 0xFE /*hub sentinel mount_id*/, ++_usb_diag_seq,
                              CMD_HUB_DIAG, payload, 13);
    Serial.write(buf, n);
}

// ---- Uniform health telemetry (hub node) ----
// One CMD_HEALTH to the PC over Serial every HEALTH_INTERVAL_MS, plus an
// immediate anomaly send on: first report, low heap, loop stall, or a jump in
// ESP-NOW send failures.  The hub's own health rides USB (no radio cost).
static uint32_t _health_last_ms      = 0;
static uint32_t _health_anom_ms      = 0;
static uint16_t _health_loop_max_ms  = 0;   // worst loop-iteration gap since last send
static uint32_t _health_last_txfail  = 0;
static uint32_t _health_fail_live    = 0;   // live-mount failures at last check
static bool     _health_first_sent   = false;

static void send_own_health(bool anomaly) {
    PayloadHealth h = {};
    h.node_type     = HEALTH_NODE_HUB;
    h.reset_reason  = _reset_reason;
    h.uptime_s      = millis() / 1000UL;
    h.free_heap     = (uint32_t)esp_get_free_heap_size();
    h.min_free_heap = (uint32_t)esp_get_minimum_free_heap_size();
    h.loop_max_ms   = _health_loop_max_ms;
    h.tx_fail       = (uint16_t)_espnow_fail_total;
    h.rssi          = 0;
    h.flags         = anomaly ? 0x01 : 0x00;
    h.node_u32      = _ghost_rx_drops;
    uint8_t buf[PKT_BUF_SIZE + 4];
    uint16_t n = build_health(buf, 0xFE /*hub sentinel*/, ++_usb_diag_seq, &h);
    Serial.write(buf, n);
    _health_last_ms     = millis();
    _health_loop_max_ms = 0;
    _health_last_txfail = _health_fail_live;
    _health_first_sent  = true;
}

// Called from the 500 ms self-check tick.
static void health_check(uint32_t now) {
    uint32_t fail_live = 0;
    for (int i = 0; i < NUM_MOUNTS; i++)
        if (_mount_last_seen[i] && (now - _mount_last_seen[i]) < SELF_WEDGE_ALIVE_MS)
            fail_live += _espnow_fail_cum[i];
    bool anomaly =
        (!_health_first_sent && now > 3000) ||
        (esp_get_free_heap_size() < HEALTH_LOW_HEAP_BYTES) ||
        (_health_loop_max_ms > HEALTH_LOOP_STALL_MS) ||
        (fail_live - _health_last_txfail >= HEALTH_TXFAIL_JUMP);
    _health_fail_live = fail_live;
    if (anomaly && (now - _health_anom_ms) >= HEALTH_ANOMALY_GAP_MS) {
        _health_anom_ms = now;
        send_own_health(true);
        return;
    }
    if (now - _health_last_ms >= HEALTH_INTERVAL_MS)
        send_own_health(false);
}

// Send a one-shot hub event to the PC over Serial ONLY (point-to-point, like
// send_usb_diag — never TCP/WS).  Used to capture mount connect transitions and
// dropped ghost frames in the PC log so the phantom-camera cause can be pinned.
//   kind: 0 = mount came online (real connect)   1 = ghost frame dropped (rssi==0)
static void send_hub_event(uint8_t kind, uint8_t mount_id, int8_t rssi,
                           uint8_t state, uint8_t flags) {
    uint32_t up = millis() / 1000UL;
    uint8_t p[9];
    p[0] = kind;
    p[1] = mount_id;
    p[2] = (uint8_t)rssi;
    p[3] = state;
    p[4] = flags;
    p[5] = (up >> 24) & 0xFF;
    p[6] = (up >> 16) & 0xFF;
    p[7] = (up >>  8) & 0xFF;
    p[8] =  up        & 0xFF;
    uint8_t buf[PKT_BUF_SIZE + 4];
    uint16_t n = build_packet(buf, 0xFE /*hub sentinel*/, ++_usb_diag_seq,
                              CMD_HUB_EVENT, p, 9);
    Serial.write(buf, n);
}

// Full ESP-NOW reinit — tears down and rebuilds the whole ESP-NOW stack and all
// mount peers.  This is what a hub power cycle does and is the confirmed cure for
// the hub→mount send wedge that peer-refresh alone cannot clear.  Mirrors the
// AMOLED's espnow_full_reinit().  Runs from loop() (never a callback), so the
// deinit/init is safe.  WiFi AP / TCP / WS are untouched.
// Re-register the ESP-NOW callbacks and every bound peer.  Shared by both
// recovery paths below.
static bool hub_espnow_rebuild() {
    if (esp_now_init() != ESP_OK) return false;
    esp_now_register_recv_cb(on_espnow_recv);
    esp_now_register_send_cb(on_espnow_sent);
    for (int i = 0; i < NUM_MOUNTS; i++) {
        if (!mount_mac_valid(i)) continue;   // unbound slot — no peer
        esp_now_peer_info_t peer = {};
        memcpy(peer.peer_addr, _mount_mac[i], 6);
        peer.channel = AP_CHANNEL;
        peer.ifidx   = WIFI_IF_AP;
        peer.encrypt = false;
        esp_now_add_peer(&peer);
        espnow_peer_long_range(peer.peer_addr);
        _espnow_fails[i]    = 0;
        _espnow_fail_run[i] = 0;
    }
    return true;
}

static void hub_espnow_full_reinit() {
    _last_reinit_ms = millis();   // shared cooldown stamp: PC- and self-triggered
    Serial.println("[ESP-NOW] Full reinit — start");
    esp_now_deinit();
    delay(50);
    if (!hub_espnow_rebuild()) {
        Serial.println("[ESP-NOW] reinit FAILED — will retry on next request");
        return;
    }
    Serial.println("[ESP-NOW] Full reinit — done");
}

// Stage 1b: bounce the WiFi driver itself, not just ESP-NOW.
//
// ESP-NOW rides on the WiFi MAC's TX path, so esp_now_deinit()/init() leaves
// that path untouched — which is why a reinit never cleared this wedge and the
// ladder had nothing between it and rebooting the whole hub.  Stopping and
// restarting WiFi resets the layer the wedge actually lives in, at the cost of
// dropping the AP for a moment: WiFi clients reconnect on their own, and mounts
// ride it out easily (their hub-silence watchdog is 10 s).
static void hub_wifi_full_reinit() {
    _last_reinit_ms      = millis();   // also suppress a stage-1 reinit right after
    _last_wifi_reinit_ms = millis();
    Serial.println("[WIFI] Full WiFi + ESP-NOW reinit — start");
    esp_now_deinit();
    WiFi.softAPdisconnect(true);
    WiFi.mode(WIFI_OFF);
    delay(200);
    WiFi.mode(WIFI_AP);
    if (!WiFi.softAPConfig(AP_IP, AP_GATEWAY, AP_SUBNET))
        Serial.println("[WIFI] softAPConfig failed on reinit");
    WiFi.softAP(AP_SSID, AP_PASSWORD, AP_CHANNEL);
    esp_wifi_set_ps(WIFI_PS_NONE);
    delay(100);
    if (!hub_espnow_rebuild()) {
        Serial.println("[WIFI] ESP-NOW re-init FAILED after WiFi bounce");
        return;
    }
    Serial.printf("[WIFI] Full reinit — done (AP %s ch %d)\n",
                  WiFi.softAPIP().toString().c_str(), AP_CHANNEL);
}

// ---------------------------------------------------------------------------
// OSC control server — Bitfocus Companion / QLab, direct to the hub
// ---------------------------------------------------------------------------
// Same /pts/... address space as the PC app's OSC server (docs/companion.md):
// Companion or QLab reaches the hub over the CamMount AP or the WiFi→LAN
// bridge and drives mounts with NO PC in the rig.  Parses the OSC 1.0 subset
// we use (messages, #bundle, i/f/s/T/F args; numerics coerced to int32).
//
// Jogs are re-streamed at 20 Hz (the mount dead-man needs a live stream) with
// a 15 s TTL bounding a lost UDP release.  OSC jogs stamp _ws_last_jog_ms so
// the existing radio-contention protection (WS STATUS suppression) applies,
// and every OSC command stamps _last_client_cmd_ms (defers the maintenance
// restart while Companion is in use).

#include <WiFiUdp.h>

#define OSC_PORT           9700
#define OSC_JOG_STREAM_MS  50          // 20 Hz
#define OSC_JOG_TTL_MS     15000UL

static WiFiUDP  _osc_udp;
static int16_t  _osc_jog[NUM_MOUNTS][4]    = {};
static uint32_t _osc_jog_until[NUM_MOUNTS] = {};   // 0 = no active OSC jog
static uint32_t _osc_last_stream_ms        = 0;
static int8_t   _osc_subject_sel[NUM_MOUNTS] = {-1, -1, -1, -1, -1};

static int osc_pad4(int n) { return (n + 3) & ~3; }

// Read a NUL-terminated, 4-byte-padded OSC string at ofs.  Returns the string
// (guaranteed NUL-terminated within the buffer) or nullptr; *next = following offset.
static const char *osc_str(const uint8_t *d, int len, int ofs, int *next) {
    int end = ofs;
    while (end < len && d[end] != 0) end++;
    if (end >= len) { *next = len; return nullptr; }
    *next = osc_pad4(end + 1);
    return (const char *)(d + ofs);
}

static void osc_send_jog_pkt(uint8_t mid) {
    uint8_t i = (uint8_t)(mid - 1);
    uint8_t p[10];
    write_be16(p + 0, (uint16_t)_osc_jog[i][0]);
    write_be16(p + 2, (uint16_t)_osc_jog[i][1]);
    write_be16(p + 4, (uint16_t)_osc_jog[i][2]);
    write_be16(p + 6, (uint16_t)_osc_jog[i][3]);
    p[8] = _mount_pt_preset[i];
    p[9] = _mount_sl_preset[i];
    ui_send_to_mount(mid, CMD_JOG, p, 10);
    _ws_last_jog_ms = millis();
}

static void osc_stop_jog(uint8_t mid) {
    uint8_t i = (uint8_t)(mid - 1);
    bool was_active = (_osc_jog_until[i] != 0);
    _osc_jog_until[i] = 0;
    memset(_osc_jog[i], 0, sizeof(_osc_jog[i]));
    osc_send_jog_pkt(mid);              // zeros → controlled deceleration
    if (was_active) Serial.printf("[OSC] CAM %d jog stop\n", mid);
}

static void osc_dispatch(const char *addr, const int32_t *a, int argc) {
    char buf[72];
    strncpy(buf, addr, sizeof(buf) - 1);
    buf[sizeof(buf) - 1] = 0;
    const char *tok[6]; int nt = 0;
    for (char *t = strtok(buf, "/"); t && nt < 6; t = strtok(nullptr, "/"))
        tok[nt++] = t;
    if (nt < 2 || strcmp(tok[0], "pts") != 0) return;

    _last_client_cmd_ms = millis();     // Companion counts as client activity

    if (nt == 2 && strcmp(tok[1], "estop") == 0) {
        Serial.println("[OSC] E-STOP ALL");
        for (int i = 0; i < NUM_MOUNTS; i++) {
            _osc_jog_until[i] = 0;
            memset(_osc_jog[i], 0, sizeof(_osc_jog[i]));
        }
        ui_send_to_mount(MOUNT_BROADCAST, CMD_E_STOP, nullptr, 0);
        return;
    }

    if (nt < 4 || strcmp(tok[1], "cam") != 0) return;
    int mid = atoi(tok[2]);
    if (mid < 1 || mid > NUM_MOUNTS) return;
    uint8_t idx = (uint8_t)(mid - 1);
    const char *verb = tok[3];

    if (strcmp(verb, "estop") == 0) {
        Serial.printf("[OSC] CAM %d E-STOP\n", mid);
        _osc_jog_until[idx] = 0;
        memset(_osc_jog[idx], 0, sizeof(_osc_jog[idx]));
        ui_send_to_mount((uint8_t)mid, CMD_E_STOP, nullptr, 0);

    } else if (strcmp(verb, "goto") == 0 && argc >= 1) {
        if (a[0] >= 1 && a[0] <= 10) {
            uint8_t p[3] = { (uint8_t)(a[0] - 1),
                             _mount_pt_preset[idx], _mount_sl_preset[idx] };
            Serial.printf("[OSC] CAM %d goto slot %ld\n", mid, (long)a[0]);
            ui_send_to_mount((uint8_t)mid, CMD_GOTO_SLOT, p, 3);
        }

    } else if (strcmp(verb, "store") == 0 && argc >= 1) {
        if (a[0] >= 1 && a[0] <= 10) {
            uint8_t s = (uint8_t)(a[0] - 1);
            Serial.printf("[OSC] CAM %d store slot %ld\n", mid, (long)a[0]);
            ui_send_to_mount((uint8_t)mid, CMD_STORE_POS, &s, 1);
        }

    } else if (strcmp(verb, "clear") == 0 && argc >= 1) {
        if (a[0] >= 1 && a[0] <= 10) {
            uint8_t s = (uint8_t)(a[0] - 1);
            Serial.printf("[OSC] CAM %d clear slot %ld\n", mid, (long)a[0]);
            ui_send_to_mount((uint8_t)mid, CMD_CLEAR_POS, &s, 1);
        }

    } else if (strcmp(verb, "jog") == 0) {
        if (nt >= 5 && strcmp(tok[4], "stop") == 0) { osc_stop_jog((uint8_t)mid); return; }
        bool any = false;
        for (int k = 0; k < 4; k++) {
            int32_t v = (k < argc) ? a[k] : 0;
            if (v >  1000) v =  1000;
            if (v < -1000) v = -1000;
            _osc_jog[idx][k] = (int16_t)v;
            if (v != 0) any = true;
        }
        if (any) {
            if (_osc_jog_until[idx] == 0)
                Serial.printf("[OSC] CAM %d jog start\n", mid);
            _osc_jog_until[idx] = millis() + OSC_JOG_TTL_MS;
            osc_send_jog_pkt((uint8_t)mid);
        } else {
            osc_stop_jog((uint8_t)mid);
        }

    } else if (strcmp(verb, "speed") == 0 && nt >= 5 && argc >= 1) {
        if (a[0] >= 1 && a[0] <= 4) {
            uint8_t grp;
            if      (strcmp(tok[4], "pt") == 0) { grp = GROUP_PAN_TILT;    _mount_pt_preset[idx] = (uint8_t)a[0]; }
            else if (strcmp(tok[4], "sl") == 0) { grp = GROUP_SLIDER_ZOOM; _mount_sl_preset[idx] = (uint8_t)a[0]; }
            else return;
            uint8_t p[2] = { grp, (uint8_t)a[0] };
            Serial.printf("[OSC] CAM %d speed/%s %ld\n", mid, tok[4], (long)a[0]);
            ui_send_to_mount((uint8_t)mid, CMD_SET_ACTIVE_PRESET, p, 2);
        }

    } else if (strcmp(verb, "subject") == 0 && argc >= 1) {
        if (a[0] >= 0 && a[0] <= 7) {
            _osc_subject_sel[idx] = (int8_t)a[0];
            Serial.printf("[OSC] CAM %d subject %ld\n", mid, (long)a[0]);
            if (_mount_last_state[idx] == STATE_LOOK_AT_MOVE ||
                _mount_last_state[idx] == STATE_LOOK_AT_PRE_AIM) {
                uint8_t s = (uint8_t)a[0];
                ui_send_to_mount((uint8_t)mid, CMD_SWITCH_SUBJECT, &s, 1);
            }
        }

    } else if (strcmp(verb, "lookat") == 0 && argc >= 1) {
        if (a[0] == 0 || a[0] == 1) {
            uint8_t subj = (_osc_subject_sel[idx] >= 0)
                               ? (uint8_t)_osc_subject_sel[idx]
                               : ((_mount_la_subject[idx] <= 7)
                                      ? _mount_la_subject[idx] : 0);
            uint8_t p[3] = { subj, (uint8_t)a[0], _mount_sl_preset[idx] };
            Serial.printf("[OSC] CAM %d lookat %s subject %u\n",
                          mid, a[0] ? ">" : "<", subj);
            ui_send_to_mount((uint8_t)mid, CMD_START_LOOK_AT_MOVE, p, 3);
            intercept_la_move_dir((uint8_t)mid, (uint8_t)a[0]);  // arrow flash everywhere
        }
    }
}

static void osc_handle_message(const uint8_t *d, int len) {
    int next = 0;
    const char *addr = osc_str(d, len, 0, &next);
    if (!addr || addr[0] != '/') return;
    int ofs = next;
    int32_t args[8]; int argc = 0;
    if (ofs < len && d[ofs] == ',') {
        const char *tags = osc_str(d, len, ofs, &next);
        if (!tags) return;
        ofs = next;
        for (int ti = 1; tags[ti] != 0 && argc < 8; ti++) {
            char t = tags[ti];
            if (t == 'i' && ofs + 4 <= len) {
                args[argc++] = (int32_t)be32(d + ofs); ofs += 4;
            } else if (t == 'f' && ofs + 4 <= len) {
                args[argc++] = (int32_t)lroundf(be_float(d + ofs)); ofs += 4;
            } else if (t == 'T') { args[argc++] = 1;
            } else if (t == 'F') { args[argc++] = 0;
            } else if (t == 's') {
                osc_str(d, len, ofs, &next); ofs = next;   // skip strings
            } else break;                                   // unsupported tag
        }
    }
    osc_dispatch(addr, args, argc);
}

// Messages or #bundle (recursive) — QLab wraps cues in bundles.
static void osc_handle_packet(const uint8_t *d, int len) {
    if (len >= 16 && memcmp(d, "#bundle\0", 8) == 0) {
        int ofs = 16;                    // header + 8-byte timetag (ignored)
        while (ofs + 4 <= len) {
            int32_t sz = (int32_t)be32(d + ofs);
            ofs += 4;
            if (sz <= 0 || ofs + sz > len) break;
            osc_handle_packet(d + ofs, sz);
            ofs += osc_pad4(sz);
        }
        return;
    }
    osc_handle_message(d, len);
}

// Called every loop(): drain datagrams + drive the 20 Hz jog re-stream.
static void osc_poll() {
    int psize;
    while ((psize = _osc_udp.parsePacket()) > 0) {
        static uint8_t rx[512];
        int n = _osc_udp.read(rx, sizeof(rx));
        if (n > 0) osc_handle_packet(rx, n);
    }
    uint32_t now = millis();
    if (now - _osc_last_stream_ms >= OSC_JOG_STREAM_MS) {
        _osc_last_stream_ms = now;
        for (int i = 0; i < NUM_MOUNTS; i++) {
            if (_osc_jog_until[i] == 0) continue;
            if (now >= _osc_jog_until[i]) {
                Serial.printf("[OSC] CAM %d jog TTL expired — stopping "
                              "(lost release?)\n", i + 1);
                osc_stop_jog((uint8_t)(i + 1));
            } else {
                osc_send_jog_pkt((uint8_t)(i + 1));
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

void setup() {
    Serial.begin(921600);
    // Make USB CDC TX non-blocking.  Default timeout is 100 ms — if the PC app
    // stops draining the port (Python thread stall, GIL contention, etc.) every
    // Serial.write() in broadcast_to_all() blocks for 100 ms.  The 32-deep relay
    // queue then takes 3+ seconds to drain, stalling loop() and making both the
    // hub-display joystick AND the PC joystick unresponsive until the PC is
    // restarted.  With timeout=0 writes return immediately when the buffer is full
    // — STATUS packets may be dropped but loop() never stalls.
    Serial.setTxTimeoutMs(0);

    crash_report_print();   // report the previous panic, if any

    // Ignore the host's DTR/RTS so the PC reopening COM3 can't reset the hub.
    // That host-triggered reset (reset reason USB) was the root of the comms
    // "wedge": a reconnect reopened the port → reset the hub → re-enumerate →
    // reopen → reset … an 80×/night reboot loop.  enableReboot() exists ONLY on
    // the TinyUSB USBCDC class, so this REQUIRES board setting
    //   Tools → USB Mode → "USB-OTG (TinyUSB)"   (ARDUINO_USB_MODE == 0).
    // The S3's default Hardware-CDC-and-JTAG port has no way to disable it.
    // Guarded so the sketch still compiles in either USB mode (no-op in HWCDC).
#if ARDUINO_USB_CDC_ON_BOOT && (ARDUINO_USB_MODE == 0)
    Serial.enableReboot(false);
#endif

    pkt_parser_init(&_serial_parser);
    memset(_la_dir, -1, sizeof(_la_dir));
    memset(_mount_last_state, 0xFF, sizeof(_mount_last_state));
    _reset_reason = (uint8_t)esp_reset_reason();   // why this boot happened
    // RTC_NOINIT memory is undefined after power-on/brownout and only
    // meaningful across software resets — zero the self-restart streak on any
    // non-software boot.
    if (esp_reset_reason() != ESP_RST_SW) _self_restart_streak = 0;
    Serial.println("\n=== ESP32 Camera Mount Hub (XIAO) ===");
    _disp_mux = xSemaphoreCreateMutex();
    Serial.printf("Reset reason: %d  (self-restart streak: %lu)\n",
                  (int)_reset_reason, (unsigned long)_self_restart_streak);

    // Display UART — start before anything else so the display gets updates
    // as soon as the hub is ready.
    Serial1.begin(DISP_UART_BAUD, SERIAL_8N1, DISP_RX_PIN, DISP_TX_PIN);

    // --- WiFi Access Point ---
    WiFi.mode(WIFI_AP);
    // softAPConfig() MUST be called before softAP() to take effect.
    if (!WiFi.softAPConfig(AP_IP, AP_GATEWAY, AP_SUBNET))
        Serial.println("WARNING: softAPConfig failed — AP will use the default IP");
    WiFi.softAP(AP_SSID, AP_PASSWORD, AP_CHANNEL);
    esp_wifi_set_ps(WIFI_PS_NONE);
    Serial.printf("AP  SSID : %s\n", AP_SSID);
    Serial.printf("AP  IP   : %s\n", WiFi.softAPIP().toString().c_str());
    Serial.printf("AP  MAC  : %s\n", WiFi.softAPmacAddress().c_str());

    // --- Relay queue ---
    // BEFORE ESP-NOW, not after.  esp_now_register_recv_cb() makes
    // on_espnow_recv live immediately; it runs in the wifi task and ends in
    // xQueueSend(_relay_queue, ...).  A frame arriving before the queue exists
    // is xQueueSend(NULL), which FreeRTOS does not tolerate:
    //
    //   assert failed: xQueueGenericSend queue.c:936 (pxQueue)   task 'wifi'
    //
    // The window used to run from the callback registration, through an NVS
    // read and one esp_now_add_peer() per paired mount, down to here.  With a
    // single mount on the bench it nearly always closed in time.  With five
    // mounts sending STATUS at 10 Hz it is ~50 chances a second to lose, and
    // the hub boot looped - 3 of 7 boots died here in one capture.  It also
    // explains commanded restarts reporting PANIC instead of SW: the restart
    // worked, then setup lost this race and the panic is what rebooted it.
    _relay_queue   = xQueueCreate(RELAY_QUEUE_DEPTH, sizeof(RelayMsg));
    _ws_rx_queue   = xQueueCreate(WS_RX_QUEUE_DEPTH, sizeof(WsRxMsg));

    // --- ESP-NOW ---
    if (esp_now_init() != ESP_OK) {
        Serial.println("ERROR: ESP-NOW init failed — halting.");
        while (true) delay(1000);
    }
    esp_now_register_recv_cb(on_espnow_recv);
    esp_now_register_send_cb(on_espnow_sent);

    // Load the paired-mount table from NVS and register a peer per bound slot.
    // Unbound slots pair automatically on first contact (mount_table_observe).
    mount_table_load();
    Serial.println("Paired mounts (NVS):");
    for (int i = 0; i < NUM_MOUNTS; i++) {
        if (!mount_mac_valid(i)) {
            Serial.printf("  CAM %d : (unpaired — waiting for first contact)\n", i + 1);
            continue;
        }
        Serial.printf("  CAM %d : %02X:%02X:%02X:%02X:%02X:%02X\n", i + 1,
                      _mount_mac[i][0], _mount_mac[i][1], _mount_mac[i][2],
                      _mount_mac[i][3], _mount_mac[i][4], _mount_mac[i][5]);
        esp_now_peer_info_t peer = {};
        memcpy(peer.peer_addr, _mount_mac[i], 6);
        peer.channel = AP_CHANNEL;
        peer.ifidx   = WIFI_IF_AP;
        peer.encrypt = false;
        if (esp_now_add_peer(&peer) == ESP_OK)
            espnow_peer_long_range(peer.peer_addr);
        else
            Serial.printf("WARNING: failed to add peer %d\n", i + 1);
    }
    disp_send_mount_table();   // seed the display's Mounts panel (it also
                               // requests this itself when it boots later)

#if DEMO_MODE
    demo_init();   // invent five mounts (after the queue exists)
#endif

    // --- TCP server ---
    for (int i = 0; i < MAX_CLIENTS; i++) {
        pkt_parser_init(&_slots[i].parser);
        _slots[i].active = false;
    }
    _tcp_server.begin();
    Serial.printf("TCP listening on port %d\n", TCP_PORT);

    // --- OSC control (Bitfocus Companion / QLab) ---
    _osc_udp.begin(OSC_PORT);
    Serial.printf("OSC control : UDP %d  (/pts/... — see docs/companion.md)\n",
                  OSC_PORT);

    // --- HTTP / WebSocket ---
    _ws.onEvent(on_ws_event);
    _http_server.addHandler(&_ws);
    _http_server.on("/", HTTP_GET, [](AsyncWebServerRequest *req) {
        // Serve via the (const uint8_t*, len) overload, NOT the const char* one.
        // In ESP_Async_WebServer 3.10.3, send(int,const char*,const char*) builds
        // an AsyncBasicResponse whose ctor does `String _content = content;` — one
        // contiguous ~112 KB heap copy.  On the XIAO S3's fragmented internal heap
        // (WiFi + ESP-NOW + AsyncTCP all resident) that allocation fails, _content
        // stays empty, and the reply ships as 200 / Content-Length: 0 (white page).
        // (The deprecated send_P forwards to the same copying path — same failure.)
        // The uint8_t*/len overload routes to AsyncProgmemResponse, which holds the
        // flash pointer and streams it in small buffers — no large allocation.
        // sizeof-1 = the literal's length (the raw string has no interior NULs).
        req->send(200, "text/html", (const uint8_t *)WEB_APP_HTML, sizeof(WEB_APP_HTML) - 1);
    });
    _http_server.begin();
    Serial.println("Ready.");

    esp_wifi_set_max_tx_power(84);   // max (~20.5 dBm) — every dB counts at 50 m

    // Hardware watchdog — resets the chip if loop() stalls for > 30 s
    // (e.g. AsyncTCP deadlock, ESP-NOW stack hang, lwIP timeout).
    // trigger_panic = true so the timeout actually RESETS the chip; with false
    // it only logs and never recovers — defeating the point of the watchdog.
    esp_task_wdt_config_t twdt_cfg = {
        .timeout_ms     = HW_WDT_TIMEOUT_MS,
        .idle_core_mask = 0,
        .trigger_panic  = true,
    };
    esp_task_wdt_reconfigure(&twdt_cfg);
    esp_task_wdt_add(NULL);
}

void send_heartbeat() {
    // Send a proper CMD_PING to every mount individually.
    // esp_now_send(NULL,...) is unreliable in ESP-IDF v5 — always use
    // explicit peer MACs via ui_send_to_mount so the packets are delivered.
    uint8_t payload[4];
    uint32_t ts = (uint32_t)millis();
    payload[0] = (ts >> 24) & 0xFF;
    payload[1] = (ts >> 16) & 0xFF;
    payload[2] = (ts >>  8) & 0xFF;
    payload[3] =  ts        & 0xFF;
    ui_send_to_mount(MOUNT_BROADCAST, CMD_PING, payload, 4);
}

// ---------------------------------------------------------------------------
// Autonomous self-recovery checks  (called from loop() every ~500 ms)
// ---------------------------------------------------------------------------

// Detect the hub→mount TX wedge and run the reinit→restart ladder without
// needing the PC.  Signature: sends to a mount FAIL back-to-back while its
// STATUS keeps arriving.  The 2 s heartbeat guarantees the fail counter moves
// even with no client traffic.
static void check_self_recovery(uint32_t now) {
    // The streak only exists to stop a ~40 s boot loop, and surviving this much
    // uptime proves we aren't in one — so clear it on uptime alone.  Requiring
    // 10 min *wedge-free* deadlocked a persistently wedged hub: the streak could
    // never clear while the wedge kept stamping _last_wedge_ms, so restarts
    // stayed disabled for good and the rig sat dead until someone power-cycled it.
    if (_self_restart_streak && now >= HEALTHY_CLEAR_MS) {
        Serial.printf("[SELF] 10 min wedge-free — clearing self-restart streak (%lu)\n",
                      (unsigned long)_self_restart_streak);
        _self_restart_streak = 0;
    }

    uint32_t worst_age = 0;
    int      worst_i   = -1;
    for (int i = 0; i < NUM_MOUNTS; i++) {
        bool alive   = _mount_last_seen[i] &&
                       (now - _mount_last_seen[i] < SELF_WEDGE_ALIVE_MS);
        bool failing = _espnow_fail_run[i] >= SELF_WEDGE_MIN_FAILS;
        if (alive && failing) {
            if (!_tx_wedge_since_ms[i]) {
                _tx_wedge_since_ms[i] = now ? now : 1;
                Serial.printf("[SELF] TX wedge suspected on mount %d "
                              "(sends failing, STATUS still arriving)\n", i + 1);
            }
            _last_wedge_ms = now;
            uint32_t age = now - _tx_wedge_since_ms[i];
            if (age >= worst_age) { worst_age = age; worst_i = i; }
        } else if (_tx_wedge_since_ms[i]) {
            Serial.printf("[SELF] TX wedge cleared on mount %d after %lu ms\n",
                          i + 1, (unsigned long)(now - _tx_wedge_since_ms[i]));
            _tx_wedge_since_ms[i]  = 0;
            _restart_block_logged  = false;
        }
    }
    if (worst_i < 0) return;

    uint32_t wsec = worst_age / 1000UL;
    uint8_t  wsec8 = (wsec > 255) ? 255 : (uint8_t)wsec;

    // Stage 1: full ESP-NOW reinit (shared cooldown with the PC-commanded path,
    // so with a PC attached its ~3 s reinit suppresses a duplicate here).
    if (worst_age >= SELF_REINIT_AFTER_MS &&
            (now - _last_reinit_ms) >= SELF_REINIT_COOLDOWN_MS) {
        Serial.printf("[SELF] Wedge on mount %d for %lu ms — full ESP-NOW reinit\n",
                      worst_i + 1, (unsigned long)worst_age);
        send_hub_event(2, (uint8_t)(worst_i + 1), 0, wsec8, _espnow_fail_run[worst_i]);
        hub_espnow_full_reinit();
        return;
    }

    // Stage 1b: the ESP-NOW reinit didn't clear it, so bounce WiFi itself.
    // The wedge lives below ESP-NOW, so stage 1 cannot reach it — this is the
    // rung that was missing, and the reason the ladder used to jump straight to
    // rebooting the hub.  Costs a brief AP dropout; mounts don't notice.
    if (worst_age >= SELF_WIFI_REINIT_AFTER_MS &&
            (now - _last_wifi_reinit_ms) >= SELF_WIFI_REINIT_COOLDOWN_MS) {
        Serial.printf("[SELF] Wedge on mount %d for %lu ms despite ESP-NOW reinit "
                      "— bouncing WiFi\n", worst_i + 1, (unsigned long)worst_age);
        send_hub_event(2, (uint8_t)(worst_i + 1), 0, wsec8, _espnow_fail_run[worst_i]);
        hub_wifi_full_reinit();
        return;
    }

    // Stage 2: neither reinit cleared it — restart the whole hub.  25 s beats
    // the PC app's 35 s escalation, so this fires first even with a PC attached.
    if (worst_age >= SELF_RESTART_AFTER_MS) {
        if (_self_restart_streak >= SELF_RESTART_MAX_STREAK) {
            if (!_restart_block_logged) {
                _restart_block_logged = true;
                Serial.printf("[SELF] Wedge persists but self-restart streak = %lu — "
                              "restarts held off until %lu min uptime "
                              "(reinit still retrying every %lu s)\n",
                              (unsigned long)_self_restart_streak,
                              (unsigned long)(HEALTHY_CLEAR_MS / 60000UL),
                              (unsigned long)(SELF_REINIT_COOLDOWN_MS / 1000UL));
            }
            return;
        }
        Serial.printf("[SELF] Wedge on mount %d for %lu ms despite reinit — "
                      "restarting hub\n", worst_i + 1, (unsigned long)worst_age);
        send_hub_event(3, (uint8_t)(worst_i + 1), 0, wsec8, 0);
        _self_restart_streak++;
        Serial.flush();
        delay(50);
        esp_restart();
    }
}

// Proactive maintenance restart: after MAINT_RESTART_UPTIME_MS, restart the
// hub the moment the system is quiet — no client command for 20 min and every
// connected mount IDLE.  Keeps uptime permanently below the ~12 h mark where
// the WiFi-driver pathologies (TX wedge, ghost RX frames) start appearing.
// Mounts ride through it: the hub is back well inside their 10 s E-STOP window.
static void check_maintenance_restart(uint32_t now) {
    if (now < MAINT_RESTART_UPTIME_MS) return;
    if (_last_client_cmd_ms && (now - _last_client_cmd_ms) < MAINT_RESTART_IDLE_MS) return;
    for (int i = 0; i < NUM_MOUNTS; i++) {
        bool connected = _mount_last_seen[i] &&
                         (now - _mount_last_seen[i] < SELF_WEDGE_ALIVE_MS);
        if (connected && _mount_last_state[i] != STATE_IDLE) return;   // something's moving
    }
    Serial.printf("[SELF] Maintenance restart at %lu h uptime (system idle) — "
                  "keeping the WiFi driver fresh\n",
                  (unsigned long)(now / 3600000UL));
    send_hub_event(4, 0, 0, (uint8_t)(now / 3600000UL), 0);
    Serial.flush();
    delay(50);
    esp_restart();
}

// ---------------------------------------------------------------------------
// Loop
// ---------------------------------------------------------------------------

void loop() {
    esp_task_wdt_reset();

    uint32_t now = millis();

#if DEMO_MODE
    demo_tick();   // feed synthetic STATUS into the normal relay path
#endif

    // Health telemetry: worst gap between loop iterations ≈ worst iteration time.
    static uint32_t _prev_loop_ms = 0;
    if (_prev_loop_ms) {
        uint32_t gap = now - _prev_loop_ms;
        if (gap > _health_loop_max_ms)
            _health_loop_max_ms = (gap > 65535) ? 65535 : (uint16_t)gap;
    }
    _prev_loop_ms = now;

    // ---- Full hub restart (PC escalation when the reinit didn't clear it) ----
    if (_need_full_restart) {
        Serial.println("[HUB] PC-requested full restart (esp_restart)");
        Serial.flush();
        delay(50);          // let the message and any queued USB TX drain
        esp_restart();
    }

    // ---- ESP-NOW full reinit (requested by the PC when it detects a wedge) ----
    if (_espnow_need_full_reinit) {
        _espnow_need_full_reinit = false;
        hub_espnow_full_reinit();
    }

    // ---- ESP-NOW peer refresh (triggered by consecutive send failures) ----
    // Only meaningful for a mount we can actually hear: if its STATUS keeps
    // arriving while our sends fail, the peer entry may be stale and re-adding
    // it can clear that.  A mount that simply isn't powered fails every send
    // forever, so refreshing it churns del_peer/add_peer on the WiFi driver
    // several times a second, permanently, for no possible gain — and that
    // churn is shared state with the peers that do work.
    for (int i = 0; i < NUM_MOUNTS; i++) {
        if (!_espnow_need_refresh[i]) continue;
        _espnow_need_refresh[i] = false;
        bool alive = _mount_last_seen[i] &&
                     (now - _mount_last_seen[i] < SELF_WEDGE_ALIVE_MS);
        if (!alive) continue;   // absent mount — failures are expected, don't churn
        Serial.printf("ESP-NOW: refreshing peer %d after %d consecutive send failures\n",
                      i + 1, ESPNOW_MAX_CONSEC_FAILS);
        refresh_espnow_peer(i);
    }

    // ---- Autonomous self-recovery (wedge ladder + maintenance restart) ----
    static uint32_t last_self_check = 0;
    if (now - last_self_check >= 500) {
        last_self_check = now;
        check_self_recovery(now);
        check_maintenance_restart(now);
        conflict_staleness_check(now);
        health_check(now);
    }

    if (now - last_hb >= HEARTBEAT_MS) {
        send_heartbeat();
        last_hb = now;
    }

    // ---- Accept new TCP connections ----
    WiFiClient incoming = _tcp_server.accept();
    if (incoming) {
        bool placed = false;
        for (int i = 0; i < MAX_CLIENTS; i++) {
            if (!_slots[i].active || !_slots[i].client.connected()) {
                _slots[i].client = incoming;
                pkt_parser_init(&_slots[i].parser);
                _slots[i].active = true; placed = true;
                Serial.printf("TCP client %d connected\n", i + 1);
                break;
            }
        }
        if (!placed) { incoming.stop(); Serial.println("Max TCP clients reached"); }

        // Rate-limited, because this is a RIG-WIDE burst on a per-client event:
        // an ESP-NOW peer teardown plus a 182-byte state dump for every mount,
        // and the replies go to every client, not just the new one.
        //
        // That was safe when a client meant a PC app connecting once a session.
        // It stopped being safe when a satellite began holding a client link on
        // behalf of phones: each reconnect fired the burst, and the burst
        // starved the very link whose failure had caused the reconnect.  One rig
        // showed every uplink write to the hub refused for a 30-second window
        // while this churned.
        //
        // A genuinely new client still gets its state — it only waits if
        // another client was served within the interval, in which case the data
        // it needs has just been broadcast anyway.
        if (now - _last_accept_burst_ms >= ACCEPT_BURST_MIN_MS) {
            _last_accept_burst_ms = now;
            for (int i = 0; i < NUM_MOUNTS; i++) {
                if (_mount_last_seen[i] > 0) {
                    // Refresh ESP-NOW peer before querying — clears any stale send
                    // state that may have accumulated while the PC was disconnected.
                    refresh_espnow_peer(i);
                    ui_send_to_mount(i + 1, CMD_GET_STATE, nullptr, 0);
                }
            }
        }
    }

    // ---- OSC control (Companion / QLab) → mounts ----
    osc_poll();

    // ---- TCP clients → mounts ----
    for (int i = 0; i < MAX_CLIENTS; i++) {
        if (!_slots[i].active) continue;
        if (!_slots[i].client.connected()) {
            _slots[i].client.stop(); _slots[i].active = false;
            Serial.printf("TCP client %d disconnected\n", i + 1);
            continue;
        }
        while (_slots[i].client.available()) {
            ParsedPacket pkt;
            if (pkt_feed(&_slots[i].parser, _slots[i].client.read(), &pkt))
                forward_to_mounts(pkt);
        }
    }

    // ---- USB Serial → mounts ----
    while (Serial.available()) {
        _usb_rx_bytes++;                       // diag: bytes actually read from the PC
        ParsedPacket pkt;
        if (pkt_feed(&_serial_parser, (uint8_t)Serial.read(), &pkt)) {
            _usb_rx_pkts++;                     // diag: complete packets framed from the PC
            if (pkt.cmd == CMD_HUB_REINIT_ESPNOW) {
                _espnow_need_full_reinit = true;  // hub-targeted; actioned in loop(), not forwarded
            } else if (pkt.cmd == CMD_HUB_RESTART) {
                _need_full_restart = true;        // hub-targeted; actioned in loop(), not forwarded
            } else {
                forward_to_mounts(pkt);
            }
        }
    }

    // ---- USB wedge diagnostic → PC over Serial ONLY (not TCP/WS) ----
    // Reports how many bytes/packets the hub has actually received from the PC.
    // During a PC↔hub USB wedge the PC can compare: if these keep climbing while
    // its own commands go unacked, the stall is hub-side; if they freeze while
    // the PC is still sending, the bytes never arrive — a host-side OUT halt.
    if (now - _last_usb_diag_ms >= USB_DIAG_INTERVAL_MS) {
        _last_usb_diag_ms = now;
        send_usb_diag();
    }

    // Broadcast drops, reported only when they happen.  A steady trickle means
    // a client is not draining its socket; silence means every client is
    // keeping up and the non-blocking send costs nothing.
    static uint32_t _last_bcast_report_ms = 0;
    if (now - _last_bcast_report_ms >= 30000UL) {
        _last_bcast_report_ms = now;
        if (_bcast_dropped)
            DIAG_PRINTF("[BCAST] %lu of %lu client writes dropped "
                          "(slow reader — frames shed, loop NOT blocked)\n",
                          (unsigned long)_bcast_dropped,
                          (unsigned long)_bcast_sent);
        _bcast_dropped = _bcast_sent = 0;
    }

    // ---- WebSocket RX → mounts (drained here, not in AsyncTCP callback) ----
    {
        WsRxMsg ws_msg;
        while (xQueueReceive(_ws_rx_queue, &ws_msg, 0) == pdTRUE) {
            PacketParser p; pkt_parser_init(&p); ParsedPacket pkt;
            for (uint16_t i = 0; i < ws_msg.len; i++) {
                if (pkt_feed(&p, ws_msg.data[i], &pkt)) {
                    forward_to_mounts(pkt);
                    // Track jog activity so STATUS broadcasts can be suppressed
                    // while the radio is busy with concurrent ESP-NOW traffic.
                    if (pkt.cmd == CMD_JOG) _ws_last_jog_ms = millis();
                    break;
                }
            }
        }
    }

    // ---- Display UART receive (config commands from touchscreen) ----
    while (Serial1.available())
        process_disp_byte((uint8_t)Serial1.read());

    // ---- Relay ESP-NOW packets → clients + display ----
    RelayMsg msg;
    while (xQueueReceive(_relay_queue, &msg, 0) == pdTRUE) {
        // ---- Pairing rules ----
        // The first packet in every message carries the sender's claimed mount
        // ID — run it through the binding rules (bind / move / conflict).
        // Ghost frames (rssi==0) never touch the table.  Messages from MACs
        // that end up unbound (rejected claim, or unparseable noise from an
        // unknown sender) are dropped, not relayed to clients.
        if (msg.rssi != 0) {
            PacketParser p0; pkt_parser_init(&p0); ParsedPacket pk0;
            for (uint16_t i = 0; i < msg.len; i++) {
                if (pkt_feed(&p0, msg.data[i], &pk0)) {
                    msg.src_idx = mount_table_observe(msg.src_mac, pk0.mount_id,
                                                      msg.rssi);
                    break;
                }
            }
        }
        if (msg.src_idx >= NUM_MOUNTS) continue;

        broadcast_to_all(msg.data, msg.len);   // serial + TCP (full rate)

        bool ws_send  = (msg.len >= 7);  // tentatively send to WS; may be cleared by STATUS rate-limit
        bool ws_force = false;           // one-shot events override the rate-limit
        if (msg.src_idx < NUM_MOUNTS) {
            // Parse ALL packets in this relay message — the Teensy may concatenate
            // multiple packets (e.g. STATUS + CALIB_PROMPT) into one ESP-NOW send.
            // The old break-after-first approach silently dropped the second packet.
            PacketParser p; pkt_parser_init(&p); ParsedPacket pkt;
            for (uint16_t i = 0; i < msg.len; i++) {
                if (pkt_feed(&p, msg.data[i], &pkt)) {
                    process_status_for_display(msg, pkt);
                    // Forward config report to display
                    if (pkt.cmd == CMD_CONFIG_REPORT
                            && pkt.payload_len >= CONFIG_REPORT_PAYLOAD_LEN
                            && msg.src_idx < NUM_MOUNTS) {
                        disp_config_report(msg.src_idx + 1, pkt.payload);
                    }
                    // Forward limits-found notification to display
                    if (pkt.cmd == CMD_LIMITS_FOUND
                            && pkt.payload_len >= 9
                            && msg.src_idx < NUM_MOUNTS) {
                        disp_limits_found(msg.src_idx + 1, pkt.payload);
                        ws_force = true;   // one-shot — must reach WS
                    }
                    // Forward home-complete notification to display
                    if (pkt.cmd == CMD_HOME_COMPLETE
                            && pkt.payload_len >= 1
                            && msg.src_idx < NUM_MOUNTS) {
                        disp_home_complete(msg.src_idx + 1, pkt.payload[0]);
                        ws_force = true;
                    }
                    // Forward subject validity mask to display (v2 look-at)
                    if (pkt.cmd == CMD_SUBJECT_LIST
                            && pkt.payload_len >= SUBJECT_LIST_PAYLOAD_LEN
                            && msg.src_idx < NUM_MOUNTS) {
                        disp_subject_mask(msg.src_idx + 1, pkt.payload);
                        ws_force = true;
                    }
                    // Forward look-at status subject_id to display (v2 look-at)
                    if (pkt.cmd == CMD_LOOK_AT_STATUS
                            && pkt.payload_len >= 13   // subject_id at byte 12
                            && msg.src_idx < NUM_MOUNTS) {
                        disp_look_at_status(msg.src_idx + 1, pkt.payload[12]);
                        ws_force = true;   // one-shot — must reach WS clients
                    }
                    // Forward calibration step notification to display
                    if (pkt.cmd == CMD_CALIB_PROMPT
                            && pkt.payload_len >= 1
                            && msg.src_idx < NUM_MOUNTS) {
                        disp_calib_prompt(msg.src_idx + 1, pkt.payload[0]);
                        ws_force = true;   // one-shot — must reach WS even if STATUS is rate-limited
                    }
                    // Suppress STATUS to WS while jogging (radio shared with ESP-NOW)
                    // and rate-limit to 5 Hz when idle.  This prevents TCP ACK
                    // starvation that previously caused lwIP to close the connection.
                    if (pkt.cmd == CMD_STATUS) {
                        uint32_t t = millis();
                        if (t - _ws_last_jog_ms < WS_JOG_SUPPRESS_MS) {
                            ws_send = false;   // radio busy with ESP-NOW jog traffic
                        } else if (t - _ws_status_ms[msg.src_idx] < WS_STATUS_INTERVAL_MS) {
                            ws_send = false;   // idle rate limit
                        } else {
                            _ws_status_ms[msg.src_idx] = t;
                        }
                    }
                    // Reset parser to catch any additional packets in this message.
                    pkt_parser_init(&p);
                }
            }
        }
        if (ws_send || ws_force) {
            uint8_t cmd = msg.data[6];
            if (ws_force
                    || cmd == CMD_STATUS || cmd == CMD_LIMITS_FOUND || cmd == CMD_HOME_COMPLETE
                    || cmd == CMD_STATE_REPORT || cmd == CMD_CONFIG_REPORT
                    || cmd == CMD_SUBJECT_LIST || cmd == CMD_LOOK_AT_STATUS
                    || cmd == CMD_CALIB_PROMPT)
                _ws.binaryAll(const_cast<uint8_t*>(msg.data), (size_t)msg.len);
        }
    }

    // ---- Mount disconnect timeout ----
    now = millis();
    for (int i = 0; i < NUM_MOUNTS; i++) {
        if (_mount_last_seen[i] > 0 &&
                now - _mount_last_seen[i] > MOUNT_TIMEOUT_MS) {
            _mount_last_seen[i] = 0;
            disp_set_disconnected(i + 1);
        }
    }

    // ---- Display reconciliation sweep ----
    // The transition above sends SET_DISCONNECTED exactly once, but the display
    // can silently drop it (its handler bails if the LVGL mutex is busy >100 ms),
    // and after a hub reboot _mount_last_seen[] is zeroed so the hub forgets which
    // tiles it lit.  Either way the display latches a phantom "connected" cam
    // forever (the ghost cam3 0 dBm/JOGGING bug).  Reconcile: periodically re-send
    // SET_DISCONNECTED for every mount the hub considers offline, so a stale tile
    // always clears within one sweep regardless of how it got latched.
    static uint32_t _last_disc_sweep_ms = 0;
    if (now - _last_disc_sweep_ms >= 5000) {
        _last_disc_sweep_ms = now;
        for (int i = 0; i < NUM_MOUNTS; i++)
            if (_mount_last_seen[i] == 0)
                disp_set_disconnected(i + 1);
    }

    // ---- Periodic client count → display ----
    if (now - _last_client_update_ms >= CLIENT_UPDATE_INTERVAL_MS) {
        _last_client_update_ms = now;
        uint8_t tc = 0;
        for (int i = 0; i < MAX_CLIENTS; i++)
            if (_slots[i].active && _slots[i].client.connected()) tc++;
        uint8_t wc = (uint8_t)_ws.count();
        if (tc != _tcp_count || wc != _ws_count) {
            _tcp_count = tc; _ws_count = wc;
            disp_update_clients(tc, wc);
        }
    }

    // ---- WebSocket housekeeping (every loop — required by mathieucarbou fork) ----
    _ws.cleanupClients();

}
