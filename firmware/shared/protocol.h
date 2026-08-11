#pragma once
/*
 * Shared packet protocol for camera mount controller.
 *
 * ── SINGLE SOURCE OF TRUTH ──────────────────────────────────────────────────
 * This header is the canonical protocol definition.  Two other sources mirror
 * parts of it and are VERIFIED AGAINST IT by tools/check_protocol.py (run
 * automatically by the pre-commit hook in .githooks/):
 *   - pc_app/comms/protocol.py           (Python enums + encoders/decoders)
 *   - firmware/esp32_hub/web_app.h       (JS constants inside the web app)
 * firmware/teensy41_mount/protocol.h is a one-line shim including this file.
 * When you change the protocol: edit HERE first, then update the mirrors —
 * the checker will list exactly what is missing or mismatched.
 * ────────────────────────────────────────────────────────────────────────────
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

// ---------------------------------------------------------------------------
// STATUS target_slot values
// ---------------------------------------------------------------------------
// 0-9 name the position slot a GOTO_SLOT move is heading for.
//
// On a LOOK-AT mount the grid has no position slots 9 and 10 — slots 0-7 are
// the subjects and 8/9 are the guide arrows — so 8 and 9 are free to report a
// running look-at slider move, and mean that ONLY when look_at_mode is set.
// On any other mount 8 and 9 keep their ordinary meaning of position slots 9
// and 10, so readers must check the mode before interpreting them.
//
// This exists so a client can see which arrow is genuinely running from the
// MOUNT's own telemetry.  Previously the only direction signal was
// CMD_LA_MOVE_DIR, which the hub injects when it relays the command — that
// echoes the hub's intent, so a command lost on the radio still lit the arrow
// for a move that never started.
#define TARGET_SLOT_NONE    0xFF
#define TARGET_SLOT_LA_MIN  8     // look-at slider move running toward min (left arrow)
#define TARGET_SLOT_LA_MAX  9     // look-at slider move running toward max (right arrow)

// CMD_STATE_REPORT payload size (10 slots × 16 bytes + 22 bytes metadata = 182)
#define STATE_REPORT_PAYLOAD_LEN  182
// CMD_SAVE_SPEEDS payload size (4 PT + 4 SL + 1 ZM) × 8 bytes = 72
#define SAVE_SPEEDS_PAYLOAD_LEN    72
// CMD_CONFIG_REPORT payload size: 1 orientation byte + 72 speed bytes + 2 stall thresholds = 75
#define CONFIG_REPORT_PAYLOAD_LEN  75

// Pairing management payload sizes (mirror the disp_uart.h DISP_MSG_* messages)
#define MOUNT_TABLE_PAYLOAD_LEN    30   // 5 × MAC(6); an all-zero slot = unbound
#define PAIR_CONFLICT_PAYLOAD_LEN  13   // cam(1) + new_mac(6) + old_mac(6)
#define MOUNT_ROUTE_PAYLOAD_LEN     5   // one byte per cam: 0 = direct, N = via satellite N
// Satellite location names, so a client can say "via Foyer" instead of "via
// SAT 2".  The slot number is an artefact of TCP accept order and means nothing
// to anyone standing in the building; the name is the thing an operator can act
// on.  Kept the same 12 characters + NUL the hub and satellite already agree on
// for the AP SSID (HUB_NAME_MAX), so one name works everywhere it is shown.
#define SAT_NAME_MAX               12
#define SAT_NAME_LEN               13   // SAT_NAME_MAX + NUL
// Mirrors MAX_SATELLITES in the hub sketch, which static_asserts against this.
// It lives here because it sizes a wire payload, and a payload length that only
// one end knows is how a parser starts reading the next packet's header.
#define SAT_SLOTS                   6
// kind(1) + txfail(2) + reinits(2) + rx_stale_s(2) + tx_stale_s(2)
//        + tx_refused(2) + last_tx_err(2)
// The last two were added after the first real capture: txfail stayed frozen
// across a three-minute outage, which means the sends were never reaching the
// send callback at all — esp_now_send() was refusing them synchronously.  The
// count says how often, and the esp_err_t says which refusal.
#define MOUNT_EVENT_PAYLOAD_LEN     13
#define MOUNT_EVENT_ISOLATED        1   // restarted itself: no RX and no TX
#define SAT_HELLO_PAYLOAD_LEN      SAT_NAME_LEN                // satellite → hub
#define SAT_NAMES_PAYLOAD_LEN      (SAT_SLOTS * SAT_NAME_LEN)  // hub → clients
// Longest BMD camera-control command we will relay.  Theirs are a 4-byte header
// plus payload padded to a 4-byte boundary; 40 covers everything in the
// published protocol with room to spare, and bounds the mount's buffer.
#define CAM_CONTROL_MAX_LEN        40
#define PAIR_DECIDE_PAYLOAD_LEN     8   // cam(1) + decision(1) + new_mac(6)

// v2 look-at subject constants
#define MAX_SUBJECTS            8
#define MAX_SLIDER_MOVES        8
#define SUBJECT_NAME_LEN        16
// Wire layout per subject record: valid(1) + name(16) + x_mm(4f) + y_mm(4f) + z_mm(4f) = 29 bytes
#define SUBJECT_RECORD_LEN      29
#define SUBJECT_LIST_PAYLOAD_LEN  (MAX_SUBJECTS * SUBJECT_RECORD_LEN)   // 232

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
    CMD_GET_POSITION     = 0x15,   // request an immediate CMD_POSITION reply (no payload)

    // PC -> Mount  (v2 — look-at tracking)
    CMD_ADD_SUBJECT_START  = 0x20,  // begin subject calibration: subject_id(1) + name(16) = 17B
    CMD_ADD_SUBJECT_SET_A  = 0x21,  // record point A at slider home (no payload)
    CMD_ADD_SUBJECT_SET_B  = 0x22,  // record point B at slider max (no payload)
    CMD_ADD_SUBJECT_ABORT  = 0x23,  // cancel calibration in progress (no payload)
    CMD_DELETE_SUBJECT     = 0x24,  // delete subject: subject_id(1)
    CMD_SET_REF            = 0x25,  // set pan/tilt session reference: subject_id(1)
    CMD_SET_SLIDER_MOVE    = 0x26,  // store slider move: slot(1)+start_mm(4f)+end_mm(4f)+preset(1) = 10B
    CMD_START_LOOK_AT_MOVE = 0x27,  // start tracking: subject_id(1)+direction(1)+speed_preset(1) = 3B
    CMD_SWITCH_SUBJECT     = 0x28,  // switch tracking target mid-move: subject_id(1)
    CMD_GET_SUBJECTS       = 0x29,  // request subject list (no payload)

    // Mount -> PC
    CMD_STATUS           = 0x80,
    CMD_LIMITS_FOUND     = 0x81,
    CMD_ACK              = 0x82,
    CMD_NACK             = 0x83,
    CMD_PONG             = 0x84,
    CMD_STATE_REPORT     = 0x85,   // full state: 10 slots + active presets + limits
    CMD_CONFIG_REPORT    = 0x86,   // config: orientation(1) + speeds(72: 4PT+4SL+1ZM × 8B) + stall(2) = 75 bytes
    CMD_HOME_COMPLETE    = 0x87,   // homing finished (1 byte: axis)

    // Mount -> PC  (v2)
    CMD_SUBJECT_LIST     = 0x90,   // all subjects: 8 × SUBJECT_RECORD_LEN bytes (232B)
    CMD_LOOK_AT_STATUS   = 0x91,   // live telemetry: slider_mm(4f)+pan_deg(4f)+tilt_deg(4f)+subj_id(1)+flags(1) = 14B
    CMD_REF_CONFIRMED    = 0x92,   // reference set echo: pan_deg(4f)+tilt_deg(4f) = 8B
    CMD_CALIB_PROMPT     = 0x93,   // calibration step notification: sub_state(1)

    // Hub-injected (never sent by Teensy — generated by hub ESP32 for all clients)
    CMD_LA_MOVE_DIR      = 0x94,   // look-at move direction: direction(1) — 0=min(◀), 1=max(▶), 0xFF=stopped
    CMD_HUB_DIAG         = 0x95,   // hub→USB-PC only: usb_rx_bytes(u32)+usb_rx_pkts(u32) = 8B.
                                   // Diagnostic for the PC↔hub USB wedge: lets the PC see whether
                                   // its bytes are still reaching the hub (hub-side stall) or not
                                   // (host-side OUT-pipe halt).  Sent ONLY over Serial, never TCP/WS.
    CMD_HUB_REINIT_ESPNOW = 0x96,  // PC→hub: do a full esp_now_deinit()+init()+re-add peers.
                                   // Recovers the hub→mount ESP-NOW send wedge without a power
                                   // cycle.  Consumed by the hub, NOT forwarded to mounts.
    CMD_HUB_RESTART       = 0x97,  // PC→hub: full esp_restart() of the hub.  The escalation when
                                   // the esp_now reinit (0x96) doesn't clear the wedge — replicates
                                   // the power cycle that's the only confirmed cure.  Hub-consumed.
    CMD_HUB_EVENT         = 0x98,  // hub→all clients: notable hub event for the PC log.  9-byte
                                   // payload: kind(1)+mount_id(1)+rssi(1)+state(1)+flags(1)+uptime_s(u32).
                                   // kind 0 = mount came online (real connect; rssi/state/flags real)
                                   // kind 1 = ghost STATUS frame dropped (rssi==0 phantom-cam guard)
                                   // kind 2 = autonomous ESP-NOW reinit (wedge; state=wedge secs,
                                   //          flags=consecutive send-fail run on that mount)
                                   // kind 3 = autonomous hub restart imminent (wedge persisted;
                                   //          state=wedge secs)
                                   // kind 4 = maintenance restart imminent (long uptime + idle;
                                   //          state=uptime hours)
                                   // kind 5 = mount paired, first contact (state/flags = MAC[4]/[5])
                                   // kind 6 = mount binding moved / renumbered (mount=new cam,
                                   //          state=old cam, flags=MAC[5])
                                   // kind 7 = pairing conflict rejected (mount=claimed cam,
                                   //          state/flags = claimant MAC[4]/[5])
                                   // Diagnostic, but sent to ALL clients: running the PC app
                                   // on TCP is what stops the host resetting the hub, and
                                   // Serial-only telemetry made that trade away the ability
                                   // to see the hub restart at all.
    CMD_POSITION          = 0x9A,  // mount → clients: live axis positions, 17-byte payload
                                   // (see PayloadPosition).  Adaptive rate: 5 Hz while any
                                   // axis is in motion, 1 Hz at rest; also sent immediately
                                   // on CMD_GET_POSITION.  Deliberately NOT part of the 50 Hz
                                   // STATUS — position is slow data (see CMD_HEALTH rationale).
    CMD_HEALTH            = 0x99,  // node → PC log: uniform health record, 24-byte payload
                                   // (see PayloadHealth).  Every node emits one every
                                   // HEALTH_INTERVAL_MS (10 s), deferred briefly around jog
                                   // traffic, plus an immediate anomaly-flagged send on: first
                                   // report after boot, low heap, loop stall, or a TX-fail jump.
                                   // Sender identity: packet mount_id 1-5 = that mount (byte[0]
                                   // says bridge vs teensy), 0xFE = hub, 0xFD = hub display.

    // ── Pairing management: hub mount-table access for ALL clients ──────────
    // The hub owns the paired-mount table (5 slots × MAC, in NVS).  These
    // commands give TCP / WebSocket / USB clients the same view / set / clear
    // capability the 7" display already has, so the PC app and web app can
    // manage pairing too — always operating on the hub's stored table, never a
    // local copy.  Hub-consumed (client→hub) or hub-originated (hub→clients);
    // never forwarded to mounts.  Payloads mirror the disp_uart.h DISP_MSG_*
    // pairing messages byte-for-byte, so the hub reuses one code path.
    CMD_GET_MOUNT_TABLE   = 0x9B,  // client→hub, no payload: request a CMD_MOUNT_TABLE push
    CMD_MOUNT_TABLE       = 0x9C,  // hub→clients, 30B: 5 × MAC(6); an all-zero slot = unbound
    CMD_PAIR_CONFLICT     = 0x9D,  // hub→clients, 13B: cam(1)+new_mac(6)+old_mac(6); cam=0 = dismiss
    CMD_PAIR_DECIDE       = 0x9E,  // client→hub, 8B: cam(1)+decision(1: 1=replace/set, 0=ignore)+new_mac(6)
    CMD_PAIR_FORGET       = 0x9F,  // client→hub, 1B: cam — clear (unbind) that slot (live mount re-pairs in ~5 s)
    CMD_MOUNT_ROUTE       = 0xA0,  // hub→clients, 5B: one byte per cam — how the hub reaches it.

    // ── Camera control over the mount's BLE link ────────────────────────────
    // PC app / web app → hub → mount → Blackmagic camera.
    //
    // The payload is the Blackmagic Camera Control command VERBATIM — the same
    // bytes the SDI path carries, documented in the camera's own manual.  The
    // mount does not parse it; it writes it to the camera's incoming-control
    // characteristic and nothing else.
    //
    // Deliberate.  Every future camera function — iris, zoom, white balance,
    // record — is then a new payload composed by the PC app, with no mount
    // firmware change and nothing to keep in sync across three codebases.  The
    // mount is a pipe, and the one thing it must get right is delivering the
    // bytes unaltered.
    //
    // Instantaneous autofocus, for reference:
    //   FF 04 00 00  00 01 01 00  00 00 00 00
    //   |  |  |  |   |  |  |  operation 0 = assign
    //   |  |  |  |   |  |  data type
    //   |  |  |  |   |  parameter 1 = instantaneous autofocus
    //   |  |  |  |   category 0 = lens
    //   |  |  |  reserved
    //   |  |  command id 0 = change configuration
    //   |  payload length
    //   destination 255 = broadcast (the camera on this mount)
    // Satellite → hub, SAT_HELLO_PAYLOAD_LEN: the satellite's location name,
    // sent once per uplink connection.  It travels inside a normal relay
    // envelope, so the hub must check for it BEFORE looking the envelope's MAC
    // up in the mount table — a satellite's own MAC is not a mount's, and an
    // unrecognised MAC otherwise walks straight into the pairing rules.
    CMD_SAT_HELLO         = 0xA3,
    // Hub → clients, SAT_NAMES_PAYLOAD_LEN: SAT_SLOTS × SAT_NAME_LEN, indexed
    // by the same slot number CMD_MOUNT_ROUTE reports.  An empty string means
    // that slot is unoccupied, or is a satellite too old to introduce itself —
    // in both cases a client should fall back to showing the slot number.
    CMD_SAT_NAMES         = 0xA4,
    // Hub → all mounts, no payload: "a satellite just came up — look again".
    //
    // A mount picks its base once, at boot, and never rescans while the base it
    // has still answers.  That is deliberate (a five-minute rescan cost 174 TX
    // wedges a night) but it has one bad case: restart a satellite mid-show and
    // the mounts that fell back to the distant hub STAY there.  Seen on a rig —
    // mount 4 sat on the hub at -77 dBm when the satellite beside it was back
    // and would have given it -45, and only a power cycle moved it.
    //
    // The hub already learns the exact moment a satellite returns, because the
    // satellite introduces itself with CMD_SAT_HELLO on every connect.  This
    // passes that on.  One scan, not a schedule — the thing being avoided is a
    // standing rescan, not a rescan.
    CMD_RESCAN_BASES      = 0xA5,
    // Mount → clients, MOUNT_EVENT_PAYLOAD_LEN: why this mount restarted itself,
    // reported once after it comes back.
    //
    // The isolation restart is the mount's own last resort, and until now it
    // was invisible: while it is isolated it cannot transmit, so nothing
    // reaches comms.log, and the restart that rescues it resets every counter
    // that would have explained it.  A mount went from txfail 21 and a flat
    // -48 dBm to total silence in one step, was gone three minutes, and came
    // back with nothing to say about it.
    //
    // So the numbers are stashed in RTC_NOINIT before the restart and sent
    // afterwards.  It rides the ordinary relay path, which forwards any mount
    // packet to serial and TCP, so no hub change is needed.
    CMD_MOUNT_EVENT       = 0xA6,
    CMD_CAM_CONTROL       = 0xA1,  // client→hub→mount, 1-40B: BMD command, sent as-is
    // Camera → mount → hub → clients: the camera's own status notifications,
    // relayed verbatim in the same framing as CMD_CAM_CONTROL.  Same reasoning
    // in reverse — the mount does not decode them, so a client can learn about
    // a new camera parameter without any firmware changing.
    //
    // This is what makes a UI able to show the camera's REAL settings rather
    // than what it last asked for.  A control that echoes its own commands
    // lies whenever the camera is also being operated by hand.
    CMD_CAM_STATUS        = 0xA2,  // mount→hub→clients, 1-40B: BMD status, as-is


                                   //   0        = direct, on the hub's own ESP-NOW radio
                                   //   1..6     = relayed by that satellite (slot number)
                                   // Kept separate from CMD_MOUNT_TABLE because a route changes
                                   // whenever a mount roams, where a pairing almost never does.
                                   // Pushed on every change and on CMD_GET_MOUNT_TABLE.
                                   // Without it, RSSI is unreadable: it is measured wherever the
                                   // frame arrived, so a mount can read -40 because a satellite
                                   // is next to it, and drop to -85 for no visible reason when
                                   // that satellite dies and it falls back to the hub.
} CmdType;

// CMD_HEALTH node_type values (payload byte [0])
#define HEALTH_NODE_HUB      0
#define HEALTH_NODE_BRIDGE   1   // mount-side ESP32 (AMOLED)
#define HEALTH_NODE_TEENSY   2
#define HEALTH_NODE_DISPLAY  3

// Uniform health cadence / anomaly thresholds (shared by all nodes)
#define HEALTH_INTERVAL_MS        10000UL
#define HEALTH_ANOMALY_GAP_MS      2000UL   // min spacing between anomaly-triggered sends
#define HEALTH_JOG_DEFER_MS         300UL   // hold a send this long after jog traffic
#define HEALTH_LOW_HEAP_BYTES     30720UL   // free heap below this → anomaly
#define HEALTH_LOOP_STALL_MS        500     // worst loop iteration above this → anomaly
#define HEALTH_TXFAIL_JUMP            8     // tx-fail delta since last send → anomaly

// PayloadHealth.flags bits.  Spare bits in a byte that already ships every 10 s
// from every node — no payload growth, no protocol version to think about.
#define HEALTH_FLAG_ANOMALY    0x01   // this send was triggered by a threshold
// bit1/bit2: BLE camera link on a mount bridge.  A mount on a rig cannot have
// its serial read — the USB port is inside the enclosure and the Teensy owns
// the cable — so [BLECAM] output is invisible exactly where the measurement has
// to happen.  These two bits put the state in comms.log with the rest of the
// telemetry, which is where it can be correlated with txfail anyway.
// Set by every current amoled build, so its ABSENCE is the useful signal: that
// mount is running firmware from before camera support and needs reflashing.
// Without it, old firmware and a camera that is merely switched off look
// identical from the PC app, and they need completely different actions.
#define HEALTH_FLAG_BLE_BUILD  0x02   // firmware has camera support at all
#define HEALTH_FLAG_BLE_LINK   0x04   // ...and the camera is currently paired
// Set when a camera write has failed since the last report, and cleared by
// sending it.  A mount on a rig has no readable serial, so without this a write
// that the camera rejects is indistinguishable from one it ignored — which is
// exactly the ambiguity that made a wrong characteristic look like a working
// link doing nothing.
#define HEALTH_FLAG_CAM_WR_ERR 0x08
// Set once the camera's status notifications are actually subscribed.  A link
// can be paired, discovered and writable — autofocus working proves all three —
// while notifications were never enabled, and nothing else distinguishes that
// from a camera that simply has not reported yet.
#define HEALTH_FLAG_CAM_SUBSCR 0x10
// Set once at least one camera notification has actually been RECEIVED.
// Subscribing successfully and receiving anything are different things, and
// without this they are indistinguishable: a CCCD written to the wrong
// descriptor reports success and then silence, which looks exactly like a
// relay that is dropping the packets afterwards.
#define HEALTH_FLAG_CAM_RX     0x20

// This mount has no camera bond at all.  NOT a fault — on a rig with fewer
// cameras than mounts it is the normal state for most of them, so a client
// should stay quiet about it rather than warn.
//
// It is worth a bit because it separates two things that otherwise look
// identical: a mount with no camera, and a mount whose camera is switched off.
// The second is worth saying; the first is just Tuesday.  A mount reporting
// this also never initiates a camera connection, so it cannot take the link
// from whichever mount the camera belongs to.
#define HEALTH_FLAG_CAM_UNPAIRED 0x40

// The mount's camera-status cache is full, so at least one parameter is being
// dropped and whatever it is will never appear in a client.  Measured on a
// Pocket Cinema Camera 4K: it reports 33 distinct parameters, against a cache
// that was 8 and then 32 — both too small, and both failed SILENTLY, which is
// how gain and white balance went missing twice.
//
// This is the last spare bit in the byte, spent on a silent-data-loss condition
// that has already cost three rounds of guessing.  The mount does say it on
// serial, but a mount on a rig has its USB port inside the enclosure — so
// serial is exactly where this cannot be read.
#define HEALTH_FLAG_CAM_CACHE_FULL 0x80

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
    // Extended fields: active preset indices, slot bitmasks, active look-at subject
    uint8_t  active_pt_preset;     // 1-4; which PT speed preset is currently active
    uint8_t  active_sl_preset;     // 1-4; which SL speed preset is currently active
    uint16_t slot_occupied_mask;   // bits 0-9: slot N is STORED or AT_POSITION
    uint16_t slot_at_mask;         // bits 0-9: slot N is AT_POSITION
    uint8_t  target_slot;          // slot being moved to; see TARGET_SLOT_* below
    uint8_t  active_la_subject;    // active look-at subject (0-7, 0xFF = none)
} PayloadStatus;                   // wire: 10 bytes

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

// CMD_HEALTH payload (24 bytes) — uniform across all node types.
typedef struct __attribute__((packed)) {
    uint8_t  node_type;       // HEALTH_NODE_*
    uint8_t  reset_reason;    // esp_reset_reason() / Teensy SRC_SRSR low byte
    uint32_t uptime_s;
    uint32_t free_heap;       // bytes free now
    uint32_t min_free_heap;   // lowest ever seen this boot (leak/fragmentation trend)
    uint16_t loop_max_ms;     // worst loop/task iteration since the last report
    uint16_t tx_fail;         // cumulative link send failures (wraps; deltas matter)
    int8_t   rssi;            // last link RSSI where meaningful, else 0
    uint8_t  flags;           // HEALTH_FLAG_* — bit0 anomaly, bit1/2 BLE camera
    uint32_t node_u32;        // node-specific: hub=ghost_rx_drops, bridge=reinit count
} PayloadHealth;              // wire: 24 bytes, all multi-byte fields big-endian
                              // (build_health() is with the other builders below)

// CMD_POSITION payload (17 bytes) — live axis positions in physical units.
typedef struct __attribute__((packed)) {
    uint8_t pan_deg[4];       // BE float — degrees
    uint8_t tilt_deg[4];      // BE float — degrees
    uint8_t slider_mm[4];     // BE float — millimetres (0 when no slider)
    int32_t zoom_steps;       // BE int32 — raw steps (zoom has no physical unit;
                              //            meaningless for LANC zoom)
    uint8_t moving_mask;      // bit per axis (0=pan 1=tilt 2=slider 3=zoom):
                              //   stepper still in motion — lets a client (or a
                              //   future VISCA gateway) detect true arrival
} PayloadPosition;            // wire: 17 bytes

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

// Build a STATUS packet (10-byte payload).
static inline uint16_t build_status(uint8_t *buf, uint8_t mount_id,
                                     uint16_t seq, const PayloadStatus *s) {
    uint8_t payload[10];
    payload[0] = s->state;
    payload[1] = s->flags;
    payload[2] = s->active_pt_preset;
    payload[3] = s->active_sl_preset;
    write_be16(payload + 4, s->slot_occupied_mask);
    write_be16(payload + 6, s->slot_at_mask);
    payload[8] = s->target_slot;
    payload[9] = s->active_la_subject;

    return build_packet(buf, mount_id, seq, CMD_STATUS, payload, 10);
}

// Encode a PayloadHealth into its 24-byte wire form (big-endian fields).
// Split out so nodes that ship the payload through another framing (the
// display's DISP_MSG_HEALTH, the bridge's send_to_hub) can reuse it.
static inline void encode_health_payload(uint8_t p[24], const PayloadHealth *h) {
    p[0] = h->node_type;
    p[1] = h->reset_reason;
    write_be32(p + 2,  h->uptime_s);
    write_be32(p + 6,  h->free_heap);
    write_be32(p + 10, h->min_free_heap);
    write_be16(p + 14, h->loop_max_ms);
    write_be16(p + 16, h->tx_fail);
    p[18] = (uint8_t)h->rssi;
    p[19] = h->flags;
    write_be32(p + 20, h->node_u32);
}

// Build a CMD_HEALTH packet (24-byte payload) — uniform for every node type.
static inline uint16_t build_health(uint8_t *buf, uint8_t mount_id, uint16_t seq,
                                     const PayloadHealth *h) {
    uint8_t p[24];
    encode_health_payload(p, h);
    return build_packet(buf, mount_id, seq, CMD_HEALTH, p, 24);
}

// Build a CMD_POSITION packet (17-byte payload, physical units).
static inline uint16_t build_position(uint8_t *buf, uint8_t mount_id, uint16_t seq,
                                       float pan_deg, float tilt_deg,
                                       float slider_mm, int32_t zoom_steps,
                                       uint8_t moving_mask) {
    uint8_t p[17];
    write_be_float(p + 0,  pan_deg);
    write_be_float(p + 4,  tilt_deg);
    write_be_float(p + 8,  slider_mm);
    write_be32(p + 12, (uint32_t)zoom_steps);
    p[16] = moving_mask;
    return build_packet(buf, mount_id, seq, CMD_POSITION, p, 17);
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

// CMD_START_LOOK_AT_MOVE payload (3 bytes)
typedef struct __attribute__((packed)) {
    uint8_t subject_id;    // 0–7
    uint8_t direction;     // 0 = go to min/left limit, 1 = go to max/right limit
    uint8_t speed_preset;  // 1–4
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
