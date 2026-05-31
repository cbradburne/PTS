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
#include "../shared/disp_uart.h"
#include "web_app.h"
#include "hub_types.h"   // RelayMsg — must be last so it follows all other includes

// ---------------------------------------------------------------------------
// Configuration — edit before flashing
// ---------------------------------------------------------------------------

#define AP_SSID      "CamMount"
#define AP_PASSWORD  "camctrl123"
#define AP_CHANNEL   1
#define TCP_PORT     7777
#define MAX_CLIENTS  4

static const uint8_t MOUNT_MACS[NUM_MOUNTS][6] = {
    { 0x44, 0x1b, 0xf6, 0x86, 0x1e, 0x90 },   // Mount 1        44:1B:F6:86:1E:90
    { 0x44, 0x1b, 0xf6, 0x86, 0x22, 0xB4 },   // Mount 2        44:1B:F6:86:22:B4
    { 0x3c, 0x0f, 0x02, 0xc0, 0x1d, 0xc8 },   // Mount 3        3C:0F:02:C0:1D:C8   // Home 1C:DB:D4:7B:57:94
    { 0x1c, 0xdb, 0xd4, 0x7b, 0x58, 0x30 },   // Mount 4        1C:DB:D4:7B:58:30
    { 0x3c, 0x0f, 0x02, 0xc0, 0x24, 0x30 },   // Mount 5        3C:0F:02:C0:24:30
};

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
static portMUX_TYPE _disp_mux = portMUX_INITIALIZER_UNLOCKED;

// ---------------------------------------------------------------------------
// Display send helpers
// ---------------------------------------------------------------------------

static void disp_update_cam(uint8_t mount_id, uint8_t state, uint8_t flags, int8_t rssi) {
    uint8_t buf[4] = { mount_id, state, flags, (uint8_t)rssi };
    portENTER_CRITICAL(&_disp_mux);
    disp_uart_send(Serial1, DISP_MSG_UPDATE_CAM, buf, sizeof(buf));
    portEXIT_CRITICAL(&_disp_mux);
}

static void disp_set_disconnected(uint8_t mount_id) {
    portENTER_CRITICAL(&_disp_mux);
    disp_uart_send(Serial1, DISP_MSG_SET_DISCONNECTED, &mount_id, 1);
    portEXIT_CRITICAL(&_disp_mux);
}

static void disp_update_preset(uint8_t mount_id, uint8_t pt, uint8_t sz) {
    uint8_t buf[3] = { mount_id, pt, sz };
    portENTER_CRITICAL(&_disp_mux);
    disp_uart_send(Serial1, DISP_MSG_UPDATE_PRESET, buf, sizeof(buf));
    portEXIT_CRITICAL(&_disp_mux);
}

static void disp_update_clients(uint8_t tcp_count, uint8_t ws_count) {
    uint8_t buf[2] = { tcp_count, ws_count };
    portENTER_CRITICAL(&_disp_mux);
    disp_uart_send(Serial1, DISP_MSG_UPDATE_CLIENTS, buf, sizeof(buf));
    portEXIT_CRITICAL(&_disp_mux);
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
    portENTER_CRITICAL(&_disp_mux);
    disp_uart_send(Serial1, DISP_MSG_UPDATE_SLOTS, buf, sizeof(buf));
    portEXIT_CRITICAL(&_disp_mux);
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

static void refresh_espnow_peer(uint8_t idx) {
    // Delete and re-add the peer to clear any stale internal send state that
    // accumulates while the mount is offline (failed-send state in the ESP-NOW
    // stack causes subsequent sends to silently fail even after the mount reboots).
    esp_now_del_peer(MOUNT_MACS[idx]);
    esp_now_peer_info_t peer = {};
    memcpy(peer.peer_addr, MOUNT_MACS[idx], 6);
    peer.channel = AP_CHANNEL;
    peer.ifidx   = WIFI_IF_AP;
    peer.encrypt = false;
    esp_now_add_peer(&peer);
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

static void on_espnow_sent(const wifi_tx_info_t *info, esp_now_send_status_t status) {
    const uint8_t *mac_addr = info->des_addr;
    for (int i = 0; i < NUM_MOUNTS; i++) {
        if (memcmp(mac_addr, MOUNT_MACS[i], 6) != 0) continue;
        if (status == ESP_NOW_SEND_SUCCESS) {
            _espnow_fails[i] = 0;
        } else {
            if (++_espnow_fails[i] >= ESPNOW_MAX_CONSEC_FAILS) {
                _espnow_fails[i]          = 0;
                _espnow_need_refresh[i]   = true;  // handled safely in loop()
            }
        }
        break;
    }
}

static void process_status_for_display(const RelayMsg &msg, const ParsedPacket &pkt);

#define RELAY_QUEUE_DEPTH 32
static QueueHandle_t _relay_queue;

// WS receive queue — AsyncTCP task enqueues raw bytes; main loop processes them.
// Keeps all AsyncWebSocket state access (send + receive) on the same task and
// prevents the race between binaryAll() (main loop) and on_ws_event (AsyncTCP).
#define WS_RX_QUEUE_DEPTH 16
static QueueHandle_t _ws_rx_queue;

static int8_t    _mount_rssi[NUM_MOUNTS]      = {};
static uint32_t  _mount_last_seen[NUM_MOUNTS] = {};

// Last known look-at move direction per mount (-1=none, 0=min/◀, 1=max/▶).
// Set when any client sends CMD_START_LOOK_AT_MOVE; broadcast to all clients.
static int8_t    _la_dir[NUM_MOUNTS];   // initialised to -1 in setup()
static uint16_t  _la_dir_seq = 0;       // sequence counter for hub-injected CMD_LA_MOVE_DIR packets
#define MOUNT_TIMEOUT_MS  3000

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
static void intercept_la_move_dir(uint8_t mount_id, uint8_t dir);
static void ui_send_to_mount(uint8_t mount_id, CmdType cmd,
                              const uint8_t *payload, uint8_t plen);

// ---------------------------------------------------------------------------

static void ui_send_to_mount(uint8_t mount_id, CmdType cmd,
                              const uint8_t *payload, uint8_t plen) {
    static uint16_t ui_seq = 0;
    uint8_t  raw[PKT_BUF_SIZE + 4];
    uint16_t raw_len = build_packet(raw, mount_id, ++ui_seq, cmd, payload, plen);
    if (mount_id == MOUNT_BROADCAST) {
        for (int i = 0; i < NUM_MOUNTS; i++)
            esp_now_send(MOUNT_MACS[i], raw, raw_len);
    } else if (mount_id >= 1 && mount_id <= NUM_MOUNTS) {
        esp_now_send(MOUNT_MACS[mount_id - 1], raw, raw_len);
    }
}

// ---------------------------------------------------------------------------
// ESP-NOW receive callback
// ---------------------------------------------------------------------------

static void on_espnow_recv(const esp_now_recv_info_t *recv_info,
                           const uint8_t *data, int len) {
    if (len <= 0 || len > (int)sizeof(RelayMsg::data)) return;
    uint8_t src_idx = 0xFF;
    for (int i = 0; i < NUM_MOUNTS; i++) {
        if (memcmp(recv_info->src_addr, MOUNT_MACS[i], 6) == 0) {
            src_idx = (uint8_t)i; break;
        }
    }
    RelayMsg msg;
    msg.len     = (uint16_t)len;
    msg.rssi    = (int8_t)recv_info->rx_ctrl->rssi;
    msg.src_idx = src_idx;
    memcpy(msg.data, data, len);

    xQueueSend(_relay_queue, &msg, 0);
}

// ---------------------------------------------------------------------------
// Broadcast to all clients
// ---------------------------------------------------------------------------

// Sends to USB serial and TCP clients.  WebSocket is handled separately in the
// relay loop with per-mount rate limiting to avoid overflowing the WS send queue.
static void broadcast_to_all(const uint8_t *data, uint16_t len) {
    Serial.write(data, len);
    for (int i = 0; i < MAX_CLIENTS; i++) {
        if (_slots[i].active && _slots[i].client.connected())
            _slots[i].client.write(data, len);
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
    portENTER_CRITICAL(&_disp_mux);
    disp_uart_send(Serial1, DISP_MSG_LA_MOVE_DIR, dbuf, 2);
    portEXIT_CRITICAL(&_disp_mux);
    // TCP / serial clients + WebSocket clients
    uint8_t raw[PKT_BUF_SIZE + 4];
    uint16_t rlen = build_packet(raw, mount_id, ++_la_dir_seq,
                                 CMD_LA_MOVE_DIR, &dir, 1);
    broadcast_to_all(raw, rlen);
    _ws.binaryAll(raw, (size_t)rlen);   // hub-injected — not in relay queue, must send explicitly
}

static void forward_to_mounts(const ParsedPacket &pkt) {
    uint8_t  raw[PKT_BUF_SIZE + 4];
    uint16_t raw_len = build_packet(raw, pkt.mount_id, pkt.seq, pkt.cmd,
                                    pkt.payload, pkt.payload_len);
    if (pkt.mount_id == MOUNT_BROADCAST) {
        for (int i = 0; i < NUM_MOUNTS; i++)
            esp_now_send(MOUNT_MACS[i], raw, raw_len);
    } else if (pkt.mount_id >= 1 && pkt.mount_id <= NUM_MOUNTS) {
        esp_now_send(MOUNT_MACS[pkt.mount_id - 1], raw, raw_len);
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
    portENTER_CRITICAL(&_disp_mux);
    disp_uart_send(Serial1, DISP_MSG_LIMITS_FOUND, buf, 10);
    portEXIT_CRITICAL(&_disp_mux);
}

// Forward CMD_HOME_COMPLETE from a mount to the display board via DISP_UART.
// payload: 1-byte axis.
static void disp_home_complete(uint8_t mount_id, uint8_t axis) {
    uint8_t buf[2] = { mount_id, axis };
    portENTER_CRITICAL(&_disp_mux);
    disp_uart_send(Serial1, DISP_MSG_HOME_COMPLETE, buf, 2);
    portEXIT_CRITICAL(&_disp_mux);
}

// Forward CMD_CONFIG_REPORT from a mount to the display board via DISP_UART.
// payload: 75-byte config report from the mount (1 orientation + 72 speeds + 2 stall thresholds).
static void disp_config_report(uint8_t mount_id, const uint8_t *payload75) {
    uint8_t buf[76];
    buf[0] = mount_id;
    memcpy(buf + 1, payload75, 75);
    portENTER_CRITICAL(&_disp_mux);
    disp_uart_send(Serial1, DISP_MSG_CONFIG_REPORT, buf, 76);
    portEXIT_CRITICAL(&_disp_mux);
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
    portENTER_CRITICAL(&_disp_mux);
    disp_uart_send(Serial1, DISP_MSG_SUBJECT_MASK, buf, 2);
    portEXIT_CRITICAL(&_disp_mux);
}

// Forward CMD_LOOK_AT_STATUS subject_id to the display board via DISP_UART.
// payload: 14-byte LOOK_AT_STATUS; subject_id is at payload[12].
static void disp_look_at_status(uint8_t mount_id, uint8_t subject_id) {
    uint8_t buf[2] = { mount_id, subject_id };
    portENTER_CRITICAL(&_disp_mux);
    disp_uart_send(Serial1, DISP_MSG_LOOK_AT_STATUS, buf, 2);
    portEXIT_CRITICAL(&_disp_mux);
}

// Forward CMD_CALIB_PROMPT sub_state to the display board via DISP_UART.
static void disp_calib_prompt(uint8_t mount_id, uint8_t sub_state) {
    uint8_t buf[2] = { mount_id, sub_state };
    portENTER_CRITICAL(&_disp_mux);
    disp_uart_send(Serial1, DISP_MSG_CALIB_PROMPT, buf, 2);
    portEXIT_CRITICAL(&_disp_mux);
}

static void process_status_for_display(const RelayMsg &msg, const ParsedPacket &pkt) {
    if (pkt.cmd != CMD_STATUS)     return;
    if (pkt.payload_len < 2)       return;   // need at least state + flags
    if (msg.src_idx >= NUM_MOUNTS) return;

    uint8_t mount_id    = msg.src_idx + 1;
    bool    was_offline = (_mount_last_seen[msg.src_idx] == 0);

    _mount_rssi[msg.src_idx]      = msg.rssi;
    _mount_last_seen[msg.src_idx] = millis();

    if (was_offline) {
        refresh_espnow_peer(msg.src_idx);
        ui_send_to_mount(mount_id, CMD_GET_STATE, nullptr, 0);
    }

    // Teensy STATUS payload (10 bytes):
    //   [0] state  [1] flags  [2] pt_preset  [3] sl_preset
    //   [4..5] slot_occupied  [6..7] slot_at  [8] target_slot  [9] active_la_subject
    uint8_t state = pkt.payload[0];
    uint8_t flags = pkt.payload[1];

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
    if (type == DISP_MSG_SEND_CMD && len >= 3) {
        uint8_t mount_id = d[0];
        CmdType cmd      = (CmdType)d[1];
        uint8_t plen     = d[2];
        if (len >= 3 + plen) {
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

const uint32_t HEARTBEAT_MS = 2000;   // 2 s — well within mount's 5 s timeout
uint32_t last_hb = 0;

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

void setup() {
    Serial.begin(921600);
    pkt_parser_init(&_serial_parser);
    memset(_la_dir, -1, sizeof(_la_dir));
    Serial.println("\n=== ESP32 Camera Mount Hub (XIAO) ===");

    // Display UART — start before anything else so the display gets updates
    // as soon as the hub is ready.
    Serial1.begin(DISP_UART_BAUD, SERIAL_8N1, DISP_RX_PIN, DISP_TX_PIN);

    // --- WiFi Access Point ---
    WiFi.mode(WIFI_AP);
    WiFi.softAP(AP_SSID, AP_PASSWORD, AP_CHANNEL);
    esp_wifi_set_ps(WIFI_PS_NONE);
    Serial.printf("AP  SSID : %s\n", AP_SSID);
    Serial.printf("AP  IP   : %s\n", WiFi.softAPIP().toString().c_str());
    Serial.printf("AP  MAC  : %s\n", WiFi.softAPmacAddress().c_str());

    // --- ESP-NOW ---
    if (esp_now_init() != ESP_OK) {
        Serial.println("ERROR: ESP-NOW init failed — halting.");
        while (true) delay(1000);
    }
    esp_now_register_recv_cb(on_espnow_recv);
    esp_now_register_send_cb(on_espnow_sent);
    for (int i = 0; i < NUM_MOUNTS; i++) {
        esp_now_peer_info_t peer = {};
        memcpy(peer.peer_addr, MOUNT_MACS[i], 6);
        peer.channel = AP_CHANNEL;
        peer.ifidx   = WIFI_IF_AP;
        peer.encrypt = false;
        if (esp_now_add_peer(&peer) == ESP_OK)
            Serial.printf("Peer %d registered\n", i + 1);
        else
            Serial.printf("WARNING: failed to add peer %d\n", i + 1);
    }

    // --- Relay queue ---
    _relay_queue   = xQueueCreate(RELAY_QUEUE_DEPTH, sizeof(RelayMsg));
    _ws_rx_queue   = xQueueCreate(WS_RX_QUEUE_DEPTH, sizeof(WsRxMsg));

    // --- TCP server ---
    for (int i = 0; i < MAX_CLIENTS; i++) {
        pkt_parser_init(&_slots[i].parser);
        _slots[i].active = false;
    }
    _tcp_server.begin();
    Serial.printf("TCP listening on port %d\n", TCP_PORT);

    // --- HTTP / WebSocket ---
    _ws.onEvent(on_ws_event);
    _http_server.addHandler(&_ws);
    _http_server.on("/", HTTP_GET, [](AsyncWebServerRequest *req) {
        req->send_P(200, "text/html", WEB_APP_HTML);
    });
    _http_server.begin();
    Serial.println("Ready.");

    esp_wifi_set_max_tx_power(78); 
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
// Loop
// ---------------------------------------------------------------------------

void loop() {

    uint32_t now = millis();

    // ---- ESP-NOW peer refresh (triggered by consecutive send failures) ----
    for (int i = 0; i < NUM_MOUNTS; i++) {
        if (_espnow_need_refresh[i]) {
            _espnow_need_refresh[i] = false;
            Serial.printf("ESP-NOW: refreshing peer %d after %d consecutive send failures\n",
                          i + 1, ESPNOW_MAX_CONSEC_FAILS);
            refresh_espnow_peer(i);
        }
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
        
        for (int i = 0; i < NUM_MOUNTS; i++) {
            // If we have seen this mount before, tell it to send its state
            if (_mount_last_seen[i] > 0) {
                ui_send_to_mount(i + 1, CMD_GET_STATE, nullptr, 0);
            }
        }
    }

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
        ParsedPacket pkt;
        if (pkt_feed(&_serial_parser, (uint8_t)Serial.read(), &pkt))
            forward_to_mounts(pkt);
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
