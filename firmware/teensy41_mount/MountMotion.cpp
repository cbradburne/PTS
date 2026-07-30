/*
 * MountMotion.cpp — Teensy 4.1 implementation
 *
 * Pin assignments — Teensy 4.1 PCB:
 *
 *   Axis    | STEP | DIR | DIAG | Serial | TX  | RX
 *   --------|------|-----|------|--------|-----|----
 *   PAN     |  23  |  22 |   3  |   5    |  20 |  21
 *   TILT    |  19  |  18 |   9  |   4    |  17 |  16
 *   SLIDER  |  41  |  40 |  13  |   3    |  14 |  15
 *   ZOOM    |  39  |  38 |  33  |   8    |  35 |  34
 *
 *   Note: pin 13 is also the Teensy onboard LED; used as DIAG input here.
 *
 * Reserved pins:
 *   24, 25 — Serial6 TX/RX  (ESP32 comms)
 *   28, 29 — Serial7 RX/TX  (LANC zoom)
 *
 * Each TMC2209 has its own dedicated bidirectional serial port (TX + RX).
 * All drivers use UART address 0 (MS1=GND, MS2=GND on every board).
 * Wiring per driver:  TX ─ 1kΩ ─ PDN_UART ← RX
 *
 * DIAG pin — hardware StallGuard output (push-pull):
 *   TMC2209 drives DIAG HIGH when SG_RESULT < SGTHRS × 2 (stall) in SpreadCycle mode.
 *   LOW during normal operation, HIGH on stall.
 *
 * Microstep settings (configured over UART by TMCStepper — MS1/MS2 set address only):
 *
 *   Axis    | Motor  | Gear / belt         | Microsteps | Resolution
 *   --------|--------|---------------------|------------|-----------------------------
 *   PAN     | 0.9°   | 9:1 gear            |    256     | ≈ 1.39 arcsec / step
 *   TILT    | 0.9°   | 8:1 gear            |    256     | ≈ 1.56 arcsec / step
 *   SLIDER  | 1.8°   | GT2 20T (40 mm/rev) |     32     | 6.25 µm / step
 *   ZOOM    | 1.8°   | —                   |    256     | zoom
 *
 *   Note: TMC2209 has hardware 256-step MicroPlyer interpolation.  At 256 commanded
 *   microsteps the step/dir interface aligns with the internal interpolation, giving
 *   the smoothest possible motion with no additional latency.
 *   Lower microsteps on SLIDER retain more torque for the heavy mount carriage.
 */

#include "MountMotion.h"

// Static ISR flag — set by DIAG rising-edge interrupt, cleared before each move
volatile bool MountMotion::_stall_isr_fired = false;

// ---------------------------------------------------------------------------
// Pin map — Teensy 4.1 PCB
// ---------------------------------------------------------------------------

static constexpr uint8_t PIN_STEP[4] = { 23, 19, 41, 39 };  // PAN, TILT, SLIDER, ZOOM
static constexpr uint8_t PIN_DIR[4]  = { 22, 18, 40, 38 };
static constexpr uint8_t PIN_DIAG[4] = {  3,  9, 13, 33 };  // DIAG1 from each TMC2209

// ---------------------------------------------------------------------------
// TMC2209 UART — one dedicated bidirectional serial port per driver
//
//   Axis    | Serial | TX  | RX  | DIAG
//   --------|--------|-----|-----|-----
//   PAN     |   5    |  20 |  21 |   3
//   TILT    |   4    |  17 |  16 |   9
//   SLIDER  |   3    |  14 |  15 |  13
//   ZOOM    |   8    |  35 |  34 |  33
//
//   All drivers at address 0 (MS1=GND, MS2=GND — no addressing needed).
//   Bidirectional: register readback (DRV_STATUS, StallGuard) fully available.
// ---------------------------------------------------------------------------

static HardwareSerial* const TMC_SERIAL[4] = { &Serial5, &Serial4, &Serial3, &Serial8 };
static constexpr uint32_t TMC_BAUD = 115200;

// Microstep resolution per axis  { PAN, TILT, SLIDER, ZOOM }
static constexpr uint16_t MICROSTEPS[4] = { 256, 256, 32, 32 };

// Default run current (mA)
static constexpr uint16_t DEFAULT_CURRENT_MA[4] = { 800, 800, 2000, 1800 };


// Limit find speed (steps/sec) and back-off after stall (steps)
// NOTE: StallGuard on TMC2209 requires ~20 RPM minimum to produce reliable SG_RESULT.
// Both SLIDER and ZOOM are now 32 µsteps:
//   6000 steps/s ÷ (32 µstep × 200 steps/rev) × 60 ≈ 56 RPM  ✓
static constexpr uint32_t LIMIT_FIND_SPEED       = 6000;   // steps/s — both axes (32 µstep)
static constexpr uint32_t LIMIT_FIND_ACCEL       = 150000; // steps/s² — ramp = 6000/150000 = 0.04 s
static constexpr int32_t  LIMIT_BACK_OFF[4]      = { 0, 0, 300, 300 };
// Minimum ramp time (seconds) enforced for all moveTo() GOTO moves.
// Caps the acceleration to  speed / GOTO_MIN_RAMP_S  so that even at fast
// presets the mount eases in and out rather than lurching.
// 0.8 s gives a smooth, professional-feeling position move at any preset.
static constexpr float GOTO_MIN_RAMP_S = 1.0f;  //0.8f;

// Per-axis safety margin (steps) subtracted from each stored limit end.
// Slider: at 32 µstep / 40 mm pitch, 1 step = 6.25 µm → 300 steps ≈ 1.9 mm
// Zoom:   tune independently — set 0 if the zoom has no physical runout concern.
static constexpr int32_t  LIMIT_SAFETY_MARGIN[4] = { 0, 0, 100, 100 };
static constexpr uint32_t LIMIT_FIND_TIMEOUT_MS  = 2000000;
// Ignore stall for this many ms after a new move starts (must exceed the ramp time).
// Ramp time = 6000/150000 = 0.04 s — 300 ms gives plenty of margin.
static constexpr uint32_t LIMIT_STALL_SETTLE_MS  = 300;

// Zoom now uses 32 µsteps (same as slider) — no separate speed/accel needed.
// These are kept as aliases so the axis-specific code paths still compile.
static constexpr uint32_t LIMIT_FIND_SPEED_ZOOM      = 1600;
static constexpr uint32_t LIMIT_FIND_ACCEL_ZOOM      = 50000;
static constexpr uint32_t LIMIT_STALL_SETTLE_ZOOM_MS = 600;

// ── Per-axis limit-find tuning ───────────────────────────────────────────────
//
// LIMIT_FIND_CURRENT_SCALE
//   Fraction of DEFAULT_CURRENT_MA used during limit finding.
//   Lower current → lighter apparent load → higher SG_RESULT during travel →
//   wider useful SGTHRS range.  Zoom runs at 1800 mA full; reducing to 30 %
//   (540 mA) brings SG_RESULT into a range where multiple SGTHRS values work.
static constexpr float LIMIT_FIND_CURRENT_SCALE[4] = { 0.75f, 0.75f, 0.75f, 0.9f };

// SGTHRS_DIVISOR
//   Divides the user-facing threshold (0-255) before writing to the SGTHRS
//   register, remapping the full UI range to the axis's effective chip range.
//
//   Slider: SG_RESULT spans 0-500+ during motion → chip range 0-255 usable → divisor 1.
//   Zoom:   at 540 mA the useful chip range is roughly 1-10, so divisor 25 maps:
//             user   25 → chip  1   (minimum — least sensitive)
//             user  128 → chip  5   (mid-range starting point)
//             user  250 → chip 10   (maximum — most sensitive, false-stall risk)
//   Tune SGTHRS_DIVISOR[3] if the effective chip range turns out wider or narrower.
static constexpr uint8_t SGTHRS_DIVISOR[4] = { 1, 1, 1, 5 };

// Jog watchdog: stop all axes if no JOG packet arrives within this window.
// PC/web app sends at 20 Hz (50 ms); allow 10 misses before declaring lost.
static constexpr uint32_t JOG_WATCHDOG_MS = 500;

// Deceleration uses this fraction of the preset acceleration so stops feel smooth.
// 0.5 → stop time is 2× longer than the acceleration ramp-up.
static constexpr float JOG_STOP_ACCEL_SCALE = 1.0f; //0.8f;

// Joystick expo curve — RC-style weighted blend of linear and cubic responses.
// Applied to the normalised stick deflection (0–1) before setting motor speed.
//
// Formula:  output = (1 - strength) × x  +  strength × x³
//
// This is mathematically guaranteed to return exactly 1.0 at full deflection
// (x=1) regardless of the strength value — full stick always means full speed.
//
//   0.0 = linear    (no expo)
//   0.5 = mild      — 50% stick → 31% speed
//   0.7 = moderate  — 50% stick → 24% speed  (default, good for live production)
//   1.0 = cubic     — 50% stick → 12.5% speed (very pronounced)
static constexpr float JOG_EXPO_STRENGTH = 0.7f;

// ---------------------------------------------------------------------------
// Physical unit conversion constants
//
//   PAN/TILT  presets stored as deg/sec  and deg/sec²
//   SLIDER    presets stored as mm/sec   and mm/sec²
//   ZOOM      presets stored as deg/sec  and deg/sec²  (direct-drive assumption)
// ---------------------------------------------------------------------------
static constexpr float MOTOR_DEG_PER_STEP_PT = 0.9f;    // PAN/TILT motors: 0.9°/full step
static constexpr float MOTOR_DEG_PER_STEP_SZ = 1.8f;    // SLIDER/ZOOM motors: 1.8°/full step
static constexpr float GEAR_RATIO_PAN        = 7.5f;    // 7.5:1 gearbox on PAN
static constexpr float GEAR_RATIO_TILT       = 7.5f;    // 7.5:1 gearbox on TILT
static constexpr float SLIDER_MM_PER_REV     = 40.0f;   // GT2 20T belt: 40 mm/rev
static constexpr float GEAR_RATIO_ZOOM       = 1.0f;    // ZOOM: direct drive (adjust as needed)

// Convert physical speed to µsteps/sec (or µsteps/sec² for acceleration).
// axis  0=PAN, 1=TILT → speed in deg/sec
// axis  2=SLIDER       → speed in mm/sec
// axis  3=ZOOM         → speed in deg/sec
static float physToUSteps(int axis, float speed) {
    switch (axis) {
        case AXIS_PAN:
            return speed * MICROSTEPS[AXIS_PAN]  * GEAR_RATIO_PAN  / MOTOR_DEG_PER_STEP_PT;
        case AXIS_TILT:
            return speed * MICROSTEPS[AXIS_TILT] * GEAR_RATIO_TILT / MOTOR_DEG_PER_STEP_PT;
        case AXIS_SLIDER:
            // full steps/rev = 360 / MOTOR_DEG_PER_STEP_SZ = 200
            return speed * MICROSTEPS[axis] * (360.0f / MOTOR_DEG_PER_STEP_SZ) / SLIDER_MM_PER_REV;
        case AXIS_ZOOM:
            return speed * MICROSTEPS[axis] * GEAR_RATIO_ZOOM / MOTOR_DEG_PER_STEP_SZ;
        default:
            return speed;
    }
}

// Apply expo curve to a normalised joystick deflection (0.0–1.0).
// Uses the RC-style blend formula — guaranteed to return exactly 1.0 at x=1.0.
static inline float jogExpo(float x) {
    if (x <= 0.0f) return 0.0f;
    if (x >= 1.0f) return 1.0f;
    return (1.0f - JOG_EXPO_STRENGTH) * x
         + JOG_EXPO_STRENGTH * x * x * x;
}

// Default speed presets — values are in PHYSICAL UNITS:
//   PT  : deg/sec,  deg/sec²
//   SL  : mm/sec,   mm/sec²
//   ZM  : deg/sec,  deg/sec²   (zoom motor turns a focus ring)
// Index 0 unused (presets are 1-4).
// Ramp times (speed / accel):
//   Preset 1: 0.10 s   Preset 2: 0.10 s   Preset 3: 0.25 s   Preset 4: 0.50 s
static const SpeedPreset DEFAULT_PT_PRESETS[5] = {
    {0,0}, {1, 1}, {5, 5}, {10, 10}, {15, 15}
};
static const SpeedPreset DEFAULT_SL_PRESETS[5] = {
    {0,0}, {1, 1}, {10, 10}, {20, 20}, {40, 40}
};
static const SpeedPreset DEFAULT_ZM_PRESET = {40, 40};

// ---------------------------------------------------------------------------
// Constructor
// ---------------------------------------------------------------------------

MountMotion::MountMotion()
    : _stepper{nullptr, nullptr, nullptr, nullptr}
    , _tmc{nullptr, nullptr, nullptr, nullptr}
    , _state(STATE_IDLE)
    , _flags(0)
    , _pan_invert(false)
    , _tilt_invert(false)
    , _slider_invert(false)
    , _zoom_invert(false)
    , _has_slider(true)
    , _lf_state(LimitFindState::IDLE)
    , _lf_axis(AXIS_SLIDER)
    , _lf_cb(nullptr)
    , _lf_min_found(0)
    , _homing_only(false)
    , _jogging(false)
    , _jog_last_ms(0)
    // v2 look-at
    , _la_subject_x(0.0f), _la_subject_y(0.0f), _la_subject_z(2000.0f)
    , _la_max_steps_s{0.0f, 0.0f}
    , _la_last_update_ms(0)
    , _la_subject_id(0xFF)
    , _la_slew_until_ms(0)
    // v2 calibration
    , _pan_deg_per_step(NOMINAL_PAN_DEG_PER_STEP)
    , _tilt_deg_per_step(NOMINAL_TILT_DEG_PER_STEP)
    , _slider_mm_per_step(NOMINAL_SLIDER_MM_PER_STEP)
    // v2 session reference
    , _pan_ref_deg(0.0f), _tilt_ref_deg(0.0f), _ref_set(false)
{
    for (int i = 0; i < 4; i++) {
        _position[i]          = 0;
        _min_limit[i]         = 0;
        _max_limit[i]         = 0;
        _limits_set[i]        = false;
        _jog_vel[i]           = 0;
        _jog_dir[i]           = 0;
        _jog_preset[i]        = 0;
        _goto_target[i]       = 0;
        _goto_dir[i]          = 0;
        _goto_max_spd_st[i]   = 1;
        _goto_accel_st[i]     = 1;
        _goto_decel_dist[i]   = 1.0f;
        _stall_threshold[i]   = DEFAULT_STALL_THRESHOLD[i];
        _pending_dir[i]       = 0;
        _pending_preset[i]    = 0;
        _pending_spd_factor[i]= 0.0f;
    }
    _has_goto_target  = false;
    _la_pt_dir[0]     = 0;
    _la_pt_dir[1]     = 0;
    memcpy(_pt_presets, DEFAULT_PT_PRESETS, sizeof(_pt_presets));
    memcpy(_sl_presets, DEFAULT_SL_PRESETS, sizeof(_sl_presets));
    _zoom_preset = DEFAULT_ZM_PRESET;
    // Default: has slider — reflected in flags from the start
    _flags = FLAG_HAS_SLIDER;
}

// ---------------------------------------------------------------------------
// begin()
// ---------------------------------------------------------------------------

void MountMotion::begin() {
    // Initialise TeensyStep4 timer system
    TS4::begin();

    // Allocate steppers with their pin assignments and add to group
    for (int i = 0; i < 4; i++) {
        _stepper[i] = new TS4::Stepper(PIN_STEP[i], PIN_DIR[i]);
        const SpeedPreset &sp = (i < 2) ? _pt_presets[2]
                                        : (i == 2) ? _sl_presets[2]
                                        : _zoom_preset;
        _stepper[i]->setAcceleration((uint32_t)physToUSteps(i, sp.acceleration));
        _stepper[i]->setMaxSpeed(    (uint32_t)physToUSteps(i, sp.max_speed));
        _group.add(*_stepper[i]);
    }

    // TMC2209 DIAG is a push-pull output: LOW during normal operation, HIGH on stall.
    for (int i = 0; i < 4; i++) {
        pinMode(PIN_DIAG[i], INPUT);
    }

    // Initialise each TMC2209 on its own bidirectional serial port.
    // All drivers at address 0 (MS1=GND, MS2=GND).
    // RS = 0.11 ohm (standard TMC2209 sense resistor).
    for (int i = 0; i < 4; i++) {
        TMC_SERIAL[i]->begin(TMC_BAUD);
        _tmc[i] = new TMC2209Stepper(TMC_SERIAL[i], 0.11f, 0);
    }

    for (int i = 0; i < 4; i++) {
        _tmc[i]->begin();
        _tmc[i]->toff(5);
        _tmc[i]->en_spreadCycle(false);      // StealthChop for smooth/silent motion
        _tmc[i]->rms_current(DEFAULT_CURRENT_MA[i]);
        _tmc[i]->pwm_autoscale(true);
        _tmc[i]->SGTHRS(DEFAULT_STALL_THRESHOLD[i]);

        // TMC2209 DIAG pin is push-pull and goes HIGH automatically when
        // SG_RESULT < SGTHRS × 2 (stall) while SpreadCycle is active.
        // No extra register write is needed — SGTHRS above is sufficient.

        // These three registers are critical: if any one is missed (marginal solder
        // joint / intermittent UART connection), the driver falls back to its MS1/MS2
        // hardware microstep default (as low as 2-8 µsteps) and runs 32-128× too fast.
        // Send each one 3 times with a short delay so a single glitch cannot corrupt all.
        for (int attempt = 0; attempt < 3; attempt++) {
            _tmc[i]->pdn_disable(true);       // enable UART control (not PDN pin)
            delay(2);
            _tmc[i]->mstep_reg_select(true);  // use MRES register, not MS1/MS2 pins
            delay(2);
            _tmc[i]->microsteps(MICROSTEPS[i]);
            delay(2);
        }

        Serial.printf("TMC axis %d: Serial%d microsteps=%u current=%umA DIAG=%d\n",
                      i, (int[]){5,4,3,8}[i], MICROSTEPS[i], DEFAULT_CURRENT_MA[i],
                      (int)PIN_DIAG[i]);
    }
}

// ---------------------------------------------------------------------------
// update() — call every loop()
// ---------------------------------------------------------------------------

void MountMotion::update() {
    // Sync positions from TeensyStep4
    for (int i = 0; i < 4; i++) {
        _position[i] = _stepper[i]->getPosition();
        _stepper[i]->cleanupTimer();
    }

    // Periodically re-send the critical TMC2209 UART registers.
    // A motor-load brownout can reset the driver, losing pdn_disable /
    // mstep_reg_select / microsteps and causing the motor to suddenly run
    // at the MS1/MS2 hardware-default microstep rate (up to 128× too fast).
    // Re-sending every 2 s means a reset is corrected within one interval.
    // UART writes are independent of STEP/DIR — safe to send during motion.
    uint32_t now = millis();
    if (now - _tmc_refresh_ms >= 2000) {
        _tmc_refresh_ms = now;
        for (int i = 0; i < 4; i++) {
            _tmc[i]->pdn_disable(true);
            _tmc[i]->mstep_reg_select(true);
            _tmc[i]->microsteps(MICROSTEPS[i]);
        }
    }

    if (_state == STATE_JOGGING) {
        _updateJog();
    }

    // Zoom dead-man outside STATE_JOGGING.  The look-at zoom path in jog()
    // starts an unbounded rotateAsync() while _state stays LOOK_AT_MOVE /
    // PRE_AIM — states where _updateJog() (and its watchdog) never runs.  The
    // rotation can also outlive the move itself (look-at completes → IDLE with
    // zoom still turning).  If the link dies with the stick held, nothing
    // upstream is guaranteed to stop it — so enforce the same 500 ms rule here
    // for a zoom axis rotating in any non-jog state.  Every other unbounded
    // motion is either watchdogged (_updateJog) or self-terminating (GOTO,
    // look-at slider, pre-aim, homing); LANC zoom has its own watchdog.
    if (_state != STATE_JOGGING && _jog_dir[AXIS_ZOOM] != 0 &&
            (millis() - _jog_last_ms > JOG_WATCHDOG_MS)) {
        Serial.println("ZOOM watchdog: no packet — stopping zoom");
        uint32_t stop_accel = (uint32_t)(physToUSteps(AXIS_ZOOM,
                                  _zoom_preset.acceleration) * JOG_STOP_ACCEL_SCALE);
        noInterrupts();
        _stepper[AXIS_ZOOM]->setAcceleration(stop_accel);
        _stepper[AXIS_ZOOM]->stopAsync();
        interrupts();
        _jog_dir[AXIS_ZOOM] = 0;
        _jog_vel[AXIS_ZOOM] = 0;
    }

    if (_state == STATE_FINDING_LIMITS) {
        _updateLimitFind();
    }

    if (_state == STATE_LOOK_AT_PRE_AIM) {
        _updatePreAim();
    }

    if (_state == STATE_LOOK_AT_MOVE) {
        _updateLookAt();
    }

    if (_state == STATE_MOVING_TO_POS) {
        _updateGoto();
    }
}

// ---------------------------------------------------------------------------
// jog()
// ---------------------------------------------------------------------------

void MountMotion::jog(int16_t pan, int16_t tilt, int16_t slider, int16_t zoom,
                      uint8_t pt_preset, uint8_t sz_preset) {
    if (_state == STATE_FINDING_LIMITS) return;

    // During a look-at slider move, pan/tilt/slider are owned by the look-at
    // controller.  Drive zoom only and return WITHOUT changing _state — if we
    // set _state = STATE_JOGGING the update() loop stops calling _updateLookAt()
    // and pan/tilt tracking freezes mid-move.
    if (_state == STATE_LOOK_AT_MOVE || _state == STATE_LOOK_AT_PRE_AIM) {
        sz_preset = constrain(sz_preset, 1, 4);
        int16_t vel = _applyOrientation(AXIS_ZOOM, zoom);
        _jog_vel[AXIS_ZOOM] = vel;
        // Feed the dead-man on every packet (matches the main jog() rule):
        // the zoom watchdog in update() stops this axis if the link dies
        // while the stick is held — _updateJog() doesn't run in this state.
        _jog_last_ms = millis();
        const SpeedPreset &spd  = _zoom_preset;
        int32_t  max_spd_st = (int32_t)physToUSteps(AXIS_ZOOM, spd.max_speed);
        uint32_t accel_st   = (uint32_t)physToUSteps(AXIS_ZOOM, spd.acceleration);
        uint32_t stop_accel = (uint32_t)(accel_st * JOG_STOP_ACCEL_SCALE);
        if (vel == 0) {
            _pending_dir[AXIS_ZOOM] = 0;
            if (_jog_dir[AXIS_ZOOM] != 0) {
                noInterrupts();
                _stepper[AXIS_ZOOM]->setAcceleration(stop_accel);
                _stepper[AXIS_ZOOM]->stopAsync();
                interrupts();
                _jog_dir[AXIS_ZOOM] = 0;
            }
        } else {
            float  spd_factor = jogExpo(fabsf((float)vel) / 1000.0f);
            int8_t new_dir    = (vel > 0) ? 1 : -1;
            if (_jog_dir[AXIS_ZOOM] != new_dir || _jog_preset[AXIS_ZOOM] != sz_preset) {
                _pending_dir[AXIS_ZOOM] = 0;
                noInterrupts();
                _stepper[AXIS_ZOOM]->setMaxSpeed(new_dir * max_spd_st);
                _stepper[AXIS_ZOOM]->setAcceleration(accel_st);
                _stepper[AXIS_ZOOM]->rotateAsync();
                interrupts();
                _stepper[AXIS_ZOOM]->overrideSpeed(spd_factor);
                _jog_dir[AXIS_ZOOM]    = new_dir;
                _jog_preset[AXIS_ZOOM] = sz_preset;
            } else {
                _pending_dir[AXIS_ZOOM] = 0;
                // If the motor stopped (expo drove speed near zero), restart it.
                if (!_stepper[AXIS_ZOOM]->isMoving) {
                    noInterrupts();
                    _stepper[AXIS_ZOOM]->setMaxSpeed(new_dir * max_spd_st);
                    _stepper[AXIS_ZOOM]->setAcceleration(accel_st);
                    _stepper[AXIS_ZOOM]->rotateAsync();
                    interrupts();
                }
                _stepper[AXIS_ZOOM]->overrideSpeed(spd_factor);
            }
        }
        return;   // _state stays STATE_LOOK_AT_MOVE/PRE_AIM — look-at controller keeps running
    }

    pt_preset = constrain(pt_preset, 1, 4);
    sz_preset = constrain(sz_preset, 1, 4);

    int16_t raw[4] = { pan, tilt, _has_slider ? slider : (int16_t)0, zoom };
    for (int i = 0; i < 4; i++)
        _jog_vel[i] = _applyOrientation((Axis)i, raw[i]);

    // Update watchdog on EVERY call (including all-zero) so it does not fire
    // while the joystick is intentionally centred during a soft deceleration.
    _jog_last_ms = millis();
    _state       = STATE_JOGGING;

    bool any_nonzero = false;
    for (int i = 0; i < 4; i++) if (_jog_vel[i] != 0) any_nonzero = true;

    for (int i = 0; i < 4; i++) {
        uint8_t  preset       = (i < 2) ? pt_preset : sz_preset;
        const SpeedPreset &spd = (i < 2) ? _pt_presets[preset]
                                          : (i == 2) ? _sl_presets[preset]
                                          : _zoom_preset;
        int32_t  max_spd_st  = (int32_t)physToUSteps(i, spd.max_speed);
        uint32_t accel_st    = (uint32_t)physToUSteps(i, spd.acceleration);
        uint32_t stop_accel  = (uint32_t)(accel_st * JOG_STOP_ACCEL_SCALE);
        int16_t  vel         = _jog_vel[i];
        float    spd_factor  = jogExpo(fabsf((float)vel) / 1000.0f);

        if (vel == 0) {
            // Soft stop — clear any pending reversal and decelerate gently.
            _pending_dir[i] = 0;
            if (_jog_dir[i] != 0) {
                noInterrupts();
                _stepper[i]->setAcceleration(stop_accel);
                _stepper[i]->stopAsync();
                interrupts();
                _jog_dir[i] = 0;
            }
            continue;
        }

        int8_t new_dir = (vel > 0) ? 1 : -1;

        if (_jog_dir[i] != new_dir) {
            if (_jog_dir[i] != 0) {
                // Direction reversal while actively rotating — pass the new direction
                // directly to rotateAsync() so TeensyStep4 handles the deceleration
                // through zero and reacceleration as a single continuous profile,
                // avoiding the judder of a discrete stop-then-restart.
                _pending_dir[i] = 0;
                noInterrupts();
                _stepper[i]->setMaxSpeed(new_dir * max_spd_st);
                _stepper[i]->setAcceleration(accel_st);
                _stepper[i]->rotateAsync();
                interrupts();
                _stepper[i]->overrideSpeed(spd_factor);
                _jog_dir[i]    = new_dir;
                _jog_preset[i] = preset;
            } else {
                // Stopped or decelerating after a centre-stop — start the new
                // direction immediately; rotateAsync() overrides any deceleration.
                _pending_dir[i] = 0;
                noInterrupts();
                _stepper[i]->setMaxSpeed(new_dir * max_spd_st);
                _stepper[i]->setAcceleration(accel_st);
                _stepper[i]->rotateAsync();
                interrupts();
                _stepper[i]->overrideSpeed(spd_factor);
                _jog_dir[i]    = new_dir;
                _jog_preset[i] = preset;
            }
        } else if (_jog_preset[i] != preset) {
            // Same direction, different preset — update speed parameters.
            _pending_dir[i] = 0;
            noInterrupts();
            _stepper[i]->setMaxSpeed(new_dir * max_spd_st);
            _stepper[i]->setAcceleration(accel_st);
            _stepper[i]->rotateAsync();
            interrupts();
            _stepper[i]->overrideSpeed(spd_factor);
            _jog_preset[i] = preset;
        } else {
            // Same direction, same preset — just scale speed.
            // If the motor stopped (expo drove speed near zero), restart it.
            _pending_dir[i] = 0;
            if (!_stepper[i]->isMoving) {
                noInterrupts();
                _stepper[i]->setMaxSpeed(new_dir * max_spd_st);
                _stepper[i]->setAcceleration(accel_st);
                _stepper[i]->rotateAsync();
                interrupts();
            }
            _stepper[i]->overrideSpeed(spd_factor);
        }
    }

    _jogging = any_nonzero;
}


void MountMotion::_updateJog() {
    // Safety: stop if no JOG packet received recently (connection lost mid-jog).
    // Exception: if every axis already has _jog_dir==0, stopAsync() was already
    // called and a controlled deceleration is in progress.  Calling stopAll()
    // again would re-issue stopAsync() mid-decel and cause the abrupt stop the
    // user experiences.  Only fire the watchdog when at least one axis is still
    // actively rotating (jog_dir != 0), meaning the joystick was held on when
    // contact was lost.
    if (millis() - _jog_last_ms > JOG_WATCHDOG_MS) {
        bool any_active_dir = false;
        for (int i = 0; i < 4; i++) {
            if (_jog_dir[i] != 0) { any_active_dir = true; break; }
        }
        if (any_active_dir) {
            Serial.println("JOG watchdog: no packet — stopping");
            stopAll();
            return;
        }
        // All axes are already decelerating to a stop — reset the timestamp so
        // this block doesn't re-evaluate on every loop tick until they finish.
        _jog_last_ms = millis();
    }

    bool any_active = false;

    for (int i = 0; i < 4; i++) {
        // ── Pending direction: start once the motor has fully stopped ────────
        if (_pending_dir[i] != 0 && !_stepper[i]->isMoving) {
            uint8_t  preset  = _pending_preset[i];
            float    factor  = _pending_spd_factor[i];
            int8_t   dir     = _pending_dir[i];
            const SpeedPreset &spd = (i < 2) ? _pt_presets[preset]
                                              : (i == 2) ? _sl_presets[preset]
                                              : _zoom_preset;
            noInterrupts();
            _stepper[i]->setMaxSpeed(dir * (int32_t)physToUSteps(i, spd.max_speed));
            _stepper[i]->setAcceleration((uint32_t)physToUSteps(i, spd.acceleration));
            _stepper[i]->rotateAsync();
            interrupts();
            _stepper[i]->overrideSpeed(factor);
            _jog_dir[i]    = dir;
            _jog_preset[i] = preset;
            _pending_dir[i] = 0;
        }

        if (_stepper[i]->isMoving || _pending_dir[i] != 0) any_active = true;

        // ── Soft limit enforcement ───────────────────────────────────────────
        if (!_limits_set[i] || _jog_vel[i] == 0) continue;

        int32_t pos = _stepper[i]->getPosition();
        if (_jog_vel[i] < 0 && pos <= _min_limit[i] + LIMIT_BACK_OFF[i]) {
            _jog_vel[i] = 0;
            _pending_dir[i] = 0;
            noInterrupts(); _stepper[i]->stopAsync(); interrupts();
            _setFlag(FLAG_AT_MIN_LIMIT);
        } else if (_jog_vel[i] > 0 && pos >= _max_limit[i] - LIMIT_BACK_OFF[i]) {
            _jog_vel[i] = 0;
            _pending_dir[i] = 0;
            noInterrupts(); _stepper[i]->stopAsync(); interrupts();
            _setFlag(FLAG_AT_MAX_LIMIT);
        } else {
            _clearFlag(FLAG_AT_MIN_LIMIT);
            _clearFlag(FLAG_AT_MAX_LIMIT);
        }
    }

    // Transition to IDLE once all motors have fully stopped and no new direction
    // is queued (joystick was returned to centre and deceleration is complete).
    if (!_jogging && !any_active) {
        _state = STATE_IDLE;
    }
}

// ---------------------------------------------------------------------------
// jogPanTilt()  — pan/tilt jog only, slider/zoom left untouched
// ---------------------------------------------------------------------------

void MountMotion::jogPanTilt(int16_t pan, int16_t tilt, uint8_t pt_preset) {
    if (_state == STATE_FINDING_LIMITS)  return;
    if (_state == STATE_LOOK_AT_MOVE ||
        _state == STATE_LOOK_AT_PRE_AIM) return;   // look-at owns pan/tilt

    pt_preset = constrain(pt_preset, 1, 4);

    _jog_vel[AXIS_PAN]  = _applyOrientation(AXIS_PAN,  pan);
    _jog_vel[AXIS_TILT] = _applyOrientation(AXIS_TILT, tilt);
    _jog_last_ms        = millis();
    _jogging            = (_jog_vel[AXIS_PAN] != 0 || _jog_vel[AXIS_TILT] != 0);
    _state              = STATE_JOGGING;

    for (int i = 0; i < 2; i++) {   // 0=PAN, 1=TILT only
        const SpeedPreset &spd = _pt_presets[pt_preset];
        int32_t  max_spd_st  = (int32_t)physToUSteps(i, spd.max_speed);
        uint32_t accel_st    = (uint32_t)physToUSteps(i, spd.acceleration);
        uint32_t stop_accel  = (uint32_t)(accel_st * JOG_STOP_ACCEL_SCALE);
        int16_t  vel         = _jog_vel[i];
        float    spd_factor  = jogExpo(fabsf((float)vel) / 1000.0f);

        if (vel == 0) {
            _pending_dir[i] = 0;
            if (_jog_dir[i] != 0) {
                noInterrupts();
                _stepper[i]->setAcceleration(stop_accel);
                _stepper[i]->stopAsync();
                interrupts();
                _jog_dir[i] = 0;
            }
            continue;
        }

        int8_t new_dir = (vel > 0) ? 1 : -1;
        if (_jog_dir[i] != new_dir) {
            if (_jog_dir[i] != 0) {
                // Direction reversal — hand directly to rotateAsync() for a smooth
                // continuous profile through zero, avoiding stop-then-restart judder.
                _pending_dir[i] = 0;
                noInterrupts();
                _stepper[i]->setMaxSpeed(new_dir * max_spd_st);
                _stepper[i]->setAcceleration(accel_st);
                _stepper[i]->rotateAsync();
                interrupts();
                _stepper[i]->overrideSpeed(spd_factor);
                _jog_dir[i]    = new_dir;
                _jog_preset[i] = pt_preset;
            } else {
                _pending_dir[i] = 0;
                noInterrupts();
                _stepper[i]->setMaxSpeed(new_dir * max_spd_st);
                _stepper[i]->setAcceleration(accel_st);
                _stepper[i]->rotateAsync();
                interrupts();
                _stepper[i]->overrideSpeed(spd_factor);
                _jog_dir[i]    = new_dir;
                _jog_preset[i] = pt_preset;
            }
        } else if (_jog_preset[i] != pt_preset) {
            _pending_dir[i] = 0;
            noInterrupts();
            _stepper[i]->setMaxSpeed(new_dir * max_spd_st);
            _stepper[i]->setAcceleration(accel_st);
            _stepper[i]->rotateAsync();
            interrupts();
            _stepper[i]->overrideSpeed(spd_factor);
            _jog_preset[i] = pt_preset;
        } else {
            // Same direction, same preset — just scale speed.
            // If the motor stopped (expo drove speed near zero), restart it.
            _pending_dir[i] = 0;
            if (!_stepper[i]->isMoving) {
                noInterrupts();
                _stepper[i]->setMaxSpeed(new_dir * max_spd_st);
                _stepper[i]->setAcceleration(accel_st);
                _stepper[i]->rotateAsync();
                interrupts();
            }
            _stepper[i]->overrideSpeed(spd_factor);
        }
    }
}

// ---------------------------------------------------------------------------
// jogSliderZoom()  — slider/zoom jog only, pan/tilt left untouched
// Used when CV tracking owns pan/tilt and the operator moves the slider joystick.
// ---------------------------------------------------------------------------

void MountMotion::jogSliderZoom(int16_t slider, int16_t zoom, uint8_t sz_preset) {
    if (_state == STATE_FINDING_LIMITS)  return;
    if (_state == STATE_LOOK_AT_MOVE ||
        _state == STATE_LOOK_AT_PRE_AIM) return;   // look-at owns slider

    sz_preset = constrain(sz_preset, 1, 4);

    _jog_vel[AXIS_SLIDER] = _has_slider ? _applyOrientation(AXIS_SLIDER, slider) : 0;
    _jog_vel[AXIS_ZOOM]   = _applyOrientation(AXIS_ZOOM, zoom);
    _jog_last_ms          = millis();
    _jogging              = (_jog_vel[AXIS_PAN]    != 0 || _jog_vel[AXIS_TILT]   != 0 ||
                             _jog_vel[AXIS_SLIDER] != 0 || _jog_vel[AXIS_ZOOM]   != 0);
    _state                = STATE_JOGGING;

    for (int i = 2; i < 4; i++) {   // 2=SLIDER, 3=ZOOM only
        const SpeedPreset &spd  = (i == 2) ? _sl_presets[sz_preset] : _zoom_preset;
        int32_t  max_spd_st     = (int32_t)physToUSteps(i, spd.max_speed);
        uint32_t accel_st       = (uint32_t)physToUSteps(i, spd.acceleration);
        uint32_t stop_accel     = (uint32_t)(accel_st * JOG_STOP_ACCEL_SCALE);
        int16_t  vel            = _jog_vel[i];
        float    spd_factor     = jogExpo(fabsf((float)vel) / 1000.0f);

        if (vel == 0) {
            _pending_dir[i] = 0;
            if (_jog_dir[i] != 0) {
                noInterrupts();
                _stepper[i]->setAcceleration(stop_accel);
                _stepper[i]->stopAsync();
                interrupts();
                _jog_dir[i] = 0;
            }
            continue;
        }

        int8_t new_dir = (vel > 0) ? 1 : -1;
        if (_jog_dir[i] != new_dir) {
            if (_jog_dir[i] != 0) {
                _pending_dir[i] = 0;
                noInterrupts();
                _stepper[i]->setMaxSpeed(new_dir * max_spd_st);
                _stepper[i]->setAcceleration(accel_st);
                _stepper[i]->rotateAsync();
                interrupts();
                _stepper[i]->overrideSpeed(spd_factor);
                _jog_dir[i]    = new_dir;
                _jog_preset[i] = sz_preset;
            } else {
                _pending_dir[i] = 0;
                noInterrupts();
                _stepper[i]->setMaxSpeed(new_dir * max_spd_st);
                _stepper[i]->setAcceleration(accel_st);
                _stepper[i]->rotateAsync();
                interrupts();
                _stepper[i]->overrideSpeed(spd_factor);
                _jog_dir[i]    = new_dir;
                _jog_preset[i] = sz_preset;
            }
        } else if (_jog_preset[i] != sz_preset) {
            _pending_dir[i] = 0;
            noInterrupts();
            _stepper[i]->setMaxSpeed(new_dir * max_spd_st);
            _stepper[i]->setAcceleration(accel_st);
            _stepper[i]->rotateAsync();
            interrupts();
            _stepper[i]->overrideSpeed(spd_factor);
            _jog_preset[i] = sz_preset;
        } else {
            _pending_dir[i] = 0;
            if (!_stepper[i]->isMoving) {
                noInterrupts();
                _stepper[i]->setMaxSpeed(new_dir * max_spd_st);
                _stepper[i]->setAcceleration(accel_st);
                _stepper[i]->rotateAsync();
                interrupts();
            }
            _stepper[i]->overrideSpeed(spd_factor);
        }
    }
}

// moveSliderTo()  — slider-only async move, does not cancel pan/tilt jog
// ---------------------------------------------------------------------------

void MountMotion::moveSliderTo(int32_t target, uint8_t sl_preset) {
    if (_state == STATE_FINDING_LIMITS) return;
    if (!_has_slider) return;

    sl_preset = constrain(sl_preset, 1, 4);
    const SpeedPreset &sp = _sl_presets[sl_preset];

    int32_t t = _applyOrientationPos(AXIS_SLIDER, target);
    _clampToLimits(t, AXIS_SLIDER);

    noInterrupts();
    _stepper[AXIS_SLIDER]->setMaxSpeed(    (uint32_t)physToUSteps(AXIS_SLIDER, sp.max_speed));
    _stepper[AXIS_SLIDER]->setAcceleration((uint32_t)physToUSteps(AXIS_SLIDER, sp.acceleration));
    _stepper[AXIS_SLIDER]->setTargetAbs(t);
    _stepper[AXIS_SLIDER]->moveAsync();
    interrupts();

    _goto_target[AXIS_SLIDER] = t;
    // Do NOT change _state — the caller (jogPanTilt) keeps STATE_JOGGING so
    // _updateJog() continues driving pan/tilt and refreshing the watchdog.
}

// ---------------------------------------------------------------------------
// Steps of position error considered "arrived" — sub-arcminute for pan/tilt.
// Defined here (before moveTo) so the sync calculation in moveTo can use it.
static constexpr int32_t GOTO_ARRIVE_STEPS = 10;

// moveTo()
// ---------------------------------------------------------------------------

void MountMotion::moveTo(int32_t pan, int32_t tilt, int32_t slider, int32_t zoom,
                          uint8_t pt_preset, uint8_t sl_preset, bool sync) {
    if (_state == STATE_FINDING_LIMITS) return;

    // If jogging was in progress, hard-stop before starting a position move.
    if (_jogging) {
        noInterrupts();
        for (int i = 0; i < 4; i++) { _stepper[i]->emergencyStop(); _jog_dir[i] = 0; }
        interrupts();
        _jogging = false;
    }

    // sl_preset defaults to pt_preset when not explicitly provided (sl_preset == 0).
    if (sl_preset == 0) sl_preset = pt_preset;

    // If no slider hardware, keep slider at current position (no movement)
    int32_t targets[4] = { pan, tilt,
                            _has_slider ? slider : _stepper[AXIS_SLIDER]->getPosition(),
                            zoom };

    // Apply orientation: convert logical (user-facing) → physical (motor) space.
    // PAN, TILT, SLIDER: self-inverse sign flip when the axis is inverted.
    // ZOOM:  no orientation inversion (zoom has no direction concept).
    targets[AXIS_PAN]    = _applyOrientationPos(AXIS_PAN,    targets[AXIS_PAN]);
    targets[AXIS_TILT]   = _applyOrientationPos(AXIS_TILT,   targets[AXIS_TILT]);
    targets[AXIS_SLIDER] = _has_slider
                           ? _applyOrientationPos(AXIS_SLIDER, targets[AXIS_SLIDER])
                           : targets[AXIS_SLIDER];

    // Clamp slider and zoom to limits
    _clampToLimits(targets[AXIS_SLIDER], AXIS_SLIDER);
    _clampToLimits(targets[AXIS_ZOOM],   AXIS_ZOOM);

    pt_preset = constrain(pt_preset, 1, 4);
    sl_preset = constrain(sl_preset, 1, 4);

    // ── Speed computation (with optional sync) ───────────────────────────────
    // When sync=true (slot recalls): scale all axes so they arrive together.
    //   The dominant (slowest) axis sets t_move; every other axis is scaled
    //   by k = t_arrive[i]/t_move (≤ 1), both max_speed and accel, to keep
    //   the decel-ramp shape proportional.  Ensures simultaneous arrival.
    //
    // When sync=false (look-at subject switch): each axis runs at its full
    //   preset speed independently — prevents a tiny pan correction from
    //   slowing a large tilt move to a crawl.
    //
    // retargetTo() does NOT re-sync (axes may desync on mid-move retargets).
    float dist_st[4];
    float t_move = 0.f;

    // First pass: collect distances and compute t_move (used only when sync=true)
    for (int i = 0; i < 4; i++) {
        dist_st[i] = fabsf((float)(targets[i] - _stepper[i]->getPosition()));
        if (sync && dist_st[i] > (float)GOTO_ARRIVE_STEPS) {
            const SpeedPreset &sp = (i < 2) ? _pt_presets[pt_preset]
                                            : (i == 2) ? _sl_presets[sl_preset]
                                            : _zoom_preset;
            float spd = max(1.0f, physToUSteps(i, sp.max_speed));
            float t   = dist_st[i] / spd;
            if (t > t_move) t_move = t;
        }
    }

    // Second pass: set per-axis speeds and start motors
    for (int i = 0; i < 4; i++) {
        const SpeedPreset &sp = (i < 2) ? _pt_presets[pt_preset]
                                        : (i == 2) ? _sl_presets[sl_preset]
                                        : _zoom_preset;
        float preset_spd = max(1.0f, physToUSteps(i, sp.max_speed));
        float preset_acc = max(1.0f, physToUSteps(i, sp.acceleration));

        float actual_spd, actual_acc;
        if (sync && t_move > 0.f && dist_st[i] > (float)GOTO_ARRIVE_STEPS) {
            // k = t_arrive[i] / t_move (= 1 for dominant axis, < 1 for faster axes)
            float k  = (dist_st[i] / preset_spd) / t_move;
            actual_spd = max(1.0f, preset_spd * k);
            actual_acc = max(1.0f, preset_acc * k);
        } else {
            // No sync (or negligible travel): run at full preset speed.
            actual_spd = preset_spd;
            actual_acc = preset_acc;
        }

        _goto_target[i]     = targets[i];
        _goto_max_spd_st[i] = (uint32_t)actual_spd;
        _goto_accel_st[i]   = (uint32_t)actual_acc;

        // Cap the P-controller decel window to the actual move distance.
        // Without this cap, raising speed without proportionally raising accel
        // makes decel_dist = spd²/(2·acc) grow as v², so short moves (like
        // look-at subject switches of 5-15°) start at factor = dist/decel_dist
        // which can be < 10% of full speed — the motor crawls the whole way.
        // With the cap, factor starts at 1.0 and the hardware accel ramp limits
        // the actual speed naturally for short distances.
        {
            float decel_dist_full = (actual_acc > 0.f)
                                    ? (actual_spd * actual_spd) / (2.f * actual_acc)
                                    : actual_spd;
            float move_dist = dist_st[i];
            _goto_decel_dist[i] = (move_dist > (float)GOTO_ARRIVE_STEPS && move_dist < decel_dist_full)
                                  ? fmaxf(move_dist, (float)(GOTO_ARRIVE_STEPS * 2))
                                  : decel_dist_full;
        }

        // Start rotateAsync() with positive max speed — overrideSpeed() sign
        // controls direction, so we never need to call rotateAsync() again.
        // This is the same pattern as teensy_follower: one rotateAsync() at the
        // start, then signed overrideSpeed() drives everything from there.
        noInterrupts();
        _stepper[i]->setMaxSpeed((int32_t)actual_spd);
        _stepper[i]->setAcceleration((int32_t)actual_acc);
        _stepper[i]->rotateAsync();
        interrupts();
        _goto_dir[i] = 1;  // rotateAsync registered positive; overrideSpeed sign is relative
    }
    _has_goto_target = true;

    _jogging = false;
    _state   = STATE_MOVING_TO_POS;
}

// ---------------------------------------------------------------------------
// moveRel() — relative move in logical step space
// ---------------------------------------------------------------------------
//
// The raw positions from getPosition() are in physical (motor) space.
// moveTo() applies _applyOrientationPos() to convert logical → physical.
// If we passed physical positions directly to moveTo() with delta=0 (no
// movement intended) and an axis is inverted, moveTo() would negate the
// current position and send the axis to the mirror of where it is — causing
// e.g. the slider to sprint to home when only a pan nudge was requested.
//
// The fix: invert the physical position back to logical space first, then
// add the delta (which is always in logical space), then hand to moveTo().
// Since _applyOrientationPos is self-inverse (pure sign flip), applying it
// once here undoes the physical→logical direction.

void MountMotion::moveRel(int32_t d_pan, int32_t d_tilt, int32_t d_slider, int32_t d_zoom,
                           uint8_t pt_preset, uint8_t sl_preset) {
    // Convert physical → logical (invert orientation on inverted axes)
    int32_t pan_log    = _pan_invert    ? -_stepper[AXIS_PAN]->getPosition()    : _stepper[AXIS_PAN]->getPosition();
    int32_t tilt_log   = _tilt_invert    ? -_stepper[AXIS_TILT]->getPosition()   : _stepper[AXIS_TILT]->getPosition();
    int32_t slider_log = _slider_invert ? -_stepper[AXIS_SLIDER]->getPosition() : _stepper[AXIS_SLIDER]->getPosition();
    int32_t zoom_log   = _zoom_invert   ? -_stepper[AXIS_ZOOM]->getPosition()   : _stepper[AXIS_ZOOM]->getPosition();

    moveTo(pan_log + d_pan, tilt_log + d_tilt, slider_log + d_slider, zoom_log + d_zoom,
           pt_preset, sl_preset);
}

// ---------------------------------------------------------------------------
// stopAll() / emergencyStop()
// ---------------------------------------------------------------------------

void MountMotion::stopAll() {
    // stopAsync() → startStopping() → startRotate() writes v_tgt_sqr (64-bit)
    // without interrupt protection — same race as rotateAsync().  Wrap all
    // four calls under one noInterrupts() window so no ISR fires mid-write.
    noInterrupts();
    for (int i = 0; i < 4; i++) {
        _stepper[i]->stopAsync();
        _jog_dir[i]    = 0;
        _jog_preset[i] = 0;
        _goto_dir[i]   = 0;
    }
    interrupts();
    _jogging         = false;
    _has_goto_target = false;
    _la_pt_dir[0]    = 0;
    _la_pt_dir[1]    = 0;
    if (_state != STATE_FINDING_LIMITS)
        _state = STATE_IDLE;
}

void MountMotion::emergencyStop() {
    // emergencyStop() stops the timer and nulls stpTimer — protect the same way.
    noInterrupts();
    for (int i = 0; i < 4; i++) {
        _stepper[i]->emergencyStop();
        _jog_dir[i]    = 0;
        _jog_preset[i] = 0;
        _goto_dir[i]   = 0;
    }
    interrupts();
    _jogging         = false;
    _has_goto_target = false;
    _la_pt_dir[0]    = 0;
    _la_pt_dir[1]    = 0;
    if (_lf_state != LimitFindState::IDLE) {
        detachInterrupt(digitalPinToInterrupt(PIN_DIAG[(int)_lf_axis]));
    }
    _lf_state     = LimitFindState::IDLE;
    _homing_only  = false;
    _state        = STATE_IDLE;
}

// ---------------------------------------------------------------------------
// Position
// ---------------------------------------------------------------------------

int32_t MountMotion::getPosition(Axis axis) const {
    return _stepper[(int)axis]->getPosition();
}

void MountMotion::zeroPosition(Axis axis) {
    _stepper[(int)axis]->setPosition(0);
    _position[(int)axis] = 0;
}

// ---------------------------------------------------------------------------
// Limits
// ---------------------------------------------------------------------------

void MountMotion::setLimits(Axis axis, int32_t min_steps, int32_t max_steps) {
    _min_limit[(int)axis]  = min_steps;
    _max_limit[(int)axis]  = max_steps;
    _limits_set[(int)axis] = true;
    _setFlag(FLAG_LIMITS_SET);
}

bool    MountMotion::hasLimits(Axis axis)    const { return _limits_set[(int)axis]; }
int32_t MountMotion::getMinLimit(Axis axis)  const { return _min_limit[(int)axis]; }
int32_t MountMotion::getMaxLimit(Axis axis)  const { return _max_limit[(int)axis]; }

// ---------------------------------------------------------------------------
// findLimits() — state machine driven from update()
// ---------------------------------------------------------------------------

void MountMotion::findLimits(Axis axis, LimitsFoundCb cb) {
    if (axis != AXIS_SLIDER && axis != AXIS_ZOOM) return;
    if (_state == STATE_FINDING_LIMITS) return;

    emergencyStop();

    _lf_axis     = axis;
    _lf_cb       = cb;
    _lf_state    = LimitFindState::MOVING_TO_MIN;
    _lf_start_ms = millis();
    _state       = STATE_FINDING_LIMITS;

    // Stay in StealthChop (do NOT force SpreadCycle) — working examples show StallGuard
    // functions correctly via TCOOLTHRS even in StealthChop mode on BTT TMC2209 boards.
    // Use per-axis current scale: lower current widens the useful SGTHRS range (zoom especially).
    _tmc[(int)axis]->rms_current(DEFAULT_CURRENT_MA[(int)axis] * LIMIT_FIND_CURRENT_SCALE[(int)axis]);
    // Scale user threshold to the axis's effective chip range via SGTHRS_DIVISOR.
    uint8_t sgthrs = max((uint8_t)1,
                         (uint8_t)(_stall_threshold[(int)axis] / SGTHRS_DIVISOR[(int)axis]));
    _tmc[(int)axis]->SGTHRS(sgthrs);
    _tmc[(int)axis]->TCOOLTHRS(0xFFFFF);

    // Arm the DIAG rising-edge interrupt — fires when stall is detected
    _stall_isr_fired = false;
    attachInterrupt(digitalPinToInterrupt(PIN_DIAG[(int)axis]), _diag_isr, RISING);

    {
        uint32_t spd = (axis == AXIS_ZOOM) ? LIMIT_FIND_SPEED_ZOOM : LIMIT_FIND_SPEED;
        uint32_t acc = (axis == AXIS_ZOOM) ? LIMIT_FIND_ACCEL_ZOOM : LIMIT_FIND_ACCEL;
        _stepper[(int)axis]->setMaxSpeed(spd);
        _stepper[(int)axis]->setAcceleration(acc);
    }
    // For inverted zoom the home/min end is in the positive direction.
    // Both findLimits and findHome must seek the same physical end stop first
    // so that position is zeroed at the same point and limits remain valid.
    {
        bool go_positive = (axis == AXIS_ZOOM && _zoom_invert);
        _stepper[(int)axis]->setTargetAbs(go_positive ? 10000000L : -10000000L);
    }

    // ── Diagnostic: read back chip registers to confirm writes took effect ──
    delay(5);  // brief settle for UART writes to complete
    uint32_t gconf_rb     = _tmc[(int)axis]->GCONF();
    uint32_t tcool_rb     = _tmc[(int)axis]->TCOOLTHRS();
    uint8_t  sgthrs_rb    = _tmc[(int)axis]->SGTHRS();
    bool     spread_rb    = (gconf_rb >> 2) & 0x01;
    bool     pdn_rb       = (gconf_rb >> 6) & 0x01;
    Serial.printf("[LimitFind] axis=%d  SGTHRS=%u (chip=%u)  TCOOLTHRS=0xFFFFF (chip=0x%X)\n",
                  (int)axis, sgthrs, sgthrs_rb, tcool_rb);
    Serial.printf("[LimitFind] GCONF=0x%08X  en_spreadCycle=%d (want 0)  pdn_disable=%d (want 1)\n",
                  gconf_rb, (int)spread_rb, (int)pdn_rb);
    Serial.printf("[LimitFind] DIAG pin %d = %s (expect LOW at standstill — normal)\n",
                  (int)PIN_DIAG[(int)axis],
                  digitalRead(PIN_DIAG[(int)axis]) == HIGH ? "HIGH(stall)" : "LOW(ok)");
    Serial.printf("[LimitFind] SG_RESULT at standstill = %u\n",
                  _tmc[(int)axis]->SG_RESULT());
    Serial.println("[LimitFind] Moving to min...");

    _stepper[(int)axis]->moveAsync();
}

// ---------------------------------------------------------------------------
// findHome() — move to min end stop, zero there, back off.
// ---------------------------------------------------------------------------

void MountMotion::findHome(Axis axis, LimitsFoundCb cb) {
    if (axis != AXIS_SLIDER && axis != AXIS_ZOOM) return;
    if (_state == STATE_FINDING_LIMITS) return;

    emergencyStop();

    _lf_axis     = axis;
    _lf_cb       = cb;
    _lf_state    = LimitFindState::MOVING_TO_MIN;
    _lf_start_ms = millis();
    _homing_only = true;
    _state       = STATE_FINDING_LIMITS;

    _tmc[(int)axis]->rms_current(DEFAULT_CURRENT_MA[(int)axis] * LIMIT_FIND_CURRENT_SCALE[(int)axis]);
    _tmc[(int)axis]->SGTHRS(max((uint8_t)1,
                                (uint8_t)(_stall_threshold[(int)axis] / SGTHRS_DIVISOR[(int)axis])));
    _tmc[(int)axis]->TCOOLTHRS(0xFFFFF);  // enable StallGuard at all speeds

    _stall_isr_fired = false;
    attachInterrupt(digitalPinToInterrupt(PIN_DIAG[(int)axis]), _diag_isr, RISING);

    {
        uint32_t spd = (axis == AXIS_ZOOM) ? LIMIT_FIND_SPEED_ZOOM : LIMIT_FIND_SPEED;
        uint32_t acc = (axis == AXIS_ZOOM) ? LIMIT_FIND_ACCEL_ZOOM : LIMIT_FIND_ACCEL;
        _stepper[(int)axis]->setMaxSpeed(spd);
        _stepper[(int)axis]->setAcceleration(acc);
    }
    // If zoom is inverted, "fully zoomed out" (home) is at the positive physical
    // end — reverse the homing direction so we seek the correct end stop.
    bool go_positive = (axis == AXIS_ZOOM && _zoom_invert);
    _stepper[(int)axis]->setTargetAbs(go_positive ? 10000000L : -10000000L);
    _stepper[(int)axis]->moveAsync();
}

// ---------------------------------------------------------------------------
// _updateGoto() — velocity P-loop for position moves
//
// Called every loop() while STATE_MOVING_TO_POS.  Each axis runs rotateAsync()
// continuously; overrideSpeed() adjusts the fraction of max speed each tick.
// When err < decel_dist the speed scales proportionally so the axis decelerates
// naturally to a stop at the target — identical physics to a trapezoidal profile
// but implemented in software so the target can be changed at any time with no
// motor stop.
// ---------------------------------------------------------------------------

void MountMotion::_updateGoto() {
    bool any_active = false;

    for (int i = 0; i < 4; i++) {
        // If this axis already stopped and arrived, check whether retargetTo()
        // moved the target again — if so, restart rotateAsync for this axis.
        if (_goto_dir[i] == 0) {
            int32_t err = _goto_target[i] - _stepper[i]->getPosition();
            if (abs(err) > GOTO_ARRIVE_STEPS) {
                noInterrupts();
                _stepper[i]->setMaxSpeed((int32_t)_goto_max_spd_st[i]);
                _stepper[i]->setAcceleration(_goto_accel_st[i]);
                _stepper[i]->rotateAsync();
                interrupts();
                _goto_dir[i] = 1;
                // Recompute decel window for the new distance (retarget changed target)
                float spd = (float)_goto_max_spd_st[i];
                float acc = (float)_goto_accel_st[i];
                float decel_full = (acc > 0.f) ? (spd * spd) / (2.f * acc) : spd;
                float new_dist   = fabsf((float)err);
                _goto_decel_dist[i] = (new_dist < decel_full)
                                      ? fmaxf(new_dist, (float)(GOTO_ARRIVE_STEPS * 2))
                                      : decel_full;
            } else {
                continue;
            }
        }

        int32_t err = _goto_target[i] - _stepper[i]->getPosition();

        // ── Arrived ──────────────────────────────────────────────────────────
        if (abs(err) <= GOTO_ARRIVE_STEPS) {
            _stepper[i]->overrideSpeed(0.0f);
            noInterrupts();
            _stepper[i]->stopAsync();
            interrupts();
            _goto_dir[i] = 0;
            continue;
        }

        any_active = true;

        // Signed P-controller factor using the pre-computed (distance-capped)
        // decel window.  overrideSpeed() accepts negative values for reverse.
        float factor = (float)err / _goto_decel_dist[i];
        factor = constrain(factor, -1.0f, 1.0f);

        _stepper[i]->overrideSpeed(factor);
    }

    if (!any_active) {
        _has_goto_target = false;
        _state = STATE_IDLE;
    }
}

void MountMotion::_updateLimitFind() {
    Axis ax  = _lf_axis;
    int  idx = (int)ax;

    uint32_t elapsed   = millis() - _lf_start_ms;
    bool timed_out     = (elapsed > LIMIT_FIND_TIMEOUT_MS);
    uint32_t settle_ms = (_lf_axis == AXIS_ZOOM) ? LIMIT_STALL_SETTLE_ZOOM_MS : LIMIT_STALL_SETTLE_MS;
    bool stall_settled = (elapsed > settle_ms);  // ignore DIAG during accel ramp

    // ── Diagnostic: print DIAG + SG_RESULT every 250 ms ──────────────────
    static uint32_t _lf_last_print = 0;
    if (millis() - _lf_last_print > 250) {
        _lf_last_print = millis();
        int      pin_raw  = digitalRead(PIN_DIAG[idx]);
        bool     isr_flag = _stall_isr_fired;
        uint16_t sg       = _tmc[idx]->SG_RESULT();
        uint16_t thr      = (uint16_t)_stall_threshold[idx] * 2;
        uint32_t drv      = _tmc[idx]->DRV_STATUS();
        bool stst         = (drv >> 31) & 1;
        int32_t  pos      = _stepper[idx]->getPosition();
        Serial.printf("[LimitFind] t=%lums  settled=%d  pin=%d  isr=%d  SG=%u  thr=%u  stst=%d  pos=%ld\n",
                      elapsed, (int)stall_settled,
                      pin_raw, (int)isr_flag,
                      sg, thr, (int)stst, pos);
    }

    switch (_lf_state) {
        case LimitFindState::MOVING_TO_MIN: {
            if (timed_out || (stall_settled && _checkStall(ax))) {
                _stepper[idx]->stopAsync();
                //_lf_min_found = _stepper[idx]->getPosition();
                _stepper[idx]->setPosition(0);
                _lf_min_found = 0;

                // Back off from the end stop — direction is opposite to the
                // initial move (negative for normal, positive for inverted zoom).
                bool go_positive = (ax == AXIS_ZOOM && _zoom_invert);
                _stepper[idx]->setTargetAbs(go_positive ? -LIMIT_BACK_OFF[idx]
                                                        :  LIMIT_BACK_OFF[idx]);
                _stepper[idx]->moveAsync();
                _lf_state    = LimitFindState::BACKING_OFF_MIN;
                _lf_start_ms = millis();
            }
            break;
        }

        case LimitFindState::BACKING_OFF_MIN: {
            if (!_stepper[idx]->isMoving) {
                if (_homing_only) {
                    // findHome() — we have zeroed and backed off, we're done.
                    detachInterrupt(digitalPinToInterrupt(PIN_DIAG[idx]));
                    _tmc[idx]->TCOOLTHRS(0);
                    _tmc[idx]->rms_current(DEFAULT_CURRENT_MA[idx]);
                    _lf_state    = LimitFindState::IDLE;
                    _homing_only = false;
                    _state       = STATE_IDLE;
                    // findLimits() stores limits as (min=0, max=travel) for normal
                    // axes, or (min=-travel, max=0) for inverted zoom.  findHome()
                    // re-zeroes at the same physical point, so valid limits remain
                    // correct.
                    // Guard: if stored limits are inverted (min >= max) they are stale
                    // from a previous buggy findLimits run.  Clear them so the axis
                    // moves freely and the user can run findLimits again to fix them.
                    if (_limits_set[idx] && _min_limit[idx] >= _max_limit[idx]) {
                        _limits_set[idx] = false;
                        _min_limit[idx]  = 0;
                        _max_limit[idx]  = 0;
                    }
                    if (_lf_cb) _lf_cb(_lf_axis, _min_limit[idx], _max_limit[idx]);
                } else {
                    // findLimits() — continue to max end stop.
                    // For inverted zoom the max (zoom-in) end is in the negative
                    // direction; all other axes move positive to find their max.
                    _stall_isr_fired = false;  // clear any flag from min phase
                    {
                        bool go_max_positive = !(ax == AXIS_ZOOM && _zoom_invert);
                        _stepper[idx]->setTargetAbs(go_max_positive ? 10000000L : -10000000L);
                    }
                    _stepper[idx]->moveAsync();
                    _lf_state    = LimitFindState::MOVING_TO_MAX;
                    _lf_start_ms = millis();
                }
            }
            break;
        }

        case LimitFindState::MOVING_TO_MAX: {
            if (timed_out || (stall_settled && _checkStall(ax))) {
                _stepper[idx]->stopAsync();
                int32_t max_found = _stepper[idx]->getPosition();

                // Restore normal operation — full current, disable StallGuard
                detachInterrupt(digitalPinToInterrupt(PIN_DIAG[idx]));
                _tmc[idx]->TCOOLTHRS(0);
                _tmc[idx]->rms_current(DEFAULT_CURRENT_MA[idx]);

                // Position was zeroed at the min/home stall (position 0 = home).
                // For normal axes the usable range goes from 0 upward (positive).
                // For inverted zoom the range goes from 0 downward (negative), so
                // max_found is a large negative number.  Store as (negative, 0) so
                // that min_limit < max_limit and the jog soft-limit checks work correctly.
                if (ax == AXIS_ZOOM && _zoom_invert) {
                    setLimits(ax,
                              max_found + LIMIT_BACK_OFF[idx] + LIMIT_SAFETY_MARGIN[idx], // negative
                              0);   // home/end-stop is always position 0
                } else {
                    setLimits(ax, 0,
                              max_found - LIMIT_BACK_OFF[idx] - LIMIT_SAFETY_MARGIN[idx]);
                }

                _lf_state = LimitFindState::IDLE;
                _state    = STATE_IDLE;

                if (_lf_cb) _lf_cb(ax, _min_limit[idx], _max_limit[idx]);
            }
            break;
        }

        default:
            break;
    }
}

// ---------------------------------------------------------------------------
// _checkStall() — read DIAG1 pin (HIGH = stall detected by TMC2209)
// ---------------------------------------------------------------------------

bool MountMotion::_checkStall(Axis axis) {
    bool pin_high = (digitalRead(PIN_DIAG[(int)axis]) == HIGH);

    // ISR set on RISING edge — but only treat it as a real stall if the pin is
    // still HIGH right now.  Step-pulse EMI can fire a brief noise spike that
    // sets the flag even though the driver never actually stalled; requiring the
    // pin to still be HIGH filters those transient false positives.
    if (_stall_isr_fired) {
        _stall_isr_fired = false;
        if (pin_high) {
            Serial.printf("[_checkStall] ISR + pin HIGH → stall  axis=%d\n", (int)axis);
            return true;
        }
        Serial.printf("[_checkStall] ISR fired but pin LOW — noise, ignoring  axis=%d\n", (int)axis);
    }

    // Fallback: pin held HIGH without an ISR (e.g. edge was missed during a
    // critical section).
    if (pin_high) {
        Serial.printf("[_checkStall] pin poll HIGH → stall  axis=%d\n", (int)axis);
        return true;
    }
    return false;
}

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

void MountMotion::setSpeedPreset(AxisGroup group, uint8_t preset, SpeedPreset params) {
    if (group == GROUP_ZOOM) { _zoom_preset = params; return; }
    preset = constrain(preset, 1, 4);
    if (group == GROUP_PAN_TILT) _pt_presets[preset] = params;
    else                         _sl_presets[preset] = params;
}

SpeedPreset MountMotion::getSpeedPreset(AxisGroup group, uint8_t preset) const {
    if (group == GROUP_ZOOM) return _zoom_preset;
    preset = constrain(preset, 1, 4);
    if (group == GROUP_PAN_TILT) return _pt_presets[preset];
    return _sl_presets[preset];
}

void MountMotion::setZoomPreset(SpeedPreset params) {
    _zoom_preset = params;
}

SpeedPreset MountMotion::getZoomPreset() const {
    return _zoom_preset;
}

void MountMotion::setOrientation(bool pan_invert, bool tilt_invert, bool slider_invert, bool zoom_invert) {
    _pan_invert    = pan_invert;
    _tilt_invert   = tilt_invert;
    _slider_invert = slider_invert;
    _zoom_invert   = zoom_invert;
}

void MountMotion::setHasSlider(bool has_slider) {
    _has_slider = has_slider;
    if (has_slider)
        _setFlag(FLAG_HAS_SLIDER);
    else
        _clearFlag(FLAG_HAS_SLIDER);
}

void MountMotion::setRunCurrent(Axis axis, uint16_t current_ma) {
    _tmc[(int)axis]->rms_current(current_ma);
}

void MountMotion::setStallThreshold(Axis axis, uint8_t threshold) {
    _stall_threshold[(int)axis] = threshold;
    // Also write to the TMC chip's SGTHRS register immediately so the next
    // findLimits() (and eeprom_load restoration) uses the correct value.
    // Apply the same per-axis divisor that findLimits() uses.
    if (_tmc[(int)axis]) {
        uint8_t sgthrs = max((uint8_t)1,
                             (uint8_t)(threshold / SGTHRS_DIVISOR[(int)axis]));
        _tmc[(int)axis]->SGTHRS(sgthrs);
    }
}

// ---------------------------------------------------------------------------
// Status
// ---------------------------------------------------------------------------

bool MountMotion::isMoving() const {
    for (int i = 0; i < 4; i++)
        if (_stepper[i]->isMoving) return true;
    return false;
}

void MountMotion::printDriverDiagnostics() {
    static const char* AXIS_NAME[4]  = { "PAN ", "TILT", "SLDR", "ZOOM" };
    static const int   SERIAL_NUM[4] = { 5, 4, 3, 8 };

    Serial.println("\n=== TMC2209 Driver Diagnostics (bidirectional UART) ===");
    for (int i = 0; i < 4; i++) {
        Serial.printf("\n--- Axis %d (%s) | Serial%d | STEP=%d DIR=%d DIAG=%d ---\n",
                      i, AXIS_NAME[i], SERIAL_NUM[i],
                      (int)PIN_STEP[i], (int)PIN_DIR[i], (int)PIN_DIAG[i]);

        // Read all registers
        uint32_t gconf      = _tmc[i]->GCONF();
        uint32_t gstat      = _tmc[i]->GSTAT();
        uint32_t chopconf   = _tmc[i]->CHOPCONF();
        uint32_t ihold_irun = _tmc[i]->IHOLD_IRUN();
        uint32_t drv_status = _tmc[i]->DRV_STATUS();

        // Note: gconf==0xFFFFFFFF would indicate a framing error in some libraries,
        // but TMCStepper returns 0 on timeout/CRC failure for most registers.
        // Use IOIN version byte below as the authoritative read-health check.

        // ── GCONF key bits ────────────────────────────────────────────────
        bool pdn_disable     = (gconf >> 6) & 0x01;  // must be 1 for UART microstep control
        bool mstep_reg_sel   = (gconf >> 7) & 0x01;  // must be 1 to use MRES register
        bool en_spreadcycle  = (gconf >> 2) & 0x01;  // 0=StealthChop, 1=SpreadCycle
        Serial.printf("  GCONF       : 0x%08X\n", gconf);
        Serial.printf("    pdn_disable    = %d  (%s)\n", (int)pdn_disable,
                      pdn_disable   ? "OK — UART control enabled"
                                    : "*** FAIL — PDN pin controls mode, not UART ***");
        Serial.printf("    mstep_reg_sel  = %d  (%s)\n", (int)mstep_reg_sel,
                      mstep_reg_sel ? "OK — MRES register used"
                                    : "*** FAIL — MS1/MS2 pins control microsteps ***");
        Serial.printf("    en_spreadCycle = %d  (%s)\n", (int)en_spreadcycle,
                      en_spreadcycle ? "SpreadCycle" : "StealthChop");

        // ── GSTAT — driver reset / error flags ────────────────────────────
        bool reset_flag  = (gstat >> 0) & 0x01;  // 1 = driver was power-cycled/reset
        bool drv_err     = (gstat >> 1) & 0x01;
        bool uv_cp       = (gstat >> 2) & 0x01;  // undervoltage on charge-pump
        Serial.printf("  GSTAT       : 0x%08X\n", gstat);
        Serial.printf("    reset_flag = %d  (%s)\n", (int)reset_flag,
                      reset_flag ? "*** driver was reset since last readout — registers may be default ***"
                                 : "not reset");
        Serial.printf("    drv_err    = %d%s\n", (int)drv_err, drv_err ? "  *** ERROR ***" : "");
        Serial.printf("    uv_cp      = %d%s\n", (int)uv_cp,   uv_cp   ? "  *** undervoltage ***" : "");

        // ── CHOPCONF — microstep resolution ───────────────────────────────
        // MRES field bits [27:24]: 0=256, 1=128, 2=64, 3=32, 4=16, 5=8, 6=4, 7=2, 8=1
        uint8_t  mres_reg = (chopconf >> 24) & 0x0F;
        uint16_t mres_val = (uint16_t)(256 >> mres_reg);
        Serial.printf("  CHOPCONF    : 0x%08X\n", chopconf);
        Serial.printf("    MRES       = %u microsteps", mres_val);
        if (mres_val != MICROSTEPS[i])
            Serial.printf("  *** MISMATCH — expected %u ***", (int)MICROSTEPS[i]);
        Serial.println();

        // ── IHOLD_IRUN — current settings ────────────────────────────────
        uint8_t ihold = (ihold_irun >> 0) & 0x1F;   // bits [4:0]
        uint8_t irun  = (ihold_irun >> 8) & 0x1F;   // bits [12:8]
        // TMCStepper sets VSENSE (CHOPCONF bit 17) automatically:
        //   VSENSE=0 → V_FS=0.325V,  VSENSE=1 → V_FS=0.180V  (lower currents)
        // I_rms = (IRUN+1) × V_FS / (32 × √2 × (Rsense+0.02Ω))
        bool     vsense   = (chopconf >> 17) & 0x01;
        float    v_fs     = vsense ? 0.180f : 0.325f;
        uint16_t irun_ma  = (uint16_t)((irun + 1) * v_fs * 1000.0f
                                       / (32.0f * 1.41421f * (0.11f + 0.02f)));
        Serial.printf("  IHOLD_IRUN  : 0x%08X  IHOLD=%u  IRUN=%u  VSENSE=%d"
                      "  → ~%u mA rms  (configured %u mA)\n",
                      ihold_irun, (unsigned)ihold, (unsigned)irun, (int)vsense,
                      (unsigned)irun_ma, (unsigned)DEFAULT_CURRENT_MA[i]);

        // ── IOIN — hardware version register (never written by firmware) ──
        // Bits [31:24] = VERSION, always 0x21 on TMC2209.
        // This is the definitive UART-read health check: if it returns
        // non-zero with the correct version byte, reads are working;
        // if it returns 0x00000000, the Teensy RX pin has no return path.
        uint32_t ioin    = _tmc[i]->IOIN();
        uint8_t  version = (ioin >> 24) & 0xFF;
        Serial.printf("  IOIN        : 0x%08X  version=0x%02X  →  ", ioin, version);
        if (version == 0x21)
            Serial.println("TMC2209 confirmed — UART reads OK");
        else if (version == 0x00)
            Serial.println("*** 0x00 — UART reads broken (check RX wiring to PDN_UART) ***");
        else
            Serial.printf("*** unexpected version 0x%02X ***\n", version);

        // ── DRV_STATUS ────────────────────────────────────────────────────
        uint16_t sg_result = (drv_status >> 10) & 0x3FF;
        uint8_t  cs_actual = (drv_status >> 16) & 0x1F;
        bool     stst      = (drv_status >> 31) & 0x01;
        Serial.printf("  DRV_STATUS  : 0x%08X\n", drv_status);
        Serial.printf("    SG_RESULT  = %u  (%s)\n", sg_result,
                      sg_result == 0 ? "STALL" : "ok");
        Serial.printf("    CS_ACTUAL  = %u / 31\n", cs_actual);
        Serial.printf("    Standstill = %s\n", stst ? "yes" : "no");

        // ── Computed step rates for each preset ──────────────────────────
        if (i == AXIS_ZOOM) {
            // ZOOM has a single independent preset (not 4)
            uint32_t spd_st   = (uint32_t)physToUSteps(i, _zoom_preset.max_speed);
            uint32_t accel_st = (uint32_t)physToUSteps(i, _zoom_preset.acceleration);
            Serial.printf("  Zoom preset : %5lu µstep/s  %6lu µstep/s²"
                          "  (phys: %lu °/s  %lu °/s²)\n",
                          spd_st, accel_st,
                          (unsigned long)_zoom_preset.max_speed,
                          (unsigned long)_zoom_preset.acceleration);
        } else {
            Serial.println("  Preset step rates (µsteps/sec | µsteps/sec²):");
            for (int p = 1; p <= 4; p++) {
                const SpeedPreset &sp = (i < 2) ? _pt_presets[p] : _sl_presets[p];
                uint32_t spd_st   = (uint32_t)physToUSteps(i, sp.max_speed);
                uint32_t accel_st = (uint32_t)physToUSteps(i, sp.acceleration);
                Serial.printf("    preset %d: %5lu µstep/s  %6lu µstep/s²"
                              "  (phys: %lu %s  %lu %s²)\n",
                              p, spd_st, accel_st,
                              (unsigned long)sp.max_speed,
                              (i == 2) ? "mm/s" : "°/s",
                              (unsigned long)sp.acceleration,
                              (i == 2) ? "mm/s" : "°/s");
            }
        }

        // ── DIAG pin ─────────────────────────────────────────────────────
        Serial.printf("  DIAG pin %-2d : %s\n", (int)PIN_DIAG[i],
                      digitalRead(PIN_DIAG[i]) ? "HIGH (stall!)" : "LOW (ok)");
    }
    Serial.println("\n=== End Diagnostics ===\n");
}

MountStatusSnapshot MountMotion::getStatus() const {
    MountStatusSnapshot s;
    for (int i = 0; i < 4; i++)
        s.pos[i] = _stepper[i]->getPosition();
    s.state = _state;
    s.flags = _flags;
    return s;
}

float MountMotion::positionPhys(uint8_t axis) const {
    if (axis > AXIS_ZOOM) return 0.0f;
    float steps_per_unit = physToUSteps(axis, 1.0f);   // usteps per deg / mm
    if (steps_per_unit == 0.0f) return 0.0f;
    return (float)_stepper[axis]->getPosition() / steps_per_unit;
}

uint8_t MountMotion::movingMask() const {
    uint8_t m = 0;
    for (int i = 0; i < 4; i++)
        if (_stepper[i]->isMoving) m |= (uint8_t)(1u << i);
    return m;
}

// ---------------------------------------------------------------------------
// Private helpers
// ---------------------------------------------------------------------------

void MountMotion::_applyPreset(uint8_t pt_preset, uint8_t sl_preset) {
    pt_preset = constrain(pt_preset, 1, 4);
    sl_preset = constrain(sl_preset, 1, 4);

    // Set each axis to its own preset ceiling.
    // _group.startMove() synchronises by finding the axis whose travel takes
    // longest relative to its maxSpeed, uses that duration for all axes, and
    // slows the remaining axes down proportionally — so no axis ever exceeds
    // the maxSpeed set here.
    for (int i = 0; i < 4; i++) {
        const SpeedPreset &sp = (i < 2) ? _pt_presets[pt_preset]
                                        : (i == 2) ? _sl_presets[sl_preset]
                                        : _zoom_preset;
        _stepper[i]->setMaxSpeed(    (uint32_t)physToUSteps(i, sp.max_speed));
        _stepper[i]->setAcceleration((uint32_t)physToUSteps(i, sp.acceleration));
    }
}

int16_t MountMotion::_applyOrientation(Axis axis, int16_t velocity) const {
    if (axis == AXIS_PAN    && _pan_invert)    return -velocity;
    if (axis == AXIS_TILT   && _tilt_invert)  return -velocity;
    if (axis == AXIS_SLIDER && _slider_invert) return -velocity;
    if (axis == AXIS_ZOOM   && _zoom_invert)   return -velocity;
    return velocity;
}

int32_t MountMotion::_applyOrientationPos(Axis axis, int32_t target) const {
    // Mirror the target about 0 when inverted
    if (axis == AXIS_PAN    && _pan_invert)    return -target;
    if (axis == AXIS_TILT   && _tilt_invert)  return -target;
    if (axis == AXIS_SLIDER && _slider_invert) return -target;
    if (axis == AXIS_ZOOM   && _zoom_invert)   return -target;
    return target;
}

void MountMotion::_clampToLimits(int32_t &target, Axis axis) const {
    if (!_limits_set[(int)axis]) return;
    target = constrain(target, _min_limit[(int)axis], _max_limit[(int)axis]);
}

// ===========================================================================
// v2 — Look-at tracking
// ===========================================================================

// ---------------------------------------------------------------------------
// setLookAtSubject()  — update the 3D world target (safe to call mid-move)
// ---------------------------------------------------------------------------

void MountMotion::setLookAtSubject(float sx, float sy, float sz, uint8_t subject_id) {
    // Atomic write — update atomically enough for Teensy (no ISR touches these)
    _la_subject_x  = sx;
    _la_subject_y  = sy;
    _la_subject_z  = sz;
    _la_subject_id = subject_id;

    // When switching subjects during an active look-at move, arm the slew-accel
    // grace period so _driveTowardTarget() uses reduced acceleration for the full
    // duration of the reposition, not just on ticks where abs_err happens to be
    // above the KP threshold (which can shrink quickly as the motor starts moving).
    if (_state == STATE_LOOK_AT_MOVE || _state == STATE_LOOK_AT_PRE_AIM) {
        _la_slew_until_ms = millis() + LOOK_AT_SLEW_DURATION_MS;
    }
}

// ---------------------------------------------------------------------------
// startLookAtMove()
//   Slider moves from slider_start_steps → slider_end_steps at sl_preset.
//   Pan and tilt are driven by the look-at controller at up to max_pt_deg_s.
//   Returns false if the reference is not set or slider limits not found.
// ---------------------------------------------------------------------------

bool MountMotion::startLookAtMove(int32_t slider_start_steps, int32_t slider_end_steps,
                                   uint8_t sl_preset, float max_pt_deg_s) {
    if (!_ref_set)               return false;
    if (!_limits_set[AXIS_SLIDER]) return false;

    // Stop anything currently running
    emergencyStop();

    sl_preset = constrain(sl_preset, 1, 4);

    // Apply orientation / clamp slider targets
    int32_t s_start = _applyOrientationPos(AXIS_SLIDER, slider_start_steps);
    int32_t s_end   = _applyOrientationPos(AXIS_SLIDER, slider_end_steps);
    _clampToLimits(s_start, AXIS_SLIDER);
    _clampToLimits(s_end,   AXIS_SLIDER);

    // ── Convert deg/s → µstep/s for look-at speed limits ────────────────────
    // Uses each axis's own calibrated deg/µstep so they scale independently
    // if calibration refinement nudges the values apart.
    _la_max_steps_s[0] = max_pt_deg_s / _pan_deg_per_step;   // pan
    _la_max_steps_s[1] = max_pt_deg_s / _tilt_deg_per_step;  // tilt
    _la_pt_dir[0] = 0;
    _la_pt_dir[1] = 0;

    // ── PRE-AIM PHASE ────────────────────────────────────────────────────────
    // Compute the correct pan/tilt angles for the current slider position and
    // start driving there BEFORE the slider moves.  This eliminates the visible
    // jerk at look-at start caused by any position error left over from
    // calibration (e.g. camera at SET_B tilt != look-at formula tilt).
    {
        int32_t sl_phys_now = _stepper[AXIS_SLIDER]->getPosition();
        float cx_now = (float)sl_phys_now * _slider_mm_per_step;
        if (_slider_invert) cx_now = -cx_now;

        float dx = _la_subject_x - cx_now;
        float dy = _la_subject_y;
        float dz = _la_subject_z;

        float pan_deg_now  = atan2f(dx, dz) * (180.0f / (float)M_PI);
        float tilt_deg_now = atan2f(dy, sqrtf(dx * dx + dz * dz)) * (180.0f / (float)M_PI);

        int32_t pan_tgt  = (int32_t)((pan_deg_now  - _pan_ref_deg)  / _pan_deg_per_step);
        int32_t tilt_tgt = (int32_t)((tilt_deg_now - _tilt_ref_deg) / _tilt_deg_per_step);
        if (_pan_invert)  pan_tgt  = -pan_tgt;
        if (_tilt_invert) tilt_tgt = -tilt_tgt;

        _la_pre_aim_pan_tgt  = pan_tgt;
        _la_pre_aim_tilt_tgt = tilt_tgt;

        int32_t pan_err  = pan_tgt  - _stepper[AXIS_PAN ]->getPosition();
        int32_t tilt_err = tilt_tgt - _stepper[AXIS_TILT]->getPosition();

        Serial.printf("[LookAt] PRE-AIM: pan_tgt=%ld (err=%ld)  tilt_tgt=%ld (err=%ld)\n",
                      (long)pan_tgt, (long)pan_err, (long)tilt_tgt, (long)tilt_err);

        if (labs(pan_err) <= 50 && labs(tilt_err) <= 50) {
            // Already close enough — skip pre-aim and start slider immediately
            Serial.printf("[LookAt] PRE-AIM skipped (already on target)\n");
        } else {
            // Drive pan/tilt to the correct start position at full look-at speed
            // (no BRAKE_FACTOR cap here — we want a crisp, fast snap to position).
            float pan_spd_s  = _la_max_steps_s[0];
            float tilt_spd_s = _la_max_steps_s[1];
            noInterrupts();
            _stepper[AXIS_PAN ]->setMaxSpeed((uint32_t)pan_spd_s);
            _stepper[AXIS_PAN ]->setAcceleration((uint32_t)(pan_spd_s  * 3.0f));
            _stepper[AXIS_PAN ]->setTargetAbs(pan_tgt);
            _stepper[AXIS_PAN ]->moveAsync();
            _stepper[AXIS_TILT]->setMaxSpeed((uint32_t)tilt_spd_s);
            _stepper[AXIS_TILT]->setAcceleration((uint32_t)(tilt_spd_s * 3.0f));
            _stepper[AXIS_TILT]->setTargetAbs(tilt_tgt);
            _stepper[AXIS_TILT]->moveAsync();
            interrupts();

            // Store slider move params — slider will start once pre-aim settles
            _la_stored_s_end      = s_end;
            _la_stored_sl_preset  = sl_preset;

            _state = STATE_LOOK_AT_PRE_AIM;
            _setFlag(FLAG_LOOK_AT_ACTIVE);
            return true;
        }
    }

    // ── Slider starts immediately (pre-aim was not needed) ───────────────────
    const SpeedPreset &sp = _sl_presets[sl_preset];
    noInterrupts();
    _stepper[AXIS_SLIDER]->setMaxSpeed(    (uint32_t)physToUSteps(AXIS_SLIDER, sp.max_speed));
    _stepper[AXIS_SLIDER]->setAcceleration((uint32_t)physToUSteps(AXIS_SLIDER, sp.acceleration));
    _stepper[AXIS_SLIDER]->setTargetAbs(s_end);
    _stepper[AXIS_SLIDER]->moveAsync();
    interrupts();
    _goto_target[AXIS_SLIDER] = s_end;

    _la_last_update_ms = millis();
    _state = STATE_LOOK_AT_MOVE;
    _setFlag(FLAG_LOOK_AT_ACTIVE);
    return true;
}

// ---------------------------------------------------------------------------
// aimAtSubject()
//   Compute the pan/tilt angles needed to point at the current look-at subject
//   from the current slider position, then command a regular moveTo() for pan
//   and tilt only (slider and zoom stay where they are).  No STATE_LOOK_AT_MOVE
//   transition — the axes just drive to the computed target and stop.
// ---------------------------------------------------------------------------

bool MountMotion::aimAtSubject(uint8_t pt_preset) {
    if (!_ref_set) return false;

    // Current slider position in world-frame mm (undo motor-frame inversion)
    int32_t sl_phys = _stepper[AXIS_SLIDER]->getPosition();
    float cx = (float)sl_phys * _slider_mm_per_step;
    if (_slider_invert) cx = -cx;

    // Vector from camera to subject
    float dx = _la_subject_x - cx;
    float dy = _la_subject_y;
    float dz = _la_subject_z;

    // Required world-frame angles
    float pan_deg  = atan2f(dx, dz) * (180.0f / (float)M_PI);
    float tilt_deg = atan2f(dy, sqrtf(dx * dx + dz * dz)) * (180.0f / (float)M_PI);

    // Convert to LOGICAL step targets.
    // moveTo() calls _applyOrientationPos() on PAN, TILT, and SLIDER internally,
    // so we must NOT pre-apply the inversion flags here — doing so would
    // double-invert and send the axes to the mirror of the correct position.
    int32_t pan_target  = (int32_t)((pan_deg  - _pan_ref_deg)  / _pan_deg_per_step);
    int32_t tilt_target = (int32_t)((tilt_deg - _tilt_ref_deg) / _tilt_deg_per_step);

    Serial.printf("[Aim] sl_phys=%ld cx=%.1fmm  subj=(%.1f, %.1f, %.1f)mm\n",
                  (long)sl_phys, cx, _la_subject_x, _la_subject_y, _la_subject_z);
    Serial.printf("[Aim] dx=%.1f dy=%.1f dz=%.1f  pan=%.2f° tilt=%.2f°\n",
                  dx, dy, dz, pan_deg, tilt_deg);
    Serial.printf("[Aim] ref: pan_ref=%.4f° tilt_ref=%.4f°\n",
                  _pan_ref_deg, _tilt_ref_deg);
    Serial.printf("[Aim] target: pan=%ld  tilt=%ld  (logical steps)\n",
                  (long)pan_target, (long)tilt_target);

    // Convert current physical slider position to logical for moveTo().
    // Zoom has no orientation transform in moveTo() — pass its physical position
    // directly so the zoom stays exactly where it is.
    // (Same pattern as moveRel() — see comment there for the full explanation.)
    int32_t sl_log   = _slider_invert ? -sl_phys : sl_phys;
    int32_t zoom_phys = _stepper[AXIS_ZOOM]->getPosition();

    // sync=false: each axis runs at full preset speed.  Syncing pan to tilt
    // (or vice versa) for a large/small distance ratio would make one axis
    // crawl — for a quick look-at re-aim we want both axes as fast as possible.
    moveTo(pan_target, tilt_target, sl_log, zoom_phys, pt_preset, pt_preset, /*sync=*/false);
    return true;
}

// ---------------------------------------------------------------------------
// stopLookAtMove()
// ---------------------------------------------------------------------------

void MountMotion::stopLookAtMove() {
    if (_state != STATE_LOOK_AT_MOVE && _state != STATE_LOOK_AT_PRE_AIM) return;
    noInterrupts();
    for (int i = 0; i < 4; i++) {
        _stepper[i]->stopAsync();
        _jog_dir[i] = 0;   // clear any zoom (or other) jog that ran during the move
    }
    interrupts();
    _jogging      = false;
    _la_pt_dir[0] = 0;
    _la_pt_dir[1] = 0;
    _clearFlag(FLAG_LOOK_AT_ACTIVE);
    _state = STATE_IDLE;
}

// ---------------------------------------------------------------------------
// _updatePreAim()  — called from update() while STATE_LOOK_AT_PRE_AIM
//
// Waits for pan and tilt to reach their pre-computed start positions, then
// starts the slider and transitions to STATE_LOOK_AT_MOVE.
//
// The pre-aim targets were stored by startLookAtMove() and the steppers were
// already commanded via setTargetAbs()+moveAsync().  We poll here rather than
// relying on a callback so the logic stays inside the regular update() loop
// with no interrupt-context overhead.
// ---------------------------------------------------------------------------

void MountMotion::_updatePreAim() {
    int32_t pan_pos  = _stepper[AXIS_PAN ]->getPosition();
    int32_t tilt_pos = _stepper[AXIS_TILT]->getPosition();
    int32_t pan_err  = labs(_la_pre_aim_pan_tgt  - pan_pos);
    int32_t tilt_err = labs(_la_pre_aim_tilt_tgt - tilt_pos);

    // Settled = both axes within 50 steps of target and neither is moving
    bool pan_done  = (pan_err  <= 50) || !_stepper[AXIS_PAN ]->isMoving;
    bool tilt_done = (tilt_err <= 50) || !_stepper[AXIS_TILT]->isMoving;

    if (!pan_done || !tilt_done) return;   // still settling — check again next loop()

    // Pan/tilt are on target.  Start the slider.
    Serial.printf("[LookAt] Pre-aim done (pan_err=%ld tilt_err=%ld) — starting slider\n",
                  (long)pan_err, (long)tilt_err);

    const SpeedPreset &sp = _sl_presets[_la_stored_sl_preset];
    noInterrupts();
    _stepper[AXIS_SLIDER]->setMaxSpeed(    (uint32_t)physToUSteps(AXIS_SLIDER, sp.max_speed));
    _stepper[AXIS_SLIDER]->setAcceleration((uint32_t)physToUSteps(AXIS_SLIDER, sp.acceleration));
    _stepper[AXIS_SLIDER]->setTargetAbs(_la_stored_s_end);
    _stepper[AXIS_SLIDER]->moveAsync();
    interrupts();
    _goto_target[AXIS_SLIDER] = _la_stored_s_end;

    _la_pt_dir[0]      = 0;
    _la_pt_dir[1]      = 0;
    _la_last_update_ms = millis();
    _state             = STATE_LOOK_AT_MOVE;
}

// ---------------------------------------------------------------------------
// _updateLookAt()  — called from update() every LOOK_AT_INTERVAL_MS
// ---------------------------------------------------------------------------

void MountMotion::_updateLookAt() {
    uint32_t now = millis();
    if (now - _la_last_update_ms < LOOK_AT_INTERVAL_MS) return;
    _la_last_update_ms = now;

    // 1. Current slider position in mm
    int32_t sl_phys = _stepper[AXIS_SLIDER]->getPosition();
    float cx = (float)sl_phys * _slider_mm_per_step;
    // Undo orientation: the stepper position is in motor-frame (possibly inverted).
    // We want world-frame mm along the rail (always positive from home).
    if (_slider_invert) cx = -cx;

    // 2. Vector from camera to subject
    float dx = _la_subject_x - cx;
    float dy = _la_subject_y;
    float dz = _la_subject_z;   // Z positive = into room (away from rail)

    // 3. Required pan/tilt angles (world frame)
    float pan_rad  = atan2f(dx, dz);
    float tilt_rad = atan2f(dy, sqrtf(dx * dx + dz * dz));
    float pan_deg  = pan_rad  * (180.0f / (float)M_PI);
    float tilt_deg = tilt_rad * (180.0f / (float)M_PI);

    // 4. Convert world-frame angles to target step counts via session reference
    int32_t pan_target  = (int32_t)((pan_deg  - _pan_ref_deg)  / _pan_deg_per_step);
    int32_t tilt_target = (int32_t)((tilt_deg - _tilt_ref_deg) / _tilt_deg_per_step);

    // Apply orientation inversions
    if (_pan_invert)  pan_target  = -pan_target;
    if (_tilt_invert) tilt_target = -tilt_target;

    // ── Rate-limited serial debug (every 500 ms) ─────────────────────────────
    static uint32_t _la_dbg_ms = 0;
    if (now - _la_dbg_ms >= 500) {
        _la_dbg_ms = now;
        int32_t pan_pos  = _stepper[AXIS_PAN ]->getPosition();
        int32_t tilt_pos = _stepper[AXIS_TILT]->getPosition();
        float pan_err  = (float)(pan_target  - pan_pos);
        float tilt_err = (float)(tilt_target - tilt_pos);
        Serial.printf("[LA] sl_phys=%ld  cx=%.1fmm  subj=(%.1f,%.1f,%.1f)\n",
                      (long)sl_phys, cx,
                      _la_subject_x, _la_subject_y, _la_subject_z);
        Serial.printf("[LA] dx=%.1f dz=%.1f  pan=%.3f° tilt=%.3f°\n",
                      dx, dz, pan_deg, tilt_deg);
        Serial.printf("[LA] ref: pan_ref=%.3f° tilt_ref=%.3f°\n",
                      _pan_ref_deg, _tilt_ref_deg);
        Serial.printf("[LA] pan_tgt=%ld pos=%ld err=%.0f | tilt_tgt=%ld pos=%ld err=%.0f\n",
                      (long)pan_target, (long)pan_pos, pan_err,
                      (long)tilt_target, (long)tilt_pos, tilt_err);
        Serial.printf("[LA] slider moving=%d\n", (int)_stepper[AXIS_SLIDER]->isMoving);
    }
    // ─────────────────────────────────────────────────────────────────────────

    // 5. Coordinated pan/tilt motion during a slew (subject switch).
    //
    // Without coordination, the axis with the smaller angular error arrives
    // first and the camera drifts sideways before the other axis catches up —
    // a visible arc rather than a straight line in angle-space.
    //
    // Fix: compare time-to-arrive for each axis:
    //      T = error / max_speed
    // The axis with the LARGER T (further from target in time) runs at its
    // full speed limit.  The other axis is scaled down so its T matches:
    //      speed_fast = speed_fast_max × (err_fast / err_slow) × (spd_slow_max / spd_slow_max)
    //                 = err_fast × spd_slow_max / err_slow    [if tilt is slower]
    //
    // Coordination is active only during the slew grace period so normal
    // steady-state tracking (small oscillating errors) is not affected.
    // We stop coordinating once either axis drops below MIN_SYNC_ERR steps
    // so the P-controller can nail the final approach independently.
    float pan_speed_limit  = _la_max_steps_s[0];
    float tilt_speed_limit = _la_max_steps_s[1];

    if (millis() < _la_slew_until_ms) {
        int32_t cur_pan_pos  = _stepper[AXIS_PAN ]->getPosition();
        int32_t cur_tilt_pos = _stepper[AXIS_TILT]->getPosition();
        float pe = fabsf((float)(pan_target  - cur_pan_pos));
        float te = fabsf((float)(tilt_target - cur_tilt_pos));

        // Only synchronise while both axes still have meaningful travel left.
        constexpr float MIN_SYNC_ERR = 200.0f;   // ~0.1° at typical resolution
        if (pe > MIN_SYNC_ERR && te > MIN_SYNC_ERR) {
            float ps = _la_max_steps_s[0];   // pan  max steps/s
            float ts = _la_max_steps_s[1];   // tilt max steps/s
            // T_pan = pe/ps,  T_tilt = te/ts.  The larger T is the bottleneck.
            if ((pe / ps) >= (te / ts)) {
                // Pan takes longer — keep pan at full, slow tilt to match.
                tilt_speed_limit = (te * ps) / pe;   // = ts × (T_tilt / T_pan)
            } else {
                // Tilt takes longer — keep tilt at full, slow pan to match.
                pan_speed_limit  = (pe * ts) / te;   // = ps × (T_pan  / T_tilt)
            }
            // Clamp (rounding safety — neither limit should exceed its axis max)
            pan_speed_limit  = fminf(pan_speed_limit,  ps);
            tilt_speed_limit = fminf(tilt_speed_limit, ts);
        }
    }

    _driveTowardTarget(AXIS_PAN,  pan_target,  pan_speed_limit);
    _driveTowardTarget(AXIS_TILT, tilt_target, tilt_speed_limit);

    // 7. Detect slider arrival — transition to IDLE when slider finishes.
    // Guard: require BOTH !isMoving AND proximity to the stored destination.
    // Using only !isMoving would fire immediately if the slider was already at the
    // destination when startLookAtMove() was called (e.g. user presses the arrow
    // toward the limit the slider is already at), because moveAsync() targeting
    // the current position leaves isMoving=false from the very first tick.
    {
        int32_t sl_pos = _stepper[AXIS_SLIDER]->getPosition();
        int32_t sl_err = labs(sl_pos - _goto_target[AXIS_SLIDER]);
        if (!_stepper[AXIS_SLIDER]->isMoving && sl_err <= GOTO_ARRIVE_STEPS) {
            Serial.printf("[LA] Slider arrived at phys=%ld (dest=%ld err=%ld) — ending look-at\n",
                          (long)sl_pos, (long)_goto_target[AXIS_SLIDER], (long)sl_err);
            stopLookAtMove();
        }
    }
}

// ---------------------------------------------------------------------------
// _driveTowardTarget()  — velocity controller for one axis (PAN or TILT)
//
// Called every 20 ms.  Computes a signed speed and applies it via
// setMaxSpeed(signed) + rotateAsync() — see earlier note on why this pattern
// is used instead of overrideSpeed().
//
// Speed selection — the minimum of two profiles:
//
//   v_p   = (|err| / KP_STEPS) × max_speed   [P-proportional]
//   v_sqrt = sqrt(2 × acc × |err|)            [constant-deceleration stopping]
//
//   • For small errors (< ~156 steps): v_p < v_sqrt → P wins.
//     Motor speed is proportional to error — steady-state tracking of a
//     slowly-moving geometric target.
//
//   • For large errors (subject switch, > ~156 steps): v_sqrt < v_p → sqrt wins.
//     Speed is exactly what is needed to decelerate and arrive at the target
//     without overshoot.  The camera sweeps quickly to the new subject and
//     stops cleanly — no bounce.
//
// The crossover point (v_p == v_sqrt) is at:
//   err_crossover = 2·acc·KP_STEPS² / max_steps_s²
//                = v_max_braking · KP_STEPS / max_steps_s   (substituting acc formula)
//                = KP_STEPS · BRAKE_FACTOR                  (simplifying)
//
// Example (normal): KP_STEPS=5000, BRAKE_FACTOR=0.25 → crossover = 1250 steps ≈ 0.59°
//   • err > 1250: v_sqrt > v_p — motor runs at exactly v_max_braking toward target
//   • err < 1250: v_p < v_sqrt — speed is proportional to error (P-tracking)
//
// Because the braking distance from v_max_braking equals the crossover distance
// (by construction), the motor joins the P-region exactly as it reaches v_p speed
// — zero excess velocity, no overshoot.
//
// During a subject switch (slew mode), SLEW_BRAKE_FACTOR replaces BRAKE_FACTOR.
// Both v_max_braking and acc scale down proportionally so the profile stays
// self-consistent — the motor moves slower but still stops cleanly.
// (Reducing motor_acc while keeping v_max_braking the same would break stopping.)
// ---------------------------------------------------------------------------

void MountMotion::_driveTowardTarget(int axis, int32_t target, float max_steps_s) {
    float err     = (float)(target - _stepper[axis]->getPosition());
    float abs_err = fabsf(err);

    // Proportional speed: tracks a slowly-moving target; clamped to max.
    float v_p = (abs_err / (float)LOOK_AT_KP_STEPS) * max_steps_s;
    if (v_p > max_steps_s) v_p = max_steps_s;

    // Square-root braking speed: fastest speed that allows stopping at target
    // without overshoot.
    //
    // LOOK_AT_BRAKE_FACTOR caps the peak speed for subject switches / large
    // corrections as a fraction of max_pt_dps (90 deg/s).  Tracking during
    // a slider move is governed by the P-gain, not this cap, so raising it
    // does not make continuous tracking feel faster or more jerky.
    //   0.10 → ~9 deg/s,   very smooth (but can't track fast sliders)
    //   0.20 → ~18 deg/s,  smooth broadcast pan
    //   0.25 → ~22.5 deg/s (current — enough margin for 80 mm/s slider)
    //   0.40 → ~36 deg/s,  fast subject switch
    // During a subject switch (slew mode), limit peak speed so the camera ramps
    // up gently rather than lurching to the new target.
    //
    // IMPORTANT: we reduce v_max_braking (not motor_acc) so that acc scales
    // down proportionally.  The v_sqrt stopping profile assumes deceleration = acc;
    // if we instead set motor_acc = acc * small_factor while keeping v_max_braking
    // the same, the motor needs (1/factor)× the designed braking distance to stop,
    // causing massive overshoot.  Reducing v_max_braking keeps the physics
    // self-consistent: the motor moves slower but always stops cleanly at target.
    //
    // LOOK_AT_BRAKE_FACTOR      = 0.25  → normal peak ≈ 22.5 deg/s
    // LOOK_AT_SLEW_BRAKE_FACTOR = 0.10  → slew peak   ≈  9   deg/s  (gentle)
    //
    // Two conditions trigger slew mode — either is sufficient:
    //   1. abs_err > KP_STEPS × BRAKE_FACTOR (1250 steps) — catches the full
    //      sqrt-braking phase; the old KP_STEPS threshold left 1250–5000 steps
    //      at full speed, causing jerk during mid-move subject switches.
    //   2. millis() < _la_slew_until_ms — 2 s grace period armed when a new
    //      subject is selected mid-move, keeps motion gentle as error shrinks
    //      into the P-control region.
    constexpr float LOOK_AT_BRAKE_FACTOR       = 0.25f;
    constexpr float LOOK_AT_SLEW_BRAKE_FACTOR  = 0.15f;
    constexpr float LOOK_AT_SLEW_ERR_THRESHOLD = LOOK_AT_KP_STEPS * LOOK_AT_BRAKE_FACTOR;

    // How much faster the motor ramps UP to peak speed compared to the braking
    // curve's deceleration rate.  The braking curve (v_sqrt) is computed from acc
    // and governs the final approach — it is unaffected by this multiplier.
    // A higher value lets the camera get moving immediately after a subject switch
    // without reducing peak speed.  Safe to raise because:
    //   • Higher stepper acc makes the braking curve more conservative (shorter
    //     braking distance needed), so it cannot cause overshoot.
    //   • During an active slider move the angular target shifts every 20 ms,
    //     so any transient overshoot self-corrects on the very next tick.
    // 3.0 = ramps to peak speed ~3× faster than the minimum needed to stop cleanly.
    constexpr float LOOK_AT_SLEW_ACCEL_BOOST   = 3.0f;

    bool in_slew = (abs_err > LOOK_AT_SLEW_ERR_THRESHOLD)
                || (millis() < _la_slew_until_ms);

    float effective_brake = in_slew ? LOOK_AT_SLEW_BRAKE_FACTOR : LOOK_AT_BRAKE_FACTOR;
    float v_max_braking   = max_steps_s * effective_brake;

    // acc is derived so braking_distance == P/sqrt crossover distance.
    //   acc = v_max_braking · max_steps_s / (2·KP_STEPS)
    // This value is used for the v_sqrt braking curve only.  The stepper itself
    // is given a boosted acceleration so it reaches v_max_braking faster, while
    // the braking profile (and therefore stopping accuracy) is unchanged.
    float acc        = (v_max_braking * max_steps_s) / (2.0f * LOOK_AT_KP_STEPS);
    float stepper_acc = in_slew ? (acc * LOOK_AT_SLEW_ACCEL_BOOST) : acc;

    float v_sqrt = sqrtf(2.0f * acc * abs_err);
    if (v_sqrt > v_max_braking) v_sqrt = v_max_braking;

    // Use the smaller of the two: P for steady tracking, sqrt for large-error braking.
    float abs_speed = (v_p < v_sqrt) ? v_p : v_sqrt;

    int32_t signed_spd;
    if      (err >  0.5f) signed_spd =  (int32_t)abs_speed;
    else if (err < -0.5f) signed_spd = -(int32_t)abs_speed;
    else                  signed_spd = 0;

    noInterrupts();
    _stepper[axis]->setMaxSpeed(signed_spd);
    _stepper[axis]->setAcceleration((uint32_t)(stepper_acc > 0.0f ? stepper_acc : 1.0f));
    _stepper[axis]->rotateAsync();
    interrupts();
}

// ===========================================================================
// v2 — Calibration support setters / getters
// ===========================================================================

void MountMotion::setDegPerStep(float pan_dps, float tilt_dps) {
    if (pan_dps  > 0.0f) _pan_deg_per_step  = pan_dps;
    if (tilt_dps > 0.0f) _tilt_deg_per_step = tilt_dps;
}

void MountMotion::setSliderMmPerStep(float mm_per_step) {
    if (mm_per_step > 0.0f) _slider_mm_per_step = mm_per_step;
}

void MountMotion::setLookAtRef(float pan_ref_deg, float tilt_ref_deg) {
    _pan_ref_deg  = pan_ref_deg;
    _tilt_ref_deg = tilt_ref_deg;
    _ref_set      = true;
    _setFlag(FLAG_REF_SET);
    Serial.printf("[SetRef] pan_ref=%.4f°  tilt_ref=%.4f°\n",
                  pan_ref_deg, tilt_ref_deg);
}

float MountMotion::getPanDeg() const {
    if (!_ref_set) return 0.0f;
    float steps = (float)_stepper[AXIS_PAN]->getPosition();
    if (_pan_invert) steps = -steps;
    return _pan_ref_deg + steps * _pan_deg_per_step;
}

float MountMotion::getTiltDeg() const {
    if (!_ref_set) return 0.0f;
    float steps = (float)_stepper[AXIS_TILT]->getPosition();
    if (_tilt_invert) steps = -steps;
    return _tilt_ref_deg + steps * _tilt_deg_per_step;
}

float MountMotion::getSliderMm() const {
    float steps = (float)_stepper[AXIS_SLIDER]->getPosition();
    if (_slider_invert) steps = -steps;
    return steps * _slider_mm_per_step;
}

// ===========================================================================
// Smooth mid-move retarget
// ===========================================================================
//
// moveTo() drives axes via rotateAsync() + overrideSpeed() so that _updateGoto()
// can adjust speed and direction each loop cycle without ever stopping the motor.
// retargetTo() simply updates the stored targets and speed ceilings; the already-
// running _updateGoto() loop picks up the new values on its next tick and steers
// toward the new position — no motor command is issued here, so there is no stop.
//
// If the mount is not already in STATE_MOVING_TO_POS we forward to moveTo().
//
void MountMotion::retargetTo(int32_t pan, int32_t tilt, int32_t slider, int32_t zoom,
                              uint8_t pt_preset, uint8_t sl_preset) {
    if (_state != STATE_MOVING_TO_POS) {
        moveTo(pan, tilt, slider, zoom, pt_preset, sl_preset);
        return;
    }

    if (sl_preset == 0) sl_preset = pt_preset;
    pt_preset = constrain(pt_preset, 1, 4);
    sl_preset = constrain(sl_preset, 1, 4);

    int32_t targets[4] = { pan, tilt,
                            _has_slider ? slider : _stepper[AXIS_SLIDER]->getPosition(),
                            zoom };

    targets[AXIS_PAN]    = _applyOrientationPos(AXIS_PAN,    targets[AXIS_PAN]);
    targets[AXIS_TILT]   = _applyOrientationPos(AXIS_TILT,   targets[AXIS_TILT]);
    targets[AXIS_SLIDER] = _has_slider
                           ? _applyOrientationPos(AXIS_SLIDER, targets[AXIS_SLIDER])
                           : targets[AXIS_SLIDER];
    _clampToLimits(targets[AXIS_SLIDER], AXIS_SLIDER);
    _clampToLimits(targets[AXIS_ZOOM],   AXIS_ZOOM);

    // Update speed ceilings for the new preset
    for (int i = 0; i < 4; i++) {
        const SpeedPreset &sp = (i < 2) ? _pt_presets[pt_preset]
                                        : (i == 2) ? _sl_presets[sl_preset]
                                        : _zoom_preset;
        _goto_max_spd_st[i] = (uint32_t)max(1.0f, physToUSteps(i, sp.max_speed));
        _goto_accel_st[i]   = (uint32_t)max(1.0f, physToUSteps(i, sp.acceleration));
        _goto_target[i]     = targets[i];
    }
    // _updateGoto() running in update() steers toward the new targets on its
    // next tick — no motor command needed here.
    _has_goto_target = true;
}
