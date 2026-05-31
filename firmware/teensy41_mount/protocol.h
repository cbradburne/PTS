#pragma once
/*
 * Shared packet protocol for camera mount controller.
 *
 * Packet format:
 *   [0xAA][0x55][LEN:u8][MOUNT_ID:u8][SEQ_HI:u8][SEQ_LO:u8][CMD:u8][PAYLOAD...][CRC_HI:u8][CRC_LO:u8]
 *
 *   LEN  = number of bytes from MOUNT_ID through end of PAYLOAD (does not include CRC).
 *   CRC  = CRC-16/CCITT-FALSE over bytes from LEN byte through end of PAYLOAD.
 *
 *   MOUNT_ID: 0x00 = broadcast, 0x01-0x05 = individual mounts.
 *   SEQ:      rolling u16 sequence number for ACK/NACK matching.
 */

#include <stdint.h>
#include <stdbool.h>
#include <string.h>

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

#define PKT_START_1         0xAA
#define PKT_START_2         0x55
#define MOUNT_BROADCAST     0x00
#define NUM_MOUNTS          5
#define NUM_POSITIONS       10
#define PACKET_MIN_SIZE     9
#define PACKET_MAX_PAYLOAD  256   // raised: CMD_SUBJECT_LIST needs 232 bytes

// CMD_STATE_REPORT payload size (10 slots × 16 bytes + 22 bytes metadata = 182)
#define STATE_REPORT_PAYLOAD_LEN  182
// CMD_SAVE_SPEEDS payload size (4 PT + 4 SL + 1 ZM) × 8 bytes = 72
#define SAVE_SPEEDS_PAYLOAD_LEN    72
// CMD_CONFIG_REPORT payload size: 1 orientation byte + 72 speed bytes + 2 stall thresholds = 75
#define CONFIG_REPORT_PAYLOAD_LEN  75

// v2 look-at subject constants
#define MAX_SUBJECTS            8
#define MAX_SLIDER_MOVES        8
#define SUBJECT_NAME_LEN        16
// Wire layout per subject record: valid(1) + name(16) + x_mm(4f) + y_mm(4f) + z_mm(4f) = 29 bytes
#define SUBJECT_RECORD_LEN      29
#define SUBJECT_LIST_PAYLOAD_LEN  (MAX_SUBJECTS * SUBJECT_RECORD_LEN)   // 232

#define MOUNT_STATE_CONNECTED 1
#define MOUNT_STATE_MOVING    2
#define FLAG_PAN_SPEED_HIGH   0x01

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

typedef enum : uint8_t {
    // PC -> Mount
    CMD_JOG              = 0x01,
    CMD_GOTO             = 0x02,
    CMD_SAVE_POS         = 0x03,   // legacy — use CMD_STORE_POS
    CMD_SET_SPEED_PRESET = 0x04,
    CMD_SET_LIMITS       = 0x05,
    CMD_FIND_LIMITS      = 0x06,
    CMD_SET_ORIENTATION  = 0x07,
    CMD_E_STOP           = 0x08,
    CMD_GET_STATUS       = 0x09,
    CMD_PING             = 0x0A,
    CMD_GET_STATE        = 0x0B,   // request full state dump (slots + presets + limits)
    CMD_STORE_POS        = 0x0C,   // store current position in slot N (1 byte: slot 0-9)
    CMD_CLEAR_POS        = 0x0D,   // clear slot N (1 byte: slot 0-9)
    CMD_SET_ACTIVE_PRESET= 0x0E,   // change active preset (2 bytes: group, preset 1-4)
    CMD_SAVE_SPEEDS      = 0x0F,   // write 9 speed presets to EEPROM (72 bytes)
    CMD_GOTO_SLOT        = 0x10,   // recall stored position by slot index (2 bytes: slot 0-9, pt_preset 1-4)
    CMD_MOVE_REL         = 0x11,   // move relative to current position — same payload as CMD_GOTO (17 bytes)
    CMD_GET_CONFIG       = 0x12,   // request speed presets + orientation from mount (no payload)
    CMD_FIND_HOME        = 0x13,   // home one axis to its min end stop (1 byte: axis)
    CMD_SET_STALL_THRESHOLD = 0x14, // set and persist StallGuard threshold (2 bytes: axis, threshold)

    // PC -> Mount  (v2 — look-at tracking)
    CMD_ADD_SUBJECT_START  = 0x20,  // begin subject calibration: subject_id(1) + name(16) = 17B
    CMD_ADD_SUBJECT_SET_A  = 0x21,  // record point A at slider home (no payload)
    CMD_ADD_SUBJECT_SET_B  = 0x22,  // record point B at slider max (no payload)
    CMD_ADD_SUBJECT_ABORT  = 0x23,  // cancel calibration in progress (no payload)
    CMD_DELETE_SUBJECT     = 0x24,  // delete subject: subject_id(1)
    CMD_SET_REF            = 0x25,  // set pan/tilt session reference: subject_id(1)
    CMD_SET_SLIDER_MOVE    = 0x26,  // store slider move: slot(1)+start_mm(4f)+end_mm(4f)+preset(1) = 10B
    CMD_START_LOOK_AT_MOVE = 0x27,  // start tracking: slider_slot(1)+subject_id(1)+max_pt_deg_s(4f) = 6B
    CMD_SWITCH_SUBJECT     = 0x28,  // switch tracking target mid-move: subject_id(1)
    CMD_GET_SUBJECTS       = 0x29,  // request subject list (no payload)

    // Mount -> PC
    CMD_STATUS           = 0x80,
    CMD_LIMITS_FOUND     = 0x81,
    CMD_ACK              = 0x82,
    CMD_NACK             = 0x83,
    CMD_PONG             = 0x84,
    CMD_STATE_REPORT     = 0x85,   // full state: 10 slots + active presets + limits
    CMD_CONFIG_REPORT    = 0x86,   // config: orientation flags + 9 speed presets (73 bytes)
    CMD_HOME_COMPLETE    = 0x87,   // homing finished (1 byte: axis)

    // Mount -> PC  (v2)
    CMD_SUBJECT_LIST     = 0x90,   // all subjects: 8 × SUBJECT_RECORD_LEN bytes (232B)
    CMD_LOOK_AT_STATUS   = 0x91,   // live telemetry: slider_mm(4f)+pan_deg(4f)+tilt_deg(4f)+subj_id(1)+flags(1) = 14B
    CMD_REF_CONFIRMED    = 0x92,   // reference set echo: pan_deg(4f)+tilt_deg(4f) = 8B
    CMD_CALIB_PROMPT     = 0x93,   // calibration step notification: sub_state(1)
} CmdType;

// ---------------------------------------------------------------------------
// Axis / group identifiers
// ---------------------------------------------------------------------------

typedef enum : uint8_t {
    AXIS_PAN    = 0,
    AXIS_TILT   = 1,
    AXIS_SLIDER = 2,
    AXIS_ZOOM   = 3,
} Axis;

typedef enum : uint8_t {
    GROUP_PAN_TILT    = 0,
    GROUP_SLIDER_ZOOM = 1,   // used for slider presets (4 presets)
    GROUP_ZOOM        = 2,   // zoom has a single independent preset
} AxisGroup;

// ---------------------------------------------------------------------------
// Mount state / flags
// ---------------------------------------------------------------------------

typedef enum : uint8_t {
    STATE_IDLE                = 0,
    STATE_JOGGING             = 1,
    STATE_MOVING_TO_POS       = 2,
    STATE_FINDING_LIMITS      = 3,
    STATE_ERROR               = 4,
    STATE_LOOK_AT_MOVE        = 5,   // v2: slider moving, pan/tilt tracking subject
    STATE_CALIBRATING_SUBJECT = 6,   // v2: 2-point subject calibration in progress
    STATE_LOOK_AT_PRE_AIM     = 7,   // v2: pre-aiming pan/tilt before slider starts
} MountState;

#define FLAG_AT_MIN_LIMIT   0x01
#define FLAG_AT_MAX_LIMIT   0x02
#define FLAG_STALL_ERROR    0x04
#define FLAG_LIMITS_SET     0x08
#define FLAG_HAS_SLIDER     0x10   // mount has a physical slider axis
#define FLAG_REF_SET        0x20   // v2: pan/tilt session reference has been established
#define FLAG_LOOK_AT_ACTIVE 0x40   // v2: look-at move is currently running
#define FLAG_LOOK_AT_MODE   0x80   // v2: slider uses 3D triangulation mode

// ---------------------------------------------------------------------------
// NACK error codes
// ---------------------------------------------------------------------------

typedef enum : uint8_t {
    NACK_BAD_CRC       = 0x01,
    NACK_BAD_LENGTH    = 0x02,
    NACK_UNKNOWN_CMD   = 0x03,
    NACK_INVALID_PARAM = 0x04,
    NACK_BUSY          = 0x05,
    NACK_NO_REF        = 0x06,   // v2: look-at refused — session reference not set
} NackError;

// ---------------------------------------------------------------------------
// Payload structs  (all fields big-endian on wire — use encode/decode helpers)
// ---------------------------------------------------------------------------

typedef struct __attribute__((packed)) {
    int16_t pan;
    int16_t tilt;
    int16_t slider;
    int16_t zoom;
} PayloadJog;           // velocities clamped [-1000, 1000]

typedef struct __attribute__((packed)) {
    int32_t pan;
    int32_t tilt;
    int32_t slider;
    int32_t zoom;
    int8_t  speed_preset;
} PayloadGoto;

typedef struct __attribute__((packed)) {
    uint8_t slot;       // 0-9
} PayloadSavePos;       // legacy alias; CMD_STORE_POS / CMD_CLEAR_POS use same layout

typedef struct __attribute__((packed)) {
    uint8_t  axis_group;
    uint8_t  preset;    // 1-4
    int32_t  max_speed;
    int32_t  accel;
} PayloadSetSpeedPreset;

typedef struct __attribute__((packed)) {
    uint8_t axis;
    int32_t min_steps;
    int32_t max_steps;
} PayloadSetLimits;

typedef struct __attribute__((packed)) {
    uint8_t axis;       // AXIS_SLIDER or AXIS_ZOOM only
} PayloadFindLimits;

typedef struct __attribute__((packed)) {
    uint8_t flags;      // bit0=pan_invert, bit1=slider_invert, bit2=has_slider, bit3=zoom_invert, bit4=lanc_zoom, bit6=look_at_mode
} PayloadSetOrientation;

typedef struct __attribute__((packed)) {
    uint32_t timestamp_ms;
} PayloadPing;

// Mount -> PC

typedef struct __attribute__((packed)) {
    uint8_t state;
    uint8_t flags;
    // Extended fields (added): active preset indices and slot bitmasks
    uint8_t  active_pt_preset;     // 1-4; which PT speed preset is currently active
    uint8_t  active_sl_preset;     // 1-4; which SL speed preset is currently active
    uint16_t slot_occupied_mask;   // bits 0-9: slot N is STORED or AT_POSITION
    uint16_t slot_at_mask;         // bits 0-9: slot N is AT_POSITION
    uint8_t  target_slot;          // slot currently being moved to (0xFF = none)
} PayloadStatus;                   // wire: 25 bytes

typedef struct __attribute__((packed)) {
    uint8_t axis;
    int32_t min_steps;
    int32_t max_steps;
} PayloadLimitsFound;

typedef struct __attribute__((packed)) {
    uint16_t acked_seq;
} PayloadAck;

typedef struct __attribute__((packed)) {
    uint16_t nacked_seq;
    uint8_t  error;
} PayloadNack;

// CMD_STATE_REPORT layout (182 bytes, decoded inline — too large for a stack struct):
//   +000..+159  10 × (int32 pan, int32 tilt, int32 slider, int32 zoom) BE = 160 bytes
//   +160        slot_occupied_mask  uint16 BE  (bits 0-9)
//   +162        slot_at_mask        uint16 BE  (bits 0-9)
//   +164        active_pt_preset    uint8
//   +165        active_sl_preset    uint8
//   +166        slider_min_steps    int32 BE
//   +170        slider_max_steps    int32 BE
//   +174        zoom_min_steps      int32 BE
//   +178        zoom_max_steps      int32 BE

// CMD_SAVE_SPEEDS layout (72 bytes, decoded inline):
//   +00..+31  4 × PT preset: uint32 max_speed, uint32 accel (presets 1-4)
//   +32..+63  4 × SL preset: uint32 max_speed, uint32 accel (presets 1-4)
//   +64..+71  1 × ZM preset: uint32 max_speed, uint32 accel

// ---------------------------------------------------------------------------
// CRC-16/CCITT-FALSE  (poly=0x1021, init=0xFFFF)
// ---------------------------------------------------------------------------

static inline uint16_t crc16(const uint8_t *data, uint16_t len) {
    uint16_t crc = 0xFFFF;
    for (uint16_t i = 0; i < len; i++) {
        crc ^= (uint16_t)data[i] << 8;
        for (uint8_t bit = 0; bit < 8; bit++) {
            if (crc & 0x8000)
                crc = (crc << 1) ^ 0x1021;
            else
                crc <<= 1;
        }
    }
    return crc;
}

// ---------------------------------------------------------------------------
// Big-endian helpers
// ---------------------------------------------------------------------------

static inline uint16_t be16(const uint8_t *p) {
    return ((uint16_t)p[0] << 8) | p[1];
}
static inline uint32_t be32(const uint8_t *p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] <<  8) |  (uint32_t)p[3];
}
static inline int32_t be32s(const uint8_t *p) {
    return (int32_t)be32(p);
}
static inline int16_t be16s(const uint8_t *p) {
    return (int16_t)be16(p);
}
static inline void write_be16(uint8_t *p, uint16_t v) {
    p[0] = (v >> 8) & 0xFF; p[1] = v & 0xFF;
}
static inline void write_be32(uint8_t *p, uint32_t v) {
    p[0] = (v >> 24) & 0xFF; p[1] = (v >> 16) & 0xFF;
    p[2] = (v >>  8) & 0xFF; p[3] =  v & 0xFF;
}
// Float big-endian helpers (IEEE 754, avoids strict-aliasing UB via memcpy)
static inline float be_float(const uint8_t *p) {
    uint32_t u = be32(p);
    float f;
    memcpy(&f, &u, 4);
    return f;
}
static inline void write_be_float(uint8_t *p, float f) {
    uint32_t u;
    memcpy(&u, &f, 4);
    write_be32(p, u);
}

// ---------------------------------------------------------------------------
// Packet builder
// ---------------------------------------------------------------------------

// buf must be at least PACKET_MIN_SIZE + payload_len bytes.
// Returns total bytes written.
static inline uint16_t build_packet(uint8_t *buf, uint8_t mount_id,
                                     uint16_t seq, CmdType cmd,
                                     const uint8_t *payload, uint8_t payload_len) {
    uint8_t length = 1 + 2 + 1 + payload_len;  // mount_id + seq + cmd + payload

    buf[0] = PKT_START_1;
    buf[1] = PKT_START_2;
    buf[2] = length;
    buf[3] = mount_id;
    buf[4] = (seq >> 8) & 0xFF;
    buf[5] =  seq & 0xFF;
    buf[6] = (uint8_t)cmd;
    if (payload && payload_len > 0)
        memcpy(&buf[7], payload, payload_len);

    uint16_t crc = crc16(&buf[2], 1 + length);  // from LEN byte through payload
    buf[7 + payload_len] = (crc >> 8) & 0xFF;
    buf[8 + payload_len] =  crc & 0xFF;

    return 9 + payload_len;
}

// ---------------------------------------------------------------------------
// Packet parser state machine  (call pkt_feed() byte by byte)
// ---------------------------------------------------------------------------

#define PKT_BUF_SIZE (PACKET_MIN_SIZE + PACKET_MAX_PAYLOAD)

typedef struct {
    uint8_t  buf[PKT_BUF_SIZE];
    uint16_t pos;
    bool     synced;
} PacketParser;

typedef struct {
    bool     valid;
    uint8_t  mount_id;
    uint16_t seq;
    CmdType  cmd;
    uint8_t *payload;
    uint8_t  payload_len;
} ParsedPacket;

static inline void pkt_parser_init(PacketParser *p) {
    p->pos    = 0;
    p->synced = false;
}

// Returns true when a complete, valid packet is ready in *out.
// out->payload points into p->buf — copy it before the next call.
static inline bool pkt_feed(PacketParser *p, uint8_t byte, ParsedPacket *out) {
    if (!p->synced) {
        if (p->pos == 0 && byte == PKT_START_1) {
            p->buf[p->pos++] = byte;
        } else if (p->pos == 1 && byte == PKT_START_2) {
            p->buf[p->pos++] = byte;
            p->synced = true;
        } else {
            p->pos = 0;
        }
        return false;
    }

    if (p->pos < PKT_BUF_SIZE)
        p->buf[p->pos++] = byte;

    // Once we have 3 bytes we know the length
    if (p->pos < 3) return false;

    uint8_t length      = p->buf[2];
    uint16_t total_size = 3 + length + 2;

    if (p->pos < total_size) return false;

    // Validate CRC
    uint16_t calc_crc = crc16(&p->buf[2], 1 + length);
    uint16_t recv_crc = be16(&p->buf[3 + length]);

    p->pos    = 0;
    p->synced = false;

    if (calc_crc != recv_crc) return false;
    if (length < 4)           return false;

    out->valid       = true;
    out->mount_id    = p->buf[3];
    out->seq         = be16(&p->buf[4]);
    out->cmd         = (CmdType)p->buf[6];
    out->payload     = &p->buf[7];
    out->payload_len = length - 4;

    return true;
}

// ---------------------------------------------------------------------------
// ACK / NACK helpers for firmware
// ---------------------------------------------------------------------------

static inline uint16_t build_ack(uint8_t *buf, uint8_t mount_id,
                                  uint16_t reply_seq, uint16_t acked_seq) {
    uint8_t payload[2];
    write_be16(payload, acked_seq);
    return build_packet(buf, mount_id, reply_seq, CMD_ACK, payload, 2);
}

static inline uint16_t build_nack(uint8_t *buf, uint8_t mount_id,
                                   uint16_t reply_seq, uint16_t nacked_seq,
                                   NackError error) {
    uint8_t payload[3];
    write_be16(payload, nacked_seq);
    payload[2] = (uint8_t)error;
    return build_packet(buf, mount_id, reply_seq, CMD_NACK, payload, 3);
}

// Build a STATUS packet (25-byte payload).
static inline uint16_t build_status(uint8_t *buf, uint8_t mount_id,
                                     uint16_t seq, const PayloadStatus *s) {
    uint8_t payload[9]; // Reduced from 25 to 9
    payload[0] = s->state;
    payload[1] = s->flags;
    payload[2] = s->active_pt_preset;
    payload[3] = s->active_sl_preset;
    write_be16(payload + 4, s->slot_occupied_mask);
    write_be16(payload + 6, s->slot_at_mask);
    payload[8] = s->target_slot;

    return build_packet(buf, mount_id, seq, CMD_STATUS, payload, 9);
}

static inline uint16_t build_limits_found(uint8_t *buf, uint8_t mount_id,
                                           uint16_t seq, Axis axis,
                                           int32_t min_s, int32_t max_s) {
    uint8_t payload[9];
    payload[0] = (uint8_t)axis;
    write_be32(payload + 1, (uint32_t)min_s);
    write_be32(payload + 5, (uint32_t)max_s);
    return build_packet(buf, mount_id, seq, CMD_LIMITS_FOUND, payload, 9);
}

static inline uint16_t build_pong(uint8_t *buf, uint8_t mount_id,
                                   uint16_t seq, uint32_t timestamp_ms) {
    uint8_t payload[4];
    write_be32(payload, timestamp_ms);
    return build_packet(buf, mount_id, seq, CMD_PONG, payload, 4);
}

// ---------------------------------------------------------------------------
// v2 — Calibration sub-state notifications  (sent in CMD_CALIB_PROMPT)
// ---------------------------------------------------------------------------

typedef enum : uint8_t {
    CALIB_MOVING_TO_A  = 0x01,   // slider is moving to home — wait
    CALIB_WAIT_SET_A   = 0x02,   // at home: aim camera then send CMD_ADD_SUBJECT_SET_A
    CALIB_MOVING_TO_B  = 0x03,   // slider is moving to max — wait
    CALIB_WAIT_SET_B   = 0x04,   // at max: re-aim camera then send CMD_ADD_SUBJECT_SET_B
    CALIB_SOLVED       = 0x05,   // 3D position solved and saved to EEPROM
    CALIB_ERROR        = 0x06,   // calibration failed (parallel rays / bad geometry)
} CalibPrompt;

// ---------------------------------------------------------------------------
// v2 — Payload structs
// ---------------------------------------------------------------------------

// CMD_ADD_SUBJECT_START payload (17 bytes)
typedef struct __attribute__((packed)) {
    uint8_t subject_id;                  // 0–7
    char    name[SUBJECT_NAME_LEN];      // up to 16 bytes, null-padded
} PayloadAddSubjectStart;

// CMD_SET_SLIDER_MOVE payload (10 bytes)
typedef struct __attribute__((packed)) {
    uint8_t slot;          // 0–7
    // start_mm and end_mm stored as BE float (use write_be_float / be_float)
    uint8_t start_mm[4];
    uint8_t end_mm[4];
    uint8_t speed_preset;  // 1–4
} PayloadSetSliderMove;

// CMD_START_LOOK_AT_MOVE payload (6 bytes)
typedef struct __attribute__((packed)) {
    uint8_t slider_slot;       // 0–7
    uint8_t subject_id;        // 0–7
    uint8_t max_pt_deg_s[4];   // BE float — max pan/tilt speed during tracking (deg/s)
} PayloadStartLookAtMove;

// ---------------------------------------------------------------------------
// v2 — Build helpers  (Mount → PC)
// ---------------------------------------------------------------------------

// CMD_LOOK_AT_STATUS  (14 bytes)
static inline uint16_t build_look_at_status(uint8_t *buf, uint8_t mount_id,
                                             uint16_t seq,
                                             float slider_pos_mm, float pan_deg,
                                             float tilt_deg, uint8_t subject_id,
                                             uint8_t flags) {
    uint8_t payload[14];
    write_be_float(payload + 0,  slider_pos_mm);
    write_be_float(payload + 4,  pan_deg);
    write_be_float(payload + 8,  tilt_deg);
    payload[12] = subject_id;
    payload[13] = flags;
    return build_packet(buf, mount_id, seq, CMD_LOOK_AT_STATUS, payload, 14);
}

// CMD_REF_CONFIRMED  (8 bytes)
static inline uint16_t build_ref_confirmed(uint8_t *buf, uint8_t mount_id,
                                            uint16_t seq,
                                            float pan_deg, float tilt_deg) {
    uint8_t payload[8];
    write_be_float(payload + 0, pan_deg);
    write_be_float(payload + 4, tilt_deg);
    return build_packet(buf, mount_id, seq, CMD_REF_CONFIRMED, payload, 8);
}

// CMD_CALIB_PROMPT  (1 byte)
static inline uint16_t build_calib_prompt(uint8_t *buf, uint8_t mount_id,
                                           uint16_t seq, CalibPrompt sub_state) {
    uint8_t b = (uint8_t)sub_state;
    return build_packet(buf, mount_id, seq, CMD_CALIB_PROMPT, &b, 1);
}

// CMD_SUBJECT_LIST  (232 bytes)
// subjects[] must be an array of MAX_SUBJECTS entries; use valid=false for empty slots.
// The caller fills the buffer: valid(1) + name[16] + x_mm(4f) + y_mm(4f) + z_mm(4f) per record.
// This helper writes a pre-encoded 232-byte payload directly.
static inline uint16_t build_subject_list(uint8_t *buf, uint8_t mount_id,
                                           uint16_t seq,
                                           const uint8_t payload[SUBJECT_LIST_PAYLOAD_LEN]) {
    return build_packet(buf, mount_id, seq, CMD_SUBJECT_LIST,
                        payload, SUBJECT_LIST_PAYLOAD_LEN);
}
