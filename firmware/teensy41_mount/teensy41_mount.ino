/*
 * teensy41_mount.ino — Camera mount main sketch (Teensy 4.1)
 *
 * Receives binary packets from ESP32 (Serial6) via the shared protocol.
 * Dispatches commands to the MountMotion library.
 * Sends STATUS packets back to ESP32 at regular intervals.
 *
 * Teensy 4.1 pin assignments:
 *   Serial6  (RX=25, TX=24)  — ESP32 comms
 *   Serial7  (RX=28, TX=29)  — LANC zoom
 *   Serial3  (RX=15, TX=14)  — TMC2209 Slider
 *   Serial4  (RX=16, TX=17)  — TMC2209 Tilt
 *   Serial5  (RX=21, TX=20)  — TMC2209 Pan
 *   Serial8  (RX=34, TX=35)  — TMC2209 Zoom
 *
 * Requires:
 *   - TeensyStep4      (https://github.com/luni64/TeensyStep4)
 *   - TMCStepper       (https://github.com/teemuatlut/TMCStepper)
 *   - EEPROM           (built-in Teensy)
 *
 *
 *  Pan Base 270 teeth
 *  Pan metal 30 teeth = 9
 *  Pan metal 36 teeth = 7.5
 *
 *  Tilt large 120 teeth
 *  Tilt small  15 teeth = 8
 *  Tilt small  16 teeth = 7.5
 */

#include <Arduino.h>
#include <EEPROM.h>
#include "MountMotion.h"       // also pulls in protocol.h

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

// Placeholder identity — NOT per-unit config.  The ESP32 bridge re-stamps its
// own runtime mount ID (chosen on its touchscreen) onto every packet it
// forwards to the hub, and the UART link is point-to-point, so the Teensy
// neither needs nor uses a real identity.  One binary serves all mounts.
#define THIS_MOUNT_ID  0

#define ESP_SERIAL      Serial6    // Teensy 4.1: TX=24, RX=25
#define ESP_SERIAL_BAUD 115200

#define STATUS_INTERVAL_MS  100   // send status to PC every 100ms

// EEPROM layout (bytes)
// v9: slider_tilt_deg added to MountCfg.  The magic MUST move with the layout —
// reading old bytes into the new shape would hand every field after the change
// a value from the wrong offset.  Speeds and orientation flags fall back to
// defaults once on first boot; limits and stall thresholds are untouched, since
// they live in their own blocks whose magics never change for exactly this.
#define EEPROM_MAGIC        0xCB0B
#define EEPROM_ADDR_MAGIC   0
#define EEPROM_ADDR_CONFIG  4

// Limits are stored in a SEPARATE region with their own fixed magic so they
// survive firmware reflashes even when EEPROM_MAGIC changes.  The address is
// fixed at 512 — well clear of the main config struct which tops out ~460 B.
// EEPROM_MAGIC_LIMITS must NEVER change.
#define EEPROM_MAGIC_LIMITS      0xCBFF
#define EEPROM_ADDR_LIMITS_MAGIC 512
#define EEPROM_ADDR_LIMITS       514   // immediately after the 2-byte magic

// Stall thresholds also get their own fixed region so user-tuned values are
// preserved across firmware reflashes (same rationale as limits above).
// Limits struct is 40 bytes → ends at 554.  Place stall block at 598.
// EEPROM_MAGIC_STALL must NEVER change.
#define EEPROM_MAGIC_STALL       0xCBFE
#define EEPROM_ADDR_STALL_MAGIC  598
#define EEPROM_ADDR_STALL        600   // 4 bytes: one threshold per axis

// AT_POSITION detection thresholds (steps)
#define AT_POS_THRESHOLD_PT   427   // ~0.1 deg at 256 µstep, 15:1 gear
#define AT_POS_THRESHOLD_SZ    80   // ~0.5 mm at 32 µstep slider
// "Parked at the end of the rail" is a REGION, not a target you either hit or
// missed: a look-at move stops near the limit and the operator reads a few mm
// either way as the end.  AT_POS_THRESHOLD_SZ is 0.5 mm, right for deciding
// whether the mount reached a stored position and far too strict for this.
#define AT_END_THRESHOLD_SZ   800   // ~5 mm at 32 µstep slider

// LANC zoom (Serial7 — RX=28, TX=29)
// The LANC device expects 9600 baud, 8E1 framing.
// Commands: "#74N0*" = zoom in speed N (1-8)
//           "#75N0*" = zoom out speed N (1-8)
//           "#7590*" = stop
#define LANC_SERIAL            Serial7
#define LANC_JOG_WATCHDOG_MS   500    // stop LANC if no jog received within this window

// ---------------------------------------------------------------------------
// EEPROM config struct — v2 additions
// ---------------------------------------------------------------------------

// Wire-format subject record (29 bytes — matches SUBJECT_RECORD_LEN in protocol.h)
// Duplicated here in a plain C struct so it can live inside EepromConfig.
struct __attribute__((packed)) SubjectRecord {
    bool  valid;                       // 1B
    char  name[SUBJECT_NAME_LEN];      // 16B
    float x, y, z;                     // 12B  (world coords in mm)
    // total = 29B — matches SUBJECT_RECORD_LEN; packed to suppress float-alignment padding
};
static_assert(sizeof(SubjectRecord) == 29, "SubjectRecord size mismatch");

struct SliderMoveRecord {
    float   start_mm;     // 4B
    float   end_mm;       // 4B
    uint8_t speed_preset; // 1B  (1-4)
    bool    valid;        // 1B
};

struct EepromConfig {
    bool        pan_invert;
    bool        tilt_invert;
    bool        slider_invert;
    bool        zoom_invert;
    bool        lanc_zoom;
    bool        has_slider;
    uint8_t     stall_threshold[4];
    int32_t     min_limit[4];
    int32_t     max_limit[4];
    bool        limits_set[4];
    SpeedPreset pt_presets[5];   // index 0 unused (presets 1-4)
    SpeedPreset sl_presets[5];   // index 0 unused (presets 1-4)
    SpeedPreset zm_preset;       // single zoom preset

    // ── v2 additions ──────────────────────────────────────────────────────
    // NOTE: subjects[] and slider_moves[] intentionally NOT here — both are
    // session-only (RAM only, cleared on power cycle).  Only axis config
    // (limits, speeds, hardware flags) belongs in EEPROM.
    float            pan_deg_per_step;     // calibrated from subject 2-point solve
    float            tilt_deg_per_step;
    float            slider_mm_per_step;   // set when slider limits are found
    bool             look_at_mode;         // v2: slider uses 3D triangulation mode
    // Rail inclination in degrees, signed; 0 is level, positive means the rail
    // RISES as the slider position increases.
    //
    // The look-at solver had both calibration viewpoints on the X axis at equal
    // height.  On a tilted rail they are not: at 21 degrees the camera climbs
    // 358 mm per metre, so a 2 m baseline puts them 717 mm apart vertically.
    // Two rays anchored at the wrong heights do not meet at the subject, and
    // tracking then drifts by degrees as the slider runs.
    float            slider_tilt_deg;
};

// ---------------------------------------------------------------------------
// Globals
// ---------------------------------------------------------------------------

static MountMotion  mount;
static PacketParser _parser;
static uint16_t     _tx_seq         = 0;
static uint32_t     _last_status_ms = 0;

// ---------------------------------------------------------------------------
// Volatile slot state (RAM only — not persisted)
// ---------------------------------------------------------------------------

#define NUM_SLOTS 10

// Stored position coordinates for each slot (all axes)
static int32_t _slot_pos[NUM_SLOTS][4];

// Bitmask: bit N set = slot N has a stored position
static uint16_t _slot_occupied = 0;

// Look-at subjects (RAM only — volatile, cleared on power cycle)
static SubjectRecord _subjects[MAX_SUBJECTS];

// Slider move records (RAM only — volatile, set fresh each session)
static SliderMoveRecord _slider_moves[MAX_SLIDER_MOVES];

// Bitmask: bit N set = mount is currently AT slot N's position
static uint16_t _slot_at       = 0;

// Active speed preset indices
static uint8_t _active_pt_preset = 2;   // 1-4
static uint8_t _active_sl_preset = 2;   // 1-4

// Target slot for in-progress CMD_GOTO_SLOT move (0xFF = none)
static uint8_t  _target_slot        = TARGET_SLOT_NONE;
static uint8_t  _prev_motion_state  = STATE_IDLE;

// ---------------------------------------------------------------------------
// v2 — Subject calibration state machine (RAM only)
// ---------------------------------------------------------------------------

enum class CalibState : uint8_t {
    IDLE,
    MOVING_TO_B,   // slider moving to opposite end after obs A recorded
    WAIT_B,        // waiting for operator to aim and send CMD_ADD_SUBJECT_SET_B
};

static int8_t     _la_last_direction = -1;   // direction of last CMD_START_LOOK_AT_MOVE (0=min, 1=max, -1=unknown)
static CalibState _calib_state      = CalibState::IDLE;
static int32_t    _calib_dest_phys  = 0;   // physical slider target set by SET_A move
static uint8_t    _calib_subject_id = 0;
static char       _calib_name[SUBJECT_NAME_LEN] = {};

// Recorded observations (filled at SET_A and SET_B)
static float   _calib_xa_mm       = 0.0f;
static int32_t _calib_pan_steps_A = 0;
static int32_t _calib_tilt_steps_A = 0;
static float   _calib_xb_mm       = 0.0f;
static int32_t _calib_pan_steps_B = 0;
static int32_t _calib_tilt_steps_B = 0;

// ---------------------------------------------------------------------------
// LANC zoom state
// ---------------------------------------------------------------------------

// LANC zoom state
static int8_t   _lanc_speed             = 0;     // last speed transmitted (-8..+8; 0=stopped)
static uint32_t _lanc_jog_last_ms       = 0;     // millis() of last non-zero zoom jog
static bool     _lanc_serial_init       = false; // true once LANC_SERIAL.begin() called
static bool     _lanc_connected         = false; // true once camera replied to our probe

// ---------------------------------------------------------------------------
// EEPROM helpers
// ---------------------------------------------------------------------------

// Shadow config — updated in place by command handlers, then flushed by save_full_config()
static EepromConfig _cfg = {};

// Limits-only record — stored at a fixed address with its own magic so that
// limits survive firmware reflashes even when the main config magic changes.
// slider_mm_per_step is included because getSliderMm() returns 0 without it,
// making triangulation fail (both xa and xb appear to be 0mm).
struct EepromLimits {
    bool    limits_set[4];
    int32_t min_limit[4];
    int32_t max_limit[4];
    float   slider_mm_per_step;   // needed for getSliderMm() to return correct values
};

static void save_limits() {
    EepromLimits lim;
    for (int i = 0; i < 4; i++) {
        lim.limits_set[i] = _cfg.limits_set[i];
        lim.min_limit[i]  = _cfg.min_limit[i];
        lim.max_limit[i]  = _cfg.max_limit[i];
    }
    lim.slider_mm_per_step = _cfg.slider_mm_per_step;
    EEPROM.put(EEPROM_ADDR_LIMITS_MAGIC, (uint16_t)EEPROM_MAGIC_LIMITS);
    EEPROM.put(EEPROM_ADDR_LIMITS, lim);
    Serial.println("[save_limits] limits written to dedicated EEPROM region");
}

static void load_limits_if_valid() {
    uint16_t magic;
    EEPROM.get(EEPROM_ADDR_LIMITS_MAGIC, magic);
    if (magic != EEPROM_MAGIC_LIMITS) {
        Serial.println("[load_limits] no valid limits block found");
        return;
    }
    EepromLimits lim;
    EEPROM.get(EEPROM_ADDR_LIMITS, lim);
    for (int i = 0; i < 4; i++) {
        if (!lim.limits_set[i]) continue;
        _cfg.limits_set[i] = true;
        _cfg.min_limit[i]  = lim.min_limit[i];
        _cfg.max_limit[i]  = lim.max_limit[i];
        if (i == AXIS_ZOOM && _cfg.zoom_invert)
            mount.setLimits((Axis)AXIS_ZOOM, -(int32_t)lim.max_limit[i], 0);
        else
            mount.setLimits((Axis)i, lim.min_limit[i], lim.max_limit[i]);
        Serial.printf("[load_limits] axis %d: min=%ld  max=%ld\n",
                      i, (long)lim.min_limit[i], (long)lim.max_limit[i]);
    }
    // Restore slider_mm_per_step so getSliderMm() returns correct values.
    // Fall back to the nominal constant if the stored value looks invalid.
    float mm_step = lim.slider_mm_per_step;
    if (mm_step <= 0.0f) mm_step = NOMINAL_SLIDER_MM_PER_STEP;
    _cfg.slider_mm_per_step = mm_step;
    mount.setSliderMmPerStep(mm_step);
    Serial.printf("[load_limits] slider_mm_per_step=%.6f\n", mm_step);
}

// Persist stall thresholds to their own fixed region so they survive a
// EEPROM_MAGIC change (firmware reflash).  Called automatically from
// save_full_config() so the region is always kept in sync.
static void save_stall_thresholds() {
    uint8_t buf[4];
    for (int i = 0; i < 4; i++) buf[i] = _cfg.stall_threshold[i];
    EEPROM.put(EEPROM_ADDR_STALL_MAGIC, (uint16_t)EEPROM_MAGIC_STALL);
    EEPROM.put(EEPROM_ADDR_STALL, buf);
    Serial.printf("[save_stall] sl=%d  zm=%d\n",
                  buf[AXIS_SLIDER], buf[AXIS_ZOOM]);
}

// Load stall thresholds from the dedicated region into _cfg and apply to
// the motor drivers.  Returns true if the region was valid.  Called during
// the magic-mismatch boot path so user-tuned values are not overwritten by
// a reflash.
static bool load_stall_thresholds_if_valid() {
    uint16_t magic;
    EEPROM.get(EEPROM_ADDR_STALL_MAGIC, magic);
    if (magic != EEPROM_MAGIC_STALL) {
        Serial.println("[load_stall] no valid stall block — using defaults");
        return false;
    }
    uint8_t buf[4];
    EEPROM.get(EEPROM_ADDR_STALL, buf);
    for (int i = 0; i < 4; i++) {
        if (buf[i] == 0) buf[i] = DEFAULT_STALL_THRESHOLD[i];  // 0 is invalid
        _cfg.stall_threshold[i] = buf[i];
        mount.setStallThreshold((Axis)i, buf[i]);
    }
    Serial.printf("[load_stall] restored: sl=%d  zm=%d\n",
                  buf[AXIS_SLIDER], buf[AXIS_ZOOM]);
    return true;
}

static void eeprom_load() {
    Serial.println("[eeprom_load] reading magic...");
    uint16_t magic;
    EEPROM.get(EEPROM_ADDR_MAGIC, magic);
    Serial.printf("[eeprom_load] magic=0x%04X (expect 0x%04X)\n", magic, (uint16_t)EEPROM_MAGIC);

    if (magic != EEPROM_MAGIC) {
        // First boot or layout change — seed _cfg with safe defaults, then
        // immediately write them to EEPROM with the new magic so that:
        //   (a) CONFIG_REPORT is truthful from the first boot, and
        //   (b) the first save_full_config() from any command doesn't burn a
        //       partially-zero _cfg (e.g. orientation flags all-false) that
        //       would silently overwrite real settings on the next load.
        Serial.println("[eeprom_load] magic mismatch — writing defaults to EEPROM");
        // Stall thresholds survive in their own fixed region; only fall back
        // to DEFAULT_STALL_THRESHOLD when that region is also uninitialised.
        if (!load_stall_thresholds_if_valid()) {
            for (int i = 0; i < 4; i++)
                _cfg.stall_threshold[i] = DEFAULT_STALL_THRESHOLD[i];
        }
        _cfg.has_slider   = true;
        _cfg.look_at_mode = false;
        // Seed preset defaults to match MountMotion's DEFAULT_*_PRESETS.
        // Index 0 unused; presets 1-4 match the arrays in MountMotion.cpp.
        const SpeedPreset pt_defs[5] = {{0,0},{1,1},{5,5},{10,10},{15,15}};
        const SpeedPreset sl_defs[5] = {{0,0},{1,1},{10,10},{20,20},{40,40}};
        for (int i = 0; i < 5; i++) {
            _cfg.pt_presets[i] = pt_defs[i];
            _cfg.sl_presets[i] = sl_defs[i];
        }
        _cfg.zm_preset = {40, 40};
        // Limits survive magic changes via their own dedicated block.
        load_limits_if_valid();
        // Write defaults now so EEPROM is consistent from this point forward.
        save_full_config();
        return;
    }

    Serial.println("[eeprom_load] magic OK — reading config struct...");
    EepromConfig cfg;
    EEPROM.get(EEPROM_ADDR_CONFIG, cfg);
    Serial.printf("[eeprom_load] lanc_zoom=%d  pan_inv=%d  tilt_inv=%d  slider_inv=%d  zoom_inv=%d  has_slider=%d\n",
                  (int)cfg.lanc_zoom, (int)cfg.pan_invert, (int)cfg.tilt_invert,
                  (int)cfg.slider_invert, (int)cfg.zoom_invert, (int)cfg.has_slider);

    mount.setOrientation(cfg.pan_invert, cfg.tilt_invert, cfg.slider_invert, cfg.zoom_invert);
    mount.setHasSlider(cfg.has_slider);
    mount.setLookAtMode(cfg.look_at_mode);
    if (cfg.lanc_zoom) lanc_ensure_init();

    for (int i = 0; i < 4; i++) {
        mount.setStallThreshold((Axis)i, cfg.stall_threshold[i]);
        if (cfg.limits_set[i]) {
            // EEPROM always stores (0, +travel).  For inverted zoom the motor
            // coordinate frame runs (-travel, 0), so flip sign at load time.
            if (i == AXIS_ZOOM && cfg.zoom_invert)
                mount.setLimits((Axis)AXIS_ZOOM, -(int32_t)cfg.max_limit[i], 0);
            else
                mount.setLimits((Axis)i, cfg.min_limit[i], cfg.max_limit[i]);
        }
    }

    for (int p = 1; p <= 4; p++) {
        mount.setSpeedPreset(GROUP_PAN_TILT,    p, cfg.pt_presets[p]);
        mount.setSpeedPreset(GROUP_SLIDER_ZOOM, p, cfg.sl_presets[p]);
    }
    mount.setZoomPreset(cfg.zm_preset);

    // v2 — restore calibrated deg/step and mm/step values.
    // Validate pan/tilt dps against nominal ±25%.  A bad refinement from a
    // prior failed calibration can save a wildly wrong value that causes every
    // subsequent calibration to produce pan angles > 90° (rays pointing
    // backward) and fail.  If out of range, fall back to nominal so the next
    // calibration starts clean.
    if (cfg.pan_deg_per_step > 0.0f && cfg.tilt_deg_per_step > 0.0f) {
        const float pan_lo  = NOMINAL_PAN_DEG_PER_STEP  * 0.75f;
        const float pan_hi  = NOMINAL_PAN_DEG_PER_STEP  * 1.25f;
        const float tilt_lo = NOMINAL_TILT_DEG_PER_STEP * 0.75f;
        const float tilt_hi = NOMINAL_TILT_DEG_PER_STEP * 1.25f;
        bool dps_ok = (cfg.pan_deg_per_step  >= pan_lo  && cfg.pan_deg_per_step  <= pan_hi &&
                       cfg.tilt_deg_per_step >= tilt_lo && cfg.tilt_deg_per_step <= tilt_hi);
        if (dps_ok) {
            mount.setDegPerStep(cfg.pan_deg_per_step, cfg.tilt_deg_per_step);
            Serial.printf("[eeprom_load] dps OK: pan=%.8f  tilt=%.8f\n",
                          cfg.pan_deg_per_step, cfg.tilt_deg_per_step);
        } else {
            cfg.pan_deg_per_step  = NOMINAL_PAN_DEG_PER_STEP;
            cfg.tilt_deg_per_step = NOMINAL_TILT_DEG_PER_STEP;
            mount.setDegPerStep(NOMINAL_PAN_DEG_PER_STEP, NOMINAL_TILT_DEG_PER_STEP);
            Serial.printf("[eeprom_load] dps OUT OF RANGE — reset to nominal: pan=%.8f  tilt=%.8f\n",
                          NOMINAL_PAN_DEG_PER_STEP, NOMINAL_TILT_DEG_PER_STEP);
        }
    }
    if (cfg.slider_mm_per_step > 0.0f) mount.setSliderMmPerStep(cfg.slider_mm_per_step);
    // The look-at maths lives in MountMotion; it needs the rail geometry too.
    mount.setSliderTiltDeg(cfg.slider_tilt_deg);

    Serial.printf("[eeprom_load] sl_mm_step=%.8f\n", cfg.slider_mm_per_step);

    // Mirror loaded config into _cfg so save_full_config() doesn't clobber
    // valid EEPROM data when any single command triggers a save.
    _cfg = cfg;
}

static void save_full_config() {
    EEPROM.put(EEPROM_ADDR_MAGIC,  (uint16_t)EEPROM_MAGIC);
    EEPROM.put(EEPROM_ADDR_CONFIG, _cfg);
    save_stall_thresholds();  // keep dedicated region in sync for flash-survival
}

// ---------------------------------------------------------------------------
// Send a packet to the ESP32
// ---------------------------------------------------------------------------

static void send_packet(CmdType cmd, const uint8_t *payload, uint8_t len) {
    uint8_t  buf[PKT_BUF_SIZE + 4];
    uint16_t total = build_packet(buf, THIS_MOUNT_ID, ++_tx_seq, cmd, payload, len);
    ESP_SERIAL.write(buf, total);
}

// ---------------------------------------------------------------------------
// LANC zoom helpers
// ---------------------------------------------------------------------------

// Initialise LANC serial port once (safe to call multiple times).
static void lanc_ensure_init() {
    if (!_lanc_serial_init) {
        LANC_SERIAL.begin(9600, SERIAL_8E1);
        _lanc_serial_init = true;
        // Camera initiates the handshake by sending %000* — we reply &00080* each time.
        // Do NOT send proactively; just wait for the camera.
        Serial.println("[LANC] Serial7 init: 9600 8E1 — waiting for camera %000* handshake");
    }
}

// Map joystick velocity (-1000..+1000) to LANC speed (-8..+8).
// Protocol encodes speed as digit 1 (slow) to 8 (fast) in the command string.
// Any non-zero velocity produces at least speed 1 (ceiling division).
static int8_t lanc_map_speed(int16_t vel) {
    if (vel == 0) return 0;
    int abs_vel = abs((int)vel);
    int8_t spd  = (int8_t)((abs_vel * 8 + 999) / 1000);  // ceiling → 1..8
    if (spd > 8) spd = 8;
    return (vel > 0) ? spd : (int8_t)-spd;
}

// Transmit a LANC zoom command.
//   speed > 0 → zoom in  : "#74N0*"  (N = speed, 1-8)
//   speed < 0 → zoom out : "#75N0*"  (N = -speed, 1-8)
//   speed = 0 → stop     : "#7590*"
static void lanc_send_zoom(int8_t speed) {
    char cmd[8];
    if (speed > 0) {
        cmd[0]='#'; cmd[1]='7'; cmd[2]='4'; cmd[3]=(char)('0'+speed);
        cmd[4]='0'; cmd[5]='*'; cmd[6]='\0';
    } else if (speed < 0) {
        cmd[0]='#'; cmd[1]='7'; cmd[2]='5'; cmd[3]=(char)('0'+(-speed));
        cmd[4]='0'; cmd[5]='*'; cmd[6]='\0';
    } else {
        cmd[0]='#'; cmd[1]='7'; cmd[2]='5'; cmd[3]='9';
        cmd[4]='0'; cmd[5]='*'; cmd[6]='\0';
    }
    LANC_SERIAL.print(cmd);
    Serial.printf("[LANC] TX: %s\n", cmd);
}

// Service the LANC RX buffer.
// Must be called from loop() whenever lanc_zoom is active.
static void lanc_service() {
    static char    rx_buf[12];
    static uint8_t rx_len = 0;

    while (LANC_SERIAL.available()) {
        char c = (char)LANC_SERIAL.read();

        if (c == '%' || c == '$' || c == '&') {
            rx_len = 0;
        }
        if (rx_len < (uint8_t)(sizeof(rx_buf) - 1)) {
            rx_buf[rx_len++] = c;
            rx_buf[rx_len]   = '\0';
        }
        if (c == '*') {
            if (strcmp(rx_buf, "%000*") == 0) {
                // Camera handshake — must reply with &00080* every time it arrives.
                // The camera will not accept any commands until it receives this reply.
                LANC_SERIAL.print("&00080*");
                if (!_lanc_connected) {
                    _lanc_connected = true;
                    Serial.println("[LANC] Connected — replied &00080* to camera handshake");
                }
            } else {
                // Camera response to a command (e.g. "$74300*" = zoom in ack)
                Serial.printf("[LANC] RX: %s\n", rx_buf);
            }
            rx_len = 0;
        }
    }
}

// Live positions in physical units, adaptive rate (5 Hz moving / 1 Hz idle) —
// see CMD_POSITION in shared/protocol.h.  Deliberately separate from the
// 50 Hz STATUS: position is slow data and rides a slow packet.
static void send_position() {
    MountStatusSnapshot s = mount.getStatus();
    uint8_t p[17];
    write_be_float(p + 0,  mount.positionPhys(AXIS_PAN));
    write_be_float(p + 4,  mount.positionPhys(AXIS_TILT));
    write_be_float(p + 8,  _cfg.has_slider ? mount.positionPhys(AXIS_SLIDER) : 0.0f);
    write_be32(p + 12, (uint32_t)s.pos[AXIS_ZOOM]);
    p[16] = mount.movingMask();
    send_packet(CMD_POSITION, p, 17);
}

static void send_status() {
    MountStatusSnapshot s = mount.getStatus();

    // v2: keep FLAG_REF_SET and FLAG_LOOK_AT_ACTIVE in flags byte
    // (MountMotion already sets/clears these via _setFlag/_clearFlag)

    // PayloadStatus: 10 bytes.
    uint8_t payload[10];

    payload[0] = (uint8_t)s.state;           // State (e.g., IDLE, MOVING)
    payload[1] = s.flags | (_cfg.look_at_mode ? (uint8_t)FLAG_LOOK_AT_MODE : 0);  // Flags (at limits, ref_set, look_at_active, look_at_mode)
    payload[2] = _active_pt_preset;          // PT Speed preset index
    payload[3] = _active_sl_preset;          // SL Speed preset index

    // Use big-endian helpers for the 16-bit masks (as defined in protocol.h)
    write_be16(payload + 4, _slot_occupied); // Bits 0-9: which slots have data
    write_be16(payload + 6, _slot_at);       // Bits 0-9: which slots are at position

    payload[8] = _target_slot;               // The slot we are currently moving to
    payload[9] = mount.getLaSubjectId();     // Active look-at subject (0-7, 0xFF = none)

    send_packet(CMD_STATUS, payload, 10);
}

// CMD_CONFIG_REPORT — orientation flags + 9 speed presets + 2 stall thresholds
// Payload layout: 75 bytes
//   [0]       orientation flags (bit0=pan_invert, bit1=slider_invert, bit2=has_slider,
//                                bit3=zoom_invert, bit4=lanc_zoom, bit5=tilt_invert)
//   [1..32]   4 × PT preset: uint32 max_speed, uint32 accel (presets 1-4)
//   [33..64]  4 × SL preset: uint32 max_speed, uint32 accel (presets 1-4)
//   [65..72]  1 × ZM preset: uint32 max_speed, uint32 accel
//   [73]      stall_threshold[AXIS_SLIDER]
//   [74]      stall_threshold[AXIS_ZOOM]
//   [75..76]  slider tilt, int16, tenths of a degree, signed
static void send_config_report() {
    uint8_t payload[CONFIG_REPORT_PAYLOAD_LEN];
    payload[0] = (_cfg.pan_invert    ? 0x01 : 0) |
                 (_cfg.slider_invert ? 0x02 : 0) |
                 (_cfg.has_slider    ? 0x04 : 0) |
                 (_cfg.zoom_invert   ? 0x08 : 0) |
                 (_cfg.lanc_zoom     ? 0x10 : 0) |
                 (_cfg.tilt_invert   ? 0x20 : 0) |
                 (_cfg.look_at_mode  ? 0x40 : 0);
    for (int p = 1; p <= 4; p++) {
        int off = 1 + (p - 1) * 8;
        write_be32(payload + off,     _cfg.pt_presets[p].max_speed);
        write_be32(payload + off + 4, _cfg.pt_presets[p].acceleration);
    }
    for (int p = 1; p <= 4; p++) {
        int off = 33 + (p - 1) * 8;
        write_be32(payload + off,     _cfg.sl_presets[p].max_speed);
        write_be32(payload + off + 4, _cfg.sl_presets[p].acceleration);
    }
    write_be32(payload + 65, _cfg.zm_preset.max_speed);
    write_be32(payload + 69, _cfg.zm_preset.acceleration);
    payload[73] = _cfg.stall_threshold[AXIS_SLIDER];
    payload[74] = _cfg.stall_threshold[AXIS_ZOOM];
    // [75..76] rail inclination, tenths of a degree, signed.  Reported so the
    // config dialog shows what the MOUNT holds rather than what was last typed.
    {
        int16_t t10 = (int16_t)lroundf(_cfg.slider_tilt_deg * 10.0f);
        payload[75] = (uint8_t)((uint16_t)t10 >> 8);
        payload[76] = (uint8_t)((uint16_t)t10 & 0xFF);
    }
    send_packet(CMD_CONFIG_REPORT, payload, CONFIG_REPORT_PAYLOAD_LEN);
}

// CMD_STATE_REPORT — lightweight bitmasks + active presets (6 bytes)
static void send_state_report() {
    uint8_t payload[6];
    uint8_t *p = payload;

    write_be16(p,     _slot_occupied);  // +0: Which slots have data
    write_be16(p + 2, _slot_at);        // +2: Which slots are currently at position
    p[4] = _active_pt_preset;           // +4: Active PT speed preset
    p[5] = _active_sl_preset;           // +5: Active SL speed preset

    send_packet(CMD_STATE_REPORT, payload, 6);
}

// ---------------------------------------------------------------------------
// AT_POSITION detection — called from loop()
// ---------------------------------------------------------------------------

static void update_slot_at_mask() {
    MountStatusSnapshot s = mount.getStatus();

    // _slot_pos stores logical (pre-orientation) coordinates so that moveTo()
    // arrives at the correct physical position.  Convert the current physical
    // positions to the same logical space before comparing.
    int32_t cur[4];
    cur[AXIS_PAN]    = _cfg.pan_invert    ? -s.pos[AXIS_PAN]    : s.pos[AXIS_PAN];
    cur[AXIS_TILT]   = _cfg.tilt_invert   ? -s.pos[AXIS_TILT]   : s.pos[AXIS_TILT];
    cur[AXIS_SLIDER] = _cfg.slider_invert ? -s.pos[AXIS_SLIDER] : s.pos[AXIS_SLIDER];
    cur[AXIS_ZOOM]   = s.pos[AXIS_ZOOM];

    uint16_t new_at = 0;
    for (int slot = 0; slot < NUM_SLOTS; slot++) {
        if (!(_slot_occupied & (1u << slot))) continue;

        if (abs(cur[AXIS_PAN]    - _slot_pos[slot][AXIS_PAN])    <= AT_POS_THRESHOLD_PT &&
            abs(cur[AXIS_TILT]   - _slot_pos[slot][AXIS_TILT])   <= AT_POS_THRESHOLD_PT &&
            abs(cur[AXIS_SLIDER] - _slot_pos[slot][AXIS_SLIDER]) <= AT_POS_THRESHOLD_SZ &&
            abs(cur[AXIS_ZOOM]   - _slot_pos[slot][AXIS_ZOOM])   <= AT_POS_THRESHOLD_SZ) {
            new_at |= (1u << slot);
        }
    }
    // In look-at mode slots 8 and 9 hold no stored position — they ARE the
    // ◀/▶ arrows, and a lit arrow asserts "the slider is parked at that end of
    // the rail".  Report it through the same mask as every other border, so it
    // clears the moment the slider leaves the end no matter which client moved
    // it.  The PC app previously latched this locally, which meant it could
    // only notice slides it had sent itself: one driven from the hub display or
    // the web app left the arrow green with the slider mid-rail.
    //
    // Which physical limit each arrow means is exactly the mapping
    // CMD_START_LOOK_AT_MOVE uses — slider_invert flips what "left" is — and
    // the two must agree, or the arrow that takes you to an end is not the one
    // that lights when you get there.
    if (_cfg.look_at_mode && _cfg.has_slider && mount.hasLimits(AXIS_SLIDER)) {
        int32_t sl_phys   = s.pos[AXIS_SLIDER];
        int32_t phys_min  = mount.getMinLimit(AXIS_SLIDER);
        int32_t phys_max  = mount.getMaxLimit(AXIS_SLIDER);
        int32_t left_end  = _cfg.slider_invert ? phys_max : phys_min;
        int32_t right_end = _cfg.slider_invert ? phys_min : phys_max;
        if (abs(sl_phys - left_end)  <= AT_END_THRESHOLD_SZ) new_at |= (1u << 8);
        if (abs(sl_phys - right_end) <= AT_END_THRESHOLD_SZ) new_at |= (1u << 9);
    }

    _slot_at = new_at;
}

// ---------------------------------------------------------------------------
// Home-complete callback  (called by findHome() when homing finishes)
// ---------------------------------------------------------------------------

static void on_home_complete(Axis axis, int32_t min_steps, int32_t max_steps) {
    int idx = (int)axis;

    // MountMotion may have cleared broken limits (min >= max from a stale
    // findLimits run).  Sync EEPROM so the cleared state survives a reboot.
    if (!mount.hasLimits(axis)) {
        _cfg.limits_set[idx] = false;
        _cfg.min_limit[idx]  = 0;
        _cfg.max_limit[idx]  = 0;
        save_full_config();
        save_limits();   // keep dedicated limits block in sync
    }

    // Notify PC — 1-byte payload: axis
    uint8_t ax = (uint8_t)axis;
    send_packet(CMD_HOME_COMPLETE, &ax, 1);
}

// ---------------------------------------------------------------------------
// Limit-found callback
// ---------------------------------------------------------------------------

static void on_limits_found(Axis axis, int32_t min_steps, int32_t max_steps) {
    // EEPROM always stores (0, +travel) regardless of inversion.
    // For inverted zoom the motor frame has (min=-travel, max=0); normalise.
    int32_t eeprom_max = (axis == AXIS_ZOOM && _cfg.zoom_invert)
                         ? -min_steps   // travel = abs(min_steps)
                         : max_steps;
    _cfg.limits_set[(int)axis] = true;
    _cfg.min_limit[(int)axis]  = 0;
    _cfg.max_limit[(int)axis]  = eeprom_max;

    // v2 — compute and persist slider_mm_per_step from measured travel
    if (axis == AXIS_SLIDER && eeprom_max > 0) {
        // Nominal: SLIDER_MM_PER_REV / (MICROSTEPS * steps_per_rev)
        // Use the nominal constant from MountMotion.h — exact unless belt is changed.
        float mm_per_step = NOMINAL_SLIDER_MM_PER_STEP;
        _cfg.slider_mm_per_step = mm_per_step;
        mount.setSliderMmPerStep(mm_per_step);
        Serial.printf("[on_limits_found] slider_mm_per_step=%.6f  travel=%ld steps = %.1f mm\n",
                      mm_per_step, (long)eeprom_max, (float)eeprom_max * mm_per_step);
    }

    save_full_config();
    save_limits();   // also write to dedicated limits block — survives future magic changes

    // Notify PC
    uint8_t buf[PKT_BUF_SIZE + 4];
    uint16_t len = build_limits_found(buf, THIS_MOUNT_ID, ++_tx_seq,
                                      axis, min_steps, max_steps);
    ESP_SERIAL.write(buf, len);
}

// ---------------------------------------------------------------------------
// v2 — Send subject list (CMD_SUBJECT_LIST, 232-byte payload)
// ---------------------------------------------------------------------------

static void send_subject_list() {
    uint8_t payload[SUBJECT_LIST_PAYLOAD_LEN];
    for (int i = 0; i < MAX_SUBJECTS; i++) {
        int off = i * SUBJECT_RECORD_LEN;
        const SubjectRecord &r = _subjects[i];
        payload[off + 0] = r.valid ? 1 : 0;
        memcpy(payload + off + 1, r.name, SUBJECT_NAME_LEN);
        write_be_float(payload + off + 17, r.x);
        write_be_float(payload + off + 21, r.y);
        write_be_float(payload + off + 25, r.z);
    }
    send_packet(CMD_SUBJECT_LIST, payload, SUBJECT_LIST_PAYLOAD_LEN);
}

// ---------------------------------------------------------------------------
// v2 — Send look-at status telemetry (CMD_LOOK_AT_STATUS, 14-byte payload)
// ---------------------------------------------------------------------------

static void send_look_at_status() {
    uint8_t payload[14];
    write_be_float(payload + 0,  mount.getSliderMm());
    write_be_float(payload + 4,  mount.getPanDeg());
    write_be_float(payload + 8,  mount.getTiltDeg());
    payload[12] = mount.getLaSubjectId();

    uint8_t flags = 0;
    if (mount.isRefSet())               flags |= FLAG_REF_SET;
    if (mount.getState() == STATE_LOOK_AT_MOVE ||
        mount.getState() == STATE_LOOK_AT_PRE_AIM) flags |= FLAG_LOOK_AT_ACTIVE;
    payload[13] = flags;

    send_packet(CMD_LOOK_AT_STATUS, payload, 14);
}

// ---------------------------------------------------------------------------
// v2 — Ray-ray closest-point solver for subject 3D position
//
// Ray A: starts at (xa, 0, 0) in direction vA (from pan_A_deg / tilt_A_deg)
// Ray B: starts at (xb, 0, 0) in direction vB (from pan_B_deg / tilt_B_deg)
// Returns false if rays are nearly parallel (sin² < 1e-6).
// ---------------------------------------------------------------------------

// Camera position for a slider coordinate, in world millimetres.
//
// The rail is not necessarily level.  x is the distance ALONG it, so the
// world position is that distance resolved into horizontal and vertical
// components — which is the whole of the fix: everything below used to treat
// the slider coordinate as a horizontal displacement with the camera at a
// constant height.
static void slider_world_pos(float x_along, float *wx, float *wy) {
    float t = _cfg.slider_tilt_deg * (float)DEG_TO_RAD;
    *wx = x_along * cosf(t);
    *wy = x_along * sinf(t);
}

static bool solve_subject_3d(float xa, float xb,
                               float pan_A_deg,  float tilt_A_deg,
                               float pan_B_deg,  float tilt_B_deg,
                               float *sx, float *sy, float *sz) {
    float pa = pan_A_deg  * (float)DEG_TO_RAD;
    float ta = tilt_A_deg * (float)DEG_TO_RAD;
    float pb = pan_B_deg  * (float)DEG_TO_RAD;
    float tb = tilt_B_deg * (float)DEG_TO_RAD;

    Serial.printf("[Solve] xa=%.1f xb=%.1f\n", xa, xb);
    Serial.printf("[Solve] pan_A=%.4f° tilt_A=%.4f°\n", pan_A_deg, tilt_A_deg);
    Serial.printf("[Solve] pan_B=%.4f° tilt_B=%.4f°\n", pan_B_deg, tilt_B_deg);

    // Unit ray directions: (sin(pan)*cos(tilt), sin(tilt), cos(pan)*cos(tilt))
    float vAx = sinf(pa) * cosf(ta);
    float vAy = sinf(ta);
    float vAz = cosf(pa) * cosf(ta);
    float vBx = sinf(pb) * cosf(tb);
    float vBy = sinf(tb);
    float vBz = cosf(pb) * cosf(tb);

    Serial.printf("[Solve] vA=(%.4f, %.4f, %.4f)\n", vAx, vAy, vAz);
    Serial.printf("[Solve] vB=(%.4f, %.4f, %.4f)\n", vBx, vBy, vBz);

    // Origins resolved onto the real rail, which may climb.  This was
    // (xa - xb, 0, 0) — both viewpoints at the same height — and on a tilted
    // rail that is simply not where the camera was.
    float oax, oay, obx, oby;
    slider_world_pos(xa, &oax, &oay);
    slider_world_pos(xb, &obx, &oby);
    float wx = oax - obx;
    float wy = oay - oby;

    float a = 1.0f;                            // dot(vA, vA) = 1
    float b = vAx*vBx + vAy*vBy + vAz*vBz;   // dot(vA, vB)
    float c = 1.0f;                            // dot(vB, vB) = 1
    float d = vAx * wx + vAy * wy;             // dot(vA, w) — wz still 0
    float e = vBx * wx + vBy * wy;             // dot(vB, w)

    float denom = a * c - b * b;              // 1 - b²
    Serial.printf("[Solve] b(dot)=%.6f  denom=%.6f\n", b, denom);
    if (fabsf(denom) < 1e-6f) {
        Serial.println("[Solve] FAIL: denom~0 (parallel rays)");
        return false;
    }

    float t = (b * e - c * d) / denom;
    float s = (a * e - b * d) / denom;
    Serial.printf("[Solve] t=%.2f  s=%.2f\n", t, s);

    // Points on each ray closest to the other
    float pAx = oax + t * vAx;  float pAy = oay + t * vAy;  float pAz = t * vAz;
    float pBx = obx + s * vBx;  float pBy = oby + s * vBy;  float pBz = s * vBz;

    Serial.printf("[Solve] pA=(%.1f, %.1f, %.1f)\n", pAx, pAy, pAz);
    Serial.printf("[Solve] pB=(%.1f, %.1f, %.1f)\n", pBx, pBy, pBz);

    // Midpoint = estimated subject position
    *sx = (pAx + pBx) * 0.5f;
    *sy = (pAy + pBy) * 0.5f;
    *sz = (pAz + pBz) * 0.5f;

    Serial.printf("[Solve] midpoint=(%.1f, %.1f, %.1f)\n", *sx, *sy, *sz);
    return true;
}

// Compute pan angle (deg) from camera position cx (mm) to subject
static float look_at_pan_deg(float cx, float sx, float sz) {
    float wx, wy;
    slider_world_pos(cx, &wx, &wy);
    return atan2f(sx - wx, sz) * (float)RAD_TO_DEG;
}
// Compute tilt angle (deg) from camera position cx (mm) to subject.
// The camera's HEIGHT matters and was previously taken as zero: on a tilted
// rail it climbs as the slider runs, so a subject held in frame at one end
// drifts vertically by the whole rise unless this is subtracted.
static float look_at_tilt_deg(float cx, float sx, float sy, float sz) {
    float wx, wy;
    slider_world_pos(cx, &wx, &wy);
    float dx = sx - wx;
    return atan2f(sy - wy, sqrtf(dx * dx + sz * sz)) * (float)RAD_TO_DEG;
}

// ---------------------------------------------------------------------------
// v2 — Calibration state machine — update() called from loop()
// ---------------------------------------------------------------------------

// Arrival threshold: within 50 motor steps of the destination is "arrived".
// moveSliderTo() is a jog-context helper that intentionally does not change
// _state, so we cannot rely on STATE_MOVING_TO_POS.  Position proximity is
// reliable and fires correctly whether the slider moved a long way or was
// already near the destination.
#define CALIB_ARRIVE_STEPS 50

static void update_calib() {
    if (_calib_state == CalibState::IDLE) return;

    if (_calib_state == CalibState::MOVING_TO_B) {
        int32_t cur = mount.getPosition(AXIS_SLIDER);
        if (labs((long)(cur - _calib_dest_phys)) <= CALIB_ARRIVE_STEPS) {
            _calib_state = CalibState::WAIT_B;
            uint8_t b = (uint8_t)CALIB_WAIT_SET_B;
            send_packet(CMD_CALIB_PROMPT, &b, 1);
            Serial.println("[Calib] Slider arrived — operator: re-aim at subject, then press SET_B");
        }
    }
    // WAIT_B handled by CMD_ADD_SUBJECT_SET_B in dispatch
}

// ---------------------------------------------------------------------------
// Command dispatch
// ---------------------------------------------------------------------------

static void dispatch(const ParsedPacket &pkt) {
    const uint8_t *p   = pkt.payload;
    uint8_t        len = pkt.payload_len;

    // Send ACK first
    uint8_t ack_buf[PKT_BUF_SIZE];
    uint16_t ack_len = build_ack(ack_buf, THIS_MOUNT_ID, ++_tx_seq, pkt.seq);
    ESP_SERIAL.write(ack_buf, ack_len);

    switch (pkt.cmd) {

        case CMD_JOG: {
            if (len < 8) break;
            int16_t pan    = be16s(p + 0);
            int16_t tilt   = be16s(p + 2);
            int16_t slider = be16s(p + 4);
            int16_t zoom   = be16s(p + 6);
            uint8_t pt_preset = (len >= 9)  ? p[8] : _active_pt_preset;
            uint8_t sz_preset = (len >= 10) ? p[9] : _active_sl_preset;
            // Optional axis mask (byte 10): bit0=pan, bit1=tilt, bit2=slider, bit3=zoom.
            // 0x0F (default) = all axes.  0x03 = pan+tilt only (CV-tracking mode).
            uint8_t axis_mask = (len >= 11) ? p[10] : 0x0F;

            if (axis_mask == 0x03) {
                // CV-tracking mode: only drive pan/tilt; slider may be running moveSliderTo()
                mount.jogPanTilt(pan, tilt, pt_preset);
            } else if (axis_mask == 0x0C) {
                // CV-tracking mode: only drive slider/zoom; pan/tilt under CV control
                if (_cfg.lanc_zoom) {
                    int16_t eff_zoom = _cfg.zoom_invert ? (int16_t)-zoom : zoom;
                    int8_t  new_spd  = lanc_map_speed(eff_zoom);
                    if (new_spd != _lanc_speed) {
                        _lanc_speed = new_spd;
                        lanc_send_zoom(new_spd);
                    }
                    if (new_spd != 0) _lanc_jog_last_ms = millis();
                    mount.jogSliderZoom(slider, 0, sz_preset);
                } else {
                    mount.jogSliderZoom(slider, zoom, sz_preset);
                }
            } else if (_cfg.lanc_zoom) {
                // LANC zoom: map zoom velocity to LANC speed; pass zoom=0 to stepper
                int16_t eff_zoom = _cfg.zoom_invert ? (int16_t)-zoom : zoom;
                int8_t  new_spd  = lanc_map_speed(eff_zoom);
                if (new_spd != _lanc_speed) {
                    _lanc_speed = new_spd;
                    lanc_send_zoom(new_spd);
                }
                if (new_spd != 0) _lanc_jog_last_ms = millis();
                mount.jog(pan, tilt, slider, 0, pt_preset, sz_preset);
            } else {
                mount.jog(pan, tilt, slider, zoom, pt_preset, sz_preset);
            }

            // If the operator manually moves any physical axis while in look-at
            // mode (but not during an active tracking move), deselect the active
            // subject so all UIs show "not tracking" (red border).
            // Only PAN or TILT drops the subject.  Moving the slider is not
            // aiming somewhere else — it is the operator repositioning the
            // camera along the rail while the triangulation keeps the subject
            // framed, which is the point of look-at mode.  The slider used to
            // be in this list, so a manual slide both deselected the subject
            // and left pan/tilt where they were.
            if (_cfg.look_at_mode &&
                    mount.getState() != STATE_LOOK_AT_MOVE &&
                    mount.getState() != STATE_LOOK_AT_PRE_AIM &&
                    (pan != 0 || tilt != 0)) {
                mount.clearLaSubject();
                send_look_at_status();
            }
            break;
        }

        case CMD_GOTO: {
            if (len < 17) break;
            int32_t pan    = be32s(p + 0);
            int32_t tilt   = be32s(p + 4);
            int32_t slider = be32s(p + 8);
            int32_t zoom   = _cfg.lanc_zoom ? 0 : be32s(p + 12);
            uint8_t preset = p[16];
            mount.moveTo(pan, tilt, slider, zoom, preset);
            break;
        }

        case CMD_MOVE_REL: {
            if (len < 17) break;
            int32_t d_pan    = be32s(p + 0);
            int32_t d_tilt   = be32s(p + 4);
            int32_t d_slider = be32s(p + 8);
            int32_t d_zoom   = _cfg.lanc_zoom ? 0 : be32s(p + 12);
            uint8_t preset   = p[16];
            // moveRel() converts physical positions to logical space before
            // adding deltas, so moveTo()'s orientation transform is not applied
            // twice (which would send uninvolved axes to their mirror position).
            mount.moveRel(d_pan, d_tilt, d_slider, d_zoom, preset);

            // Same rule as CMD_JOG above: moving a physical axis by hand while
            // in look-at mode means we are no longer aimed at the subject, so
            // deselect it and let every UI go red.  The nudge arrows on the PC
            // app and the web app send MOVE_REL, not JOG, so without this the
            // border stayed green after the camera had been moved away — the
            // operator had no way to see that pressing the slot would re-aim.
            // Slider deliberately absent — see the CMD_JOG note above.
            if (_cfg.look_at_mode &&
                    mount.getState() != STATE_LOOK_AT_MOVE &&
                    mount.getState() != STATE_LOOK_AT_PRE_AIM &&
                    (d_pan != 0 || d_tilt != 0)) {
                mount.clearLaSubject();
                send_look_at_status();
            }
            break;
        }

        case CMD_SET_SPEED_PRESET: {
            if (len < 10) break;
            uint8_t grp    = p[0];
            uint8_t preset = p[1];
            SpeedPreset sp;
            sp.max_speed    = be32(p + 2);
            sp.acceleration = be32(p + 6);
            mount.setSpeedPreset((AxisGroup)grp, preset, sp);
            if (grp == GROUP_PAN_TILT && preset >= 1 && preset <= 4)
                _cfg.pt_presets[preset] = sp;
            else if (grp == GROUP_SLIDER_ZOOM && preset >= 1 && preset <= 4)
                _cfg.sl_presets[preset] = sp;
            else if (grp == GROUP_ZOOM)
                _cfg.zm_preset = sp;
            save_full_config();
            break;
        }

        case CMD_SET_LIMITS: {
            if (len < 9) break;
            Axis    axis      = (Axis)p[0];
            int32_t min_steps = be32s(p + 1);
            int32_t max_steps = be32s(p + 5);
            mount.setLimits(axis, min_steps, max_steps);
            _cfg.limits_set[(int)axis] = true;
            _cfg.min_limit[(int)axis]  = min_steps;
            _cfg.max_limit[(int)axis]  = max_steps;
            save_full_config();
            break;
        }

        case CMD_FIND_LIMITS: {
            if (len < 1) break;
            Axis axis = (Axis)p[0];
            if (axis == AXIS_SLIDER && !_cfg.has_slider) break;  // no slider hardware
            if (axis == AXIS_ZOOM   &&  _cfg.lanc_zoom)  break;  // LANC zoom has no position motor
            // Byte 1: stall threshold (optional — persist so hub display uses same value)
            if (len >= 2 && p[1] > 0) {
                mount.setStallThreshold(axis, p[1]);
                _cfg.stall_threshold[(int)axis] = p[1];
                save_full_config();
            }
            mount.findLimits(axis, on_limits_found);
            break;
        }

        case CMD_FIND_HOME: {
            if (len < 1) break;
            Axis axis = (Axis)p[0];
            if (axis == AXIS_SLIDER && !_cfg.has_slider) break;  // no slider hardware
            if (axis == AXIS_ZOOM   &&  _cfg.lanc_zoom)  break;  // LANC zoom has no position motor
            // Byte 1: stall threshold (optional — persist so hub display uses same value)
            if (len >= 2 && p[1] > 0) {
                mount.setStallThreshold(axis, p[1]);
                _cfg.stall_threshold[(int)axis] = p[1];
                save_full_config();
            }
            mount.findHome(axis, on_home_complete);
            break;
        }

        case CMD_SET_STALL_THRESHOLD: {
            if (len < 2) break;
            Axis axis = (Axis)p[0];
            if ((int)axis < 0 || (int)axis >= 4) break;
            if (p[1] == 0) break;  // 0 is invalid — ignore
            mount.setStallThreshold(axis, p[1]);
            _cfg.stall_threshold[(int)axis] = p[1];
            save_full_config();
            // Echo the full config back so the PC dialog immediately
            // sees the confirmed saved value rather than waiting for the
            // next explicit CMD_GET_CONFIG.
            send_config_report();
            break;
        }

        case CMD_SET_ORIENTATION: {
            if (len < 1) break;
            bool pan_inv      = p[0] & 0x01;
            bool slider_inv   = p[0] & 0x02;
            bool has_slider   = p[0] & 0x04;
            bool zoom_inv     = p[0] & 0x08;
            bool lanc_zoom    = p[0] & 0x10;
            bool tilt_inv     = p[0] & 0x20;
            bool look_at_mode     = p[0] & 0x40;
            // Optional int16 in tenths of a degree.  A sender that omits it
            // leaves the stored tilt ALONE rather than levelling the rail: the
            // hub display sends this packet with the flags byte only, and an
            // Apply from the display must not silently undo the rail geometry.
            if (len >= 3) {
                int16_t t10 = (int16_t)((p[1] << 8) | p[2]);
                _cfg.slider_tilt_deg = (float)t10 / 10.0f;
                mount.setSliderTiltDeg(_cfg.slider_tilt_deg);
            }
            bool look_at_changed  = (look_at_mode != _cfg.look_at_mode);
            bool old_zoom_inv     = _cfg.zoom_invert;
            mount.setOrientation(pan_inv, tilt_inv, slider_inv, zoom_inv);
            mount.setHasSlider(has_slider);
            mount.setLookAtMode(look_at_mode);
            _cfg.pan_invert    = pan_inv;
            _cfg.tilt_invert   = tilt_inv;
            _cfg.slider_invert = slider_inv;
            _cfg.has_slider    = has_slider;
            _cfg.zoom_invert   = zoom_inv;
            _cfg.lanc_zoom     = lanc_zoom;
            _cfg.look_at_mode  = look_at_mode;
            // Positions saved in look-at mode (subject-relative pan/tilt steps)
            // are meaningless in standard mode and vice versa — clear all slots
            // whenever the mode changes so stale positions can't be recalled.
            // Also clear subjects so they don't reappear if look-at is re-enabled,
            // stop any active look-at or goto move, and broadcast the empty subject
            // list so all clients (hub display, web app, PC app) update immediately.
            if (look_at_changed) {
                memset(_slot_pos, 0, sizeof(_slot_pos));
                _slot_occupied = 0;
                _slot_at       = 0;
                memset(_subjects, 0, sizeof(_subjects));
                // The SELECTED subject too.  stopAll() does not clear it, and
                // the id outliving the mode is exactly the staleness the slot
                // and subject wipes above exist to prevent.
                mount.clearLaSubject();
                mount.stopAll();
                send_subject_list();   // broadcast empty list before send_status()
            }
            // If zoom_invert changed, re-apply the limits with the correct sign.
            // EEPROM always stores (0, +travel); only the runtime motor frame flips.
            // No EEPROM write needed — the invert flag already captures the direction.
            if (zoom_inv != old_zoom_inv && _cfg.limits_set[AXIS_ZOOM]) {
                if (zoom_inv)
                    mount.setLimits((Axis)AXIS_ZOOM, -(int32_t)_cfg.max_limit[AXIS_ZOOM], 0);
                else
                    mount.setLimits((Axis)AXIS_ZOOM, 0, _cfg.max_limit[AXIS_ZOOM]);
            }
            if (lanc_zoom) lanc_ensure_init();
            if (!lanc_zoom && _lanc_speed != 0) {
                // Turning LANC off while it was running — send stop
                _lanc_speed = 0;
                lanc_send_zoom(0);
            }
            save_full_config();
            // Always broadcast status so clients update slot bitmasks and flags
            // (especially important when look_at_mode changed and slots cleared).
            send_status();
            break;
        }

        case CMD_E_STOP: {
            mount.emergencyStop();
            break;
        }

        case CMD_GET_STATUS: {
            send_status();
            break;
        }

        case CMD_GET_POSITION: {
            send_position();
            break;
        }

        case CMD_PING: {
            if (len < 4) break;
            uint32_t ts = be32(p);
            uint8_t  buf[PKT_BUF_SIZE + 4];
            uint16_t pong_len = build_pong(buf, THIS_MOUNT_ID, ++_tx_seq, ts);
            ESP_SERIAL.write(buf, pong_len);
            break;
        }

        case CMD_GET_STATE: {
            send_state_report();
            break;
        }

        case CMD_GET_CONFIG: {
            send_config_report();
            break;
        }

        case CMD_STORE_POS: {
            if (len < 1) break;
            uint8_t slot = p[0];
            if (slot >= NUM_SLOTS) break;
            MountStatusSnapshot s = mount.getStatus();
            // getStatus() returns raw motor (physical) step coordinates.
            // moveTo() treats its inputs as logical (user-facing) coordinates and
            // applies orientation internally — so we must store the logical value.
            // For inverted axes, logical = -physical (orientation is self-inverse).
            for (int i = 0; i < 4; i++) _slot_pos[slot][i] = s.pos[i];
            if (_cfg.pan_invert)    _slot_pos[slot][AXIS_PAN]    = -_slot_pos[slot][AXIS_PAN];
            if (_cfg.tilt_invert)   _slot_pos[slot][AXIS_TILT]   = -_slot_pos[slot][AXIS_TILT];
            if (_cfg.slider_invert) _slot_pos[slot][AXIS_SLIDER] = -_slot_pos[slot][AXIS_SLIDER];
            if (_cfg.lanc_zoom)     _slot_pos[slot][AXIS_ZOOM]   =  0;  // not repeatable
            _slot_occupied |= (1u << slot);
            // Immediately report updated bitmasks so PC grid reflects the new slot
            send_status();
            break;
        }

        case CMD_GOTO_SLOT: {
            if (len < 1) break;
            uint8_t slot      = p[0];
            // Use separate active presets per axis group so a slider set to
            // speed 2 never gets driven at the pan/tilt speed 4 during recall.
            // The PC may override both via optional bytes 1 and 2.
            uint8_t pt_preset = (len >= 2) ? p[1] : _active_pt_preset;
            uint8_t sl_preset = (len >= 3) ? p[2] : _active_sl_preset;
            // Optional axis mask (byte 3).  0x0F = all axes (default).
            // 0x04 = slider only — used in CV-tracking mode so pan/tilt
            // remain under CV control while the slider moves to position.
            uint8_t axis_mask = (len >= 4) ? p[3] : 0x0F;
            if (slot >= NUM_SLOTS || !(_slot_occupied & (1u << slot))) break;
            _target_slot = slot;
            if (axis_mask == 0x04) {
                // Slider-only: move slider via individual moveAsync so the
                // ongoing jogPanTilt() is not interrupted.
                mount.moveSliderTo(_slot_pos[slot][AXIS_SLIDER], sl_preset);
            } else {
                // When LANC zoom is active the zoom motor has no position to recall —
                // always pass 0 so the stepper does not attempt to move.
                int32_t zoom_tgt = _cfg.lanc_zoom ? 0 : _slot_pos[slot][AXIS_ZOOM];

                // Smooth mid-move retarget for non-slider mounts: if already executing
                // a GOTO_SLOT, use retargetTo() which re-profiles from the current
                // velocity rather than stopping first — avoids the jerk at the handover.
                if (mount.getState() == STATE_MOVING_TO_POS) {
                    mount.retargetTo(_slot_pos[slot][AXIS_PAN],  _slot_pos[slot][AXIS_TILT],
                                     _slot_pos[slot][AXIS_SLIDER], zoom_tgt, pt_preset, sl_preset);
                } else {
                    mount.moveTo(_slot_pos[slot][AXIS_PAN],  _slot_pos[slot][AXIS_TILT],
                                 _slot_pos[slot][AXIS_SLIDER], zoom_tgt, pt_preset, sl_preset);
                }
            }
            break;
        }

        case CMD_CLEAR_POS: {
            if (len < 1) break;
            uint8_t slot = p[0];
            if (slot >= NUM_SLOTS) break;
            _slot_occupied &= ~(1u << slot);
            _slot_at       &= ~(1u << slot);
            memset(_slot_pos[slot], 0, sizeof(_slot_pos[slot]));
            // Immediately broadcast updated bitmasks so all clients (PC, hub display)
            // reflect the cleared slot — mirrors what CMD_STORE_POS does.
            send_status();
            break;
        }

        case CMD_SET_ACTIVE_PRESET: {
            // 2 bytes: group (0=PT, 1=SL), preset (1-4)
            if (len < 2) break;
            uint8_t grp    = p[0];
            uint8_t preset = p[1];
            if (preset < 1 || preset > 4) break;
            if (grp == GROUP_PAN_TILT)         _active_pt_preset = preset;
            else if (grp == GROUP_SLIDER_ZOOM) _active_sl_preset = preset;
            break;
        }

        case CMD_SAVE_SPEEDS: {
            // 72 bytes: 4 PT + 4 SL + 1 ZM presets (see protocol.h)
            if (len < SAVE_SPEEDS_PAYLOAD_LEN) break;
            for (int i = 0; i < 4; i++) {
                SpeedPreset sp;
                sp.max_speed    = be32(p + i * 8 + 0);
                sp.acceleration = be32(p + i * 8 + 4);
                mount.setSpeedPreset(GROUP_PAN_TILT, i + 1, sp);
                _cfg.pt_presets[i + 1] = sp;
            }
            for (int i = 0; i < 4; i++) {
                SpeedPreset sp;
                sp.max_speed    = be32(p + 32 + i * 8 + 0);
                sp.acceleration = be32(p + 32 + i * 8 + 4);
                mount.setSpeedPreset(GROUP_SLIDER_ZOOM, i + 1, sp);
                _cfg.sl_presets[i + 1] = sp;
            }
            {
                SpeedPreset sp;
                sp.max_speed    = be32(p + 64);
                sp.acceleration = be32(p + 68);
                mount.setZoomPreset(sp);
                _cfg.zm_preset = sp;
            }
            save_full_config();
            // Echo confirmed values back so the PC dialog sees what was saved.
            send_config_report();
            break;
        }

        // ==================================================================
        // v2 — Look-at subject commands
        // ==================================================================

        case CMD_GET_SUBJECTS: {
            send_subject_list();
            break;
        }

        case CMD_DELETE_SUBJECT: {
            if (len < 1) break;
            uint8_t id = p[0] & 0x07;
            memset(&_subjects[id], 0, sizeof(SubjectRecord));
            _subjects[id].valid = false;
            _slot_occupied &= ~(1u << id);
            send_subject_list();
            send_status();
            break;
        }

        // ------------------------------------------------------------------
        // CMD_ADD_SUBJECT_START — begin 2-point calibration.
        //   payload: subject_id(1) + name(16) = 17 bytes
        //
        // Operator has already positioned the slider at one end and aimed
        // the camera at the subject.  We record observation A immediately at
        // the current position, then automatically move the slider to the
        // opposite limit so the operator can record observation B.
        // ------------------------------------------------------------------
        case CMD_ADD_SUBJECT_START: {
            if (len < 17) break;
            // A calibration without a session reference cannot be right, and
            // fails SILENTLY: the two observations are measured from wherever
            // the head happened to sit when the Teensy last booted, so the solve
            // succeeds, reports SOLVED, stores a subject, and points nowhere.
            //
            // The reference is RAM-only and a flash clears it, which is exactly
            // when someone is most likely to recalibrate.  On 2026-08-24 that
            // put two subjects nearly 3 m above the rail — the same rig had
            // solved to 1.3 m BELOW it with a reference set.
            //
            // startLookAtMove() and aimAtSubject() have always refused without
            // one.  Refusing to CREATE what they will not use closes the gap
            // between "stored" and "usable".
            if (!mount.isRefSet()) {
                Serial.println("[Calib] REFUSED — no session reference; run Set Ref first");
                uint8_t nack_buf[PKT_BUF_SIZE];
                uint16_t nack_len = build_nack(nack_buf, THIS_MOUNT_ID, ++_tx_seq,
                                               pkt.seq, NACK_NO_REF);
                ESP_SERIAL.write(nack_buf, nack_len);
                break;
            }
            if (_calib_state != CalibState::IDLE) {
                _calib_state = CalibState::IDLE;  // abort any previous attempt
            }
            _calib_dest_phys = 0;

            _calib_subject_id = p[0] & 0x07;
            memcpy(_calib_name, p + 1, SUBJECT_NAME_LEN);
            _calib_name[SUBJECT_NAME_LEN - 1] = '\0';

            // Record observation A at the current position (operator has aimed).
            // Store LOGICAL steps (orientation inversion applied) so the calibration
            // solver's angle formula (angle = steps * dps + ref) is consistent with
            // getTiltDeg() / getPanDeg() which also use logical steps.
            _calib_xa_mm        = mount.getSliderMm();
            _calib_pan_steps_A  = _cfg.pan_invert  ? -mount.getPosition(AXIS_PAN)  : mount.getPosition(AXIS_PAN);
            _calib_tilt_steps_A = _cfg.tilt_invert ? -mount.getPosition(AXIS_TILT) : mount.getPosition(AXIS_TILT);

            Serial.printf("[Calib] START  subject=%d  name='%s'  xa=%.1fmm  pan=%ld  tilt=%ld\n",
                          (int)_calib_subject_id, _calib_name,
                          _calib_xa_mm, (long)_calib_pan_steps_A, (long)_calib_tilt_steps_A);

            // Move slider to whichever limit is FARTHEST from the current position.
            // This maximises the triangulation baseline regardless of where the
            // operator started, and avoids the edge case where the midpoint heuristic
            // picks the same end the slider is already near.
            if (mount.hasLimits(AXIS_SLIDER)) {
                // Limits and getPosition() are in PHYSICAL (motor) space.
                // Choose whichever physical end is farthest from current position.
                int32_t mn        = mount.getMinLimit(AXIS_SLIDER);
                int32_t mx        = mount.getMaxLimit(AXIS_SLIDER);
                int32_t cur       = mount.getPosition(AXIS_SLIDER);
                int32_t dist_min  = cur - mn;
                int32_t dist_max  = mx - cur;
                int32_t dest_phys = (dist_max >= dist_min) ? mx - 10 : mn + 10;

                // moveSliderTo() expects a LOGICAL position — it applies
                // _applyOrientationPos() internally.  Physical→logical is just a
                // sign flip when slider_invert is set.
                int32_t dest_log  = _cfg.slider_invert ? -dest_phys : dest_phys;

                Serial.printf("[Calib] slider: mn=%ld  mx=%ld  cur=%ld  dist_min=%ld  dist_max=%ld  dest_phys=%ld  dest_log=%ld  invert=%d\n",
                              (long)mn, (long)mx, (long)cur,
                              (long)dist_min, (long)dist_max,
                              (long)dest_phys, (long)dest_log, (int)_cfg.slider_invert);

                _calib_dest_phys = dest_phys;
                mount.moveSliderTo(dest_log, _active_sl_preset);
                _calib_state = CalibState::MOVING_TO_B;
                uint8_t b = (uint8_t)CALIB_MOVING_TO_B;
                send_packet(CMD_CALIB_PROMPT, &b, 1);
            } else {
                // Slider limits not found — cannot move to opposite end.
                // Send CALIB_ERROR so the PC shows a clear failure rather than
                // a misleading "Slider Arrived" with nowhere to have travelled.
                Serial.println("[Calib] ERROR — slider limits not set; run Find Limits first");
                _calib_state = CalibState::IDLE;
                uint8_t b = (uint8_t)CALIB_ERROR;
                send_packet(CMD_CALIB_PROMPT, &b, 1);
            }
            break;
        }

        // CMD_ADD_SUBJECT_SET_A is no longer used in the simplified flow
        // (obs A is recorded immediately in CMD_ADD_SUBJECT_START).
        // Accept and silently ignore so old clients don't cause issues.
        case CMD_ADD_SUBJECT_SET_A:
            break;

        // ------------------------------------------------------------------
        // CMD_ADD_SUBJECT_SET_B — record observation B, solve, save
        // ------------------------------------------------------------------
        case CMD_ADD_SUBJECT_SET_B: {
            if (_calib_state != CalibState::WAIT_B) break;

            // Store LOGICAL steps — same convention as observation A.
            _calib_xb_mm        = mount.getSliderMm();
            _calib_pan_steps_B  = _cfg.pan_invert  ? -mount.getPosition(AXIS_PAN)  : mount.getPosition(AXIS_PAN);
            _calib_tilt_steps_B = _cfg.tilt_invert ? -mount.getPosition(AXIS_TILT) : mount.getPosition(AXIS_TILT);

            Serial.printf("[Calib] SET_B  xb=%.1fmm  pan_steps=%ld  tilt_steps=%ld\n",
                          _calib_xb_mm, (long)_calib_pan_steps_B, (long)_calib_tilt_steps_B);
            Serial.printf("[Calib] using  dps_pan=%.8f  dps_tilt=%.8f  ref_pan=%.4f  ref_tilt=%.4f\n",
                          mount.getPanDegPerStep(), mount.getTiltDegPerStep(),
                          mount.getPanRefDeg(), mount.getTiltRefDeg());

            _calib_state = CalibState::IDLE;

            // ── Solve for 3D subject position ────────────────────────────
            float dps_p = mount.getPanDegPerStep();
            float dps_t = mount.getTiltDegPerStep();
            float ref_p = mount.getPanRefDeg();
            float ref_t = mount.getTiltRefDeg();

            float pan_A_deg  = (float)_calib_pan_steps_A  * dps_p + ref_p;
            float tilt_A_deg = (float)_calib_tilt_steps_A * dps_t + ref_t;
            float pan_B_deg  = (float)_calib_pan_steps_B  * dps_p + ref_p;
            float tilt_B_deg = (float)_calib_tilt_steps_B * dps_t + ref_t;

            // Warn if tilt observations are both near zero — this almost always
            // means the Manual Ref was set while the camera was already aimed at
            // the subject (tilted upward), making that direction read as 0°.
            // Check BEFORE any auto-reference override.
            if (fabsf(tilt_A_deg) < 3.0f && fabsf(tilt_B_deg) < 3.0f) {
                Serial.printf("[Calib] WARNING — tilt_A=%.2f° tilt_B=%.2f° are both near zero.\n",
                              tilt_A_deg, tilt_B_deg);
                Serial.println("[Calib]   Subject height (Y) will be computed as ~0.");
                Serial.println("[Calib]   If your subject is above/below the slider, the");
                Serial.println("[Calib]   Manual Ref was probably set while the camera was");
                Serial.println("[Calib]   already aimed at the subject. Set the ref with the");
                Serial.println("[Calib]   camera truly HORIZONTAL and PERPENDICULAR to the rail.");
            }

            // If the stored reference is zero (or otherwise wrong), the motor's
            // accumulated step count maps to a pan angle outside ±90°, making
            // one ray point backward and the solve fail.  Detect this and compute
            // a self-consistent auto-reference from the midpoint between the two
            // observations: this centres both angles symmetrically around 0° so
            // both rays point forward regardless of where the motor home is.
            // A small residual error (≈ subject X offset from slider midpoint /
            // depth) remains, but the solve is still usable for storing the subject.
            if (fabsf(pan_A_deg) > 85.0f || fabsf(pan_B_deg) > 85.0f) {
                float mid_p = ((float)_calib_pan_steps_A  + (float)_calib_pan_steps_B)  * 0.5f;
                float mid_t = ((float)_calib_tilt_steps_A + (float)_calib_tilt_steps_B) * 0.5f;
                float ref_p_auto = -mid_p * dps_p;
                float ref_t_auto = -mid_t * dps_t;
                pan_A_deg  = (float)_calib_pan_steps_A  * dps_p + ref_p_auto;
                tilt_A_deg = (float)_calib_tilt_steps_A * dps_t + ref_t_auto;
                pan_B_deg  = (float)_calib_pan_steps_B  * dps_p + ref_p_auto;
                tilt_B_deg = (float)_calib_tilt_steps_B * dps_t + ref_t_auto;
                Serial.printf("[Calib] auto-ref: ref_p=%.3f ref_t=%.3f"
                              "  pan_A=%.2f pan_B=%.2f (stored ref was zero/stale)\n",
                              ref_p_auto, ref_t_auto, pan_A_deg, pan_B_deg);
            }

            float sx, sy, sz;
            bool ok = solve_subject_3d(_calib_xa_mm, _calib_xb_mm,
                                        pan_A_deg, tilt_A_deg,
                                        pan_B_deg, tilt_B_deg,
                                        &sx, &sy, &sz);

            if (!ok) {
                Serial.println("[Calib] SOLVE FAILED — parallel rays (denom~0)");
                uint8_t b = (uint8_t)CALIB_ERROR;
                send_packet(CMD_CALIB_PROMPT, &b, 1);
                break;
            }
            if (sz < -200.0f) {
                Serial.printf("[Calib] SOLVE FAILED — sz=%.1fmm (subject far behind slider)\n", sz);
                uint8_t b = (uint8_t)CALIB_ERROR;
                send_packet(CMD_CALIB_PROMPT, &b, 1);
                break;
            }

            // sz is between -200 mm and 10 mm: subject is very close to the
            // slider rail (nearly beside it).  The geometry is near-degenerate
            // but usable — clamp to a small positive depth so the look-at maths
            // can compute valid angles.  Skip dps refinement in this case
            // because the clamped sz makes the computed true angles unreliable.
            bool sz_clamped = false;
            if (sz < 10.0f) {
                Serial.printf("[Calib] NOTE — sz=%.1fmm (subject very close to rail) — clamped to 10mm\n", sz);
                Serial.println("[Calib]       Tracking angles will be extreme near this end of slider.");
                sz = 10.0f;
                sz_clamped = true;
            }

            Serial.printf("[Calib] SOLVED  subject=(%d) '%s'  x=%.1f  y=%.1f  z=%.1f mm\n",
                          (int)_calib_subject_id, _calib_name, sx, sy, sz);

            // Warn if sy is suspiciously small — means tilt wasn't aimed at the
            // subject during calibration, so tilt tracking will be flat/wrong.
            if (fabsf(sy) < 50.0f) {
                Serial.printf("[Calib] WARNING — sy=%.1fmm is nearly zero.\n", sy);
                Serial.println("[Calib]   Tilt will not track correctly.");
                Serial.println("[Calib]   Re-calibrate: aim tilt UP/DOWN at the subject");
                Serial.println("[Calib]   from BOTH slider positions.");
            }

            // ── Refine deg/step from the geometry ───────────────────────
            // The solved 3D position lets us compute exactly what the pan angle
            // should have been at A and B, giving a refined deg/step.
            // Skip if sz was clamped — the clamped z would produce unreliable angles.
            float pan_A_true  = look_at_pan_deg(_calib_xa_mm, sx, sz);
            float pan_B_true  = look_at_pan_deg(_calib_xb_mm, sx, sz);
            float tilt_A_true = look_at_tilt_deg(_calib_xa_mm, sx, sy, sz);
            float tilt_B_true = look_at_tilt_deg(_calib_xb_mm, sx, sy, sz);

            int32_t pan_step_diff  = _calib_pan_steps_B  - _calib_pan_steps_A;
            int32_t tilt_step_diff = _calib_tilt_steps_B - _calib_tilt_steps_A;

            // Clamp refined dps to ±25% of nominal — a bad solve could produce
            // a wildly wrong value that poisons the next calibration attempt.
            const float pan_dps_lo  = NOMINAL_PAN_DEG_PER_STEP  * 0.75f;
            const float pan_dps_hi  = NOMINAL_PAN_DEG_PER_STEP  * 1.25f;
            const float tilt_dps_lo = NOMINAL_TILT_DEG_PER_STEP * 0.75f;
            const float tilt_dps_hi = NOMINAL_TILT_DEG_PER_STEP * 1.25f;

            // Minimum angle-variation guard: if the true angle difference across
            // the slider travel is less than 3°, the dps estimate is unreliable
            // (tiny step count magnified by floating-point noise).  Specifically,
            // a near-horizontal subject gives <0.1° tilt variation — accepting
            // that dps would corrupt all subsequent tilt tracking.  Skip refinement
            // and keep the current (nominal or previously-refined) value.
            Serial.printf("[Calib] angle variation: pan=%.3f°  tilt=%.3f°"
                          "  step_diff: pan=%ld  tilt=%ld\n",
                          fabsf(pan_B_true - pan_A_true), fabsf(tilt_B_true - tilt_A_true),
                          (long)pan_step_diff, (long)tilt_step_diff);

            if (!sz_clamped && abs(pan_step_diff) > 100) {
                if (fabsf(pan_B_true - pan_A_true) >= 3.0f) {
                    float new_dps = (pan_B_true - pan_A_true) / (float)pan_step_diff;
                    if (new_dps >= pan_dps_lo && new_dps <= pan_dps_hi) {
                        _cfg.pan_deg_per_step = new_dps;
                        mount.setDegPerStep(new_dps, mount.getTiltDegPerStep());
                        Serial.printf("[Calib] Refined pan_deg_per_step=%.8f\n", new_dps);
                    } else {
                        Serial.printf("[Calib] pan_dps=%.8f out of range [%.8f, %.8f] — not saved\n",
                                      new_dps, pan_dps_lo, pan_dps_hi);
                    }
                } else {
                    Serial.printf("[Calib] pan angle variation %.3f° < 3° — dps refinement skipped"
                                  " (keeping %.8f)\n",
                                  fabsf(pan_B_true - pan_A_true), mount.getPanDegPerStep());
                }
            }
            if (!sz_clamped && abs(tilt_step_diff) > 100) {
                if (fabsf(tilt_B_true - tilt_A_true) >= 3.0f) {
                    float new_dps = (tilt_B_true - tilt_A_true) / (float)tilt_step_diff;
                    if (new_dps >= tilt_dps_lo && new_dps <= tilt_dps_hi) {
                        _cfg.tilt_deg_per_step = new_dps;
                        mount.setDegPerStep(mount.getPanDegPerStep(), new_dps);
                        Serial.printf("[Calib] Refined tilt_deg_per_step=%.8f\n", new_dps);
                    } else {
                        Serial.printf("[Calib] tilt_dps=%.8f out of range [%.8f, %.8f] — not saved\n",
                                      new_dps, tilt_dps_lo, tilt_dps_hi);
                    }
                } else {
                    Serial.printf("[Calib] tilt angle variation %.3f° < 3° — dps refinement skipped"
                                  " (keeping %.8f)\n",
                                  fabsf(tilt_B_true - tilt_A_true), mount.getTiltDegPerStep());
                }
            }

            // ── Auto-recalibrate look-at reference from SET_B position ─────
            // The solved geometry tells us exactly what pan/tilt angle the mount
            // *should* be pointing at when the motors are at the SET_B step counts.
            // Setting the reference from this known-good position is more accurate
            // than the manual 0/0 set at power-up, and ensures the look-at formula
            // predicts the correct motor target everywhere along the slider travel.
            // All subjects share this one global reference, so recalibrating it
            // here improves accuracy for any subject calibrated in the same session.
            {
                float pan_ref_new  = pan_B_true  - (float)_calib_pan_steps_B  * mount.getPanDegPerStep();
                float tilt_ref_new = tilt_B_true - (float)_calib_tilt_steps_B * mount.getTiltDegPerStep();
                mount.setLookAtRef(pan_ref_new, tilt_ref_new);
                Serial.printf("[Calib] Auto-ref from SET_B: pan_ref=%.4f°  tilt_ref=%.4f°\n",
                              pan_ref_new, tilt_ref_new);
            }

            // ── Store subject in RAM (volatile — not persisted to EEPROM) ──
            SubjectRecord &rec = _subjects[_calib_subject_id];
            rec.valid = true;
            rec.x = sx;  rec.y = sy;  rec.z = sz;
            memcpy(rec.name, _calib_name, SUBJECT_NAME_LEN);
            _slot_occupied |= (1u << _calib_subject_id);

            Serial.printf("[Calib] Subject %d stored  (%.1f, %.1f, %.1f)mm"
                          "  ref (auto-updated): pan=%.4f°  tilt=%.4f°\n",
                          (int)_calib_subject_id, sx, sy, sz,
                          mount.getPanRefDeg(), mount.getTiltRefDeg());

            // Notify all clients
            uint8_t bsolved = (uint8_t)CALIB_SOLVED;
            send_packet(CMD_CALIB_PROMPT, &bsolved, 1);
            send_subject_list();
            send_status();

            // Auto-select the newly stored subject so all UIs (web app, hub
            // display, PC app) immediately show a green border without the
            // operator having to press the button again.
            mount.setLookAtSubject(sx, sy, sz, _calib_subject_id);
            send_look_at_status();
            break;
        }

        case CMD_ADD_SUBJECT_ABORT: {
            _calib_state = CalibState::IDLE;
            mount.stopAll();
            Serial.println("[Calib] ABORTED");
            break;
        }

        // ------------------------------------------------------------------
        // CMD_SET_REF — set pan/tilt session reference using a known subject
        //   payload: subject_id(1)
        //   subject_id = 0xFF → set reference to 0/0 at current position
        // ------------------------------------------------------------------
        case CMD_SET_REF: {
            if (len < 1) break;
            uint8_t subj_id = p[0];
            float pan_ref, tilt_ref;

            if (subj_id == 0xFF) {
                // Manual reference: assume current position = 0°/0°.
                // The angle formula everywhere is:  angle = logical_steps * dps + ref
                // so ref = -(logical_steps * dps).  Physical and logical differ by sign
                // when the axis is inverted, so we MUST apply the inversion flag here —
                // using raw physical steps gives the wrong sign on inverted axes, which
                // corrupts every subsequent observation angle and breaks subject tracking.
                int32_t pan_phys  = mount.getPosition(AXIS_PAN);
                int32_t tilt_phys = mount.getPosition(AXIS_TILT);
                int32_t pan_log   = _cfg.pan_invert  ? -pan_phys  : pan_phys;
                int32_t tilt_log  = _cfg.tilt_invert ? -tilt_phys : tilt_phys;
                pan_ref  = -(float)pan_log  * mount.getPanDegPerStep();
                tilt_ref = -(float)tilt_log * mount.getTiltDegPerStep();
                Serial.printf("[SetRef] Manual 0/0 — pan_phys=%ld (log=%ld)  tilt_phys=%ld (log=%ld)"
                              "  pan_inv=%d tilt_inv=%d\n",
                              (long)pan_phys, (long)pan_log,
                              (long)tilt_phys, (long)tilt_log,
                              (int)_cfg.pan_invert, (int)_cfg.tilt_invert);
                Serial.printf("[SetRef]   → pan_ref=%.4f°  tilt_ref=%.4f°\n",
                              pan_ref, tilt_ref);
            } else if (subj_id < MAX_SUBJECTS && _subjects[subj_id].valid) {
                // Compute expected pan/tilt at current slider position for this subject
                const SubjectRecord &r = _subjects[subj_id];
                float cx        = mount.getSliderMm();
                float exp_pan   = look_at_pan_deg(cx, r.x, r.z);
                float exp_tilt  = look_at_tilt_deg(cx, r.x, r.y, r.z);

                // Map: current_steps * deg_per_step + ref = expected_angle
                // Convert physical step counts to logical (user-facing) space by
                // applying inversion flags — same as the calibration solver does.
                int32_t pan_steps  = mount.getPosition(AXIS_PAN);
                int32_t tilt_steps = mount.getPosition(AXIS_TILT);
                if (_cfg.pan_invert)  pan_steps  = -pan_steps;
                if (_cfg.tilt_invert) tilt_steps = -tilt_steps;  // was missing — bug fix

                pan_ref  = exp_pan  - (float)pan_steps  * mount.getPanDegPerStep();
                tilt_ref = exp_tilt - (float)tilt_steps * mount.getTiltDegPerStep();

                Serial.printf("[SetRef] Subject %d '%s'  cx=%.1fmm  pan=%.2f°  tilt=%.2f°"
                              "  → pan_ref=%.4f°  tilt_ref=%.4f°\n",
                              (int)subj_id, r.name, cx, exp_pan, exp_tilt, pan_ref, tilt_ref);
            } else {
                // Invalid subject
                uint8_t nack_buf[PKT_BUF_SIZE];
                uint16_t nack_len = build_nack(nack_buf, THIS_MOUNT_ID, ++_tx_seq,
                                               pkt.seq, NACK_INVALID_PARAM);
                ESP_SERIAL.write(nack_buf, nack_len);
                break;
            }

            mount.setLookAtRef(pan_ref, tilt_ref);

            // Echo confirmed reference angles
            uint8_t ref_payload[8];
            write_be_float(ref_payload + 0, pan_ref);
            write_be_float(ref_payload + 4, tilt_ref);
            send_packet(CMD_REF_CONFIRMED, ref_payload, 8);
            break;
        }

        // ------------------------------------------------------------------
        // CMD_SET_SLIDER_MOVE — store a slider move in a slot
        //   payload: slot(1) + start_mm(4f) + end_mm(4f) + preset(1) = 10B
        // ------------------------------------------------------------------
        case CMD_SET_SLIDER_MOVE: {
            if (len < 10) break;
            uint8_t slot     = p[0] & 0x07;
            float start_mm   = be_float(p + 1);
            float end_mm     = be_float(p + 5);
            uint8_t preset   = constrain((int)p[9], 1, 4);

            _slider_moves[slot].start_mm     = start_mm;
            _slider_moves[slot].end_mm       = end_mm;
            _slider_moves[slot].speed_preset = preset;
            _slider_moves[slot].valid        = true;
            Serial.printf("[SliderMove] slot=%d  %.1f → %.1f mm  preset=%d\n",
                          (int)slot, start_mm, end_mm, (int)preset);
            break;
        }

        // ------------------------------------------------------------------
        // CMD_START_LOOK_AT_MOVE — start tracking move
        //   payload: subject_id(1) + direction(1) + speed_preset(1) = 3B
        //   direction: 0 = go to min (left), 1 = go to max (right)
        // ------------------------------------------------------------------
        case CMD_START_LOOK_AT_MOVE: {
            if (len < 3) break;
            uint8_t subj_id   = p[0] & 0x07;
            uint8_t direction = p[1];          // 0=min/left, 1=max/right
            uint8_t preset    = constrain(p[2], 1, 4);

            if (!_subjects[subj_id].valid || !mount.isRefSet() ||
                !mount.hasLimits(AXIS_SLIDER)) {
                uint8_t nack_buf[PKT_BUF_SIZE];
                uint16_t nack_len = build_nack(nack_buf, THIS_MOUNT_ID, ++_tx_seq,
                                               pkt.seq, NACK_INVALID_PARAM);
                ESP_SERIAL.write(nack_buf, nack_len);
                break;
            }

            // Compute logical start (current) and logical end (far or near limit).
            // Limits are stored in physical steps; startLookAtMove() expects logical.
            //
            // When slider_invert=true the physical rail is flipped: physical min (0/home)
            // is at the RIGHT end and physical max is at the LEFT end.  The PC sends
            // direction=0 meaning "go left" and direction=1 meaning "go right" — so
            // with inversion we must swap which physical limit we target.
            int32_t cur_phys  = mount.getPosition(AXIS_SLIDER);
            bool go_to_phys_max = (_cfg.slider_invert) ? (direction == 0) : (direction == 1);
            int32_t phys_dest   = go_to_phys_max ? mount.getMaxLimit(AXIS_SLIDER)
                                                  : mount.getMinLimit(AXIS_SLIDER);
            int32_t cur_log   = _cfg.slider_invert ? -cur_phys  : cur_phys;
            int32_t dest_log  = _cfg.slider_invert ? -phys_dest : phys_dest;

            const SubjectRecord &r = _subjects[subj_id];
            mount.setLookAtSubject(r.x, r.y, r.z, subj_id);

            Serial.printf("[LookAt] CMD  subject=%d '%s'  dir=%s  invert=%d\n",
                          (int)subj_id, r.name, direction ? "RIGHT" : "LEFT",
                          (int)_cfg.slider_invert);
            Serial.printf("[LookAt]      subj=(%.1f, %.1f, %.1f)mm\n", r.x, r.y, r.z);
            Serial.printf("[LookAt]      cur_phys=%ld  phys_dest=%ld  cur_log=%ld  dest_log=%ld\n",
                          (long)cur_phys, (long)phys_dest, (long)cur_log, (long)dest_log);

            _la_last_direction = (int8_t)direction;   // remember which end we're heading to
            bool started = mount.startLookAtMove(cur_log, dest_log, preset, 90.0f);
            if (!started) {
                Serial.printf("[LookAt] FAILED to start (ref_set=%d limits_set=%d)\n",
                              (int)mount.isRefSet(), (int)mount.hasLimits(AXIS_SLIDER));
                // Name the actual cause.  This reported BUSY whatever the
                // reason, and "busy" sends someone looking for a move in
                // progress when the mount is sitting still waiting for a
                // reference it was never given.
                uint8_t nack_buf[PKT_BUF_SIZE];
                uint16_t nack_len = build_nack(nack_buf, THIS_MOUNT_ID, ++_tx_seq,
                                               pkt.seq,
                                               mount.isRefSet() ? NACK_BUSY
                                                                : NACK_NO_REF);
                ESP_SERIAL.write(nack_buf, nack_len);
            } else {
                // Report which arrow is running, from the mount's own state.
                // Only on success: a client must never see an arrow lit for a
                // move that did not start — that is the whole point of moving
                // this off CMD_LA_MOVE_DIR, which the hub injects when it
                // relays the command and so cannot know whether it arrived.
                _target_slot = direction ? TARGET_SLOT_LA_MAX : TARGET_SLOT_LA_MIN;
                Serial.printf("[LookAt] START OK  preset=%d  max_pt_dps=90\n", (int)preset);
            }
            break;
        }

        // ------------------------------------------------------------------
        // CMD_SWITCH_SUBJECT — change tracking target (mid-move or static aim)
        // ------------------------------------------------------------------
        case CMD_SWITCH_SUBJECT: {
            if (len < 1) break;
            uint8_t subj_id = p[0] & 0x07;
            if (!_subjects[subj_id].valid) break;
            const SubjectRecord &r = _subjects[subj_id];
            mount.setLookAtSubject(r.x, r.y, r.z, subj_id);
            if (mount.getState() == STATE_LOOK_AT_MOVE ||
                mount.getState() == STATE_LOOK_AT_PRE_AIM) {
                // Already in look-at sequence — new subject will be picked up
                // automatically on the next controller tick.
                Serial.printf("[LookAt] SWITCH (mid-move) to subject=%d '%s'\n",
                              (int)subj_id, r.name);
            } else {
                // Slider not moving — aim pan/tilt at the subject from current position.
                // Use the active PT preset so the move speed matches what the user
                // has selected (not a hardcoded slow preset).
                mount.aimAtSubject(_active_pt_preset);
                Serial.printf("[LookAt] AIM (static) at subject=%d '%s'\n",
                              (int)subj_id, r.name);
            }
            // Broadcast new active subject so web app, hub display, and PC app
            // all stay in sync regardless of which device sent the switch command.
            send_look_at_status();
            break;
        }

        default: {
            // NACK — unknown command
            uint8_t nack_buf[PKT_BUF_SIZE];
            uint16_t nack_len = build_nack(nack_buf, THIS_MOUNT_ID, ++_tx_seq,
                                           pkt.seq, NACK_UNKNOWN_CMD);
            ESP_SERIAL.write(nack_buf, nack_len);
            break;
        }
    }
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

void setup() {
    Serial.begin(115200);
    // Wait up to 2 s for the USB Serial Monitor to connect so boot output isn't lost.
    // The loop exits early once a host attaches; on bench power-only it times out cleanly.
    uint32_t t0 = millis();
    while (!Serial && (millis() - t0) < 2000) {}

    Serial.println("\n=== Teensy 4.1 Mount ===");
    Serial.printf("Mount ID: %d\n", THIS_MOUNT_ID);

    ESP_SERIAL.begin(ESP_SERIAL_BAUD);
    pkt_parser_init(&_parser);

    mount.begin();
    mount.printDriverDiagnostics();   // read back TMC2209 registers immediately after config

    eeprom_load();

    // _subjects[] is a separate volatile RAM array (not in EepromConfig) so it
    // starts zeroed automatically — no explicit clear needed here.

    Serial.println("--- LANC config ---");
    Serial.printf("  lanc_zoom  : %s\n", _cfg.lanc_zoom  ? "ENABLED" : "DISABLED");
    Serial.printf("  zoom_invert: %s\n", _cfg.zoom_invert ? "yes"    : "no");
    Serial.println("  cmds: 'l'=toggle LANC  's'=status  other=motor diagnostics");
    Serial.println("-------------------");
}

// ---------------------------------------------------------------------------
// Loop
// ---------------------------------------------------------------------------

void loop() {
    mount.update();

    // Parse incoming packets from ESP32
    while (ESP_SERIAL.available()) {
        uint8_t byte = ESP_SERIAL.read();
        ParsedPacket pkt;
        if (pkt_feed(&_parser, byte, &pkt)) {
            // Accept every packet: the UART is point-to-point and the ESP32
            // bridge only forwards packets addressed to this mount (or
            // broadcast), so anything arriving here is by definition ours.
            // Filtering on a compiled-in ID only created a silent failure
            // mode when it disagreed with the bridge's runtime ID.
            dispatch(pkt);
        }
    }

    // AT_POSITION detection — update every loop (cheap bitmask compare)
    update_slot_at_mask();

    // Clear target slot when the GOTO_SLOT move has finished so all clients
    // stop flashing.  Two conditions: state left MOVING_TO_POS, or the mount
    // is now AT the target slot.
    {
        MountStatusSnapshot _s = mount.getStatus();
        uint8_t cur_state = (uint8_t)_s.state;
        if (_target_slot != TARGET_SLOT_NONE) {
            // 8 and 9 mean the look-at arrows ONLY in look-at mode.  On any
            // other mount they are ordinary position slots 9 and 10, and must
            // keep using the GOTO path below — without this gate a normal
            // recall to slot 9 or 10 would be cleared by the wrong rule.
            bool la_arrow = _cfg.look_at_mode &&
                            (_target_slot == TARGET_SLOT_LA_MIN ||
                             _target_slot == TARGET_SLOT_LA_MAX);
            if (la_arrow) {
                // Finished when the look-at controller gives up ownership,
                // whether that is arrival, an E-stop or an abort — so the
                // arrow cannot be left lit by any of them.
                bool la_running = (cur_state == STATE_LOOK_AT_MOVE ||
                                   cur_state == STATE_LOOK_AT_PRE_AIM);
                if (!la_running) _target_slot = TARGET_SLOT_NONE;
            } else {
                bool move_done = (_prev_motion_state == STATE_MOVING_TO_POS &&
                                  cur_state          != STATE_MOVING_TO_POS);
                bool arrived   = !!(_slot_at & (1u << _target_slot));
                if (move_done || arrived) _target_slot = TARGET_SLOT_NONE;
            }
        }
        _prev_motion_state = cur_state;
    }

    // LANC: read device responses / handle handshake; watchdog stops zoom if
    // no jog arrives within LANC_JOG_WATCHDOG_MS (mirrors motor jog watchdog).
    if (_cfg.lanc_zoom) {
        lanc_service();

        // Watchdog: stop if no jog received recently
        if (_lanc_speed != 0 &&
            (millis() - _lanc_jog_last_ms) > LANC_JOG_WATCHDOG_MS) {
            Serial.println("[LANC] Watchdog: no jog — sending stop");
            _lanc_speed = 0;
            lanc_send_zoom(0);
        }

    }

    // v2 — Calibration state machine
    update_calib();

    // v2 — Look-at: report the END of a move.  There is deliberately no
    // periodic send during one.
    //
    // This used to broadcast a 14-byte LOOK_AT_STATUS every STATUS_INTERVAL_MS
    // for the whole move, and every consumer read one byte of it: payload[12],
    // the subject id.  The hub forwards only that byte to the display, the web
    // app reads only that byte and acts only when it changes, and the PC app
    // reads only la.subject_id.  Nothing reads slider_mm, pan_deg, tilt_deg or
    // flags — the run advancement that once did now lives on the mount, and the
    // panel that showed them as telemetry was deleted.
    //
    // That byte is already in the ordinary STATUS packet (active_la_subject),
    // which goes out unconditionally at the same interval, move or no move.  So
    // this was a second 10 Hz stream duplicating the first, and the hub set
    // ws_force on each one — forcing a WebSocket push to every browser ten times
    // a second for a value that had not changed.
    //
    // It is not the mount's liveness signal either: STATUS is ungated and every
    // presence check upstream refreshes on ANY packet, so removing this leaves
    // "I'm alive" exactly where it was, at 10 Hz.
    //
    // The subject id still propagates the instant it changes — selection, switch
    // and both manual-deselect paths each send one, and the hub intercepts
    // CMD_SWITCH_SUBJECT directly so the display never waits on this at all.
    {
        MountState cur_state = mount.getState();
        bool la_active = (cur_state == STATE_LOOK_AT_MOVE ||
                          cur_state == STATE_LOOK_AT_PRE_AIM);
        // Send once when the look-at sequence ends entirely
        static MountState _prev_state = STATE_IDLE;
        bool prev_la = (_prev_state == STATE_LOOK_AT_MOVE ||
                        _prev_state == STATE_LOOK_AT_PRE_AIM);
        if (prev_la && !la_active) {
            send_look_at_status();  // final update
            // Persist which end the slider arrived at so all clients can restore
            // the green arrow border after a reconnect or power cycle.
            if (_la_last_direction >= 0)
                mount.setLookAtEndFlag((uint8_t)_la_last_direction);
        }
        _prev_state = cur_state;
    }

    // Periodic status broadcast
    if (millis() - _last_status_ms >= STATUS_INTERVAL_MS) {
        _last_status_ms = millis();
        send_status();
    }


    // No unsolicited position broadcast.  CMD_POSITION is answered on request
    // (see CMD_GET_POSITION above) and nothing else.
    //
    // This used to run at 5 Hz while moving and 1 Hz at rest.  The bridge has
    // dropped it since 2026-08-12 unless a GET_POSITION arrived within the last
    // two seconds, so for most of that time it was 5 Hz of UART and bridge CPU
    // spent on frames that were parsed and discarded a few centimetres away.
    //
    // Sending only on request also makes the tap explicit: whatever wants
    // positions has to ask, and is therefore visible in the log as the thing
    // that opened it.  An unsolicited stream is a producer nobody is accountable
    // for.

    // ── Uniform health telemetry (teensy node) ──────────────────────────
    // 10 s cadence + anomaly sends (first report / low RAM / loop stall).
    // No jog deferral: this rides the point-to-point UART, not the radio,
    // and the bridge re-stamps + forwards it to the hub like any packet.
    {
        static uint32_t _prev_loop_ms   = 0;
        static uint16_t _loop_max_ms    = 0;
        static uint32_t _min_free_heap  = 0xFFFFFFFF;
        static uint32_t _health_last_ms = 0;
        static uint32_t _health_anom_ms = 0;
        static bool     _health_first   = false;

        uint32_t nowh = millis();
        if (_prev_loop_ms) {
            uint32_t gap = nowh - _prev_loop_ms;
            if (gap > _loop_max_ms)
                _loop_max_ms = (gap > 65535) ? 65535 : (uint16_t)gap;
        }
        _prev_loop_ms = nowh;

        // Teensy 4.1 free RAM2 heap: distance from the break to the heap end.
        extern char _heap_end[], *__brkval;
        uint32_t free_heap = (uint32_t)(_heap_end - __brkval);
        if (free_heap < _min_free_heap) _min_free_heap = free_heap;

        bool anomaly = (!_health_first && nowh > 3000) ||
                       (free_heap < HEALTH_LOW_HEAP_BYTES) ||
                       (_loop_max_ms > HEALTH_LOOP_STALL_MS);
        bool periodic = (nowh - _health_last_ms >= HEALTH_INTERVAL_MS);
        if ((anomaly && nowh - _health_anom_ms >= HEALTH_ANOMALY_GAP_MS) || periodic) {
            PayloadHealth h = {};
            h.node_type     = HEALTH_NODE_TEENSY;
            h.reset_reason  = (uint8_t)(SRC_SRSR & 0xFF);   // imxrt reset status
            h.uptime_s      = nowh / 1000UL;
            h.free_heap     = free_heap;
            h.min_free_heap = _min_free_heap;
            h.loop_max_ms   = _loop_max_ms;
            h.tx_fail       = 0;
            h.rssi          = 0;
            h.flags         = (anomaly && !periodic) ? 0x01 : 0x00;
            h.node_u32      = 0;
            uint8_t p[24];
            encode_health_payload(p, &h);
            send_packet(CMD_HEALTH, p, 24);
            _health_last_ms = nowh;
            _loop_max_ms    = 0;
            _health_first   = true;
            if (anomaly) _health_anom_ms = nowh;
        }
    }

    // Heartbeat suppressed — re-enable for debugging by uncommenting below
    // static uint32_t _last_hb_ms = 0;
    // if (millis() - _last_hb_ms >= 3000) {
    //     _last_hb_ms = millis();
    //     Serial.printf("[alive] t=%lus  LANC:%s  conn:%s  speed:%d\n",
    //                   millis() / 1000,
    //                   _cfg.lanc_zoom  ? "ON"  : "OFF",
    //                   _lanc_connected ? "YES" : "NO",
    //                   (int)_lanc_speed);
    // }

    // Serial Monitor commands:
    //   'l' / 'L'  — toggle LANC zoom on/off (for bench testing without ESP32)
    //   's' / 'S'  — print LANC status
    //   'z'        — test zoom IN  at speed 7 (fast)
    //   'x'        — test zoom OUT at speed 7 (fast)
    //   'c'        — test zoom STOP
    //   any other  — re-read TMC2209 registers on demand
    if (Serial.available() > 0) {
        char cmd = (char)Serial.read();
        while (Serial.available()) Serial.read();  // flush remainder

        if (cmd == 's' || cmd == 'S') {
            // Print current LANC status on demand
            Serial.println("--- LANC status ---");
            Serial.printf("  lanc_zoom    : %s\n", _cfg.lanc_zoom       ? "ENABLED"  : "DISABLED");
            Serial.printf("  serial_init  : %s\n", _lanc_serial_init    ? "yes"      : "no");
            Serial.printf("  zoom_invert  : %s\n", _cfg.zoom_invert     ? "yes"      : "no");
            Serial.printf("  connected    : %s\n", _lanc_connected ? "YES" : "NO");
            Serial.printf("  current_speed: %d\n", (int)_lanc_speed);
            Serial.println("  cmds: 'l'=toggle LANC  's'=status  'z'=zoom in  'x'=zoom out  'c'=stop");
            Serial.println("-------------------");
        } else if (cmd == 'z' || cmd == 'x' || cmd == 'c') {
            // Direct zoom test — bypasses jog pipeline so you can verify the
            // camera responds to commands without needing the ESP32 connected.
            if (!_cfg.lanc_zoom) {
                Serial.println("[LANC] zoom test ignored — LANC not enabled (press 'l' first)");
            } else if (!_lanc_connected) {
                Serial.println("[LANC] zoom test ignored — camera not connected yet");
            } else {
                int8_t test_spd = (cmd == 'z') ? 7 : (cmd == 'x') ? -7 : 0;
                _lanc_speed = test_spd;
                lanc_send_zoom(test_spd);
                if (test_spd != 0) _lanc_jog_last_ms = millis();
                Serial.printf("[LANC] zoom test: speed=%d (%s)\n",
                              test_spd,
                              cmd == 'z' ? "IN" : cmd == 'x' ? "OUT" : "STOP");
            }
        } else if (cmd == 'l' || cmd == 'L') {
            _cfg.lanc_zoom = !_cfg.lanc_zoom;
            Serial.printf("[LANC] Toggled → %s\n",
                          _cfg.lanc_zoom ? "ENABLED" : "DISABLED");
            if (_cfg.lanc_zoom) {
                lanc_ensure_init();
                Serial.println("[LANC] Listening on Serial7 — waiting for camera...");
            } else {
                // Stop any running zoom before disabling
                if (_lanc_speed != 0) {
                    _lanc_speed = 0;
                    lanc_send_zoom(0);
                }
                Serial.println("[LANC] Disabled — Serial7 still open but service paused");
            }
        } else {
            mount.printDriverDiagnostics();
        }
    }
}
