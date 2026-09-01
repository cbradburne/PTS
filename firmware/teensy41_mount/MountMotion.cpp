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
 *   Axis    | Motor  | Gear / belt          | Microsteps | Resolution
 *   --------|--------|----------------------|------------|----------------------------
 *   PAN     | 0.9°   | 270t / 36t  = 7.5:1  |    256     | ≈ 1.69 arcsec / step
 *   TILT    | 0.9°   | 120t / 16t  = 7.5:1  |    256     | ≈ 1.69 arcsec / step
 *   SLIDER  | 1.8°   | GT2 20t = 40 mm/rev  |     32     | 6.25 µm / step
 *   ZOOM    | 1.8°   | direct               |     32     | zoom
 *
 *   This table is DESCRIPTION, not definition — the numbers live in the DRIVE
 *   GEOMETRY block at the top of MountMotion.h and everything derives from the
 *   tooth counts there.  It had drifted: it claimed 9:1 on PAN and 8:1 on TILT
 *   where the code used 7.5:1 for both (which the tooth counts confirm), and
 *   256 microsteps on ZOOM where the code used 32.  Three wrong numbers in a
 *   table nobody could act on, because none of them was what ran.
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
// From the header, so the microstep counts and the step sizes derived from
// them cannot drift apart.
static constexpr uint16_t MICROSTEPS[4] = { MICROSTEPS_PAN, MICROSTEPS_TILT,
                                           MICROSTEPS_SLIDER, MICROSTEPS_ZOOM };

// Default run current (mA)
static constexpr uint16_t DEFAULT_CURRENT_MA[4] = { 1300, 1300, 3000, 1300 };


// Limit find speed (steps/sec) and back-off after stall (steps)
// NOTE: StallGuard on TMC2209 requires ~20 RPM minimum to produce reliable SG_RESULT.
// Both SLIDER and ZOOM are now 32 µsteps:
//   6000 steps/s ÷ (32 µstep × 200 steps/rev) × 60 ≈ 56 RPM  ✓
// 4000 steps/s = 25 mm/s, and 4000 / (32 µstep × 200 steps/rev) × 60 = 38 RPM,
// comfortably above the ~20 RPM StallGuard needs for a usable SG_RESULT.
// It was 6000 (37.5 mm/s, 56 RPM).  Easing it is what lets the acceleration
// come down without lengthening the settle guard — see LIMIT_FIND_ACCEL.
static constexpr uint32_t LIMIT_FIND_SPEED       = 4000;   // steps/s — both axes (32 µstep)
// 3200 steps/s² = 20 mm/s² at 160 steps/mm.
//
// This was 150000 (938 mm/s²) and the slider buzzed without moving — a torque
// stall at the start of the ramp, not a StallGuard misread.  It then became
// 6400 (40 mm/s²), defended as "the one figure the hardware has demonstrated it
// can do under this load, every day, on preset 4".
//
// That reasoning was sound and its premise has since gone: the rail is tilted
// now, so the load it referred to no longer exists.  Starting UPHILL from a dead
// stop with the current scaled to 75% is a different demand from starting level,
// and the slider judders and fails to move — two of three limit finds on
// 2026-08-24 halted that way.  20 mm/s² is what the axis is observed to start
// cleanly from on the incline.
//
// LIMIT_FIND_SPEED came down with it, and that pairing is the point.  Halving
// the acceleration alone would have stretched the ramp from 0.94 s to 1.88 s,
// and the settle guard has to outlast the ramp — so the window in which a stall
// cannot be detected would have grown from 42 mm of travel to 55 mm.  Easing the
// speed to 25 mm/s keeps the ramp at 1.25 s, lets the settle guard stay where it
// is, and shrinks that blind window to 24 mm.  Slower, gentler, AND less of the
// rail travelled blind.
static constexpr uint32_t LIMIT_FIND_ACCEL       = 3200;   // steps/s² — ramp = 4000/3200 = 1.25 s
static constexpr int32_t  LIMIT_BACK_OFF[4]      = { 0, 0, 300, 300 };
// Minimum ramp time (seconds) enforced for all moveTo() GOTO moves.
// Caps the acceleration to  speed / GOTO_MIN_RAMP_S  so that even at fast
// presets the mount eases in and out rather than lurching.
// 0.8 s gives a smooth, professional-feeling position move at any preset.
static constexpr float GOTO_MIN_RAMP_S = 1.0f;  //0.8f;

// Per-axis safety margin (steps) held back from the FAR limit.  The near end
// is position 0 — the home stall itself — so only the far end takes this.
//
// SLIDER — 30 mm, and the reason is worth writing down.  The rail is tilted,
// which needed more motor current and a less twitchy StallGuard threshold to
// stop the carriage stalling part-way along a move.  A less sensitive stall
// detector notices the end stop LATER, so by the time limit-find records the
// far end the carriage has already run into it: the recorded position is not
// where the travel safely ends, it is somewhere inside the stop.  Every later
// goto to that limit then drives back to the same place and grinds.
//
// Pulling the usable far end back by 30 mm costs 30 mm of travel and means a
// move to the limit stops short of the stop rather than against it.  It is a
// workaround for late detection rather than a fix for it — the honest fix is a
// stall threshold that trips at the stop with the current the tilt demands, or
// a physical switch, either of which would let this go back to a millimetre or
// two.
//
// Expressed in mm and converted here, so it survives a pulley change: the
// tooth counts in MountMotion.h drive the conversion.
// ZOOM — tune independently; set 0 if the zoom has no physical runout concern.
static constexpr int32_t  SLIDER_SAFETY_MARGIN_MM = 30;
static constexpr int32_t  LIMIT_SAFETY_MARGIN[4] = {
    0, 0,
    (int32_t)(SLIDER_SAFETY_MARGIN_MM / NOMINAL_SLIDER_MM_PER_STEP),
    100
};
static_assert(LIMIT_SAFETY_MARGIN[AXIS_SLIDER] > LIMIT_BACK_OFF[AXIS_SLIDER],
              "the slider safety margin should exceed the jog back-off, or the "
              "soft limit a jog respects sits further out than the goto limit");
static constexpr uint32_t LIMIT_FIND_TIMEOUT_MS  = 2000000;
// Ignore stall for this many ms after a new move starts (MUST exceed the ramp
// time).  These two are one setting in two numbers: StallGuard cannot be trusted
// while the axis is still accelerating, so the guard has to outlast the ramp.
//
// It was 300 ms against a 0.04 s ramp.  Lowering the acceleration to something
// the motor can actually deliver stretches that ramp to 0.94 s, so 300 ms would
// have expired mid-ramp and traded a real stall for a false one — a worse fault,
// because it looks like a working home that stops in the wrong place.
static constexpr uint32_t LIMIT_STALL_SETTLE_MS  = 1600;

// Zoom now uses 32 µsteps (same as slider) — no separate speed/accel needed.
// These are kept as aliases so the axis-specific code paths still compile.
static constexpr uint32_t LIMIT_FIND_SPEED_ZOOM      = 1600;
static constexpr uint32_t LIMIT_FIND_ACCEL_ZOOM      = 50000;
static constexpr uint32_t LIMIT_STALL_SETTLE_ZOOM_MS = 600;

// The settle guard must outlast the acceleration ramp, on BOTH axes.  That was
// written in a comment and held by hand, and a comment does not fail the build:
// lower the acceleration without raising the settle and StallGuard starts
// watching while the axis is still accelerating, where its reading means
// nothing.  The result is a home that stops early and looks like it worked.
//
//   settle_s > speed / accel   rearranged to stay in integers:
static_assert(LIMIT_STALL_SETTLE_MS * (uint64_t)LIMIT_FIND_ACCEL
                  > 1000ULL * LIMIT_FIND_SPEED,
              "LIMIT_STALL_SETTLE_MS must exceed the slider ramp time "
              "(LIMIT_FIND_SPEED / LIMIT_FIND_ACCEL)");
static_assert(LIMIT_STALL_SETTLE_ZOOM_MS * (uint64_t)LIMIT_FIND_ACCEL_ZOOM
                  > 1000ULL * LIMIT_FIND_SPEED_ZOOM,
              "LIMIT_STALL_SETTLE_ZOOM_MS must exceed the zoom ramp time "
              "(LIMIT_FIND_SPEED_ZOOM / LIMIT_FIND_ACCEL_ZOOM)");

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

// STALL_TILT_COMP
//   A tilted rail loads the slider motor differently in each direction: going
//   up it carries the carriage AND its weight down the slope, coming down that
//   weight helps.  StallGuard measures load, so one threshold cannot serve both
//   legs of a limit find — set it for the climb and the descent detects the
//   stop late (the carriage is into it before DIAG fires); set it for the
//   descent and the climb trips on the gradient alone, part-way along the rail.
//
//   SG_RESULT falls as load rises and DIAG fires when SG_RESULT < SGTHRS x 2,
//   so a HIGHER SGTHRS trips at LOWER load.  Climbing therefore wants a lower
//   threshold, descending a higher one.  The gravity component along the rail
//   goes as sin(tilt), so that is what scales it.
//
//   This is the fraction of the threshold shifted at sin(tilt) = 1 (a vertical
//   rail).  0.6 at 21 degrees works out at +/-21%: a threshold of 80 becomes 63
//   climbing and 97 descending.
//
//   TO TUNE: watch the [LimitFind] SG= prints on USB serial for both legs.  The
//   idle SG_RESULT differs between them by roughly the gravity load; this wants
//   to be large enough that each leg's threshold sits the same distance below
//   its own idle reading.  Too small and the descent still detects late; too
//   large and the climb false-trips.
static constexpr float STALL_TILT_COMP = 0.6f;

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
// All of these now come from the DRIVE GEOMETRY block at the top of
// MountMotion.h, derived from the tooth counts.  They were second copies of the
// same finished ratios that block also computes, so a pulley change had to be
// made twice and a mount could end up moving at one scale while reporting its
// position at another.  Change the teeth in the header; this file follows.

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
            return speed * MICROSTEPS[axis] * SLIDER_FULL_STEPS_PER_REV / SLIDER_MM_PER_REV;
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
    // Pan and tilt are in here too, and were not.  They became jog-able in this
    // state when the joystick was routed to jogPanTilt() during a look-at move;
    // before that only zoom could be held here and only zoom needed covering.
    // A held stick and a dead link would otherwise leave the head turning with
    // nothing left to stop it — _updateJog() and its watchdog do not run
    // outside STATE_JOGGING, which is the whole reason this block exists.
    if (_state != STATE_JOGGING && (millis() - _jog_last_ms > JOG_WATCHDOG_MS)) {
        static const int WATCHED[3] = { AXIS_PAN, AXIS_TILT, AXIS_ZOOM };
        for (unsigned w = 0; w < 3; w++) {
            const int ax = WATCHED[w];
            if (_jog_dir[ax] == 0) continue;
            const SpeedPreset &sp = (ax == AXIS_ZOOM)
                                    ? _zoom_preset
                                    : _pt_presets[constrain(_jog_preset[ax], 1, 4)];
            Serial.printf("JOG watchdog: no packet — stopping axis %d\n", ax);
            uint32_t stop_accel = (uint32_t)(physToUSteps(ax, sp.acceleration)
                                             * JOG_STOP_ACCEL_SCALE);
            noInterrupts();
            _stepper[ax]->setAcceleration(stop_accel);
            _stepper[ax]->stopAsync();
            interrupts();
            _jog_dir[ax] = 0;
            _jog_vel[ax] = 0;
        }
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
    // Manual slide with a subject selected: keep pan and tilt on the subject.
    // The operator owns the slider, the triangulation owns pan/tilt — the same
    // split as a commanded look-at move, minus the arrival test, because there
    // is no commanded destination to arrive at.  Requires pan/tilt to be idle:
    // jogging either of those is the one thing that means "aim somewhere else",
    // and it drops the subject before reaching here.
    else if (_state == STATE_JOGGING && _look_at_mode && _ref_set &&
             _la_subject_id != 0xFF &&
             _jog_vel[AXIS_PAN] == 0 && _jog_vel[AXIS_TILT] == 0) {
        _updateLookAt(false);
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
    //
    // A goto needs exactly the same protection, and for a worse reason.
    // _updateGoto() runs ONLY in STATE_MOVING_TO_POS, and it is the only thing
    // that ever parks a goto axis: moveTo() starts each axis with an unbounded
    // rotateAsync() and relies on the P-loop to stop it at the target.  Taking
    // _state away for a zoom nudge orphans every other axis mid-flight — it
    // keeps turning, and releasing the zoom does not stop it, because both jog
    // paths only stop axes with _jog_dir[] set and a goto axis has _goto_dir[].
    // Seen on the rig on 2026-08-19: a 10 deg pan nudge ran 59 deg and was still
    // going 4.5 s later, stopped only because the next MOVE_REL re-planned it.
    bool goto_zoom_only = (_state == STATE_MOVING_TO_POS &&
                           pan == 0 && tilt == 0 && slider == 0);
    if (_state == STATE_LOOK_AT_MOVE || _state == STATE_LOOK_AT_PRE_AIM ||
            goto_zoom_only) {
        // Hand the zoom axis to the operator for the rest of this move.  The
        // goto may have been steering zoom too; if it keeps its claim the two
        // fight — the jog drives zoom away, _updateGoto() sees a large error and
        // relaunches it back.  Released axes are skipped there and the flag is
        // cleared by the next moveTo()/retargetTo()/stopAll().
        if (goto_zoom_only && zoom != 0) _goto_axis_released[AXIS_ZOOM] = true;
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
        // Pan and tilt during a look-at move are the operator taking the head.
        //
        // They used to be dropped on the floor right here. jogPanTilt() has
        // handled this case correctly for a while — drop the subject, drive the
        // axes, leave _state alone so the rail carries on — but nothing routed
        // an ordinary joystick to it: the .ino calls jogPanTilt() only for
        // axis_mask 0x03, which is CV tracking. A stick sends 0x0F and landed
        // here, so the borders went red (the .ino clears the subject either
        // way) and the head did not move.
        //
        // Not for goto_zoom_only: pan and tilt are zero by that branch's own
        // definition, and jogPanTilt() would take _state to STATE_JOGGING and
        // orphan the goto it is protecting.
        if (!goto_zoom_only) jogPanTilt(pan, tilt, pt_preset);
        return;   // _state unchanged — the look-at controller or _updateGoto()
                  // keeps running and finishes the move it was already making
    }

    pt_preset = constrain(pt_preset, 1, 4);
    sz_preset = constrain(sz_preset, 1, 4);

    int16_t raw[4] = { pan, tilt, _has_slider ? slider : (int16_t)0, zoom };
    for (int i = 0; i < 4; i++)
        _jog_vel[i] = _applyOrientation((Axis)i, raw[i]);

    // Update watchdog on EVERY call (including all-zero) so it does not fire
    // while the joystick is intentionally centred during a soft deceleration.
    _jog_last_ms = millis();

    // Reaching here with a goto running means the operator has grabbed a
    // pan/tilt/slider control, which abandons the move.  Cancel it EXPLICITLY:
    // _state is about to leave STATE_MOVING_TO_POS, so _updateGoto() — the only
    // code that parks a goto axis — stops being called.  Any axis it left
    // turning that the operator is not now jogging would keep its unbounded
    // rotateAsync() forever, unreachable by the jog watchdog, which only knows
    // about _jog_dir[].  Axes that ARE being jogged need no stop here; the loop
    // below calls rotateAsync() on them, which overrides the goto's rotation.
    if (_state == STATE_MOVING_TO_POS) {
        noInterrupts();
        for (int i = 0; i < 4; i++)
            if (_goto_dir[i] != 0 && _jog_vel[i] == 0) _stepper[i]->stopAsync();
        interrupts();
        for (int i = 0; i < 4; i++) _goto_dir[i] = 0;
        _has_goto_target = false;
    }

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
// clearLaSubject()  — drop the subject, and let go of the axes with it
// ---------------------------------------------------------------------------
//
// This was an inline that cleared four fields, and that was not enough.
//
// _updateLookAt() drives pan and tilt with an unbounded rotateAsync() and
// steers them by re-issuing setMaxSpeed() every 20 ms. The moment the subject
// goes, `aiming` is false and it stops calling _driveTowardTarget() — so the
// last speed it set stays set, and both axes carry on turning at it. Nothing
// downstream stopped them: the jog paths only stop axes with _jog_dir[] set,
// and a tracked axis has none.
//
// On the rig that was the head wandering off the moment the joystick dropped
// the subject, which is the opposite of handing the axes over.
//
// Only on the transition, and only for axes the operator has not already
// claimed: a held stick clears the subject on every packet at 20 Hz, and
// stopping the motors under it each time would be a stutter, not a hand-over.

void MountMotion::clearLaSubject() {
    bool had_subject = (_la_subject_id != 0xFF);
    _la_subject_id  = 0xFF;
    _la_blend_ms    = 0;
    _la_blend_brake = 0.0f;
    _la_blend_accel = 0.0f;
    if (!had_subject) return;

    if (_state == STATE_LOOK_AT_MOVE || _state == STATE_LOOK_AT_PRE_AIM) {
        noInterrupts();
        for (int i = 0; i < 2; i++)            // PAN, TILT
            if (_jog_dir[i] == 0) _stepper[i]->stopAsync();
        interrupts();
    }
    _la_pt_dir[0] = 0;
    _la_pt_dir[1] = 0;
}

// ---------------------------------------------------------------------------
// jogPanTilt()  — pan/tilt jog only, slider/zoom left untouched
// ---------------------------------------------------------------------------

void MountMotion::jogPanTilt(int16_t pan, int16_t tilt, uint8_t pt_preset) {
    if (_state == STATE_FINDING_LIMITS)  return;

    // Taking the joystick during a look-at move hands pan and tilt to the
    // operator and DROPS the subject — the slider carries on to the end of its
    // travel regardless.
    //
    // This used to return here, so a joystick did nothing at all while a move
    // was running.  Everywhere else in the system a pan/tilt input already
    // means "aim somewhere else" and deselects (see CMD_JOG and CMD_MOVE_REL
    // in the .ino, and the manual-slide branch in update()); the commanded move
    // was the one place that did not honour it.
    //
    // The STATE stays LOOK_AT_MOVE.  That is what keeps the slider travelling
    // and keeps _updateLookAt() running to notice it arriving — the move is
    // still in progress, it has simply stopped aiming.  Reselecting a subject
    // picks tracking straight back up.
    bool in_la_move = (_state == STATE_LOOK_AT_MOVE ||
                       _state == STATE_LOOK_AT_PRE_AIM);
    if (in_la_move) {
        if (pan != 0 || tilt != 0) {
            clearLaSubject();          // aiming somewhere else — let the subject go
        } else if (_la_subject_id != 0xFF) {
            // Still tracking and the stick is centred: nothing to hand over, and
            // running the stop code below would fight the tracker for the axes.
            return;
        }
        // Falling through with a centred stick and no subject is deliberate: it
        // is the operator RELEASING, and the axes have to be told to stop.
    }

    pt_preset = constrain(pt_preset, 1, 4);

    _jog_vel[AXIS_PAN]  = _applyOrientation(AXIS_PAN,  pan);
    _jog_vel[AXIS_TILT] = _applyOrientation(AXIS_TILT, tilt);
    _jog_last_ms        = millis();
    _jogging            = (_jog_vel[AXIS_PAN] != 0 || _jog_vel[AXIS_TILT] != 0);
    if (!in_la_move) _state = STATE_JOGGING;

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
        // The motor's ceiling is the PRESET; the synchronisation ratio is kept
        // apart and applied through overrideSpeed().  See _goto_spd_scale.
        _goto_max_spd_st[i]  = (uint32_t)preset_spd;
        _goto_spd_scale[i]   = (preset_spd > 0.f) ? (actual_spd / preset_spd) : 1.f;
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
        _stepper[i]->setMaxSpeed((int32_t)preset_spd);
        _stepper[i]->setAcceleration((int32_t)actual_acc);
        _stepper[i]->rotateAsync();
        interrupts();
        _goto_dir[i] = 1;  // rotateAsync registered positive; overrideSpeed sign is relative
    }
    // A new move re-claims the axes — except one the operator is still jogging.
    // Re-claiming that would restart the fight this flag exists to prevent, and
    // the stick is a live input: it outranks a move the operator just queued.
    for (int i = 0; i < 4; i++)
        if (_jog_dir[i] == 0) _goto_axis_released[i] = false;
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
    for (int i = 0; i < 4; i++) _goto_axis_released[i] = false;
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
    for (int i = 0; i < 4; i++) _goto_axis_released[i] = false;
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

// ---------------------------------------------------------------------------
// _beginLimitSeek() — everything findLimits() and findHome() do identically
// ---------------------------------------------------------------------------
// Both drive to the min end stop with StallGuard armed and differ only in what
// they do on arrival: findHome() stops there, findLimits() carries on to the
// max end.  They used to hold separate copies of this setup, and the copies
// drifted — findHome() went on writing SGTHRS raw after findLimits() started
// adjusting it for the rail's slope, so homing false-stalled on the climb that
// the adjustment exists to survive.
void MountMotion::_beginLimitSeek(Axis axis, LimitsFoundCb cb, bool homing_only) {
    if (axis != AXIS_SLIDER && axis != AXIS_ZOOM) return;
    if (_state == STATE_FINDING_LIMITS) return;

    emergencyStop();

    _lf_axis     = axis;
    _lf_cb       = cb;
    _lf_state    = LimitFindState::MOVING_TO_MIN;
    _lf_start_ms = millis();
    _homing_only = homing_only;
    _state       = STATE_FINDING_LIMITS;

    // Stay in StealthChop (do NOT force SpreadCycle) — working examples show StallGuard
    // functions correctly via TCOOLTHRS even in StealthChop mode on BTT TMC2209 boards.
    // Use per-axis current scale: lower current widens the useful SGTHRS range (zoom especially).
    _tmc[(int)axis]->rms_current(DEFAULT_CURRENT_MA[(int)axis] * LIMIT_FIND_CURRENT_SCALE[(int)axis]);
    // Threshold for the first leg.  Scaled to the axis's effective chip range
    // via SGTHRS_DIVISOR, and on the slider adjusted for which way this leg
    // runs along the rail — see STALL_TILT_COMP.
    _applyStallThreshold(axis, (axis == AXIS_ZOOM && _zoom_invert));
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
    // The chip readback IS the value in force — _applyStallThreshold() has just
    // written it, and on the slider it may have been adjusted for the slope, so
    // reading it back beats reporting what was asked for.
    Serial.printf("[LimitFind] axis=%d  SGTHRS chip=%u  TCOOLTHRS=0xFFFFF (chip=0x%X)\n",
                  (int)axis, sgthrs_rb, tcool_rb);
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
// findLimits() / findHome()
// ---------------------------------------------------------------------------
// findLimits(): min stop, then on to the max stop, storing both.
// findHome():   min stop only — zero there and back off.
// The seek itself is identical, so it lives in one place.

void MountMotion::findLimits(Axis axis, LimitsFoundCb cb) {
    _beginLimitSeek(axis, cb, false);
}

void MountMotion::findHome(Axis axis, LimitsFoundCb cb) {
    _beginLimitSeek(axis, cb, true);
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
        // An axis the operator took over by jogging is not ours any more, and
        // must not count as active — otherwise the move never completes.  The
        // restart branch below would otherwise relaunch it on the very next
        // tick: a jogged axis has _goto_dir == 0 and a growing target error,
        // which is precisely the condition it fires on.
        if (_goto_axis_released[i]) continue;

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
                // The ACTUAL travel speed, not the motor ceiling: since
                // _goto_max_spd_st became the preset and the synchronisation
                // ratio moved into _goto_spd_scale, using the ceiling here
                // would size the window for a speed this axis never reaches —
                // by 1/scale², which for a zoom at 0.14 is fifty times too
                // wide, and the axis would creep in from far outside it.
                float spd = (float)_goto_max_spd_st[i] * _goto_spd_scale[i];
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
        // Scaled by this axis's share of the synchronised move.  The motor's
        // max is its preset, so the P-loop's -1..1 is multiplied by the ratio
        // that makes every axis arrive together.  Doing it here rather than in
        // setMaxSpeed is what lets a retarget change speed without touching a
        // turning motor.
        float factor = (float)err / _goto_decel_dist[i];
        factor = constrain(factor, -1.0f, 1.0f) * _goto_spd_scale[i];

        _stepper[i]->overrideSpeed(factor);
    }

    if (!any_active) {
        _has_goto_target = false;
        _state = STATE_IDLE;
    }
}

// ---------------------------------------------------------------------------
// _applyStallThreshold() — SGTHRS for one leg, allowing for the rail's slope
// ---------------------------------------------------------------------------
void MountMotion::_applyStallThreshold(Axis axis, bool phys_positive) {
    int idx = (int)axis;
    float thr = (float)_stall_threshold[idx] / (float)SGTHRS_DIVISOR[idx];

    // Only the slider runs along a rail that can be tilted.  Everything else
    // keeps the threshold it was given.
    if (axis == AXIS_SLIDER && _slider_tilt_deg != 0.0f) {
        // The rail rises as the LOGICAL coordinate increases, so translate the
        // physical direction this leg travels in before comparing with the
        // tilt — slider_invert flips the two apart.
        int logical_dir = phys_positive ? 1 : -1;
        if (_slider_invert) logical_dir = -logical_dir;

        // > 0 climbing, < 0 descending, scaled by the gravity component.
        float slope = sinf(_slider_tilt_deg * (float)DEG_TO_RAD) * (float)logical_dir;
        thr *= (1.0f - STALL_TILT_COMP * slope);

        Serial.printf("[LimitFind] slider leg %s: tilt %.1f deg, slope %+.3f, "
                      "SGTHRS %u -> %u\n",
                      slope > 0 ? "CLIMBING" : "DESCENDING",
                      _slider_tilt_deg, slope,
                      (unsigned)(_stall_threshold[idx] / SGTHRS_DIVISOR[idx]),
                      (unsigned)constrain((int)(thr + 0.5f), 1, 255));
    }

    _tmc[idx]->SGTHRS((uint8_t)constrain((int)(thr + 0.5f), 1, 255));
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

    // A STALL IS THE END OF TRAVEL, so the axis must stop dead on one.
    //
    // Both legs used to call stopAsync(), which decelerates — and there is
    // nowhere left to decelerate into. At LIMIT_FIND_SPEED / LIMIT_FIND_ACCEL
    // that ramp is v²/2a = 4000²/6400 = 2500 steps, which on the slider at
    // 160 steps/mm is 15.6 mm of carriage driven INTO the end stop after the
    // stop has already been found. The operator can hear it.
    //
    // What this does NOT do is move the recorded limit. getPosition() and
    // setPosition(0) are on the line after the stop call, so the old
    // stopAsync() recorded the stall point too — the ramp happened after the
    // number was taken. Both ends are recorded where the stall was, before and
    // after this change.
    //
    // That matters for LIMIT_SAFETY_MARGIN below, which is sized to cover the
    // stall being NOTICED late — the recorded point already being inside the
    // stop. Nothing here makes that smaller, so this is not a reason to reduce
    // the margin. It was claimed as one on 2026-09-01 and it was wrong.
    //
    // emergencyStop() stops this axis's own timer (per-stepper, not the whole
    // rig), so the reading is taken at a standstill rather than a few steps
    // into a ramp. Worth having for its own sake; worth nothing in millimetres.
    //
    // A TIMEOUT is the opposite case: nothing was hit, the axis is mid-rail,
    // and slamming it to a halt for no reason would be worse than the ramp.
    // So the two are separated rather than sharing one stop.
    bool stalled = !timed_out && stall_settled && _checkStall(ax);

    switch (_lf_state) {
        case LimitFindState::MOVING_TO_MIN: {
            if (timed_out || stalled) {
                if (stalled) _stepper[idx]->emergencyStop();
                else         _stepper[idx]->stopAsync();
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
                        // This leg runs the OPPOSITE way to the last one, which
                        // on a tilted rail means the other side of the gravity
                        // load.  Re-apply the threshold for the new direction.
                        _applyStallThreshold(ax, go_max_positive);
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
            if (timed_out || stalled) {
                if (stalled) _stepper[idx]->emergencyStop();
                else         _stepper[idx]->stopAsync();
                // Read at a standstill now, so this is the stall point itself
                // rather than a position the carriage is still moving past.
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
                //
                // BOTH ends take the inset, and they did not used to.  The near
                // end was left at 0 — the home stall itself — on the reasoning
                // that home is the datum, so only the far end needed pulling
                // back.  But the reason the far end needs it applies just as
                // much here: a stall is noticed late, so the position recorded
                // at each end is already inside its stop, and driving back to
                // it drives back into the stop.
                //
                // It showed as an asymmetry.  A look-at run to one end parked
                // hard against the stop while the other stopped 31.9 mm short
                // (LIMIT_BACK_OFF 300 + margin 4800 steps, at 160 steps/mm) —
                // measured on the rig as "about 32 mm".  Same inset both ends
                // now, so the two ends of a rail move behave the same way.
                const int32_t inset = LIMIT_BACK_OFF[idx] + LIMIT_SAFETY_MARGIN[idx];
                if (ax == AXIS_ZOOM && _zoom_invert) {
                    setLimits(ax, max_found + inset,   // negative end
                                          0 - inset);  // home end
                } else {
                    setLimits(ax,         0 + inset,   // home end
                                  max_found - inset);
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

// ---------------------------------------------------------------------------
// _laAimNow() — where to POINT right now
// ---------------------------------------------------------------------------
// The blend runs in ANGLE, not in position.
//
// It used to interpolate the subject POINT from the old one to the new one and
// take atan2 of the result.  That walks the aim along a straight chord between
// two subjects, and a chord passes nearer the camera than either end.  Two
// things follow, and both were measured on the rig on 2026-08-26:
//
//   the TILT swings out and comes back — 5.1 degrees of travel to make a 2.0
//   degree move on one switch, because the aim point dips closer mid-move and
//   a nearer subject at the same height needs more tilt.  Worst when the two
//   subjects are at similar height, which is when it is most visible.
//
//   the PEAK RATE overshoots what the blend asked for.  Angular rate is
//   v_perp / r, so as r falls toward the middle of the chord the rate rises —
//   right where the smoothstep already peaks.  Measured 43-46 deg/s against a
//   designed 39.8, sitting hard against the axis ceiling.
//
// Smoothstepping the angles instead makes the angular rate genuinely the
// smoothstep the duration was computed from, and the tilt monotonic.  Both
// endpoints are re-derived from the CURRENT rail position every tick, so the
// rail is still tracked through the switch — which is the reason the point
// form was there to begin with, and is not given up by doing this.
//
// Pan and tilt share one phase, so they still land together for free.
bool MountMotion::_laBlendActive() const {
    return _la_blend_ms != 0 &&
           (millis() - _la_blend_start_ms) < _la_blend_ms;
}

float MountMotion::_laBlendPhase() const {
    if (_la_blend_ms == 0) return 1.0f;
    uint32_t elapsed = millis() - _la_blend_start_ms;
    if (elapsed >= _la_blend_ms) return 1.0f;
    float t = (float)elapsed / (float)_la_blend_ms;
    return t * t * (3.0f - 2.0f * t);            // smoothstep
}

void MountMotion::_aimFrom(float sx, float sy, float sz, float wx, float wy,
                           float *pan_deg, float *tilt_deg) const {
    float dx = sx - wx;
    float dy = sy - wy;
    float dz = sz;                                // Z positive = into the room
    *pan_deg  = atan2f(dx, dz) * (180.0f / (float)M_PI);
    *tilt_deg = atan2f(dy, sqrtf(dx * dx + dz * dz)) * (180.0f / (float)M_PI);
}

// Where the head is actually pointing — the inverse of the target arithmetic
// in _updateLookAt().  Needed when tracking resumes after the operator has had
// the joystick: the blend has to start from where they left the camera, not
// from a subject that was dropped some time ago.
void MountMotion::_headAimAngles(float *pan_deg, float *tilt_deg) const {
    int32_t pan_log  = _stepper[AXIS_PAN ]->getPosition();
    int32_t tilt_log = _stepper[AXIS_TILT]->getPosition();
    if (_pan_invert)  pan_log  = -pan_log;
    if (_tilt_invert) tilt_log = -tilt_log;
    *pan_deg  = (float)pan_log  * _pan_deg_per_step  + _pan_ref_deg;
    *tilt_deg = (float)tilt_log * _tilt_deg_per_step + _tilt_ref_deg;
}

// One copy of "a point looked at from here, at these angles, this far away".
// Two callers need it, and a third hand-rolled projection is exactly how the
// aim geometry drifted apart before.
void MountMotion::_pointFromAim(float wx, float wy, float pan_deg, float tilt_deg,
                                float h, float *sx, float *sy, float *sz) const {
    float pan_r  = pan_deg  * ((float)M_PI / 180.0f);
    float tilt_r = tilt_deg * ((float)M_PI / 180.0f);
    *sx = wx + h * sinf(pan_r);
    *sy = wy + h * tanf(tilt_r);
    *sz =      h * cosf(pan_r);
}

void MountMotion::_laAimNow(float wx, float wy,
                            float *pan_deg, float *tilt_deg) const {
    float pan_b, tilt_b;
    _aimFrom(_la_subject_x, _la_subject_y, _la_subject_z, wx, wy,
             &pan_b, &tilt_b);

    float e = _laBlendPhase();
    if (e >= 1.0f) {                              // no blend, or it has finished
        *pan_deg = pan_b;  *tilt_deg = tilt_b;
        return;
    }

    float pan_a, tilt_a;
    _aimFrom(_la_blend_from[0], _la_blend_from[1], _la_blend_from[2], wx, wy,
             &pan_a, &tilt_a);

    // Shortest way round.  dz is positive for anything in front of the rail so
    // both angles land in (-90, 90) and this never fires in practice, but a
    // wrapped difference here would be a 358 degree spin rather than a 2
    // degree correction, and that is not a failure worth discovering on air.
    float dpan = pan_b - pan_a;
    while (dpan > 180.0f)  dpan -= 360.0f;
    while (dpan < -180.0f) dpan += 360.0f;

    *pan_deg  = pan_a  + dpan * e;
    *tilt_deg = tilt_a + (tilt_b - tilt_a) * e;
}

// The aim expressed as a POINT, for the one caller that needs one: starting a
// new switch while a switch is running.  Reconstructed from the blended ANGLE
// at the blended range, so the next blend begins exactly where the camera is
// pointing rather than somewhere along the old chord.
void MountMotion::_laSubjectNow(float *sx, float *sy, float *sz) const {
    float e = _laBlendPhase();
    if (e >= 1.0f) {
        *sx = _la_subject_x;  *sy = _la_subject_y;  *sz = _la_subject_z;
        return;
    }

    int32_t sl_phys = _stepper[AXIS_SLIDER]->getPosition();
    float   cx      = (float)sl_phys * _slider_mm_per_step;
    if (_slider_invert) cx = -cx;
    float wx, wy;
    _railWorldPos(cx, &wx, &wy);

    float pan_deg, tilt_deg;
    _laAimNow(wx, wy, &pan_deg, &tilt_deg);

    // Horizontal range, blended the same way, so a switch-during-switch does
    // not jump the subject nearer or further as well as sideways.
    float ax = _la_blend_from[0] - wx, az = _la_blend_from[2];
    float bx = _la_subject_x    - wx, bz = _la_subject_z;
    float h  = sqrtf(ax * ax + az * az) * (1.0f - e)
             + sqrtf(bx * bx + bz * bz) * e;

    _pointFromAim(wx, wy, pan_deg, tilt_deg, h, sx, sy, sz);
}

void MountMotion::setLookAtSubject(float sx, float sy, float sz, uint8_t subject_id) {
    // Switching subject while already tracking one: ease the target across
    // instead of stepping it.  Start from where we are aiming NOW, which may
    // itself be part-way through an earlier blend, so a switch during a switch
    // stays continuous rather than snapping back to the previous subject.
    bool tracking_now = (_look_at_mode &&
                         (_state == STATE_LOOK_AT_MOVE ||
                          _state == STATE_LOOK_AT_PRE_AIM ||
                          _state == STATE_JOGGING));
    bool switching = (tracking_now && _la_subject_id != 0xFF &&
                      subject_id != _la_subject_id);

    // Re-acquiring after the operator has had the joystick.  There is no
    // previous subject to blend FROM — they dropped it by taking pan/tilt — but
    // the camera is pointing somewhere definite, and easing from there is
    // exactly the same problem as a switch.  Without this the target jumps to
    // the subject and the controller chases a step, which is the lurch the
    // blend exists to remove.
    bool reacquiring = (tracking_now && _la_subject_id == 0xFF && _ref_set);

    float from_x = 0.f, from_y = 0.f, from_z = 0.f;
    if (switching) {
        _laSubjectNow(&from_x, &from_y, &from_z);
    } else if (reacquiring) {
        int32_t sl_phys = _stepper[AXIS_SLIDER]->getPosition();
        float   cx      = (float)sl_phys * _slider_mm_per_step;
        if (_slider_invert) cx = -cx;
        float wx, wy;
        _railWorldPos(cx, &wx, &wy);
        float hpan, htilt;
        _headAimAngles(&hpan, &htilt);
        // At the NEW subject's range: the blend interpolates angles, so the
        // range only has to be sane, and matching the destination keeps the
        // start and end of the turn the same distance away.
        float bx = sx - wx, bz = sz;
        _pointFromAim(wx, wy, hpan, htilt, sqrtf(bx * bx + bz * bz),
                      &from_x, &from_y, &from_z);
        switching = true;            // from here on it IS a switch
    }

    // Whatever the operator was doing with pan/tilt, the tracker owns them
    // again from this moment.  Leaving the jog bookkeeping set would have
    // _driveTowardTarget() and the jog both believing they hold the axes.
    if (tracking_now && _la_subject_id == 0xFF) {
        _jog_vel[AXIS_PAN] = _jog_vel[AXIS_TILT] = 0;
        _jog_dir[AXIS_PAN] = _jog_dir[AXIS_TILT] = 0;
        _pending_dir[AXIS_PAN] = _pending_dir[AXIS_TILT] = 0;
    }

    // Atomic write — update atomically enough for Teensy (no ISR touches these)
    _la_subject_x  = sx;
    _la_subject_y  = sy;
    _la_subject_z  = sz;
    _la_subject_id = subject_id;

    if (!switching) {
        _la_blend_ms    = 0;         // first selection — aim straight at it
        _la_blend_brake = 0.0f;      // and the fixed caps govern again
        _la_blend_accel = 0.0f;
        // No blend to cover, so the grace keeps its original fixed length.
        if (_state == STATE_LOOK_AT_MOVE || _state == STATE_LOOK_AT_PRE_AIM) {
            _la_slew_until_ms = millis() + LOOK_AT_SLEW_DURATION_MS;
        }
        return;
    }

    // Duration from how far the camera actually has to turn, measured at the
    // current rail position: a nudge between two people standing together
    // should not take as long as a sweep across the room.
    int32_t sl_phys = _stepper[AXIS_SLIDER]->getPosition();
    float   cx      = (float)sl_phys * _slider_mm_per_step;
    if (_slider_invert) cx = -cx;
    float wx, wy;
    _railWorldPos(cx, &wx, &wy);

    // Same helper the tracker uses, so the travel this duration is computed
    // from cannot drift away from the angles actually flown.
    float pan_a, tilt_a, pan_b, tilt_b;
    _aimFrom(from_x, from_y, from_z, wx, wy, &pan_a, &tilt_a);
    _aimFrom(sx,     sy,     sz,     wx, wy, &pan_b, &tilt_b);
    float travel = fmaxf(fabsf(pan_b - pan_a), fabsf(tilt_b - tilt_a));

    float ms = travel * LOOK_AT_BLEND_MS_PER_DEG;
    if (ms < (float)LOOK_AT_BLEND_MIN_MS) ms = (float)LOOK_AT_BLEND_MIN_MS;
    if (ms > (float)LOOK_AT_BLEND_MAX_MS) ms = (float)LOOK_AT_BLEND_MAX_MS;

    // The ceiling sets a FLOOR on how long the turn takes.
    //
    // A smoothstep peaks at 1.5x its average rate.  Ask for a peak the axis
    // cannot deliver and the curve is not slowed, it is CLIPPED: the camera
    // saturates at the clamp, runs flat, and stops dead on arrival.  That is
    // what LOOK_AT_BLEND_MAX_MS was doing to every turn over about 58 degrees
    // — the 2200 ms ceiling demanded 49–63 deg/s from an axis that gives
    // 46.9, and all three switches measured on 2026-08-26 came back as
    // rectangles because of it.
    //
    // So a big turn takes LONGER rather than going faster.  It has to: a bell
    // covers two thirds the ground of a rectangle at the same peak speed, so
    // keeping the peak means spending the time.  Turns small enough to already
    // fit are not touched.
    float ceiling_dps = _la_max_steps_s[0] * _pan_deg_per_step;
    if (ceiling_dps > 0.0f) {
        float fit_ms = 1.5f * travel * 1000.0f
                     / (ceiling_dps * LOOK_AT_BLEND_FILL);
        if (ms < fit_ms) ms = fit_ms;
    }

    _la_blend_from[0] = from_x;
    _la_blend_from[1] = from_y;
    _la_blend_from[2] = from_z;
    _la_blend_start_ms = millis();
    _la_blend_ms       = (uint32_t)ms;

    // Let the camera actually follow the curve.  A smoothstep peaks at 1.5x its
    // average rate; the fixed slew cap is a flat fraction of the look-at max and
    // knows nothing about the blend, so it was clamping the peak to less than
    // half of what the curve asks for.  The result was constant speed for the
    // whole move and a dead stop at the end — the shape the blend exists to
    // remove.
    float max_dps  = _la_max_steps_s[0] * _pan_deg_per_step;   // = max_pt_deg_s
    float peak_dps = 1.5f * travel / (ms / 1000.0f);
    _la_blend_brake = (max_dps > 0.f)
                      ? constrain((peak_dps * LOOK_AT_BLEND_HEADROOM) / max_dps,
                                  0.0f, 1.0f)
                      : 0.0f;

    // What the curve itself asks for, which is what bounds the motor's
    // acceleration while it runs.  A smoothstep peaks at 6 x travel /
    // duration^2 — small turns demand the most, because their duration is
    // short, which is why this cannot be a fixed number.
    _la_blend_accel = LOOK_AT_ACCEL_HEADROOM * 6.0f * travel
                    / ((ms / 1000.0f) * (ms / 1000.0f));

    // Hold the gentle slew cap for the whole blend AND the settling after it.
    // The cap comes off in a step, so it has to come off when the camera is
    // already still — not while it is closing the last of its following error,
    // which is where a fixed grace put it and why the ease OUT kicked.
    _la_slew_until_ms = millis() + _la_blend_ms + LOOK_AT_SLEW_SETTLE_MS;

    Serial.printf("[LA] subject switch: %.1f deg over %lums, peak %.1f deg/s, "
                  "cap %.2f (was %.2f), held %lums\n",
                  travel, (unsigned long)_la_blend_ms, peak_dps,
                  _la_blend_brake, 0.15f,
                  (unsigned long)(_la_blend_ms + LOOK_AT_SLEW_SETTLE_MS));
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
    _la_sl_arrived_ms = 0;

    // ── PRE-AIM PHASE ────────────────────────────────────────────────────────
    // Compute the correct pan/tilt angles for the current slider position and
    // start driving there BEFORE the slider moves.  This eliminates the visible
    // jerk at look-at start caused by any position error left over from
    // calibration (e.g. camera at SET_B tilt != look-at formula tilt).
    {
        int32_t sl_phys_now = _stepper[AXIS_SLIDER]->getPosition();
        float cx_now = (float)sl_phys_now * _slider_mm_per_step;
        if (_slider_invert) cx_now = -cx_now;

        float wx_now, wy_now;
        _railWorldPos(cx_now, &wx_now, &wy_now);
        float pan_deg_now, tilt_deg_now;
        _aimFrom(_la_subject_x, _la_subject_y, _la_subject_z, wx_now, wy_now,
                 &pan_deg_now, &tilt_deg_now);

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

    // Vector from camera to subject, both on the real (possibly climbing) rail
    float wx, wy;
    _railWorldPos(cx, &wx, &wy);
    float pan_deg, tilt_deg;
    _aimFrom(_la_subject_x, _la_subject_y, _la_subject_z, wx, wy,
             &pan_deg, &tilt_deg);

    // Convert to LOGICAL step targets.
    // moveTo() calls _applyOrientationPos() on PAN, TILT, and SLIDER internally,
    // so we must NOT pre-apply the inversion flags here — doing so would
    // double-invert and send the axes to the mirror of the correct position.
    int32_t pan_target  = (int32_t)((pan_deg  - _pan_ref_deg)  / _pan_deg_per_step);
    int32_t tilt_target = (int32_t)((tilt_deg - _tilt_ref_deg) / _tilt_deg_per_step);

    Serial.printf("[Aim] sl_phys=%ld cx=%.1fmm  subj=(%.1f, %.1f, %.1f)mm\n",
                  (long)sl_phys, cx, _la_subject_x, _la_subject_y, _la_subject_z);
    Serial.printf("[Aim] rail=(%.1f, %.1f)mm  pan=%.2f° tilt=%.2f°\n",
                  wx, wy, pan_deg, tilt_deg);
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

    // sync=true, so pan and tilt arrive together.
    //
    // This used to pass false, reasoning that syncing "would make one axis
    // crawl".  It is the wrong way round: sync scales the axis that would
    // arrive EARLY down to match the one that takes longest, and leaves the
    // dominant axis at full preset speed.  The move therefore takes exactly as
    // long either way — the only difference is whether the short axis spends
    // that time moving or spends it stopped.
    //
    // Unsynced it spent it stopped, and visibly: at preset 4 (15 deg/s) a 90
    // degree pan with 3 degrees of tilt had the tilt finish 5.8 seconds before
    // the pan, so the shot swung in two separate movements.  Reported from the
    // rig on 2026-08-27, and only when parked — during a slider move the
    // subject-switch blend drives both axes off one phase and this path is
    // never taken, which is why it looked correct there.
    //
    // Slot recalls have always synced. This is the same expectation applied to
    // the same kind of move.
    moveTo(pan_target, tilt_target, sl_log, zoom_phys, pt_preset, pt_preset, /*sync=*/true);
    return true;
}

// ---------------------------------------------------------------------------
// stopLookAtMove()
// ---------------------------------------------------------------------------

void MountMotion::stopLookAtMove() {
    if (_state != STATE_LOOK_AT_MOVE && _state != STATE_LOOK_AT_PRE_AIM) return;
    _la_sl_arrived_ms = 0;   // or the next move inherits this one's clock
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

// Where the camera sits when the slider reads cx mm along the rail.  The rail
// may climb: at 21 degrees it rises 358 mm per metre, so the camera is neither
// at the along-rail distance the slider reports nor at a constant height.  The
// solver in the .ino resolves this the same way (slider_world_pos); both must
// agree or a subject is solved in one frame and tracked in another.
void MountMotion::_railWorldPos(float cx, float *wx, float *wy) const {
    float t = _slider_tilt_deg * (float)DEG_TO_RAD;
    *wx = cx * cosf(t);
    *wy = cx * sinf(t);
}

void MountMotion::_updateLookAt(bool check_slider_arrival) {
    uint32_t now = millis();
    if (now - _la_last_update_ms < LOOK_AT_INTERVAL_MS) return;
    _la_last_update_ms = now;

    // 1. Current slider position in mm
    int32_t sl_phys = _stepper[AXIS_SLIDER]->getPosition();
    float cx = (float)sl_phys * _slider_mm_per_step;
    // Undo orientation: the stepper position is in motor-frame (possibly inverted).
    // We want world-frame mm along the rail (always positive from home).
    if (_slider_invert) cx = -cx;

    // No subject: the operator has the joystick and owns pan/tilt.  Everything
    // between here and the slider-arrival check is about aiming, and aiming at
    // nothing would fight the jog for the axes — so skip it and let the move
    // run on as a slider move that happens to have started as a look-at.
    bool aiming = (_la_subject_id != 0xFF);

    // 2. Where the camera has to point, from this rail position
    float wx, wy;
    _railWorldPos(cx, &wx, &wy);

    // 3. Required pan/tilt angles (world frame).  During a subject switch this
    //    is the smoothstep applied to the ANGLES rather than to the subject
    //    point — see _laAimNow().  Both endpoints are re-derived at the rail
    //    position just computed, so the rail is still tracked throughout.
    float pan_deg, tilt_deg;
    _laAimNow(wx, wy, &pan_deg, &tilt_deg);

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
        float sx_now, sy_now, sz_now;
        _laSubjectNow(&sx_now, &sy_now, &sz_now);
        Serial.printf("[LA] sl_phys=%ld  cx=%.1fmm  subj=(%.1f,%.1f,%.1f)\n",
                      (long)sl_phys, cx,
                      sx_now, sy_now, sz_now);
        Serial.printf("[LA] rail=(%.1f,%.1f)  pan=%.3f° tilt=%.3f°  blend=%.2f\n",
                      wx, wy, pan_deg, tilt_deg, _laBlendPhase());
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

    if (aiming) {
        _driveTowardTarget(AXIS_PAN,  pan_target,  pan_speed_limit);
        _driveTowardTarget(AXIS_TILT, tilt_target, tilt_speed_limit);
    }

    // 7. Detect slider arrival — transition to IDLE when slider finishes.
    // Guard: require BOTH !isMoving AND proximity to the stored destination.
    // Using only !isMoving would fire immediately if the slider was already at the
    // destination when startLookAtMove() was called (e.g. user presses the arrow
    // toward the limit the slider is already at), because moveAsync() targeting
    // the current position leaves isMoving=false from the very first tick.
    if (check_slider_arrival) {
        int32_t sl_pos = _stepper[AXIS_SLIDER]->getPosition();
        int32_t sl_err = labs(sl_pos - _goto_target[AXIS_SLIDER]);
        if (!_stepper[AXIS_SLIDER]->isMoving && sl_err <= GOTO_ARRIVE_STEPS) {
            // The slider arriving does not mean the SHOT has arrived.
            //
            // stopLookAtMove() stops all four steppers, so ending here cut pan
            // and tilt off wherever they happened to be.  Switch subject near
            // the end of a rail move and the slider would reach the limit
            // part-way through the turn, leaving the camera pointing between
            // two people — which on a panel is a shot of nobody.
            //
            // So the move ends when the AIM has arrived, not when the rail has.
            // The slider is stationary by now, so the target angles are static
            // apart from the blend, and pan/tilt close on them in their own
            // time.  In ordinary use the aim is already there — the camera has
            // been tracking all the way along — so this changes nothing about a
            // move that had no switch in it.
            const int32_t aim_tol =
                (int32_t)(LOOK_AT_AIM_ARRIVE_DEG / _pan_deg_per_step);
            int32_t pan_err  = labs(pan_target  - _stepper[AXIS_PAN ]->getPosition());
            int32_t tilt_err = labs(tilt_target - _stepper[AXIS_TILT]->getPosition());
            bool blending    = aiming && _laBlendPhase() < 1.0f;
            // With no subject there is nothing for the aim to arrive AT, so the
            // move ends on the rail alone — waiting would hang until the
            // timeout while the operator held a perfectly deliberate frame.
            bool aim_there   = !aiming ||
                               (pan_err <= aim_tol && tilt_err <= aim_tol);

            if (_la_sl_arrived_ms == 0) _la_sl_arrived_ms = now;
            bool waited_long_enough =
                (now - _la_sl_arrived_ms) >= LOOK_AT_AIM_FINISH_MAX_MS;

            if ((!blending && aim_there) || waited_long_enough) {
                Serial.printf("[LA] Slider arrived at phys=%ld (dest=%ld err=%ld); "
                              "aim pan_err=%ld tilt_err=%ld after %lums%s — ending look-at\n",
                              (long)sl_pos, (long)_goto_target[AXIS_SLIDER], (long)sl_err,
                              (long)pan_err, (long)tilt_err,
                              (unsigned long)(now - _la_sl_arrived_ms),
                              waited_long_enough && !aim_there ? " (TIMED OUT)" : "");
                stopLookAtMove();
            }
        } else {
            _la_sl_arrived_ms = 0;   // still travelling; the clock starts on arrival
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
    // These are FRACTIONS of max_pt_deg_s, and max_pt_deg_s just halved: the
    // controller used to be handed 90 deg/s, a speed the library silently
    // clamps away (see AXIS_MAX_STEPS_S), and is now handed the 46.875 it can
    // actually reach.  Left at 0.25 and 0.15 the absolute caps would have
    // halved with it — 22.5 -> 11.7 deg/s for ordinary tracking, which is
    // below what an 80 mm/s slider needs and would have shown up as the camera
    // falling behind the rail rather than as anything to do with switching.
    //
    // Re-expressed to keep the same deg/s they have always meant:
    //     0.48  x 46.875 = 22.5 deg/s   (was 0.25 x 90)
    //     0.288 x 46.875 = 13.5 deg/s   (was 0.15 x 90)
    constexpr float LOOK_AT_BRAKE_FACTOR       = 0.48f;
    constexpr float LOOK_AT_SLEW_BRAKE_FACTOR  = 0.288f;

    // 1250 steps, stated outright.  It used to be KP x BRAKE_FACTOR, which
    // silently rode along with any change to the cap above — the threshold is
    // a distance and has no reason to move when a speed does.
    constexpr float LOOK_AT_SLEW_ERR_THRESHOLD = 1250.0f;

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

    // While a blend is running the BLEND governs.  Its cap is derived from the
    // curve's own peak rate, so the camera can follow the shape instead of
    // saturating against a flat limit and turning it back into a rectangle.
    // The fixed caps still apply everywhere else, where a step input is still
    // possible and there is a lurch to prevent.
    float fixed_brake = in_slew ? LOOK_AT_SLEW_BRAKE_FACTOR : LOOK_AT_BRAKE_FACTOR;
    float effective_brake = fixed_brake;

    // The blend's cap does not simply STOP when the blend does.
    //
    // Letting it revert on the instant was a cliff: for a 25 degree switch the
    // cap went from 0.394 to 0.15 in one tick — 35.5 deg/s to 13.5 — at exactly
    // the moment the setpoint stopped moving and while the camera was still
    // running to close its following error.  The camera was clamped from full
    // speed to a third of it between two control ticks.  One speed, then the
    // next, with nothing in between: which is what an operator sees as a sharp
    // ease OUT, and is the same fault as the step it replaced, moved to the
    // other end of the move.
    //
    // So it hands back gradually, on the same smoothstep the aim itself uses,
    // across the settle window.  By the time the fixed cap is fully in force the
    // camera has arrived and the cap does not bind, so the handover is invisible
    // whichever way it goes — and it does go both ways: a very small turn blends
    // slowly enough that its cap is BELOW the fixed one, and then this ramps up.
    if (_la_blend_ms != 0 && _la_blend_brake > 0.0f) {
        uint32_t now_ms    = millis();
        uint32_t blend_end = _la_blend_start_ms + _la_blend_ms;
        if ((int32_t)(now_ms - blend_end) < 0) {
            effective_brake = _la_blend_brake;                  // still blending
        } else {
            uint32_t since = now_ms - blend_end;
            if (since < LOOK_AT_SLEW_SETTLE_MS) {
                float u = (float)since / (float)LOOK_AT_SLEW_SETTLE_MS;
                float e = u * u * (3.0f - 2.0f * u);            // smoothstep
                effective_brake = _la_blend_brake
                                + (fixed_brake - _la_blend_brake) * e;
            }
        }
    }
    float v_max_braking   = max_steps_s * effective_brake;

    // acc is derived so braking_distance == P/sqrt crossover distance.
    //   acc = v_max_braking · max_steps_s / (2·KP_STEPS)
    // This value is used for the v_sqrt braking curve only.  The stepper itself
    // is given a boosted acceleration so it reaches v_max_braking faster, while
    // the braking profile (and therefore stopping accuracy) is unchanged.
    float acc        = (v_max_braking * max_steps_s) / (2.0f * LOOK_AT_KP_STEPS);

    // Bounded by what the CURVE needs, while a curve is running.
    //
    // acc above comes from the braking geometry alone and knows nothing about
    // the trajectory: it is 469 deg/s^2, and x3 for the slew boost, 1406.  A
    // 90 degree switch's smoothstep peaks at 47.  So the motor is allowed to
    // change speed thirty times faster than anything is asking it to, and at
    // 1406 deg/s^2 one 20 ms control tick permits a 28 deg/s step — the whole
    // plateau, between two ticks.
    //
    // A FLAT limit was the obvious fix and is the wrong one.  What a smoothstep
    // demands is 6 x travel / duration^2, and small turns are the greedy ones
    // because their duration is short: a 5 degree switch needs 333 deg/s^2
    // where a 90 degree one needs 47.  Any flat number low enough to help the
    // big turns clips the small ones — reintroducing at the small end exactly
    // the clipping just removed at the large end.
    //
    // So it is derived per switch from that curve, with headroom, and only ever
    // taken when it is the SMALLER of the two.  It cannot clip: it is computed
    // from the very trajectory it is bounding.
    //
    // The limit applies to the braking curve as well as the stepper, because
    // v_sqrt assumes deceleration == acc; hold the motor to less than the curve
    // assumed and it overshoots the target instead of stopping on it.  That is
    // also where the benefit shows up — a gentler acc means braking starts
    // further out, which is a longer, softer arrival.
    float dps_axis = (axis == AXIS_PAN) ? _pan_deg_per_step : _tilt_deg_per_step;
    if (_la_blend_accel > 0.0f && dps_axis > 0.0f) {
        float acc_limit = _la_blend_accel / dps_axis;      // deg/s^2 -> steps/s^2
        if (acc > acc_limit) acc = acc_limit;
    }

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

    // Re-plan from where every axis IS, exactly as moveTo() does.
    //
    // This used to hand each axis its full preset speed and nothing else, so a
    // mid-move recall silently became a different kind of move from a fresh
    // one: moveTo() synchronises the axes to land together, retargetTo() let
    // them finish whenever they liked.  Pressing the same slot twice therefore
    // produced two different behaviours depending only on whether the mount
    // happened to still be moving — which is precisely how the zoom fault
    // managed to look impossible for a week.
    //
    // A retarget is still smooth: nothing stops, _updateGoto() picks the new
    // ceilings up on its next tick.  Only the numbers change.
    //
    // Look-at is never running here — the guard above returns to moveTo() for
    // any state other than STATE_MOVING_TO_POS — so this is the "look-at
    // disabled" case by construction, where all four axes arrive together.
    float dist_st[4];
    float t_move = 0.f;
    for (int i = 0; i < 4; i++) {
        dist_st[i] = fabsf((float)(targets[i] - _stepper[i]->getPosition()));
        if (dist_st[i] > (float)GOTO_ARRIVE_STEPS) {
            const SpeedPreset &sp = (i < 2) ? _pt_presets[pt_preset]
                                            : (i == 2) ? _sl_presets[sl_preset]
                                            : _zoom_preset;
            float spd = max(1.0f, physToUSteps(i, sp.max_speed));
            float t   = dist_st[i] / spd;
            if (t > t_move) t_move = t;
        }
    }

    for (int i = 0; i < 4; i++) {
        const SpeedPreset &sp = (i < 2) ? _pt_presets[pt_preset]
                                        : (i == 2) ? _sl_presets[sl_preset]
                                        : _zoom_preset;
        float preset_spd = max(1.0f, physToUSteps(i, sp.max_speed));
        float preset_acc = max(1.0f, physToUSteps(i, sp.acceleration));

        float actual_spd, actual_acc;
        if (t_move > 0.f && dist_st[i] > (float)GOTO_ARRIVE_STEPS) {
            float k    = (dist_st[i] / preset_spd) / t_move;
            actual_spd = max(1.0f, preset_spd * k);
            actual_acc = max(1.0f, preset_acc * k);
        } else {
            actual_spd = preset_spd;
            actual_acc = preset_acc;
        }

        _goto_target[i]      = targets[i];
        _goto_max_spd_st[i]  = (uint32_t)preset_spd;
        _goto_spd_scale[i]   = (preset_spd > 0.f) ? (actual_spd / preset_spd) : 1.f;
        _goto_accel_st[i]    = (uint32_t)actual_acc;

        // The decel window MUST be recomputed with the speed just set.  It was
        // left untouched here, carrying whatever the previous moveTo() had
        // worked out — for a different distance, at a different speed.  Since
        // decel_dist = v²/2a, an axis retargeted from a sync-scaled 109 steps/s
        // up to its full 711 kept a window sized for a seventh of the speed:
        // roughly forty times too small, so it ran at full speed until far
        // inside the point it should have begun slowing, and overshot.  That is
        // the "press it again and it shoots past" behaviour.
        {
            float decel_dist_full = (actual_acc > 0.f)
                                    ? (actual_spd * actual_spd) / (2.f * actual_acc)
                                    : actual_spd;
            float move_dist = dist_st[i];
            _goto_decel_dist[i] = (move_dist > (float)GOTO_ARRIVE_STEPS && move_dist < decel_dist_full)
                                  ? fmaxf(move_dist, (float)(GOTO_ARRIVE_STEPS * 2))
                                  : decel_dist_full;
        }

        // Nothing is pushed to a turning motor.  The ceiling it already has is
        // its preset, which does not change; the synchronisation ratio and the
        // decel window are what this move alters, and _updateGoto() reads both
        // on its next tick.  That is what makes a retarget smooth — and it
        // avoids rotateAsync(), which would have to be given a positive max and
        // would fling an axis travelling negative the wrong way for a tick.
        //
        // Acceleration is safe to set on a moving axis on its own — the jog
        // soft-stop does exactly that — so the ramp follows the new plan too.
        noInterrupts();
        _stepper[i]->setAcceleration((int32_t)actual_acc);
        interrupts();

        // Only an axis _updateGoto() had actually PARKED needs starting, and
        // then it is stationary, so rotateAsync() is safe.
        if (_goto_dir[i] == 0 && dist_st[i] > (float)GOTO_ARRIVE_STEPS) {
            noInterrupts();
            _stepper[i]->setMaxSpeed((int32_t)preset_spd);
            _stepper[i]->rotateAsync();
            interrupts();
            _goto_dir[i] = 1;
        }

    }
    // _updateGoto() running in update() steers toward the new targets on its
    // next tick — no motor command needed here.
    // A new move re-claims the axes — except one the operator is still jogging.
    // Re-claiming that would restart the fight this flag exists to prevent, and
    // the stick is a live input: it outranks a move the operator just queued.
    for (int i = 0; i < 4; i++)
        if (_jog_dir[i] == 0) _goto_axis_released[i] = false;
    _has_goto_target = true;
}


