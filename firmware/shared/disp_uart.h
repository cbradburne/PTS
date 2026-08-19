#pragma once
/*
 * disp_uart.h — UART protocol between hub ESP32 (XIAO) and display ESP32 (Waveshare)
 *
 * Packet format:  [0xDD][type:1][len:1][payload:len][xor_checksum:1]
 *
 * Hub → Display:
 *   DISP_MSG_UPDATE_CAM        (4 bytes: mount_id, state, flags, rssi)
 *   DISP_MSG_SET_DISCONNECTED  (1 byte)
 *   DISP_MSG_UPDATE_PRESET     (3 bytes)
 *   DISP_MSG_UPDATE_CLIENTS    (2 bytes)
 *   DISP_MSG_LIMITS_FOUND     (10 bytes)
 *   DISP_MSG_HOME_COMPLETE     (2 bytes)
 *
 * Display → Hub:
 *   DISP_MSG_SEND_CMD          (variable: mount_id, cmd, plen, data[plen])
 */

#include <stdint.h>

#define DISP_UART_MAGIC  0xDD
#define DISP_UART_BAUD   460800

// Hub → Display
#define DISP_MSG_UPDATE_CAM        0x01
#define DISP_MSG_SET_DISCONNECTED  0x02
#define DISP_MSG_UPDATE_PRESET     0x03
#define DISP_MSG_UPDATE_CLIENTS    0x04
#define DISP_MSG_UPDATE_SLOTS      0x05
#define DISP_MSG_CONFIG_REPORT     0x06   // mount_id(1) + CONFIG_REPORT_PAYLOAD_LEN bytes
#define DISP_MSG_LIMITS_FOUND      0x07   // 10 bytes: mount_id, axis, min(4 BE), max(4 BE)
#define DISP_MSG_HOME_COMPLETE     0x08   //  2 bytes: mount_id, axis
// v2 — look-at / triangulation support
#define DISP_MSG_SUBJECT_LIST      0x09   // 233 bytes: mount_id + 232-byte SUBJECT_LIST payload
#define DISP_MSG_LOOK_AT_STATUS    0x0A   //   2 bytes: mount_id, subject_id (0xFF=none)
#define DISP_MSG_CALIB_PROMPT      0x0B   //   2 bytes: mount_id, CalibPrompt sub_state
#define DISP_MSG_SUBJECT_MASK      0x0C   //   2 bytes: mount_id, valid_mask (bit i = subject slot i occupied)
#define DISP_MSG_LA_MOVE_DIR       0x0D   //   2 bytes: mount_id, direction (0=min/◀, 1=max/▶, 0xFF=stopped)
// v3 — touchscreen pairing (stage 3)
#define DISP_MSG_MOUNT_TABLE       0x0E   //  30 bytes: 5 × MAC(6) — paired-mount table (all-zero = unbound)
#define DISP_MSG_PAIR_CONFLICT     0x0F   //  13 bytes: cam(1) + new_mac(6) + old_mac(6); cam=0 → dismiss

// Display → Hub
#define DISP_MSG_SEND_CMD          0x10   // variable: mount_id, cmd, plen, data[plen]
#define DISP_MSG_SEL_CAM           0x11   // 1 byte: camera index 0-4 (selected joystick target)
// v3 — touchscreen pairing (stage 3)
#define DISP_MSG_PAIR_DECIDE       0x12   // 8 bytes: cam(1) + decision(1: 1=replace, 0=ignore) + new_mac(6)
#define DISP_MSG_PAIR_FORGET       0x13   // 1 byte: cam — clear that slot's binding (a live mount re-pairs
                                          //         itself within ~5 s; Forget is for retired/dead units)
#define DISP_MSG_GET_MOUNT_TABLE   0x14   // 0 bytes: display requests a DISP_MSG_MOUNT_TABLE push
#define DISP_MSG_HEALTH            0x15   // 24 bytes: display's CMD_HEALTH payload (PayloadHealth
                                          //           wire layout) — hub wraps it into a CMD_HEALTH
                                          //           packet (mount_id 0xFD) and forwards to the PC

#define DISP_UART_MAX_PAYLOAD      80

static inline uint8_t disp_uart_checksum(uint8_t type, uint8_t len,
                                         const uint8_t *data) {
    uint8_t x = type ^ len;
    for (uint8_t i = 0; i < len; i++) x ^= data[i];
    return x;
}

// Send one framed packet over any Arduino Stream.
// Safe to call from any task that owns the Serial object.
template<typename S>
static inline void disp_uart_send(S &serial, uint8_t type,
                                   const uint8_t *payload, uint8_t len) {
    uint8_t hdr[3] = { DISP_UART_MAGIC, type, len };
    serial.write(hdr, 3);
    if (len) serial.write(payload, len);
    uint8_t cs = disp_uart_checksum(type, len, payload);
    serial.write(&cs, 1);
}

/*
 * UPDATE_CAM payload layout (20 bytes, all integers big-endian):
 *   [0]      mount_id  uint8_t
 *   [1..4]   pan       int32_t
 *   [5..8]   tilt      int32_t
 *   [9..12]  slider    int32_t
 *   [13..16] zoom      int32_t
 *   [17]     state     uint8_t
 *   [18]     flags     uint8_t
 *   [19]     rssi      int8_t
 *
 * UPDATE_PRESET payload (3 bytes):
 *   [0] mount_id, [1] pt_preset, [2] sz_preset
 *
 * UPDATE_CLIENTS payload (2 bytes):
 *   [0] tcp_count, [1] ws_count
 *
 * SET_DISCONNECTED payload (1 byte):
 *   [0] mount_id
 *
 * UPDATE_SLOTS payload (7 bytes):
 *   [0]     mount_id
 *   [1..2]  slot_occupied   uint16_t big-endian  (bit i = slot i+1 has a saved position)
 *   [3..4]  slot_at         uint16_t big-endian  (bit i = mount is currently at slot i+1)
 *   [5]     target_slot     uint8_t  (0-based slot index of current move target, 0xFF = none)
 *   [6]     state           uint8_t  (MountState — needed to colour the target dot)
 *
 * SEND_CMD payload (3 + plen bytes):
 *   [0] mount_id, [1] cmd (CmdType), [2] plen, [3..] data
 *
 * SEL_CAM payload (1 byte):
 *   [0] camera index 0-4  (hub uses this to route hardware joystick JOG commands)
 *
 * LIMITS_FOUND payload (10 bytes):
 *   [0]    mount_id
 *   [1]    axis  (AXIS_SLIDER=2, AXIS_ZOOM=3)
 *   [2..5] min_steps  int32_t big-endian
 *   [6..9] max_steps  int32_t big-endian
 *
 */
