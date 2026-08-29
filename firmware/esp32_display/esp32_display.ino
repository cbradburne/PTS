/*
 * esp32_display.ino — Display-only firmware for Waveshare ESP32-S3-Touch-LCD-7
 *
 * This board has one job: receive display-update commands from the hub ESP32
 * (XIAO ESP32S3) over UART and render them via LVGL.  No WiFi, no ESP-NOW.
 *
 * UART wiring (3 wires):
 *   Waveshare GPIO12 (TX)  →  XIAO D7 / GPIO44 (RX)
 *   Waveshare GPIO13 (RX)  ←  XIAO D6 / GPIO43 (TX)
 *   Waveshare GND          —  XIAO GND
 *
 * Arduino IDE board settings for THIS board (Waveshare):
 *   Board            : ESP32S3 Dev Module
 *   Flash Size       : 16MB
 *   Partition Scheme : 8M with spiffs  (or whichever fits your build)
 *   PSRAM            : OPI PSRAM  ← required
 *   USB CDC On Boot  : Enabled   (keeps Serial working for debug)
 */

#include "hub_display.h"
#include "../shared/disp_uart.h"
#include "../shared/protocol.h"

// ---- UART to hub ------------------------------------------------------------
// Adjust these pins if they conflict with anything on your specific revision
// of the Waveshare board.  GPIO12 and GPIO13 are not used by the LCD or touch.
#define HUB_RX_PIN  13   // display receives from hub
#define HUB_TX_PIN  12   // display sends to hub

// ---- Forward declarations ---------------------------------------------------
static void send_cmd_to_hub(uint8_t mount_id, CmdType cmd,
                             const uint8_t *payload, uint8_t plen);
static void process_uart_byte(uint8_t b);
static void dispatch_msg(uint8_t type, uint8_t len, const uint8_t *data);


// ---- UART parser state machine ----------------------------------------------
static enum { RX_MAGIC, RX_TYPE, RX_LEN, RX_PAYLOAD, RX_CHECKSUM } _rx_state = RX_MAGIC;
static uint8_t  _rx_type = 0;
static uint8_t  _rx_len  = 0;
static uint8_t  _rx_pos  = 0;
static uint8_t  _rx_buf[DISP_UART_MAX_PAYLOAD];

// ============================================================
//  Setup / loop
// ============================================================

void setup() {
    // USB serial for debug output (ESP32-S3 native USB — no GPIO pins used)
    Serial.begin(115200);

    // UART link to hub
    Serial1.begin(DISP_UART_BAUD, SERIAL_8N1, HUB_RX_PIN, HUB_TX_PIN);

    // Initialise display (starts LVGL task on core 1).
    // The send callback fires when the user issues a command from the UI —
    // we forward it to the hub via UART.
    hub_display_init(send_cmd_to_hub);

    // Ask the hub for the paired-mount table (the hub also pushes it at its
    // own boot, but the display may power up later than the hub).
    disp_send_raw(DISP_MSG_GET_MOUNT_TABLE, nullptr, 0);
}

void loop() {
    // Drain the UART receive buffer; all work happens in dispatch_msg.
    while (Serial1.available()) {
        process_uart_byte((uint8_t)Serial1.read());
    }
    // hub_ui_tick() is a no-op (LVGL runs in its own FreeRTOS task).
    hub_ui_tick();
}

// ============================================================
//  UART → hub
// ============================================================

// Mount command (e.g. JOG, STORE_POS) — wrapped in DISP_MSG_SEND_CMD.
// Called only from LVGL-task event callbacks (single-threaded), so no mutex needed.
static void send_cmd_to_hub(uint8_t mount_id, CmdType cmd,
                             const uint8_t *payload, uint8_t plen) {
    uint8_t buf[3 + plen];
    buf[0] = mount_id;
    buf[1] = (uint8_t)cmd;
    buf[2] = plen;
    if (plen) memcpy(buf + 3, payload, plen);
    disp_uart_send(Serial1, DISP_MSG_SEND_CMD, buf, 3 + plen);
}

// Raw display→hub message (pairing decisions, table requests).  Declared in
// hub_display.h so the UI code can call it; same single-caller-context rule
// as send_cmd_to_hub (LVGL task or setup, never concurrently).
void disp_send_raw(uint8_t type, const uint8_t *payload, uint8_t len) {
    disp_uart_send(Serial1, type, payload, len);
}

// ============================================================
//  UART receive parser
// ============================================================

static void process_uart_byte(uint8_t b) {
    switch (_rx_state) {
        case RX_MAGIC:
            if (b == DISP_UART_MAGIC) _rx_state = RX_TYPE;
            break;
        case RX_TYPE:
            _rx_type  = b;
            _rx_state = RX_LEN;
            break;
        case RX_LEN:
            _rx_len  = b;
            _rx_pos  = 0;
            _rx_state = (b == 0) ? RX_CHECKSUM : RX_PAYLOAD;
            break;
        case RX_PAYLOAD:
            if (_rx_pos < DISP_UART_MAX_PAYLOAD)
                _rx_buf[_rx_pos] = b;
            _rx_pos++;
            if (_rx_pos >= _rx_len) _rx_state = RX_CHECKSUM;
            break;
        case RX_CHECKSUM: {
            uint8_t expected = disp_uart_checksum(_rx_type, _rx_len, _rx_buf);
            if (b == expected) {
                dispatch_msg(_rx_type, _rx_len, _rx_buf);
            }
            _rx_state = RX_MAGIC;
            break;
        }
    }
}

static void dispatch_msg(uint8_t type, uint8_t len, const uint8_t *d) {
    switch (type) {

        case DISP_MSG_UPDATE_CAM:
            if (len < 3) break;
            // Fifth byte is optional: a hub from before the camera flag sends
            // four, and the display then simply never shows a Focus button.
            hub_ui_update_cam(d[0], d[1], d[2],
                              (len >= 4) ? (int8_t)d[3] : 0,
                              (len >= 5) ? d[4] : 0);
            break;

        case DISP_MSG_SET_DISCONNECTED:
            if (len < 1) break;
            hub_ui_set_disconnected(d[0]);
            break;

        case DISP_MSG_UPDATE_PRESET:
            if (len < 3) break;
            hub_ui_update_preset(d[0], d[1], d[2]);
            break;

        case DISP_MSG_UPDATE_CLIENTS:
            if (len < 2) break;
            hub_ui_update_clients(d[0], d[1]);
            break;

        case DISP_MSG_UPDATE_SLOTS:
            if (len < 7) break;
            hub_ui_update_slots(d[0],
                                ((uint16_t)d[1] << 8) | d[2],
                                ((uint16_t)d[3] << 8) | d[4],
                                d[5], d[6]);
            break;

        case DISP_MSG_CONFIG_REPORT:
            // payload: [0] mount_id, [1..75] 75-byte config report (orientation + speeds + stall thresholds)
            if (len < 76) break;
            hub_ui_update_config(d[0], d + 1, len - 1);
            break;

        case DISP_MSG_HOME_COMPLETE:
            // payload: [0] mount_id, [1] axis
            if (len < 2) break;
            hub_ui_notify_home_complete(d[0], d[1]);
            break;

        case DISP_MSG_CALIB_PROMPT:
            // payload: [0] mount_id, [1] CalibPrompt sub_state
            if (len < 2) break;
            hub_ui_notify_calib_prompt(d[0], d[1]);
            break;

        case DISP_MSG_LOOK_AT_STATUS:
            // payload: [0] mount_id, [1] subject_id (0-7 active, 0xFF = none)
            if (len < 2) break;
            hub_ui_notify_look_at_status(d[0], d[1]);
            break;
        case DISP_MSG_SUBJECT_MASK:
            // payload: [0] mount_id, [1] valid_mask (bit i = subject slot i stored)
            if (len < 2) break;
            hub_ui_update_subject_mask(d[0], d[1]);
            break;

        case DISP_MSG_LA_MOVE_DIR:
            // Ignored.  This carried the hub's echo of a look-at command it had
            // just relayed, which asserted motion the hub could not verify — a
            // press lost on the radio still lit an arrow here.  The arrows now
            // come from target_slot in the mount's own STATUS
            // (hub_ui_update_slots), so consuming this as well would let the
            // unverifiable source overwrite the verified one.
            break;

        case DISP_MSG_MOUNT_TABLE:
            // payload: 5 × MAC(6) — paired-mount table (all-zero = unbound)
            if (len < 30) break;
            hub_ui_update_mount_table(d);
            break;

        case DISP_MSG_PAIR_CONFLICT:
            // payload: [0] cam (0 = dismiss), [1..6] new MAC, [7..12] old MAC
            if (len < 13) break;
            hub_ui_notify_pair_conflict(d[0], d + 1, d + 7);
            break;
    }
}
