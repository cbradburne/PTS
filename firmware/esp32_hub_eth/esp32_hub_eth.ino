/*
 * esp32_hub_eth — THE TEST HUB.  Not yet the production firmware.
 *
 * firmware/esp32_hub/ is what is running on the rig and must keep working.
 * This folder is where its replacement is proven first, on the bench, before
 * the two are swapped.
 *
 * Differences from esp32_hub:
 *
 *   - Waveshare ESP32-S3-ETH instead of the XIAO: Ethernet (W5500 over SPI,
 *     PoE), which is what makes a wired backbone possible.  ETH bring-up is
 *     non-blocking and harmless with no W5500 fitted, so this also runs on a
 *     XIAO for testing everything else.
 *   - A settable location name in the AP SSID: "PTS-Concert Hall".  The
 *     production hub stays "CamMount"; the mount firmware accepts BOTH
 *     prefixes so the two can be tested side by side.
 *   - A satellite listener on port 7778 and per-mount routing, so mounts too
 *     far to hear this hub can be served over Ethernet.
 *
 * web_app.h and hub_types.h are INCLUDED from ../esp32_hub/, not copied — a
 * second copy of either is exactly how the 9-byte STATUS bug happened.
 *
 * Bench test with hub + display + mount:
 *   1. tools/build.sh flash hubeth
 *   2. Display comes up, mount pairs, positions recall — i.e. nothing regressed.
 *   3. curl -d "name=Bench" http://<hub-ip>/hubname   → restarts as "PTS-Bench"
 *   4. Long-press the mount, scan, confirm "PTS-Bench" is listed and pairs.
 *   5. [ETH] lines appear if a W5500 is present; harmless if not.
 */
/*
 * esp32_hub_eth.ino — WiFi AP + ESP-NOW hub  (Waveshare ESP32-S3-ETH)
 *
 * This board handles all networking: WiFi AP, ESP-NOW to mounts, Ethernet to
 * the satellites, TCP and WebSocket to PC/phone clients.  Display output is
 * forwarded to a separate Waveshare ESP32-S3-Touch-LCD-7 board over UART using
 * the disp_uart protocol.
 *
 * Architecture:
 *   [PC 1]    ── TCP (port 7777) ──┐
 *   [PC 2]    ── TCP (port 7777) ──┤
 *   [PC USB]  ── Serial 921600   ──┤── S3-ETH Hub ────────┬── Mount 1 ESP32
 *   [Phone]   ── WS  (port 80)   ──┘   (WiFi AP+ESP-NOW)  ├── Mount 2 ESP32
 *   [Tablet]  ── WS  (port 80)         │        │         ├── Mount 3 ESP32
 *                                UART  │        │ Ethernet├── Mount 4 ESP32
 *                    Waveshare display ┘        │         └── Mount 5 ESP32
 *                                          [satellites] ───── distant mounts
 *
 * UART wiring to display (3 wires):
 *   Hub GPIO43 (TX)  →  Waveshare display GPIO13 (RX)
 *   Hub GPIO44 (RX)  ←  Waveshare display GPIO12 (TX)
 *   Hub GND          —  Waveshare display GND
 *
 * Arduino IDE board settings for THIS board (Waveshare ESP32-S3-ETH).  These
 * must match FQBN_HUBETH in tools/build.sh — the scripted build is the one
 * that gets flashed, and a Tools-menu difference produces a different binary
 * from the same source:
 *   Board            : ESP32S3 Dev Module
 *   USB CDC On Boot  : Enabled
 *   USB Mode         : Hardware CDC and JTAG
 *   Flash Size       : 16MB (128Mb)
 *   Partition Scheme : 8M with spiffs (3MB APP/1.5MB SPIFFS)
 *   PSRAM            : Disabled
 *
 * On the USB mode, which is a real trade-off and not a free choice.  The S3
 * wires the host's DTR/RTS straight to reset and boot0 in silicon — that is
 * precisely what lets esptool flash this board with no BOOT button.  The same
 * wire means the PC can reset the hub simply by opening or closing the port,
 * and NOTHING in firmware can prevent it: enableReboot() exists only on the
 * TinyUSB USBCDC class, not on HWCDC.
 *
 * So: Hardware CDC keeps flashing button-free and accepts that the host can
 * reset the hub.  USB-OTG (TinyUSB) can refuse the reset via
 * Serial.enableReboot(false) — which is why esp32_hub.ino on the XIAO demands
 * it — but then reflashing needs BOOT held, RESET tapped, released, upload.
 * The VID/PID changes too, so Windows may hand out a new COM port.
 * Hardware CDC is chosen here because this board lives in an enclosure.
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
#include <esp_mac.h>      // esp_read_mac() — MAC-derived default hub name
#include <lwip/sockets.h> // SOL_SOCKET / SO_SNDTIMEO for the satellite link
#include "../shared/disp_uart.h"
#include "../esp32_hub/web_app.h"
#include "../esp32_hub/hub_types.h"
#include <ESPmDNS.h>
// sat_link.h first: it owns the hostname, and board_eth.h needs it to have been
// defined by the time it is included.
#include "../shared/sat_link.h"
#define ETH_HOSTNAME SAT_HUB_HOSTNAME

// The hub takes its wired address from DHCP.  The network now serves one, and
// the hub and every satellite are registered on it, so there is nothing to
// configure here — the address it received is printed at boot alongside the
// Ethernet MAC.
//
// Define these three again to pin a static address instead.  That is worth
// doing when the DHCP server only serves registered MACs and this board is not
// on the list yet: the chicken-and-egg where you need the lease to find the
// board and the board to find the MAC.  It costs you a second place the network
// is configured, so prefer registering the MAC and commenting them out again.
//
// ETH_DNS follows ETH_GATEWAY when unset, and mDNS resolves "pts-sat*.local"
// by multicast rather than by asking a server, so it is not usually needed.
//#define ETH_STATIC_IP  "192.169.1.10"
//#define ETH_SUBNET     "255.255.255.0"
//#define ETH_GATEWAY    "192.169.1.1"

#include "../shared/board_eth.h"
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
// Hardware CDC is the DEFAULT here, unlike esp32_hub, and is preferred:
// TinyUSB mode makes auto-reset-into-bootloader unreliable, so flashing can
// need the BOOT button — unacceptable on a unit sealed in an enclosure.
//
// The reason esp32_hub demands TinyUSB is Serial.enableReboot(false), which
// stops the host reopening the port from resetting the hub (an 80x/night
// reboot loop).  That API exists only on USBCDC — but HWCDC does not need it:
// it has no reboot-on-DTR logic to disable, and its connected state comes from
// USB SOF/plug detection rather than DTR, so the "hub goes mute without DTR"
// caveat in bridge.py is a TinyUSB property, not a universal one.  The
// enableReboot() call below is already #if-guarded and simply does not compile
// in this mode.
//
// STILL WORTH WATCHING ON THE BENCH: if the hub reboots whenever the PC app
// connects, reset reason USB, then this reasoning is wrong for the S3's
// USB-Serial-JTAG peripheral and the fix is one FQBN change back to
// USBMode=default.  It would be obvious immediately, not subtle.
#if ARDUINO_USB_MODE == 0
  #warning "Building in TinyUSB mode: flashing may need the BOOT button. USBMode=hwcdc is preferred here."
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

// The AP SSID is how a mount identifies this unit: its setup screen is built
// from a WiFi scan, before any ESP-NOW link exists, so the beacon is the only
// channel available at that moment.  A friendly name therefore has to live in
// the SSID itself — which is why it is composed at boot rather than fixed.
//
//   AP_SSID_PREFIX + location name   e.g. "PTS-Concert Hall"
//
// The prefix is the mount's scan filter (HUB_SSID_PREFIX in the mount sketch)
// and MUST match it.  Kept to four characters because the mount stores only 16
// usable characters per entry, leaving HUB_NAME_MAX for the name — enough for
// "Concert Hall" exactly.
#define AP_SSID_PREFIX  "PTS-"
#define HUB_NAME_MAX    12
static char AP_SSID[5 + HUB_NAME_MAX] = AP_SSID_PREFIX "Hub";

#define AP_PASSWORD  "camctrl123"
#define AP_CHANNEL   1

// Static soft-AP network config.  Clients (phone/tablet/laptop over WS/TCP)
// connect to AP_IP — this is the hub's address on its own WiFi network.
// (The directly-wired PC uses USB serial, so it's unaffected by this.)
//
// The standard SoftAP address, and the one every doc, the manual and the PC
// app's defaults have always quoted.  It used to be 169.254.22.22/16 for Dante
// audio-network compatibility, which put the AP on link-local - and once this
// hub gained Ethernet onto the Dante network itself, that /16 claimed the whole
// wired range as well, leaving two interfaces matching the same destinations.
// Dante reachability now belongs to the wire (ETH_STATIC_IP above); the AP is a
// private /24 that overlaps nothing.
static const IPAddress AP_IP     (192, 168, 4, 1);
static const IPAddress AP_GATEWAY(192, 168, 4, 1);     // the AP is its own gateway
static const IPAddress AP_SUBNET (255, 255, 255, 0);
#define TCP_PORT     SAT_HUB_CLIENT_PORT   // shared/sat_link.h — satellites dial it too
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

#include "../shared/hub_name.h"

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

// Hub UART to the display: GPIO43 (TX), GPIO44 (RX) — the same pins on
// the XIAO (D6/D7) and on the Waveshare S3-ETH, which is why this moved
// between the two boards unchanged.
// On XIAO with native USB, Serial (USB CDC) is separate from UART hardware,
// so GPIO43/44 are free for Serial1.
// UART to the 7" display.  43/44 are the S3's native UART0 pins, free here
// because the console is on native USB (CDCOnBoot), and broken out on both the
// XIAO and — expected, unverified — the Waveshare ESP32-S3-ETH.  The S3 routes
// UART through the GPIO matrix, so any free GPIO works; override here when
// porting rather than hunting through setup().
//
// Do NOT pick from: 26-32 (SPI flash), 33-37 (octal PSRAM, if fitted),
// 19/20 (USB D-/D+ — the PC app rides that), 0/3/45/46 (strapping), or the
// W5500 pins in shared/board_eth.h.
#ifndef DISP_TX_PIN
#define DISP_TX_PIN  43
#define DISP_RX_PIN  44
#endif


// Mutex protecting Serial1 (display UART) — both loop() and forward_to_mounts
// (called from loop) write to it, so no real concurrency issue, but explicit
// for clarity if this ever moves to a separate task.
// A MUTEX, not a spinlock.  Every one of these critical sections wraps
// disp_uart_send(), which does three Serial1.write() calls — and a write blocks
// when the TX buffer is full, waiting for the UART interrupt to drain it.
// portENTER_CRITICAL() disables interrupts, so that ISR could not run: the write
// waited for space only an interrupt could free, with interrupts off.  The
// interrupt watchdog then reset the hub at 300 ms — reset reason INT_WDT, seen
// three times, always under load and never reproducibly.
//
// A mutex gives the same mutual exclusion (the three writes stay one frame)
// while leaving interrupts enabled, so the UART drains and the write returns.
// Every caller is reached from loop(), never an ISR or the WiFi/AsyncTCP task,
// which is what makes a blocking primitive legal here.
static SemaphoreHandle_t _disp_mux = nullptr;

// Tolerates being called before setup() creates the mutex — some display sends
// happen during bring-up, and a missed lock there is harmless because nothing
// else is running yet.
static inline void disp_lock()   { if (_disp_mux) xSemaphoreTake(_disp_mux, portMAX_DELAY); }
static inline void disp_unlock() { if (_disp_mux) xSemaphoreGive(_disp_mux); }

// Whether each mount currently has a camera paired and connected, from the
// BLE_LINK bit of its own health report.  The hub had no use for mount health
// before this and only relayed it; the display needs this one bit to decide
// whether a Focus button means anything, and a button that does nothing is
// worse than no button.  Declared here because disp_update_cam() below reads it.
static bool _cam_linked[NUM_MOUNTS] = {};

// Declared here for the same reason _cam_linked is: disp_update_cam() below
// reads it, and the routing table itself lives with the satellite code further
// down.
static int8_t _mount_sat[NUM_MOUNTS];

// ---------------------------------------------------------------------------
// Display send helpers
// ---------------------------------------------------------------------------

static void disp_update_cam(uint8_t mount_id, uint8_t state, uint8_t flags, int8_t rssi) {
    // Rides along with the status the display already gets, rather than being
    // its own message: it then needs no change-tracking of its own and cannot
    // be missed, and a camera pairing shows up within one status refresh.
    uint8_t cf = 0;
    if (mount_id >= 1 && mount_id <= NUM_MOUNTS && _cam_linked[mount_id - 1])
        cf |= DISP_CAM_BLE_LINKED;
    // Which satellite the mount is reached through, 0 for the hub's own radio.
    // Same wire form as CMD_MOUNT_ROUTE so the two cannot drift: slot + 1.
    uint8_t sat = 0;
    if (mount_id >= 1 && mount_id <= NUM_MOUNTS && _mount_sat[mount_id - 1] >= 0)
        sat = (uint8_t)(_mount_sat[mount_id - 1] + 1);
    uint8_t buf[6] = { mount_id, state, flags, (uint8_t)rssi, cf, sat };
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

// issued - callbacks, on the hub.
//
// The mounts got this after the bench showed the pool leaking; the hub did not,
// and on 2026-09-08 the hub's own transmitter stopped at 6.00 h up and stayed
// stopped for FOURTEEN HOURS. Every mount on its radio went deaf while their
// health kept arriving, so the app showed them connected and not answering.
// The one mount reached through a satellite never noticed.
//
// Its self-rescue never fired because every rung is armed by a run of send
// FAILURES, and a callback that stops firing produces none: txfail sat at 83,
// unchanged, from the first minute to the last. A failure count cannot report
// the failure of the thing that reports failures. This can.
static volatile uint32_t _espnow_issued   = 0;   // esp_now_send() returned ESP_OK
static volatile uint32_t _espnow_cb_total = 0;   // callback fired, either status
static volatile uint32_t _espnow_cb_last_ms = 0; // ...and when it last did

static inline uint32_t espnow_in_flight() {
    uint32_t i = _espnow_issued, c = _espnow_cb_total;
    return (i >= c) ? (i - c) : 0;
}

static void on_espnow_sent(const wifi_tx_info_t *info, esp_now_send_status_t status) {
    // Before the per-mount work and outside it: the point of this counter is to
    // notice the callback not arriving at all, so it must be stamped for every
    // callback, including ones for a MAC no longer in the table.
    _espnow_cb_total   = _espnow_cb_total + 1;
    _espnow_cb_last_ms = millis();

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

// Frames the relay queue would not take.  xQueueSend() was called with a zero
// timeout and its return value discarded, so a full queue dropped mount traffic
// in complete silence — no counter, no log, nothing.  The only unmetered
// discard on the rig, and it sits on the path used by exactly one mount:
// frames from our OWN radio are enqueued by the WiFi task, asynchronously,
// while satellite frames are enqueued inside loop() in the same pass that
// drains the queue and so never find it full.
static uint32_t _relay_dropped = 0, _relay_queued = 0;

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
#define MOUNT_TIMEOUT_MS  MOUNT_PRESENCE_TIMEOUT_MS   // see shared/protocol.h

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

// "Mount is alive" = STATUS seen within this.  Was 7 s against a 10 Hz STATUS
// stream, which made it unmissable.  The mount now sends STATUS on CHANGE plus a
// 5 s refresh, so 7 s left exactly one refresh of margin: a single lost packet
// dropped a healthy mount out of the live set, and this window gates the hub's
// wedge detector, its reinit ladder and its own restart.  16 s tolerates three
// consecutive misses and is still far inside any real outage.
#define SELF_WEDGE_ALIVE_MS     MOUNT_PRESENCE_TIMEOUT_MS  // see shared/protocol.h

// Is this mount being heard?  Written out six times before this existed, and
// everything an offline mount must NOT do — advertise its locations, report a
// speed, accept a speed change — depends on getting the same answer in each
// place.  The zero check matters: a mount never seen has _mount_last_seen 0,
// and millis() - 0 is simply a large number.
static inline bool mount_is_active(int i, uint32_t now) {
    return _mount_last_seen[i] && (now - _mount_last_seen[i]) < SELF_WEDGE_ALIVE_MS;
}
#define SELF_WEDGE_MIN_FAILS     2        // uninterrupted send fails before the wedge clock starts
#define SELF_REINIT_AFTER_MS     6000UL   // wedge age → full ESP-NOW reinit
#define SELF_WIFI_REINIT_AFTER_MS 14000UL // wedge age → bounce WiFi (below ESP-NOW)
// Own cooldown: stage 1's 30 s gap must not suppress this rung, or the 25 s
// restart would always beat it and the WiFi bounce would never run at all.
#define SELF_WIFI_REINIT_COOLDOWN_MS 90000UL
#define SELF_REINIT_COOLDOWN_MS  30000UL  // min gap between reinits (PC- or self-triggered)
#define SELF_RESTART_AFTER_MS    25000UL  // wedge age → esp_restart()

// The rung the 14-hour outage needed.
//
// Everything above is armed by a run of send failures. The fault that actually
// took the rig down produces none: the send callback stops firing, so nothing
// is ever reported as failed and txfail freezes at whatever it already was.
// This arms on the callback going QUIET instead — sends accepted by the stack,
// nothing coming back — which is the one symptom that fault does have.
//
// 3 s is far longer than a send takes to complete even with retries (about a
// millisecond on air, tens with the MAC retrying), and short enough that the
// existing 6 s reinit rung still runs first when it can.
#define SELF_CB_STALL_MS         3000UL
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
static uint32_t _cb_stall_since_ms = 0;   // 0 = callbacks are arriving
static bool     _cb_stall_active   = false;
static uint32_t _last_reinit_ms       = 0;   // stamped by hub_espnow_full_reinit()
static uint32_t _last_wifi_reinit_ms  = 0;   // stamped by hub_wifi_full_reinit()
static uint32_t _last_wedge_ms        = 0;   // last time any wedge clock was active
static uint32_t _last_client_cmd_ms   = 0;   // last command from any client (TCP/WS/serial/display)
static uint8_t  _mount_last_state[NUM_MOUNTS];  // last STATUS state byte (0xFF = unknown)
// Last STATUS flags byte, kept for FLAG_HAS_SLIDER.  A rig mixes mounts with a
// rail and mounts without, and a surface should not be offered a control that
// does nothing: the web app has always zeroed the slider axis for a mount that
// has none, and OSC now matches it.
static uint8_t  _mount_flags[NUM_MOUNTS] = {};
static inline bool mount_has_slider(int i) {
    return (i >= 0 && i < NUM_MOUNTS) && (_mount_flags[i] & FLAG_HAS_SLIDER);
}
static bool     _restart_block_logged = false;  // rate-limits the streak-exceeded log line

// Per-mount live context cached from STATUS — consumed by the OSC server.
static uint8_t  _mount_pt_preset[NUM_MOUNTS]  = {2, 2, 2, 2, 2};
static uint8_t  _mount_sl_preset[NUM_MOUNTS]  = {2, 2, 2, 2, 2};
static uint8_t  _mount_la_subject[NUM_MOUNTS] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
// Slot state, cached for the OSC feedback.  It was previously forwarded
// straight to the display and not retained — nothing else needed it.
static uint16_t _mount_slot_occ[NUM_MOUNTS] = {};
static uint16_t _mount_slot_at[NUM_MOUNTS]  = {};
static uint8_t  _mount_target[NUM_MOUNTS]   = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

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
        send_pair_event((uint8_t)cur, 1 /*renumber*/, mac);
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
            send_pair_event((uint8_t)cur, 2 /*replace*/, mac);
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
    send_pair_event((uint8_t)slot, 3 /*forget*/, _mount_mac[slot]);
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

// ---------------------------------------------------------------------------
// Satellites
// ---------------------------------------------------------------------------
// A satellite is an ESP-NOW cell on the end of an Ethernet cable, serving
// mounts too far away to hear this hub directly.  It connects to a DIFFERENT
// port from the PC app: a client on 7777 is an observer, a satellite owns
// mounts, and the port is what tells them apart without touching the protocol.
//
// Routing is LEARNED, never configured.  A mount decides which satellite to
// attach to (strongest AP wins, in its own scan), so the hub finds out by
// seeing whose uplink the mount's traffic arrives on.  Nothing to keep in step
// by hand, and a mount that roams moves its own route with it.
#define MAX_SATELLITES  6

static_assert(MAX_SATELLITES == SAT_SLOTS,
              "MAX_SATELLITES and SAT_SLOTS size the same wire payload "
              "(CMD_SAT_NAMES) and must not drift apart");

struct SatSlot {
    WiFiClient   client;
    SatEnvParser parser;
    bool         active;
    // Empty until the satellite introduces itself, and empty for one too old to
    // do so — clients fall back to the slot number, which is what they showed
    // before names existed.
    char         name[SAT_NAME_LEN];
    // The address its TCP connection came from.  A satellite prints the DHCP
    // lease it was given at boot, but only to its own USB serial, which is no
    // use once the unit is rigged.  Taken here instead, so nothing has to be
    // asked of the satellite and no satellite needs reflashing to be findable.
    uint32_t     ip;
};
static SatSlot   _sat[MAX_SATELLITES];
static WiFiServer _sat_server(SAT_LINK_PORT);

// mount_id-1 -> satellite slot serving it, or -1 for "local ESP-NOW".
// (Declared up with the display helpers, which read it.)

// A satellite dropping off must not strand its mounts pointing at a dead
// socket: they revert to local ESP-NOW, which is also what happens if the
// mount roams back into range of the hub itself.
static void sat_release_mounts(int slot) {
    for (int i = 0; i < NUM_MOUNTS; i++)
        if (_mount_sat[i] == slot) {
            _mount_sat[i] = -1;
            Serial.printf("[SAT] mount %d back to local ESP-NOW\n", i + 1);
        }
}

static AsyncWebServer _http_server(80);
static AsyncWebSocket _ws("/ws");

static uint8_t   _tcp_count = 0, _ws_count = 0;
static uint32_t  _last_client_update_ms = 0;
#define CLIENT_UPDATE_INTERVAL_MS 500

static PacketParser _serial_parser;

// ---------------------------------------------------------------------------
// Forward declarations
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Addressed ESP-NOW send, skipping mounts that are not switched on
// ---------------------------------------------------------------------------
// mount_mac_valid() only asks "is this slot PAIRED?", so a paired-but-absent
// mount was transmitted to for ever.  That is the NORMAL state on this rig —
// five mounts, one permanent and the rest put out per event — so the firmware
// has to handle it rather than the operator unpairing kit between shows.
//
// The cost was ~3 failed sends a second, around the clock, and the suspicion is
// that the failure path leaks: the hub's TX stops accepting sends entirely
// every 2-6 hours (txfail freezes, every mount stops ACKing) and only a full
// restart clears it.  Failures per hour would then set how fast that arrives.
// This does not prove the theory, but transmitting to a device known to be
// absent is wrong regardless, and if the wedges thin out the theory is right.
//
// A live mount is heard from constantly (STATUS at 10 Hz, health every 10 s),
// so 30 s of silence means genuinely gone.  It resumes on its own the moment
// the mount is switched on, because the mount announces itself.
//
// NOTE: if the hub's own ESP-NOW RX ever wedged, every mount would look absent
// and this would stop all sends.  That is acceptable — a hub that cannot hear
// is not going to be heard either — and the PC app still sees the missing ACKs.
#define MOUNT_PRESENT_MS  30000UL

// How a mount is reached, decided in ONE place.  A mount attached to a
// satellite is out of radio range from here — that is why it has a satellite —
// so anything sent to it must go over the wire.
//
// ui_send_to_mount() used to skip this and always use ESP-NOW, which meant
// every hub-display button press aimed at a satellite-attached mount went out
// on a radio that mount cannot hear.  It failed silently from the operator's
// side, and the failures fed the wedge detector, which escalated to restarting
// the hub: 61 escalations on one rig, all naming the satellite mount, and each
// restart dropped the satellite link and took that mount down for real.
static void send_to_mount_routed(int idx, const uint8_t *raw, uint16_t len) {
    if (idx < 0 || idx >= NUM_MOUNTS) return;
    if (_mount_sat[idx] >= 0) {
        SatSlot &sl = _sat[_mount_sat[idx]];
        if (sl.active && sl.client.connected()) {
            // A single non-blocking send, NOT NetworkClient::write().  That
            // function waits in select() for 1 s at a time, up to 10 retries,
            // and a PARTIAL write resets the retry count - so a satellite that
            // is draining slowly can hold the caller for a minute or more.  It
            // cannot be bounded with SO_SNDTIMEO either: its send() passes
            // MSG_DONTWAIT, so that option is never consulted.
            //
            // On the satellite that cost one mount.  Here it would hold up the
            // loop serving all five, the display and every client, because one
            // satellite stopped reading.  If the frame will not go now it does
            // not go: the mount behind that satellite is unreachable either
            // way, and the rest of the rig keeps running.
            int sfd = sl.client.fd();
            if (sfd >= 0) ::send(sfd, raw, len, MSG_DONTWAIT);
        }
        return;
    }
    espnow_send_if_present(idx, raw, len);
}

static void espnow_send_if_present(int idx, const uint8_t *raw, uint16_t len) {
    if (idx < 0 || idx >= NUM_MOUNTS) return;
    if (!mount_mac_valid(idx)) return;
    uint32_t seen = _mount_last_seen[idx];
    if (seen == 0 || (millis() - seen) > MOUNT_PRESENT_MS) return;   // not here

    esp_err_t e = esp_now_send(_mount_mac[idx], raw, len);
    if (e == ESP_OK) {
        // Counted only on ESP_OK: a rejected send never took a buffer, so
        // counting it would read as an outstanding send that will never
        // complete — the very thing being watched for.
        _espnow_issued = _espnow_issued + 1;
        return;
    }

    // A REJECTED send never becomes a transmission, so it is invisible in the
    // txfail counter — which counts sends that failed on air.  That is exactly
    // what a frozen txfail looks like, so this is the line that would identify
    // the wedge: ESP_ERR_ESPNOW_NO_MEM here means the stack has run out of TX
    // buffers.  Rate-limited per mount so a persistent fault cannot bury the log.
    static uint32_t last_log[NUM_MOUNTS] = {};
    uint32_t now = millis();
    if (now - last_log[idx] < 2000) return;
    last_log[idx] = now;
    Serial.printf("[ESPNOW] send to mount %d REJECTED: %s\n", idx + 1, esp_err_to_name(e));
}

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
        // E-STOP is deliberately NOT de-duplicated.  Every other broadcast goes
        // down a satellite link once, because the satellite fans it to its own
        // peers and a second copy is pure waste.  A stop command is the one
        // where a wasted copy is cheaper than a lost one, and the extra copies
        // were there — by accident — before this.  Removing them quietly from
        // the one safety-critical path would be a poor trade.
        if (cmd == CMD_E_STOP) {
            for (int i = 0; i < NUM_MOUNTS; i++)
                if (mount_mac_valid(i)) send_to_mount_routed(i, raw, raw_len);
        } else {
            broadcast_to_mounts_routed(raw, raw_len, true);
        }
    } else if (mount_id >= 1 && mount_id <= NUM_MOUNTS) {
        if (mount_mac_valid(mount_id - 1))
            send_to_mount_routed(mount_id - 1, raw, raw_len);
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
    msg.via_sat = -1;                  // arrived on our own radio

    _relay_queued++;
    if (xQueueSend(_relay_queue, &msg, 0) != pdTRUE) _relay_dropped++;
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
//     DIAG=1 ./tools/build.sh hubeth
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

// Bounded write to USB serial: the whole frame or none of it.
//
// setup() sets setTxTimeoutMs(0) so a host that stops draining cannot stall
// loop() — that part is long since handled.  What it does not handle is that
// HWCDC::write() still returns so_far when the ring fills, so the frame is
// TRUNCATED rather than dropped, and a truncated packet desynchronises the
// reader until it happens on the next magic.
//
// All-or-nothing fixes that.  A dropped frame is harmless: the reader hunts for
// 0xAA 0x55 and checks a CRC, so it costs one status update.  A partial frame
// is not, so this never writes "as much as fits".
static uint32_t _ser_writes = 0, _ser_dropped = 0;

// ---------------------------------------------------------------------------
// Loop timing
// ---------------------------------------------------------------------------
// A 13.8-second pass wedged mount 1 into a reconnect and the hub could not say
// where the time went.  The satellite has had [RATE] since the day its problems
// started, which is exactly why they were solvable and this one was not: three
// separate theories about this stall were argued from symptoms alone and the
// leading one — a blocking Serial.write() — turned out to have been bounded in
// setup() all along.
//
// Sections are timed individually because "the loop took 13 seconds" narrows
// nothing.  MARK() charges the time since the previous mark to a section, so
// every microsecond of the pass lands somewhere and nothing hides in the gaps.
// Only the worst pass per section survives each report.
enum { SEC_TOP, SEC_ACCEPT, SEC_SAT, SEC_OSC, SEC_TCP, SEC_USB, SEC_WS,
       SEC_DISP, SEC_RELAY, SEC_N };
static const char *const SEC_NAME[SEC_N] = {
    "top", "accept", "sat", "osc", "tcp", "usb", "ws", "disp", "relay" };
static uint32_t _sec_max[SEC_N] = {};
static uint32_t _loop_count = 0, _loop_max_us = 0, _loop_report_ms = 0;
#define LOOP_REPORT_MS  30000UL

// Sent to comms.log, not to serial — the hub's USB port carries the binary
// packet stream and is not humanly readable.
//   [0]=9  [1]=worst section  [2..3]=that section ms  [4..5]=worst pass ms
//   [6..7]=loops/s  [8]=percent of serial frames dropped
//
// A percentage, not a count: the count read 255 — its cap — in all 36 reports
// of the first soak, because nothing was draining the USB port and every frame
// was being discarded.  A saturated counter says only that it saturated.  100%
// says "nobody is reading this port", 3% says "occasional pressure", and the
// two need telling apart.
static void send_loop_report() {
    int worst = 0;
    for (int i = 1; i < SEC_N; i++) if (_sec_max[i] > _sec_max[worst]) worst = i;
    uint32_t secs = LOOP_REPORT_MS / 1000UL;
    uint32_t lps  = _loop_count / (secs ? secs : 1);
    uint16_t sms  = (uint16_t)((_sec_max[worst] / 1000UL) > 65535 ? 65535 : _sec_max[worst] / 1000UL);
    uint16_t pms  = (uint16_t)((_loop_max_us   / 1000UL) > 65535 ? 65535 : _loop_max_us   / 1000UL);
    uint8_t p[9] = { 9, (uint8_t)worst, (uint8_t)(sms >> 8), (uint8_t)sms,
                     (uint8_t)(pms >> 8), (uint8_t)pms,
                     (uint8_t)(lps >> 8), (uint8_t)lps,
                     (uint8_t)(_ser_writes ? (_ser_dropped * 100UL) / _ser_writes : 0) };
    send_hub_event_raw(p);
    for (int i = 0; i < SEC_N; i++) _sec_max[i] = 0;
    _loop_count = _loop_max_us = _ser_dropped = _ser_writes = 0;
}

static void serial_write_frame(const uint8_t *d, uint16_t n) {
    _ser_writes++;
    if (Serial.availableForWrite() >= (int)n) Serial.write(d, n);
    else                                      _ser_dropped++;
}

// Dropped frames per client, reported below.  A slow client losing status
// frames is not interesting; a client losing them steadily is.
static uint32_t _bcast_dropped = 0, _bcast_sent = 0;

// Fan a MOUNT_BROADCAST frame out per route, sending it down each satellite
// link ONCE.
//
// The obvious loop — send_to_mount_routed() for every mount — puts the same
// broadcast-addressed frame down a satellite's socket once per mount routed
// there, and the satellite then fans EACH copy to every peer it holds.  Two
// mounts on one satellite therefore cost four radio transmissions per
// broadcast, not two.  Measured: PING x60 per 10 s at the relay against 1.5
// logical broadcasts a second, which is exactly 4x.
//
// The satellite already fans a broadcast correctly on its own, so one copy per
// link is all it needs.  Directly-attached mounts still get one send each: the
// hub must address them individually, because esp_now_send(NULL, ...) is
// unreliable in ESP-IDF v5.
// Pairing actions, as an EVENT rather than only a Serial.printf.
//
// Every place the hub rebinds or unbinds a mount announces itself with
// Serial.printf and nothing else — and on a bench rig the PC app owns that
// port, reading it as a packet stream, so the announcements are discarded as
// noise between frames.  The hub can therefore renumber a mount repeatedly and
// the only record of it is unreadable.
//
// That matters because rebinding zeroes _mount_last_seen, which is exactly what
// makes the next STATUS look like a fresh connect: the display flaps the mount
// in and out, and comms.log shows "mount N ONLINE" every few seconds with no
// stated cause.
//   kind 12: [1] slot+1  [2] action  [3..8] MAC
static void send_pair_event(uint8_t slot, uint8_t action, const uint8_t *mac) {
    uint8_t e[9] = { 12, (uint8_t)(slot + 1), action,
                     mac[0], mac[1], mac[2], mac[3], mac[4], mac[5] };
    send_hub_event_raw(e);
}

// An OSC camera command, named, sent where the PC log can see it.
//   kind 13: [1] mount  [2] verb  [3..4] value, big-endian
//
// Every camera command — autofocus, tally, record, iris — travels as one
// CMD_CAM_CONTROL, so the satellite's per-command-type counter buckets them all
// together and cannot tell a tally from a focus.  That counter is also printed
// as a top-3-per-window summary, so a handful of camera commands vanish behind
// PING and JOG entirely.  Asked which commands had actually gone out, the rig
// could not say.
//
// The hub knew all along and was saying so on USB serial — a port the log
// itself reports as "not being read (all frames dropped)", because the PC app
// reaches the hub over TCP.  An instrument writing into a disconnected port is
// not an instrument.  This says the same thing down the link that is actually
// connected.
//
// Value is the raw wire value, not a rounded one: for tally that is the 5.11
// fixed-point number the camera receives, so the log shows exactly what was
// sent rather than what was meant.
static void send_osc_cam_event(uint8_t mount_id, uint8_t verb, uint16_t value) {
    uint8_t e[9] = { 13, mount_id, verb,
                     (uint8_t)(value >> 8), (uint8_t)value, 0, 0, 0, 0 };
    send_hub_event_raw(e);
}

static void broadcast_to_mounts_routed(const uint8_t *raw, uint16_t len,
                                       bool require_valid_mac) {
    uint8_t sat_done = 0;   // bit per satellite slot already sent this frame
    static_assert(MAX_SATELLITES <= 8, "sat_done bitmask is a uint8_t");
    for (int i = 0; i < NUM_MOUNTS; i++) {
        int8_t sat = _mount_sat[i];
        if (sat >= 0) {
            if (sat_done & (uint8_t)(1u << sat)) continue;   // link already has it
            sat_done |= (uint8_t)(1u << sat);
        } else if (require_valid_mac && !mount_mac_valid(i)) {
            continue;
        }
        send_to_mount_routed(i, raw, len);
    }
}

static void broadcast_to_all(const uint8_t *data, uint16_t len) {
    serial_write_frame(data, len);
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

// Which path the hub currently reaches each mount by: 0 = its own radio,
// N = relayed by satellite N.  Sent on every change and whenever a client asks
// for the mount table, so a client that connects after a roam does not show the
// route as it was the last time it looked.
// Slot -> location name, for clients that would otherwise show accept order.
// Sent whenever the set changes (a satellite arriving, leaving, or naming
// itself) and alongside the mount table, so a client connecting after the fact
// is not left with numbers until the next change.
// The display needs the names too, and gets them from inside send_sat_names()
// rather than at its call sites: there are three of those and a fourth would
// have been added without this one, leaving the screen showing slot numbers
// while every other client showed names.
static void disp_send_sat_names() {
    uint8_t buf[SAT_SLOTS * SAT_NAME_LEN];
    memset(buf, 0, sizeof(buf));
    for (int i = 0; i < MAX_SATELLITES && i < SAT_SLOTS; i++)
        memcpy(buf + i * SAT_NAME_LEN, _sat[i].name, SAT_NAME_LEN);
    disp_lock();
    disp_uart_send(Serial1, DISP_MSG_SAT_NAMES, buf, sizeof(buf));
    disp_unlock();
}

static void send_sat_names() {
    disp_send_sat_names();
    // Names first, then the addresses, so a client from before the addresses
    // existed reads the names it expects and stops.  See CMD_SAT_NAMES.
    uint8_t buf[SAT_TABLE_PAYLOAD_LEN] = {};
    for (int i = 0; i < MAX_SATELLITES; i++) {
        if (!_sat[i].active) continue;
        memcpy(buf + i * SAT_NAME_LEN, _sat[i].name, SAT_NAME_LEN);
        uint8_t *ip = buf + SAT_NAMES_PAYLOAD_LEN + i * SAT_IP_LEN;
        ip[0] = (uint8_t)( _sat[i].ip        & 0xFF);   // IPAddress packs
        ip[1] = (uint8_t)((_sat[i].ip >>  8) & 0xFF);   // octets low-first
        ip[2] = (uint8_t)((_sat[i].ip >> 16) & 0xFF);
        ip[3] = (uint8_t)((_sat[i].ip >> 24) & 0xFF);
    }
    uint8_t raw[PKT_BUF_SIZE + 4];
    uint16_t n = build_packet(raw, 0xFE, ++_pair_seq, CMD_SAT_NAMES, buf, sizeof(buf));
    broadcast_to_all(raw, n);
    _ws.binaryAll(raw, (size_t)n);
}

// ---------------------------------------------------------------------------
// Mount outage accounting — how long each mount was UNCONTROLLABLE
// ---------------------------------------------------------------------------
// Every other counter on this rig measures the rig's health: uptimes, reset
// reasons, ESP-NOW reinits, tx failures.  Asked "how long could I not drive
// mount 4", none of them could answer.  The log said a bridge had restarted and
// never how long the mount was deaf, and counting events and multiplying by an
// assumed duration would have produced a confident figure that was invented.
//
// Measured here as the gap in the mount's own traffic — last packet before it
// went quiet to first packet after — which covers every cause at once (bridge
// reboot, ESP-NOW reinit, radio wedge, someone pulling the plug) without the
// hub needing to know which.  The hub is the right place to measure it: it is
// the one node that survives all of them.
//
// Resolution is bounded by the mount's STATUS cadence, not by the method — see
// MOUNT_OUTAGE_MIN_MS in shared/protocol.h.  A three-second dropout is real and
// this will not see it.
static uint32_t _out_gap_start[NUM_MOUNTS] = {};   // last_seen when it went quiet; 0 = present
static uint16_t _out_count[NUM_MOUNTS]     = {};
static uint32_t _out_total_ms[NUM_MOUNTS]  = {};
static uint32_t _out_min_ms[NUM_MOUNTS]    = {};
static uint32_t _out_max_ms[NUM_MOUNTS]    = {};

static void send_mount_outage() {
    uint8_t buf[MOUNT_OUTAGE_PAYLOAD_LEN] = {};
    for (int i = 0; i < NUM_MOUNTS; i++) {
        uint8_t *e = buf + i * MOUNT_OUTAGE_PER_MOUNT;
        uint32_t tot = _out_total_ms[i] / 1000UL;
        uint32_t mn  = _out_min_ms[i]   / 1000UL;
        uint32_t mx  = _out_max_ms[i]   / 1000UL;
        // A mount that is out RIGHT NOW has its running gap included, so the
        // total does not stall at the last recovery while it is still down.
        if (_out_gap_start[i]) {
            uint32_t live = (millis() - _out_gap_start[i]) / 1000UL;
            tot += live;
            if (live > mx) mx = live;
            e[10] |= MOUNT_OUTAGE_FLAG_NOW;
        }
        e[0] = (uint8_t)(_out_count[i] >> 8);  e[1] = (uint8_t)_out_count[i];
        e[2] = (uint8_t)(tot >> 24); e[3] = (uint8_t)(tot >> 16);
        e[4] = (uint8_t)(tot >> 8);  e[5] = (uint8_t)tot;
        if (mn > 0xFFFF) mn = 0xFFFF;
        if (mx > 0xFFFF) mx = 0xFFFF;
        e[6] = (uint8_t)(mn >> 8);   e[7] = (uint8_t)mn;
        e[8] = (uint8_t)(mx >> 8);   e[9] = (uint8_t)mx;
    }
    uint8_t raw[PKT_BUF_SIZE + 4];
    uint16_t n = build_packet(raw, 0xFE, ++_pair_seq, CMD_MOUNT_OUTAGE, buf, sizeof(buf));
    broadcast_to_all(raw, n);
    _ws.binaryAll(raw, (size_t)n);
}

// Called every loop.  Cheap: five compares.
static void mount_outage_poll(uint32_t now) {
    for (int i = 0; i < NUM_MOUNTS; i++) {
        uint32_t seen = _mount_last_seen[i];
        if (!seen) continue;                 // never heard from: not an outage, an absence
        bool present = (now - seen) < MOUNT_PRESENCE_TIMEOUT_MS;
        if (!present) {
            // Record where the gap STARTED, not where we noticed it.  The
            // operator lost control at the last packet, not one timeout later,
            // and reporting the moment of detection would understate every
            // outage by MOUNT_PRESENCE_TIMEOUT_MS.
            if (!_out_gap_start[i]) _out_gap_start[i] = seen;
        } else if (_out_gap_start[i]) {
            uint32_t dur = seen - _out_gap_start[i];   // to the first packet back
            _out_gap_start[i] = 0;
            if (dur >= MOUNT_OUTAGE_MIN_MS) {
                _out_count[i]++;
                _out_total_ms[i] += dur;
                if (dur > _out_max_ms[i]) _out_max_ms[i] = dur;
                if (!_out_min_ms[i] || dur < _out_min_ms[i]) _out_min_ms[i] = dur;
                Serial.printf("[OUTAGE] CAM %d uncontrollable %lu.%lus\n",
                              i + 1, (unsigned long)(dur / 1000),
                              (unsigned long)((dur % 1000) / 100));
                send_hub_event(14, (uint8_t)(i + 1), 0,
                               (uint8_t)((dur / 1000) >> 8), (uint8_t)(dur / 1000));
                send_mount_outage();          // totals moved; tell clients now
            }
        }
    }
}

static void send_mount_route() {
    uint8_t buf[MOUNT_ROUTE_PAYLOAD_LEN];
    for (int i = 0; i < NUM_MOUNTS; i++)
        buf[i] = (_mount_sat[i] < 0) ? 0 : (uint8_t)(_mount_sat[i] + 1);
    uint8_t raw[PKT_BUF_SIZE + 4];
    uint16_t n = build_packet(raw, 0xFE, ++_pair_seq, CMD_MOUNT_ROUTE, buf, sizeof(buf));
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
        send_mount_route();                        // ...and how each is reached
        send_sat_names();                          // ...and what to call it
        send_mount_outage();                       // ...and what it has cost so far
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


// GET_CONFIG is the PC app's keepalive: read-only, ack-tracked, and sent to
// every connected mount every ~3 s for as long as the app is open. Counting it
// as "a client is doing something" meant the 20-minute quiet window never
// opened and the 8 h maintenance restart could not fire AT ALL while the app
// was running — which is exactly when it is needed. The hub that wedged for
// fourteen hours had this backstop and it was disabled by a poll.
//
// Read-only requests are excluded here rather than at the call site so a new
// one cannot quietly re-disable it.
static inline bool cmd_is_client_activity(uint8_t cmd) {
    switch (cmd) {
        case CMD_GET_CONFIG:
        case CMD_GET_STATUS:
        case CMD_GET_SUBJECTS:
        case CMD_PING:
            return false;      // polling, not activity
        default:
            return true;
    }
}

static void forward_to_mounts(const ParsedPacket &pkt) {
    if (cmd_is_client_activity(pkt.cmd)) _last_client_cmd_ms = millis();
    if (handle_pairing_cmd(pkt)) return;   // hub-scoped pairing — handled here, not sent to mounts
#if DEMO_MODE
    if (demo_consume_cmd(pkt.mount_id, pkt.cmd, pkt.payload, pkt.payload_len)) return;
#endif
    uint8_t  raw[PKT_BUF_SIZE + 4];
    uint16_t raw_len = build_packet(raw, pkt.mount_id, pkt.seq, pkt.cmd,
                                    pkt.payload, pkt.payload_len);
    // Route per mount: a mount attached to a satellite is unreachable by radio
    // from here, and one that is not has no satellite to send through.
    if (pkt.mount_id == MOUNT_BROADCAST) {
        broadcast_to_mounts_routed(raw, raw_len, false);
    } else if (pkt.mount_id >= 1 && pkt.mount_id <= NUM_MOUNTS) {
        send_to_mount_routed(pkt.mount_id - 1, raw, raw_len);
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
// payload: CONFIG_REPORT_PAYLOAD_LEN bytes (orientation + speeds + stall
// thresholds + slider tilt).
//
// Sized from the shared constant, not from a literal.  It was 75 and 76 written
// out by hand in three places, so growing the report by two bytes for the rail
// tilt would have silently truncated it here — the display would have shown a
// level rail for a sloped one, and nothing would have looked wrong.
static void disp_config_report(uint8_t mount_id, const uint8_t *payload) {
    uint8_t buf[1 + CONFIG_REPORT_PAYLOAD_LEN];
    buf[0] = mount_id;
    memcpy(buf + 1, payload, CONFIG_REPORT_PAYLOAD_LEN);
    disp_lock();
    disp_uart_send(Serial1, DISP_MSG_CONFIG_REPORT, buf, sizeof(buf));
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

    // Route follows the traffic, in both directions.  This is the only place
    // _mount_sat[] is written, so a mount that returns to the hub's own radio
    // is un-attributed by the same rule that attributed it — which is what the
    // satellite-side-only version never did.
    if (_mount_sat[msg.src_idx] != msg.via_sat) {
        _mount_sat[msg.src_idx] = msg.via_sat;
        if (msg.via_sat < 0)
            Serial.printf("[SAT] mount %d back to local ESP-NOW\n", mount_id);
        else
            Serial.printf("[SAT] mount %d now via satellite %d\n",
                          mount_id, msg.via_sat + 1);
        send_mount_route();   // clients redraw the badge
    }

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
    _mount_flags[msg.src_idx]      = flags;   // FLAG_HAS_SLIDER, for the OSC surface

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
        _mount_slot_occ[msg.src_idx] = slot_occ;
        _mount_slot_at[msg.src_idx]  = slot_at;
        _mount_target[msg.src_idx]   = tgt;
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
        // ...and what to call the satellites, the same three things the network
        // clients get on CMD_GET_MOUNT_TABLE. The display asks for this when it
        // comes up, which is the one moment it has no names at all — without it
        // a screen that booted after the satellites would show "SAT 2" until
        // one of them happened to reconnect.
        disp_send_sat_names();
        return;
    }
    if (type == DISP_MSG_HEALTH && len >= 24) {
        // Display's health record — wrap into a CMD_HEALTH packet (sender
        // sentinel 0xFD) and forward to the PC over Serial only.
        uint8_t buf[PKT_BUF_SIZE + 4];
        uint16_t n = build_packet(buf, 0xFD /*display sentinel*/, ++_usb_diag_seq,
                                  CMD_HEALTH, d, 24);
        serial_write_frame(buf, n);
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

const uint32_t HEARTBEAT_MS = BASE_HEARTBEAT_MS;   // see shared/protocol.h
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
    // To all clients — the last of the three hub-telemetry messages to stop
    // being Serial-only.  Without it the PC app on TCP reports "no HUB_DIAG
    // received - hub firmware may predate the diagnostic", which is both
    // alarming and wrong: the firmware is current, the packet simply never
    // left by the door the client was listening at.
    // broadcast_to_all() writes to Serial itself before fanning out to the TCP
    // clients, so calling serial_write_frame() here as well put every hub event
    // on the wire twice — visible as doubled "mount 1 ONLINE" lines on a
    // serial-connected rig, and pure pressure on a TX buffer that is already
    // dropping frames.
    broadcast_to_all(buf, n);
    _ws.binaryAll(buf, (size_t)n);
}

// ---- Uniform health telemetry (hub node) ----
// One CMD_HEALTH every HEALTH_INTERVAL_MS to EVERY client, plus an
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
    // To all clients, not only Serial.  This carries uptime, heap, loop time
    // and the reset reason — the only view of whether the hub is healthy or
    // has just rebooted.  Sending it over USB alone meant that running the PC
    // app on TCP, which is what stops the host resetting the hub, silently
    // traded away every means of noticing that the hub restarted at all.
    // broadcast_to_all() writes to Serial itself before fanning out to the TCP
    // clients, so calling serial_write_frame() here as well put every hub event
    // on the wire twice — visible as doubled "mount 1 ONLINE" lines on a
    // serial-connected rig, and pure pressure on a TX buffer that is already
    // dropping frames.
    broadcast_to_all(buf, n);
    _ws.binaryAll(buf, (size_t)n);
    _health_last_ms     = millis();
    _health_loop_max_ms = 0;
    _health_last_txfail = _health_fail_live;
    _health_first_sent  = true;
}

// Called from the 500 ms self-check tick.
static void health_check(uint32_t now) {
    uint32_t fail_live = 0;
    for (int i = 0; i < NUM_MOUNTS; i++)
        if (mount_is_active(i, now))
            fail_live += _espnow_fail_cum[i];
    // fail_live sums only the mounts currently alive, so it FALLS when one ages
    // out and rises when it returns — it is not monotonic, and both operands are
    // unsigned.  Written as a bare subtraction it wrapped to ~4 billion the
    // moment a mount went quiet, which is always over any threshold: 427 of 522
    // hub health lines came out flagged, 82%, on a hub whose heap, loop time and
    // txfail were all perfectly healthy.  A warning that is on four times out of
    // five is not a warning.
    bool txfail_jump = (fail_live > _health_last_txfail) &&
                       ((fail_live - _health_last_txfail) >= HEALTH_TXFAIL_JUMP);
    bool anomaly =
        (!_health_first_sent && now > 3000) ||
        (esp_get_free_heap_size() < HEALTH_LOW_HEAP_BYTES) ||
        (_health_loop_max_ms > HEALTH_LOOP_STALL_MS) ||
        txfail_jump;
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
// Payload supplied by the caller.  Most events describe a mount and share the
// layout below, but not all do — an IP and port have no sensible home in fields
// named rssi/state/flags, and pretending otherwise reads worse at both ends.
static void send_hub_event_raw(const uint8_t p[9]) {
    uint8_t buf[PKT_BUF_SIZE + 4];
    uint16_t n = build_packet(buf, 0xFE /*hub sentinel*/, ++_usb_diag_seq,
                              CMD_HUB_EVENT, p, 9);
    // Every client, not only Serial — see send_own_health().  These are the
    // structured notable events (mount online, pairing, wedge ladder,
    // restart imminent); losing them on TCP left the PC app blind to the
    // hub's own account of what it was doing.
    // broadcast_to_all() writes to Serial itself before fanning out to the TCP
    // clients, so calling serial_write_frame() here as well put every hub event
    // on the wire twice — visible as doubled "mount 1 ONLINE" lines on a
    // serial-connected rig, and pure pressure on a TX buffer that is already
    // dropping frames.
    broadcast_to_all(buf, n);
    _ws.binaryAll(buf, (size_t)n);
}
static void send_hub_event(uint8_t kind, uint8_t mount_id, int8_t rssi,
                           uint8_t state, uint8_t flags) {
    uint32_t up = millis() / 1000UL;
    uint8_t p[9] = { kind, mount_id, (uint8_t)rssi, state, flags,
                     (uint8_t)(up >> 24), (uint8_t)(up >> 16),
                     (uint8_t)(up >> 8), (uint8_t)up };
    send_hub_event_raw(p);
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
// THE OSC server for the rig (docs/companion.md).  Companion or QLab reaches
// the hub over the CamMount AP or the WiFi→LAN bridge and drives mounts with
// NO PC involved.  Parses the OSC 1.0 subset we use (messages, #bundle,
// i/f/s/T/F args; kept as both int32 and float, see osc_handle_message).
//
// The PC app used to run a second server on this same port with this same
// address space, and they had drifted: tally and record were only in the PC
// app's, autofocus and zoom only in this one.  Companion talks to the hub, so
// a tally button was a well-formed message arriving at a server that had never
// heard of tally — and an unknown address is dropped without a word, which is
// the worst way for a control surface to fail.  That server is gone and its
// two verbs moved here.  One address space cannot disagree with itself, and
// the hub is the right survivor because it is always on and a laptop is not.
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

// ---------------------------------------------------------------------------
// OSC feedback  —  reply to whoever last commanded us
// ---------------------------------------------------------------------------
// OSC is connectionless, so there is no session to reply on.  We remember the
// source of the last accepted command and send state changes back there: a
// Companion surface that has just driven the rig is by definition reachable,
// and needs no configuration at either end.
//
// Port: the source port by default, which is what "reply to sender" means and
// works for any client using one bound socket.  Some controllers transmit from
// an ephemeral port and listen on a fixed one; set OSC_REPLY_PORT to that
// number if yours does.
#ifndef OSC_REPLY_PORT
#define OSC_REPLY_PORT  0            // 0 = reply to the source port
#endif
static IPAddress _osc_peer_ip;
static uint16_t  _osc_peer_port = 0;

// The peer survives a reboot.
//
// Feedback goes nowhere until a surface has sent us something, because that is
// the only way we learn where to send it.  That is fine while the hub stays up
// — but restart it and the desk keeps displaying whatever it last received,
// with nothing arriving to correct it.  A mount powered off in the meantime
// goes on showing its stored locations as though they were there to recall,
// and the first thing anyone presses fixes the whole surface at once, which is
// exactly the confusing part: it looks like the press did it.
//
// So the address is remembered and restored, and a restored peer queues a full
// send the same way a newly-met one does.  If the desk has moved, the stale
// address costs a few UDP packets into nothing and is corrected the moment the
// real one talks.
static void osc_peer_save() {
    _prefs.begin("osc", false);
    _prefs.putUInt("ip",   (uint32_t)_osc_peer_ip);
    _prefs.putUShort("port", _osc_peer_port);
    _prefs.end();
}

static void osc_peer_load() {
    _prefs.begin("osc", false);       // r/w so a missing namespace is created quietly
    uint32_t ip   = _prefs.getUInt("ip", 0);
    uint16_t port = _prefs.getUShort("port", 0);
    _prefs.end();
    if (!ip || !port) return;         // never had one
    _osc_peer_ip   = IPAddress(ip);
    _osc_peer_port = port;
    Serial.printf("[OSC] feedback -> %s:%d (remembered)\n",
                  _osc_peer_ip.toString().c_str(),
                  OSC_REPLY_PORT ? OSC_REPLY_PORT : port);
}

// Where OSC feedback is being sent, and how much of it — reported through the
// client channel because the hub's USB serial carries the binary packet stream
// and is not humanly readable.  Feedback failing silently is the failure mode
// this exists for: UDP gives no error, so without this the only symptom is
// buttons that never light.
//   [0]=8  [1..4]=peer IP  [5..6]=reply port  [7..8]=messages since last report
static uint16_t _osc_fb_count = 0;

static void osc_report_peer() {
    uint16_t port = OSC_REPLY_PORT ? OSC_REPLY_PORT : _osc_peer_port;
    uint8_t p[9] = { 8, _osc_peer_ip[0], _osc_peer_ip[1], _osc_peer_ip[2],
                     _osc_peer_ip[3], (uint8_t)(port >> 8), (uint8_t)port,
                     (uint8_t)(_osc_fb_count >> 8), (uint8_t)_osc_fb_count };
    send_hub_event_raw(p);
    _osc_fb_count = 0;
}


static int osc_pad4(int n) { return (n + 3) & ~3; }

// One OSC message, one int argument.  Enough for every feedback below, and it
// keeps the encoder small enough to read in one go.
static void osc_send_int(const char *addr, int32_t val) {
    if (!_osc_peer_port) return;                    // nobody has talked to us yet
    uint8_t pkt[96];
    int alen = (int)strlen(addr) + 1;
    int apad = osc_pad4(alen);
    if (apad + 8 > (int)sizeof(pkt)) return;
    memset(pkt, 0, apad);
    memcpy(pkt, addr, alen);
    int o = apad;
    pkt[o++] = ','; pkt[o++] = 'i'; pkt[o++] = 0; pkt[o++] = 0;
    write_be32(pkt + o, (uint32_t)val); o += 4;
    uint16_t port = OSC_REPLY_PORT ? OSC_REPLY_PORT : _osc_peer_port;
    _osc_udp.beginPacket(_osc_peer_ip, port);
    _osc_udp.write(pkt, o);
    _osc_udp.endPacket();
    _osc_fb_count++;
}

// Slot state as one value.  Ordered so a surface may also read it as a ramp:
// nothing stored, stored, on its way, arrived.
#define OSC_SLOT_EMPTY     0
#define OSC_SLOT_OCCUPIED  1
#define OSC_SLOT_MOVING    2
#define OSC_SLOT_AT        3

// Last values sent, so only changes go out.  A surface with fifty buttons does
// not want fifty packets a second, and Companion redraws on receipt.
static uint8_t  _fb_state[NUM_MOUNTS]    = {};
static uint8_t  _fb_active[NUM_MOUNTS]   = {};
static uint8_t  _fb_target[NUM_MOUNTS]   = {};
static uint8_t  _fb_slot[NUM_MOUNTS][NUM_POSITIONS] = {};
static uint8_t  _fb_pt[NUM_MOUNTS]       = {};
static uint8_t  _fb_sl[NUM_MOUNTS]       = {};
static uint8_t  _fb_recording[NUM_MOUNTS] = {};
static uint8_t  _fb_lamode[NUM_MOUNTS]   = {};
static int8_t   _fb_lasubj[NUM_MOUNTS]   = { -1, -1, -1, -1, -1 };
static uint8_t  _fb_calib[NUM_MOUNTS]    = {};
// The last calibration prompt, which the hub forwarded to the display board
// without keeping.  Held here so it can be published, and stamped so it can be
// let go of again: an outcome is worth showing, and worth showing for a while
// rather than until the next calibration hours later.
static uint8_t  _calib_prompt[NUM_MOUNTS]    = {};
static uint32_t _calib_prompt_ms[NUM_MOUNTS] = {};
#define CALIB_PROMPT_HOLD_MS  8000UL
static bool     _fb_valid[NUM_MOUNTS]    = {};

// The camera's OWN account of whether it is rolling, lifted out of
// CMD_CAM_STATUS as it passes through the relay.  Never set from a record
// command: a lit tally has to mean a camera that IS recording, not one that
// was asked to and might have no media in it.  False until the camera says
// otherwise, which is also what it reads as when no camera is paired.
static bool     _cam_recording[NUM_MOUNTS] = {};
static uint32_t _fb_last_full_ms = 0;
#define OSC_FB_FULL_MS  5000UL       // resend everything this often

// Emit whatever has changed for one mount.  force = send the lot, for a
// surface that has just appeared and knows nothing.
static void osc_feedback_mount(int i, bool force) {
    if (!_osc_peer_port) return;
    char a[48];
    uint8_t  st  = _mount_last_state[i];
    uint8_t  tgt = (_mount_target[i] == 0xFF) ? 0 : (uint8_t)(_mount_target[i] + 1);
    uint16_t occ = _mount_slot_occ[i];
    uint16_t at  = _mount_slot_at[i];
    uint8_t  act = mount_is_active(i, millis()) ? 1 : 0;
    uint8_t  pt  = _mount_pt_preset[i];
    uint8_t  sl  = _mount_sl_preset[i];

    // A mount that is not being heard has no slots.  occ/at/target are the last
    // values it sent, and they survive it being powered off — so a dark mount
    // went on lighting stored-location buttons on the desk, and one of them
    // could still show "moving to" for a move that ended when the power did.
    // The operator reads those buttons as "this location is there to recall",
    // which for an offline mount is exactly wrong: pressing it does nothing.
    //
    // Reported as EMPTY rather than held, so /slot/N/state means the same thing
    // whether a mount is off, has no positions stored, or has just been
    // cleared. /active already says 0 alongside it for a surface that wants to
    // tell those apart.
    if (!act) { occ = 0; at = 0; tgt = 0; }
    // The presets go the same way, and for the same reason: a speed is a
    // property of something present.  These are the hub's last-known values and
    // they survive the mount being powered off, so a dark mount went on
    // displaying a live-looking preset.  0 already meant "no rail" on the
    // slider address — "nothing to set the speed of" — which is what this is.
    //
    // It has to be here rather than beside the slider's own rail check further
    // down: pt is sent BEFORE that point, so zeroing it there changed nothing.
    if (!act) { pt = 0; sl = 0; }

    bool all = force || !_fb_valid[i];

    if (all || st != _fb_state[i]) {
        snprintf(a, sizeof(a), "/pts/cam/%d/state", i + 1);  osc_send_int(a, st);
    }
    if (all || act != _fb_active[i]) {
        snprintf(a, sizeof(a), "/pts/cam/%d/active", i + 1); osc_send_int(a, act);
    }
    if (all || tgt != _fb_target[i]) {
        snprintf(a, sizeof(a), "/pts/cam/%d/target", i + 1); osc_send_int(a, tgt);
    }
    if (all || pt != _fb_pt[i]) {
        snprintf(a, sizeof(a), "/pts/cam/%d/speed/pt", i + 1); osc_send_int(a, pt);
    }
    // 0 for a mount with no rail.  Presets are 1-4, so 0 is unambiguous, and a
    // Companion button can hide or grey itself on it rather than displaying a
    // speed for an axis that cannot move.
    if (!mount_has_slider(i)) sl = 0;
    if (all || sl != _fb_sl[i]) {
        snprintf(a, sizeof(a), "/pts/cam/%d/speed/sl", i + 1); osc_send_int(a, sl);
    }
    // One value per slot, not two booleans.  The states are not independent and
    // "moving to" is not in either mask: it is target == this slot, so a surface
    // pairing occupied+at still had to evaluate that comparison itself — the
    // very expression the per-address form exists to avoid.  Collapsing them
    // also halves the traffic and makes the change atomic; the two-boolean form
    // had a window where "occupied" had landed and "at" had not, which showed
    // on a button as the wrong colour.
    for (int sN = 0; sN < NUM_POSITIONS; sN++) {
        uint16_t bit = (uint16_t)(1u << sN);
        uint8_t  ss  = (at & bit)       ? OSC_SLOT_AT
                     : (tgt == sN + 1)  ? OSC_SLOT_MOVING
                     : (occ & bit)      ? OSC_SLOT_OCCUPIED
                                        : OSC_SLOT_EMPTY;
        if (all || ss != _fb_slot[i][sN]) {
            snprintf(a, sizeof(a), "/pts/cam/%d/slot/%d/state", i + 1, sN + 1);
            osc_send_int(a, ss);
            _fb_slot[i][sN] = ss;
        }
    }
    // Tally out.  The camera's own transport report, so a lit lamp on the desk
    // is a camera that is rolling — a record command that never took effect
    // produces nothing here.  This used to come from the PC app, which is not
    // always running; the hub is.
    uint8_t rec = _cam_recording[i] ? 1 : 0;
    if (all || rec != _fb_recording[i]) {
        snprintf(a, sizeof(a), "/pts/cam/%d/recording", i + 1); osc_send_int(a, rec);
    }

    // ── Look-at, so a surface can lay itself out correctly ──────────────────
    // Which mode a mount is in decides what buttons 9 and 10 MEAN: two stored
    // positions, or the ◀/▶ ends of the rail.  Without this a Companion page
    // has to be told by hand and goes wrong the moment the mode is changed
    // from anywhere else.
    uint8_t lamode = (act && (_mount_flags[i] & FLAG_LOOK_AT_MODE)) ? 1 : 0;
    if (all || lamode != _fb_lamode[i]) {
        snprintf(a, sizeof(a), "/pts/cam/%d/lookat/mode", i + 1);
        osc_send_int(a, lamode);
    }

    // Which subject is being tracked, or -1 for none.  The slot addresses say
    // which subjects EXIST; this is the one that is live, and it is what the
    // green border shows on every other client.
    int8_t lasubj = (act && _mount_la_subject[i] <= 7)
                        ? (int8_t)_mount_la_subject[i] : (int8_t)-1;
    if (all || lasubj != _fb_lasubj[i]) {
        snprintf(a, sizeof(a), "/pts/cam/%d/lookat/subject", i + 1);
        osc_send_int(a, lasubj);
    }

    // Calibration prompt — CalibPrompt, or 0 for "nothing in progress".  The
    // terminal values (SOLVED / ERROR) are let go of after a hold, so a button
    // shows the outcome and then returns to idle rather than sitting lit until
    // the next calibration.
    uint8_t calib = _calib_prompt[i];
    if (calib && (millis() - _calib_prompt_ms[i]) > CALIB_PROMPT_HOLD_MS &&
            (calib == CALIB_SOLVED || calib == CALIB_ERROR)) {
        calib = _calib_prompt[i] = 0;
    }
    if (!act) calib = 0;          // an offline mount is not calibrating
    if (all || calib != _fb_calib[i]) {
        snprintf(a, sizeof(a), "/pts/cam/%d/calib/prompt", i + 1);
        osc_send_int(a, calib);
    }

    _fb_state[i] = st; _fb_active[i] = act; _fb_target[i] = tgt;
    _fb_pt[i] = pt; _fb_sl[i] = sl; _fb_recording[i] = rec;
    _fb_lamode[i] = lamode; _fb_lasubj[i] = lasubj; _fb_calib[i] = calib;
    _fb_valid[i] = true;
}

// Called from loop().  Changes go out promptly; everything is resent slowly so
// a surface that joins late, or misses a UDP packet, converges without asking.
static uint32_t _osc_report_ms = 0;
#define OSC_REPORT_MS  30000UL

static void osc_feedback_poll(uint32_t now) {
    if (!_osc_peer_port) return;

    // A full send is five mounts x ~fifteen addresses = 75 UDP packets, and
    // doing them all in one pass measured as a 42 ms 'osc' section on the rig —
    // 84% of the worst loop pass, and the largest single thing in the loop.
    //
    // So at most ONE mount is brought up to date per pass.  Everything that
    // wants a full send — the 5-second resend, /pts/refresh, a peer we have not
    // met — marks mounts invalid and is paced through here, which also means
    // there is one path for it rather than three that each need remembering.
    //
    // Change-driven sends stay immediate on every pass: osc_feedback_mount()
    // compares before it sends, so a rig at rest costs nothing.
    if (now - _fb_last_full_ms >= OSC_FB_FULL_MS) {
        _fb_last_full_ms = now;
        for (int i = 0; i < NUM_MOUNTS; i++) _fb_valid[i] = false;
    }
    for (int i = 0; i < NUM_MOUNTS; i++)
        if (_fb_valid[i]) osc_feedback_mount(i, false);
    for (int i = 0; i < NUM_MOUNTS; i++)
        if (!_fb_valid[i]) { osc_feedback_mount(i, true); break; }

    // Periodic proof of life.  "Sent 340 messages to 169.254.22.30:41234" and
    // "buttons still dark" together say the problem is past the hub — a route,
    // a port, or the receiver — which is not deducible from either end alone.
    if (now - _osc_report_ms >= OSC_REPORT_MS) {
        _osc_report_ms = now;
        if (_osc_fb_count) osc_report_peer();
    }
}


// Read a NUL-terminated, 4-byte-padded OSC string at ofs.  Returns the string
// (guaranteed NUL-terminated within the buffer) or nullptr; *next = following offset.
static const char *osc_str(const uint8_t *d, int len, int ofs, int *next) {
    int end = ofs;
    while (end < len && d[end] != 0) end++;
    if (end >= len) { *next = len; return nullptr; }
    *next = osc_pad4(end + 1);
    return (const char *)(d + ofs);
}

static void osc_note_jog(uint8_t mid);   // TEMPORARY — see below

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
    osc_note_jog(mid);   // TEMPORARY — log on change only
}

// TEMPORARY — name OSC jogs into the PC log.  REMOVE WITH THE ZOOM DIAGNOSTIC.
//
// OSC commands reach the mounts through this hub and never touch the PC app, so
// nothing a surface does appears in comms.log.  That cost a day on the tally and
// then cost it again here: cam5's zoom ran 2900 steps at full preset speed after
// a goto had finished, and the goto's own plan shows it was given a ceiling of
// 109 steps/s — so it could not have been the goto.  A full-deflection OSC zoom
// jog is exactly what it was, and it was invisible.
//
// Logged on CHANGE only.  Jogs re-stream at 20 Hz to feed the mount's dead-man,
// and logging every packet would bury the log in the traffic it exists to
// explain.
static int16_t _osc_jog_logged[NUM_MOUNTS][4] = {};

static void osc_note_jog(uint8_t mid) {
    uint8_t i = (uint8_t)(mid - 1);
    if (i >= NUM_MOUNTS) return;
    if (memcmp(_osc_jog_logged[i], _osc_jog[i], sizeof(_osc_jog[i])) == 0) return;
    memcpy(_osc_jog_logged[i], _osc_jog[i], sizeof(_osc_jog[i]));
    uint16_t mask = 0, neg = 0;
    for (int k = 0; k < 4; k++) {
        if (_osc_jog[i][k]) {
            mask |= (uint16_t)(1u << k);
            if (_osc_jog[i][k] < 0) neg |= (uint16_t)(1u << k);
        }
    }
    // verb 5 = jog; value = negative-direction mask in the high byte, moving
    // axes in the low byte.  Mask 0 is a stop, which is worth a line too: it is
    // how you tell a jog that ended from one still running when the log stops.
    send_osc_cam_event(mid, 5, (uint16_t)((neg << 8) | mask));
}

static void osc_stop_jog(uint8_t mid) {
    uint8_t i = (uint8_t)(mid - 1);
    bool was_active = (_osc_jog_until[i] != 0);
    _osc_jog_until[i] = 0;
    memset(_osc_jog[i], 0, sizeof(_osc_jog[i]));
    osc_send_jog_pkt(mid);              // zeros → controlled deceleration
    if (was_active) Serial.printf("[OSC] CAM %d jog stop\n", mid);
}

static void osc_dispatch(const char *addr, const int32_t *a, const float *af,
                         int argc) {
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

    // Ask for the current picture.  A surface we have never heard from is sent
    // the lot automatically, but a Companion that restarts on the same port is
    // not "new" and would otherwise wait up to OSC_FB_FULL_MS with stale
    // buttons.  Clearing the valid flags reuses that same path rather than
    // sending from here, so the burst is paced by the poll and there is one
    // way this happens, not two.
    if (nt == 2 && strcmp(tok[1], "refresh") == 0) {
        Serial.println("[OSC] refresh — full state requested");
        for (int i = 0; i < NUM_MOUNTS; i++) _fb_valid[i] = false;
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

        // Named directions, for surfaces whose controls are buttons.  /jog
        // itself takes four signed values, which suits a stick or a fader; a
        // Companion button sends one message and wants to say "left".
        //
        // Signs come from the web app's _sendJog(), the reference every other
        // surface already follows:  pan + = right, tilt + = UP (it negates the
        // screen's down-positive Y), slider + = right, zoom + = in.  Read from
        // there rather than assumed, because a sign error here is a mount
        // driving the wrong way on a live rig with nothing to catch it.
        //
        // Absent or non-zero argument means GO, zero means STOP — so a button
        // binds press to 1 and release to 0.  Non-zero is always FULL
        // deflection: buttons send 1, and treating that as a magnitude would
        // creep the mount instead of moving it.  Use /jog for proportional.
        //
        // Each direction owns one axis and leaves the rest alone, so two held
        // buttons give a diagonal and releasing one keeps the other running.
        int axis = -1, sign = 0;
        if (nt == 5) {
            if      (strcmp(tok[4], "left")  == 0) { axis = 0; sign = -1; }
            else if (strcmp(tok[4], "right") == 0) { axis = 0; sign =  1; }
            else if (strcmp(tok[4], "up")    == 0) { axis = 1; sign =  1; }
            else if (strcmp(tok[4], "down")  == 0) { axis = 1; sign = -1; }
        } else if (nt == 6 && strcmp(tok[4], "slide") == 0 && mount_has_slider(idx)) {
            if      (strcmp(tok[5], "left")  == 0) { axis = 2; sign = -1; }
            else if (strcmp(tok[5], "right") == 0) { axis = 2; sign =  1; }
        } else if (nt == 6 && strcmp(tok[4], "zoom") == 0) {
            if      (strcmp(tok[5], "in")    == 0) { axis = 3; sign =  1; }
            else if (strcmp(tok[5], "out")   == 0) { axis = 3; sign = -1; }
        }
        if (axis >= 0) {
            bool go = (argc < 1) || (a[0] != 0);
            _osc_jog[idx][axis] = go ? (int16_t)(sign * 1000) : 0;
            bool moving = false;
            for (int k = 0; k < 4; k++) if (_osc_jog[idx][k]) moving = true;
            if (moving) {
                if (_osc_jog_until[idx] == 0)
                    Serial.printf("[OSC] CAM %d jog start\n", mid);
                _osc_jog_until[idx] = millis() + OSC_JOG_TTL_MS;
                osc_send_jog_pkt((uint8_t)mid);
            } else {
                osc_stop_jog((uint8_t)mid);
            }
            return;
        }

        bool any = false;
        for (int k = 0; k < 4; k++) {
            int32_t v = (k < argc) ? a[k] : 0;
            // Same rule the web app applies before it sends: a mount with no
            // rail gets no slider velocity, whatever the surface asked for.
            if (k == 2 && !mount_has_slider(idx)) v = 0;
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

    } else if (strcmp(verb, "speed") == 0 && nt >= 5) {
        // .../speed/pt|sl        <1-4>   set outright
        // .../speed/pt|sl/up|down        step by one, clamped
        // .../speed/pt|sl/inc            cycle 1-2-3-4-1, as the other surfaces do
        //
        // Stepping needs no argument, so one button can walk the preset without
        // the surface tracking which one is current — and the clamp means a
        // button held at either end is simply inert rather than wrapping round
        // to the opposite speed mid-shot.
        // Nothing to change the speed OF.  Ignored rather than clamped, the
        // same as a rail-less mount below: the command cannot reach the mount,
        // so accepting it would walk the hub's shadow copy while the real
        // preset stayed where it was — and the desk would show that walked
        // number the moment the mount came back, until its next status put it
        // right.  A button pressed at an absent mount should do nothing, and
        // should look like it did nothing.
        if (!mount_is_active(idx, millis())) return;

        uint8_t  grp;
        uint8_t *cur;
        if      (strcmp(tok[4], "pt") == 0) { grp = GROUP_PAN_TILT;    cur = &_mount_pt_preset[idx]; }
        else if (strcmp(tok[4], "sl") == 0) {
            // Nothing to set the speed of.  Ignored rather than clamped, so a
            // button held on a rail-less mount leaves the reported speed at 0
            // instead of walking a number that controls nothing.
            if (!mount_has_slider(idx)) return;
            grp = GROUP_SLIDER_ZOOM; cur = &_mount_sl_preset[idx];
        }
        else return;

        int want = -1;
        if (nt >= 6) {
            int base = (*cur >= 1 && *cur <= 4) ? *cur : 1;   // unknown -> 1
            if      (strcmp(tok[5], "up")   == 0) want = base + 1;
            else if (strcmp(tok[5], "down") == 0) want = base - 1;
            // Wraps, unlike up/down: this is the web app's and the PC app's
            // single-button behaviour, where one control walks the presets
            // round rather than stalling at the top.
            else if (strcmp(tok[5], "inc")  == 0) want = (base % 4) + 1;
            if (want < 1) want = 1;
            if (want > 4) want = 4;
        } else if (argc >= 1) {
            want = (int)a[0];
        }
        if (want >= 1 && want <= 4) {
            *cur = (uint8_t)want;
            uint8_t p[2] = { grp, (uint8_t)want };
            Serial.printf("[OSC] CAM %d speed/%s %d\n", mid, tok[4], want);
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

    } else if (strcmp(verb, "autofocus") == 0) {
        // Instantaneous autofocus on that mount's Blackmagic camera.
        //
        // The bytes are the Blackmagic command itself and the hub does not
        // build them from parts — CMD_CAM_CONTROL is a verbatim pipe all the
        // way to the camera, so the one thing the hub must not do is
        // reinterpret them.  Same sequence the PC app sends, and the same one
        // schoolpost/BlueMagic32 is known to work with on a Pocket 4K:
        //   FF 04 00 00  00 01 01 00  00 00 00 00
        //    |  |         |  | lens category / instantaneous autofocus
        //    |  payload length
        //    destination 255 = the camera on this mount
        static const uint8_t AF[12] = { 0xFF, 0x04, 0x00, 0x00,
                                        0x00, 0x01, 0x01, 0x00,
                                        0x00, 0x00, 0x00, 0x00 };
        Serial.printf("[OSC] CAM %d autofocus\n", mid);
        send_osc_cam_event((uint8_t)mid, 0, 0);
        ui_send_to_mount((uint8_t)mid, CMD_CAM_CONTROL, AF, sizeof(AF));

    } else if (strcmp(verb, "tally") == 0 && argc >= 1) {
        // /pts/cam/N/tally <0..1>, or .../tally/front | .../tally/rear.
        //
        // BRIGHTNESS, not a red/green program state: on a Blackmagic body that
        // comes over SDI and this rig has no SDI path to it.  An int 0/1 from a
        // Companion button means off/full; a cue sending 0.5 gets a half-lit
        // lamp, which is why the parser keeps the float.
        //
        // THE POCKET CINEMA CAMERA 4K IGNORES THIS.  Tested on the rig
        // 2026-08-17: three of these reached a paired, reporting camera and it
        // did not answer or change.  On connect that same camera volunteered
        // categories 0, 1, 3, 4, 9, 10, 12 and a raw 255 — ISO, white balance,
        // shutter angle, battery, transport, lens type, reel, take, operator —
        // and has never once mentioned category 5.  A body that describes
        // itself that exhaustively would report tally if it had it.
        //
        // Kept anyway, because the command is right by the published spec and
        // costs nothing: a studio or URSA body, which is what the tally group
        // is really aimed at, should take it.  On a Pocket the only thing that
        // lights the front indicator is the camera recording, so /record is
        // the verb that actually works there — see docs/companion.md.
        //
        // If it is ever worth chasing on a Pocket: put a card in, record, and
        // watch which category the camera reports as its own indicator comes
        // on.  That finds the parameter by measurement instead of by reading
        // numbers out of a document, which is what failed here.
        uint8_t param = 0;                                  // 0 both, 1 front, 2 rear
        if      (nt >= 5 && strcmp(tok[4], "front") == 0) param = 1;
        else if (nt >= 5 && strcmp(tok[4], "rear")  == 0) param = 2;
        float b = af[0];
        if (b < 0.0f) b = 0.0f;
        if (b > 1.0f) b = 1.0f;
        int16_t fx = (int16_t)lroundf(b * 2048.0f);         // signed 5.11 fixed point
        // Tally category 5, parameter 0/1/2, fixed16 — byte-for-byte what the
        // PC app sends, and like autofocus the hub does not build it from parts
        // beyond the one value: CMD_CAM_CONTROL is a verbatim pipe.
        // Length byte 6 is the body BEFORE padding — four header bytes plus the
        // two of fixed16.  It said 8 (the padded length), which declared the
        // padding as data.  jeppo7745's Magic Button 4k sends 6 for the same
        // shape of command and the camera's own reports are unpadded too.
        uint8_t T[12] = { 0xFF, 0x06, 0x00, 0x00,
                          0x05, param, 0x80, 0x00,
                          (uint8_t)(fx & 0xFF), (uint8_t)((fx >> 8) & 0xFF),
                          0x00, 0x00 };
        Serial.printf("[OSC] CAM %d tally %s = %.2f\n", mid,
                      param == 1 ? "front" : param == 2 ? "rear" : "both", b);
        send_osc_cam_event((uint8_t)mid, (uint8_t)(param + 1), (uint16_t)fx);
        ui_send_to_mount((uint8_t)mid, CMD_CAM_CONTROL, T, sizeof(T));

    } else if (strcmp(verb, "record") == 0) {
        // /pts/cam/N/record 0|1, or .../record/toggle.
        //
        // Toggle reads the CAMERA's reported transport rather than what was
        // last asked for, so a camera stopped at the body toggles to START and
        // not to a STOP that would do nothing.  Before the camera has ever
        // reported — no media, no BLE — that reads false and toggle starts.
        uint8_t mode;
        if (nt >= 5 && strcmp(tok[4], "toggle") == 0) mode = _cam_recording[idx] ? 0 : 2;
        else if (argc >= 1)                           mode = a[0] ? 2 : 0;
        else                                          return;
        // MEDIA CATEGORY 10, parameter 1, int8: 0 preview, 1 play, 2 record.
        //
        // Category 10, not 9.  As 9 this was accepted and did nothing, because
        // 9/1 is not the transport.  Two independent sources agree:
        //
        //   The rig, 2026-08-17.  Recording was started and stopped by hand on
        //   the camera with the log running.  Category 10 parameter 1 went
        //   0 -> 2 at 13:42:53 and 2 -> 0 at 13:43:00 — the exact seven-second
        //   take.  Category 9 parameter 1 never left 0 throughout.
        //
        //   jeppo7745's "Magic Button 4k" BMPCC4k remote, driving the same
        //   camera over the same BLE characteristic:
        //     uint8_t record[] = {255, 9, 0, 0, 10, 1, 1, 0, 0, ...}; // [8] 0/2
        //
        // Five data bytes, from that project's length byte of 9, trailing four
        // left at zero.  The camera reports MORE than it needs to be told —
        // its notification carries 2, 0, 64, 0, ... and that 64 is in our log
        // too — but the working implementation sends zeros and this matches it.
        uint8_t R[16] = { 0xFF, 0x09, 0x00, 0x00,
                          0x0A, 0x01, 0x01, 0x00,
                          mode, 0x00, 0x00, 0x00,
                          0x00, 0x00, 0x00, 0x00 };
        Serial.printf("[OSC] CAM %d record %s\n", mid, mode == 2 ? "START" : "STOP");
        send_osc_cam_event((uint8_t)mid, 4, mode);
        ui_send_to_mount((uint8_t)mid, CMD_CAM_CONTROL, R, sizeof(R));

    } else if (strcmp(verb, "refresh") == 0) {
        _fb_valid[idx] = false;                 // this mount only
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
    // Every argument is kept BOTH ways.  Rounding a float to an int is right
    // for a slot number or a preset, and wrong for tally brightness: 0.5 is a
    // half-lit lamp, and lroundf() would make it full.  So the int form stays
    // exactly as it was for every verb that had it, and the verbs that need
    // the real number read argf instead.
    int32_t args[8]; float argf[8]; int argc = 0;
    if (ofs < len && d[ofs] == ',') {
        const char *tags = osc_str(d, len, ofs, &next);
        if (!tags) return;
        ofs = next;
        for (int ti = 1; tags[ti] != 0 && argc < 8; ti++) {
            char t = tags[ti];
            if (t == 'i' && ofs + 4 <= len) {
                int32_t v = (int32_t)be32(d + ofs); ofs += 4;
                argf[argc] = (float)v; args[argc++] = v;
            } else if (t == 'f' && ofs + 4 <= len) {
                float f = be_float(d + ofs); ofs += 4;
                argf[argc] = f; args[argc++] = (int32_t)lroundf(f);
            } else if (t == 'T') { argf[argc] = 1.0f; args[argc++] = 1;
            } else if (t == 'F') { argf[argc] = 0.0f; args[argc++] = 0;
            } else if (t == 's') {
                osc_str(d, len, ofs, &next); ofs = next;   // skip strings
            } else break;                                   // unsupported tag
        }
    }
    osc_dispatch(addr, args, argf, argc);
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
        // Remember who this came from BEFORE handling it — that is where
        // feedback goes, and a controller that has just commanded the rig is by
        // definition reachable.  Captured per packet so a second surface taking
        // over simply starts receiving.
        IPAddress  from_ip   = _osc_udp.remoteIP();
        uint16_t   from_port = _osc_udp.remotePort();
        int n = _osc_udp.read(rx, sizeof(rx));
        if (n > 0) {
            bool new_peer = (from_ip != _osc_peer_ip || from_port != _osc_peer_port);
            _osc_peer_ip   = from_ip;
            _osc_peer_port = from_port;
            if (new_peer) {
                Serial.printf("[OSC] feedback -> %s:%d\n",
                              from_ip.toString().c_str(),
                              OSC_REPLY_PORT ? OSC_REPLY_PORT : from_port);
                osc_report_peer();          // and to comms.log, which is readable
                osc_peer_save();            // so a restart resumes without being asked
                // A surface we have not seen before knows nothing; queue the
                // lot, paced one mount per pass by osc_feedback_poll().
                for (int i = 0; i < NUM_MOUNTS; i++) _fb_valid[i] = false;
            }
            osc_handle_packet(rx, n);
        }
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
    // The HWCDC default TX ring is a few hundred bytes — about eight of our
    // frames — and serial_write_frame() DROPS anything that will not fit right
    // now rather than waiting.  On the bench rig that discarded 47% of
    // everything the hub said to the PC, acknowledgements included, which reads
    // downstream as a mount ignoring commands.
    //
    // 8 KB is roughly 250 frames of headroom, which covers any burst the relay
    // produces between host polls.  setTxTimeoutMs(0) below still stands: a host
    // that stops draining must never stall the hub, and the drop is the correct
    // behaviour once the buffer really is full — it just should not be full
    // during ordinary traffic.
    Serial.setTxBufferSize(8192);
    Serial.begin(921600);
    // Make USB CDC TX non-blocking.  Default timeout is 100 ms — if the PC app
    // stops draining the port (Python thread stall, GIL contention, etc.) every
    // Serial.write() in broadcast_to_all() blocks for 100 ms.  The 32-deep relay
    // queue then takes 3+ seconds to drain, stalling loop() and making both the
    // hub-display joystick AND the PC joystick unresponsive until the PC is
    // restarted.  With timeout=0 writes return immediately when the buffer is full
    // — STATUS packets may be dropped but loop() never stalls.
    //
    // "Dropped" needed qualifying: with timeout 0, HWCDC::write() still returns
    // so_far, so a full ring yields a PARTIAL frame in the stream rather than no
    // frame at all, and a truncated packet desynchronises the reader until it
    // finds the next magic.  Frames therefore go through serial_write_frame(),
    // which writes all of a packet or none of it.
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
    Serial.println("\n=== PTS Camera Mount Hub (ESP32-S3-ETH) ===");
    _disp_mux = xSemaphoreCreateMutex();
    Serial.printf("Reset reason: %d  (self-restart streak: %lu)\n",
                  (int)_reset_reason, (unsigned long)_self_restart_streak);

    // Display UART — start before anything else so the display gets updates
    // as soon as the hub is ready.
    Serial1.begin(DISP_UART_BAUD, SERIAL_8N1, DISP_RX_PIN, DISP_TX_PIN);

    // --- WiFi Access Point ---
    // Name first: AP_SSID is composed from it, so loading afterwards would
    // raise the AP under the default name until the next reboot.
    hub_name_load();
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
    // Before the first loop pass, so the first osc_feedback_poll() already has
    // somewhere to send.  _fb_valid[] starts false, so the restored peer gets
    // the same paced full send a newly-met one does.
    osc_peer_load();
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
    for (int i = 0; i < NUM_MOUNTS; i++) _mount_sat[i] = -1;   // local until proven otherwise
    _sat_server.begin();
    Serial.printf("Satellite listener on port %d\n", SAT_LINK_PORT);

    // Ethernet, and with it the only route a satellite can reach us by.  The
    // listener above binds every interface, so before this existed it was
    // reachable solely over the SoftAP — which is the one network a satellite
    // is never on.  Address comes from DHCP; nothing here waits for it, because
    // a hub with the cable out must still run the rig over ESP-NOW.
    eth_begin();

    // The wire and the AP must not land on the same subnet.  They no longer do
    // by default - the AP is 192.168.4.1/24 and the wire is link-local - but
    // both are editable, in different files, and the symptom of getting it
    // wrong is a satellite link that fails looking like anything but
    // addressing.  Cheap to check every boot and say so.
    // Tested as SUBNET OVERLAP, not as an equal address.  AP_SUBNET is
    // 255.255.0.0, so the AP claims the whole of 169.254.0.0/16 - every address
    // in that range collides, not just the identical one, and picking a
    // different host number does not help.
    IPAddress eip = ETH.localIP(), apip = WiFi.softAPIP();
    if (eip[0] != 0) {
        bool overlap = true;
        for (int i = 0; i < 4; i++)
            if ((eip[i] & AP_SUBNET[i]) != (apip[i] & AP_SUBNET[i])) { overlap = false; break; }
        if (overlap)
            Serial.printf("[ETH] WARNING: wire %s is inside the AP's subnet (%s/%s) - "
                          "two interfaces now match the same destinations and traffic "
                          "will leave by whichever lwIP checks first.  Put the wire on "
                          "the satellites' LAN, or move the AP off this range.\n",
                          eip.toString().c_str(), apip.toString().c_str(),
                          AP_SUBNET.toString().c_str());
    }

    // Answering to "pts-hub.local" is what makes a DHCP address workable: the
    // satellites resolve the name rather than holding an IP that changes under
    // them.  mdns_init() does not need an interface to be up — the component
    // follows them as they appear — so this is fine while the wire is still
    // negotiating.  Announced on the SoftAP too, which costs nothing and lets
    // the PC app reach a hub whose address nobody has written down.
    if (MDNS.begin(SAT_HUB_HOSTNAME)) {
        Serial.printf("mDNS       : %s\n", SAT_HUB_MDNS_NAME);
    } else {
        Serial.println("[MDNS] failed to start — satellites will resolve "
                       SAT_HUB_MDNS_NAME " forever and never connect");
    }
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

    // Hub location name.  Plain HTTP rather than a new protocol command: this
    // is hub-local commissioning, so it needs no change to the wire protocol
    // or its three mirrors.
    _http_server.on("/hubname", HTTP_GET, [](AsyncWebServerRequest *req) {
        char body[96];
        snprintf(body, sizeof(body), "{\"name\":\"%s\",\"ssid\":\"%s\",\"max\":%d}",
                 _hub_name, AP_SSID, HUB_NAME_MAX);
        req->send(200, "application/json", body);
    });
    _http_server.on("/hubname", HTTP_POST, [](AsyncWebServerRequest *req) {
        if (!req->hasParam("name", true)) { req->send(400, "text/plain", "no name"); return; }
        String want = req->getParam("name", true)->value();
        if (!hub_name_set(want.c_str())) { req->send(400, "text/plain", "bad name"); return; }
        // Reply BEFORE bouncing the AP: raising it drops every WiFi client,
        // including the browser waiting on this response.
        char body[96];
        snprintf(body, sizeof(body), "{\"name\":\"%s\",\"ssid\":\"%s\"}", _hub_name, AP_SSID);
        req->send(200, "application/json", body);
        _hub_name_restart_ms = millis() + 400;
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
        // A satellite-relayed mount is NOT judged by this radio.  It is alive
        // (its STATUS arrives over the wire) and unreachable from here by
        // design — being out of radio range is precisely why it has a
        // satellite — so "alive but our sends fail" describes it permanently.
        // This detector predates satellites and read that as a wedge: one rig
        // logged 61 escalations, every single one naming the satellite-attached
        // mount, and restarted the hub 12 times chasing a fault that did not
        // exist.  Each restart then dropped the satellite link and took that
        // mount down for real.
        if (_mount_sat[i] >= 0) { _tx_wedge_since_ms[i] = 0; continue; }
        bool alive   = mount_is_active(i, now);
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
    // Hub-wide, and checked before the per-mount verdict below.
    //
    // A stalled callback cannot be a mount's fault: it is this radio failing to
    // report its own transmissions, and no mount can be acknowledging while it
    // is happening. So this deliberately skips the tx_proven_ok test that
    // follows, which exists to stop the hub rebooting over one deaf mount.
    uint32_t cb_stall = 0;
    if (_espnow_cb_last_ms && espnow_in_flight() > 0)
        cb_stall = now - _espnow_cb_last_ms;
    if (cb_stall > SELF_CB_STALL_MS) {
        if (!_cb_stall_since_ms) {
            _cb_stall_since_ms = now ? now : 1;
            Serial.printf("[SELF] SEND CALLBACK STALLED — %lu sends outstanding, "
                          "nothing acknowledged for %lu ms. txfail cannot see "
                          "this; escalating on the stall itself.\n",
                          (unsigned long)espnow_in_flight(),
                          (unsigned long)cb_stall);
        }
        _last_wedge_ms = now;
        uint32_t age = now - _cb_stall_since_ms;
        if (age >= worst_age) { worst_age = age; worst_i = 0; }
        // Fall through to the ladder with tx_proven_ok forced false.
        _cb_stall_active = true;
    } else if (_cb_stall_since_ms) {
        Serial.printf("[SELF] send callback recovered after %lu ms\n",
                      (unsigned long)(now - _cb_stall_since_ms));
        _cb_stall_since_ms = 0;
        _cb_stall_active   = false;
        _restart_block_logged = false;
    }

    if (worst_i < 0) return;

    // Is it us, or is it that mount?  Any OTHER live mount with no run of send
    // failures means frames are leaving this radio and being acknowledged — so
    // the one that is failing has a receive problem of its own, and nothing
    // below can reach it.
    //
    // Every rung is hub-wide: a full ESP-NOW reinit, a WiFi bounce, a reboot.
    // Spending any of them on a mount-side fault drops the mounts that ARE
    // working — including whatever a satellite is relaying — to no purpose.
    // One rig restarted the hub 12 times in 30 minutes chasing two mounts whose
    // own receivers were dead, taking a healthy satellite-attached mount down
    // with it each time.  The PC app already makes exactly this distinction
    // before it escalates; the hub was still escalating blind.
    // ...unless the callback itself has stalled, in which case no mount can be
    // acknowledging and "another mount is fine" would be reading stale state.
    bool tx_proven_ok = false;
    for (int k = 0; k < NUM_MOUNTS && !_cb_stall_active; k++) {
        if (k == worst_i) continue;
        bool k_alive = mount_is_active(k, now);
        if (k_alive && _espnow_fail_run[k] == 0) { tx_proven_ok = true; break; }
    }
    if (tx_proven_ok) {
        static uint32_t last_note = 0;
        if (now - last_note > 60000UL) {
            last_note = now;
            Serial.printf("[SELF] Mount %d unreachable %lu ms, but another mount is "
                          "acknowledging — this radio is fine, so the fault is at "
                          "mount %d.  Holding off; it needs a power cycle or a "
                          "satellite in range.\n",
                          worst_i + 1, (unsigned long)worst_age, worst_i + 1);
        }
        return;
    }

    uint32_t wsec = worst_age / 1000UL;
    uint8_t  wsec8 = (wsec > 255) ? 255 : (uint8_t)wsec;

    // A callback stall is hub-wide and borrows worst_i = 0 to reach the ladder.
    // Without this the log would name mount 1 for a fault that has nothing to
    // do with mount 1 — and the whole reason this rung exists is that the last
    // outage was misread for fourteen hours.
    char who[48];
    if (_cb_stall_active)
        snprintf(who, sizeof(who), "the send callback (hub-wide)");
    else
        snprintf(who, sizeof(who), "mount %d", worst_i + 1);

    // ...and the EVENT has to carry it too, not just the line above.
    //
    // That line goes to Serial, which on a rigged hub is inside the enclosure
    // and read by nobody; the event is what reaches comms.log and the operator.
    // Sending worst_i + 1 regardless meant a hub-wide callback stall was logged
    // as "TX wedge on mount 1" — the exact misattribution this rung exists to
    // end, printed in the one place anyone would see it. It happened twice on
    // 2026-09-09 and read as a mount fault both times.
    //
    // 0 is "no particular mount": a stalled callback is this radio failing to
    // report its own transmissions, and no mount is implicated.
    uint8_t ev_mount = _cb_stall_active ? 0 : (uint8_t)(worst_i + 1);

    // Stage 1: full ESP-NOW reinit (shared cooldown with the PC-commanded path,
    // so with a PC attached its ~3 s reinit suppresses a duplicate here).
    if (worst_age >= SELF_REINIT_AFTER_MS &&
            (now - _last_reinit_ms) >= SELF_REINIT_COOLDOWN_MS) {
        Serial.printf("[SELF] Wedge on %s for %lu ms — full ESP-NOW reinit\n",
                      who, (unsigned long)worst_age);
        send_hub_event(2, ev_mount, 0, wsec8, _espnow_fail_run[worst_i]);
        hub_espnow_full_reinit();
        return;
    }

    // Stage 1b: the ESP-NOW reinit didn't clear it, so bounce WiFi itself.
    // The wedge lives below ESP-NOW, so stage 1 cannot reach it — this is the
    // rung that was missing, and the reason the ladder used to jump straight to
    // rebooting the hub.  Costs a brief AP dropout; mounts don't notice.
    if (worst_age >= SELF_WIFI_REINIT_AFTER_MS &&
            (now - _last_wifi_reinit_ms) >= SELF_WIFI_REINIT_COOLDOWN_MS) {
        Serial.printf("[SELF] Wedge on %s for %lu ms despite ESP-NOW reinit "
                      "— bouncing WiFi\n", who, (unsigned long)worst_age);
        send_hub_event(2, ev_mount, 0, wsec8, _espnow_fail_run[worst_i]);
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
        Serial.printf("[SELF] Wedge on %s for %lu ms despite reinit — "
                      "restarting hub\n", who, (unsigned long)worst_age);
        send_hub_event(3, ev_mount, 0, wsec8, 0);
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
        bool connected = mount_is_active(i, now);
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
    // MARK(x) charges everything since the previous mark to section x, so the
    // whole pass is accounted for and nothing hides between the sections.
    uint32_t _pass_t0 = micros(), _mark = _pass_t0;
    #define MARK(sec) do { uint32_t n_ = micros(); uint32_t d_ = n_ - _mark; \
                           if (d_ > _sec_max[sec]) _sec_max[sec] = d_; _mark = n_; } while (0)
    esp_task_wdt_reset();

    uint32_t now = millis();

    // One-shot if the wire never comes up.  Silence here would look exactly
    // like a satellite that is switched off, so it is worth a line in the log.
    eth_report_once_if_down(now, 8000);

    // Deferred restart after a rename (see the /hubname POST handler).  A full
    // restart rather than re-raising the AP in place: the SSID is baked into
    // the AP, the ESP-NOW peers and the mounts' view of this hub, so a clean
    // boot is the one path guaranteed to leave all of them consistent.  It is
    // a commissioning action, so a few seconds of outage is the right trade.
    if (_hub_name_restart_ms && (int32_t)(now - _hub_name_restart_ms) >= 0) {
        _hub_name_restart_ms = 0;
        Serial.printf("[HUB] Renamed to \"%s\" — restarting\n", AP_SSID);
        Serial.flush();
        esp_restart();
    }

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
        bool alive = mount_is_active(i, now);
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

    MARK(SEC_TOP);

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

    MARK(SEC_ACCEPT);

    // ---- Satellites ----
    {
        WiFiClient in = _sat_server.accept();
        if (in) {
            bool placed = false;
            for (int i = 0; i < MAX_SATELLITES; i++) {
                if (_sat[i].active && _sat[i].client.connected()) continue;
                if (_sat[i].active) sat_release_mounts(i);
                _sat[i].client = in;
                // NOTE: SO_SNDTIMEO would be pointless here - NetworkClient's
                // send() passes MSG_DONTWAIT so it is never consulted.  The
                // send is bounded in send_to_mount_routed() instead, by using
                // the raw fd.
                sat_env_init(&_sat[i].parser);
                _sat[i].name[0] = '\0';   // until it introduces itself
                _sat[i].ip     = (uint32_t)in.remoteIP();
                _sat[i].active = true;
                placed = true;
                Serial.printf("[SAT] satellite %d connected from %s\n",
                              i + 1, in.remoteIP().toString().c_str());
                break;
            }
            if (!placed) { in.stop(); Serial.println("[SAT] no free slot"); }
        }
        for (int i = 0; i < MAX_SATELLITES; i++) {
            if (!_sat[i].active) continue;
            if (!_sat[i].client.connected()) {
                _sat[i].client.stop();
                _sat[i].active = false;
                _sat[i].name[0] = '\0';
                sat_release_mounts(i);
                Serial.printf("[SAT] satellite %d disconnected\n", i + 1);
                _sat[i].ip = 0;
                send_sat_names();
                continue;
            }
            // Bounded per pass so one busy satellite cannot starve the loop —
            // loopmax is health telemetry and a long iteration reads as a fault.
            int budget = 512;
            while (_sat[i].client.available() && budget-- > 0) {
                int c = _sat[i].client.read();
                if (c < 0) break;
                SatEnv env;
                if (!sat_env_feed(&_sat[i].parser, (uint8_t)c, &env)) continue;

                // Route is learned in ONE place, where the relay queue is
                // drained, so local and satellite arrivals are handled by the
                // same rule.  Attributing here only ever ADDED a satellite
                // route and nothing removed it: a mount that roamed back to
                // the hub's own radio kept the stale attribution, and since
                // _mount_sat[] also decides where commands are SENT, they kept
                // going out via a satellite that had aged the mount out of its
                // peer table 60 s earlier.  STATUS still arrived locally, so
                // the mount looked alive and simply ignored everything.

                // Inject exactly as the ESP-NOW callback would, so a
                // satellite-attached mount is indistinguishable downstream —
                // pairing, conflicts, display, WS rate-limiting and all.
                if (env.frame_len > sizeof(RelayMsg::data)) continue;

                // A satellite introducing itself, not mount traffic.  Checked
                // BEFORE mount_table_find(): the envelope carries the
                // satellite's own MAC, which is not a mount's, so letting it
                // through would hand an unknown address to the pairing rules.
                {
                    PacketParser hp; pkt_parser_init(&hp); ParsedPacket hpk;
                    // Set by anything that is the SATELLITE talking about
                    // itself rather than a mount's traffic passing through.
                    bool consumed = false;
                    for (uint8_t k = 0; k < env.frame_len; k++) {
                        if (!pkt_feed(&hp, env.frame[k], &hpk)) continue;
                        // The satellite's own health, not mount traffic.  It
                        // sends with mount_id 0 because the slot is OUR accept
                        // order and it cannot know it; stamped here so the PC
                        // app can say which satellite it is looking at.  Only
                        // the header changes — the 24-byte record is forwarded
                        // exactly as the satellite built it.
                        if ((hpk.cmd == CMD_HEALTH &&
                             hpk.payload_len >= 1 &&
                             hpk.payload[0] == HEALTH_NODE_SATELLITE) ||
                            hpk.cmd == CMD_SAT_DOWNLINK) {
                            uint8_t  sb[PKT_BUF_SIZE + 4];
                            uint16_t sn = build_packet(sb,
                                                       (uint8_t)(SAT_ADDR_BASE + i + 1),
                                                       hpk.seq, hpk.cmd,
                                                       hpk.payload, hpk.payload_len);
                            broadcast_to_all(sb, sn);
                            consumed = true;
                            break;
                        }
                        if (hpk.cmd == CMD_SAT_HELLO &&
                            hpk.payload_len == SAT_HELLO_PAYLOAD_LEN) {
                            memcpy(_sat[i].name, hpk.payload, SAT_NAME_LEN);
                            _sat[i].name[SAT_NAME_LEN - 1] = '\0';
                            Serial.printf("[SAT] satellite %d is \"%s\"\n",
                                          i + 1, _sat[i].name);
                            send_sat_names();
                            // A satellite is back.  Mounts that fell back to
                            // this hub while it was away cannot know that, so
                            // tell them to look again.  Rate-limited because a
                            // flapping satellite reconnects repeatedly, and a
                            // rescan storm is the thing this must not become.
                            static uint32_t last_rescan_ms = 0;
                            uint32_t nowr = millis();
                            if (!last_rescan_ms || (nowr - last_rescan_ms) > 30000UL) {
                                last_rescan_ms = nowr ? nowr : 1;
                                uint8_t rb[PKT_BUF_SIZE + 4];
                                uint16_t rn = build_packet(rb, MOUNT_BROADCAST,
                                                           ++_pair_seq,
                                                           CMD_RESCAN_BASES, nullptr, 0);
                                // Per mount through the routed path, not a
                                // radio broadcast: the mounts that most need
                                // this are the ones that fell back to THIS hub,
                                // and send_to_mount_routed() reaches each one
                                // wherever it currently is.
                                broadcast_to_mounts_routed(rb, rn, false);
                                Serial.println("[SAT] satellite back — asking mounts to rescan");
                            }
                            consumed = true;
                        }
                        break;
                    }
                    if (consumed) continue;
                }

                RelayMsg msg;
                int8_t idx  = mount_table_find(env.mac);
                msg.len     = env.frame_len;
                msg.rssi    = env.rssi;
                msg.src_idx = (idx >= 0) ? (uint8_t)idx : 0xFF;
                memcpy(msg.src_mac, env.mac, 6);
                memcpy(msg.data, env.frame, env.frame_len);
                msg.via_sat = (int8_t)i;           // arrived over this satellite
                _relay_queued++;
                if (xQueueSend(_relay_queue, &msg, 0) != pdTRUE) _relay_dropped++;
            }
        }
    }

    MARK(SEC_SAT);

    // ---- Mount outage accounting ----
    // Every loop: five compares, and the transition is what carries the number.
    mount_outage_poll(now);
    // Resent slowly as well, so an app that connected mid-session converges,
    // and so a mount that is STILL down keeps a live total rather than a stale
    // one frozen at its last recovery.
    static uint32_t _outage_report_ms = 0;
    if (now - _outage_report_ms >= 60000UL) {
        _outage_report_ms = now;
        send_mount_outage();
    }

    // ---- OSC control (Companion / QLab) → mounts ----
    osc_poll();
    osc_feedback_poll(now);

    MARK(SEC_OSC);

    // ---- TCP clients → mounts ----
    for (int i = 0; i < MAX_CLIENTS; i++) {
        if (!_slots[i].active) continue;
        if (!_slots[i].client.connected()) {
            _slots[i].client.stop(); _slots[i].active = false;
            Serial.printf("TCP client %d disconnected\n", i + 1);
            continue;
        }
        while (_slots[i].client.available()) {
            // Counted here as well as on the USB path.  This pair answers one
            // question for the PC app - "are my bytes reaching the hub?" - and
            // it is the answer that splits a host-side wedge from a hub-side
            // one.  Counting only USB meant that running the PC app over TCP,
            // which is what stops the host resetting the hub, froze the counter
            // at zero and retired the diagnostic without saying so.
            _usb_rx_bytes++;
            ParsedPacket pkt;
            if (pkt_feed(&_slots[i].parser, _slots[i].client.read(), &pkt)) {
                _usb_rx_pkts++;
                forward_to_mounts(pkt);
            }
        }
    }

    MARK(SEC_TCP);

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
        if (_bcast_dropped) {
            DIAG_PRINTF("[BCAST] %lu of %lu client writes dropped "
                          "(slow reader — frames shed, loop NOT blocked)\n",
                          (unsigned long)_bcast_dropped,
                          (unsigned long)_bcast_sent);
            // ...and say it where it can actually be read.
            //
            // This counter has existed all along and been unreachable twice
            // over: DIAG_PRINTF compiles out unless the build sets DIAG=1, and
            // even then it goes to a serial port nothing is attached to.  So
            // the hub has been shedding frames to the PC, counting them
            // exactly, and filing the count nowhere.
            //
            // It matters more than the comment above it assumed.  "Status is
            // superseded within 200 ms" is true of STATUS and false of an ACK:
            // an acknowledgement is never repeated, so a shed one is
            // indistinguishable from a command the mount ignored.  Mount 1 is
            // currently showing 20% of its commands unanswered while both radios
            // report every send succeeding, which is what that looks like.
            uint8_t e[9] = { 10,
                (uint8_t)(_bcast_dropped >> 24), (uint8_t)(_bcast_dropped >> 16),
                (uint8_t)(_bcast_dropped >>  8), (uint8_t)_bcast_dropped,
                (uint8_t)(_bcast_sent    >> 24), (uint8_t)(_bcast_sent    >> 16),
                (uint8_t)(_bcast_sent    >>  8), (uint8_t)_bcast_sent };
            send_hub_event_raw(e);
        }
        _bcast_dropped = _bcast_sent = 0;
        if (_relay_dropped) {
            uint8_t r[9] = { 11,
                (uint8_t)(_relay_dropped >> 24), (uint8_t)(_relay_dropped >> 16),
                (uint8_t)(_relay_dropped >>  8), (uint8_t)_relay_dropped,
                (uint8_t)(_relay_queued  >> 24), (uint8_t)(_relay_queued  >> 16),
                (uint8_t)(_relay_queued  >>  8), (uint8_t)_relay_queued };
            send_hub_event_raw(r);
        }
        _relay_dropped = _relay_queued = 0;
    }

    MARK(SEC_USB);

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

    MARK(SEC_WS);

    // ---- Display UART receive (config commands from touchscreen) ----
    while (Serial1.available())
        process_disp_byte((uint8_t)Serial1.read());

    MARK(SEC_DISP);

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
                    // Lift the transport mode out of the camera's status, for
                    // the OSC tally-out.  The hub does not otherwise interpret
                    // CMD_CAM_STATUS — it is a verbatim pipe in both directions
                    // — but whether a camera is rolling is the one thing a
                    // lighting desk needs, and the PC app that used to publish
                    // it is not always running.  The hub is.
                    //
                    // BMD framing inside the payload:
                    //   [0] 0xFF  [1] len  [2..3] pad
                    //   [4] category  [5] parameter  [6] type  [7] operation
                    //   [8+] data
                    // Media category 10 parameter 1 is transport; 2 = recording.
                    // This read category 9 and never fired: the camera sat at 0
                    // there through a recording proven on 10/1.  The Magic
                    // Button 4k remote tests the same two bytes on its
                    // notifications — pData[4]==10 && pData[5]==1, pData[8] the
                    // mode — which is exactly this.
                    if (pkt.cmd == CMD_CAM_STATUS
                            && pkt.payload_len >= 9
                            && pkt.payload[4] == 10 && pkt.payload[5] == 1
                            && msg.src_idx < NUM_MOUNTS) {
                        bool rec = (pkt.payload[8] == 2);
                        if (rec != _cam_recording[msg.src_idx]) {
                            _cam_recording[msg.src_idx] = rec;
                            Serial.printf("[CAM] mount %d %s\n", msg.src_idx + 1,
                                          rec ? "RECORDING" : "stopped recording");
                        }
                    }
                    // The bridge's own health carries whether its camera is
                    // connected.  Offset 19 of the 24-byte PayloadHealth —
                    // node_type(1) reset(1) uptime(4) heap(4) minheap(4)
                    // loop(2) txfail(2) rssi(1) then flags.
                    if (pkt.cmd == CMD_HEALTH
                            && pkt.payload_len >= 24
                            && pkt.payload[0] == HEALTH_NODE_BRIDGE
                            && msg.src_idx < NUM_MOUNTS) {
                        bool linked = (pkt.payload[19] & HEALTH_FLAG_BLE_LINK) != 0;
                        if (linked != _cam_linked[msg.src_idx]) {
                            _cam_linked[msg.src_idx] = linked;
                            Serial.printf("[CAM] mount %d camera %s\n",
                                          msg.src_idx + 1,
                                          linked ? "linked" : "not linked");
                        }
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
                        _calib_prompt[msg.src_idx]    = pkt.payload[0];
                        _calib_prompt_ms[msg.src_idx] = millis();
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


    MARK(SEC_RELAY);
    uint32_t _pass = micros() - _pass_t0;
    if (_pass > _loop_max_us) _loop_max_us = _pass;
    _loop_count++;
    if (millis() - _loop_report_ms >= LOOP_REPORT_MS) {
        _loop_report_ms = millis();
        send_loop_report();
    }
    #undef MARK
}
