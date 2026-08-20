#pragma once
/*
 * MountMotion — Teensy 4.1 camera mount motion library
 *
 * Wraps TeensyStep4 for synchronised multi-axis moves with acceleration.
 * Controls four TMC2209 drivers, each on a dedicated bidirectional serial port.
 *
 * TMC2209 UART — one per axis, full duplex (TX + RX), all drivers at address 0.
 *   PAN    → Serial5  (TX=20, RX=21)
 *   TILT   → Serial4  (TX=17, RX=16)
 *   SLIDER → Serial3  (TX=14, RX=15)
 *   ZOOM   → Serial8  (TX=35, RX=34)
 *   MS1 = GND, MS2 = GND on every board (address 0, no multiplexing).
 *   Wiring per driver:  TX ─ 1kΩ ─ PDN_UART ← RX
 *
 * StallGuard via DIAG1 pin (hardware interrupt-capable):
 *   DIAG1 configured push-pull, goes HIGH on stall.
 *   PAN DIAG→3,  TILT DIAG→9,  SLIDER DIAG→13,  ZOOM DIAG→33
 *   Note: pin 13 doubles as the Teensy onboard LED; used here as input.
 *
 * Pin assignments are defined in MountMotion.cpp — adjust for your wiring.
 */

#include <Arduino.h>
#include <teensystep4.h>
#include <TMCStepper.h>
#include "protocol.h"

// ---------------------------------------------------------------------------
// Default StallGuard thresholds — also used in EepromConfig initialisation
// Order: PAN, TILT, SLIDER, ZOOM
// ---------------------------------------------------------------------------
constexpr uint8_t DEFAULT_STALL_THRESHOLD[4] = { 100, 100, 80, 80 };

// ---------------------------------------------------------------------------
// v2 — Look-at constants
// ---------------------------------------------------------------------------

// Update rate for the look-at controller (ms).  50 Hz is comfortable for smooth tracking.
#define LOOK_AT_INTERVAL_MS  20

// P-gain for the look-at follower.  At this many steps of angular error the
// motor runs at full look-at speed; below it the speed scales proportionally.
// 5000 µsteps ≈ 1.17° (pan/tilt at 256µstep, 15:1 gear, 0.9°/step).
#define LOOK_AT_KP_STEPS  5000.0f

// How long (ms) to maintain reduced slew acceleration after a mid-move subject
// switch.  The grace period ensures the accel stays gentle even as the error
// shrinks below the KP threshold during the reposition.  2 s covers a ~1.5 s
// ramp at LOOK_AT_SLEW_ACCEL_FACTOR=0.10 with margin.
#define LOOK_AT_SLEW_DURATION_MS  2000

// ---------------------------------------------------------------------------
// Subject-switch blend
// ---------------------------------------------------------------------------
// Selecting a new subject mid-move moved the TARGET in a single step, leaving
// the controller to chase a discontinuity.  However gently it was tuned to
// respond, it was still reacting to a jump: full slew speed almost at once,
// then a braking curve into the new aim.  And because pan and tilt are separate
// P-loops with separate errors, whichever had less to travel arrived first — so
// the move read as two axis motions rather than one arc.
//
// Easing the SETPOINT fixes both without touching the controller.  The subject
// position is interpolated from the old to the new one on a smoothstep, which
// has zero velocity at both ends, so the camera eases out of the old subject
// and into the new one.  Both axes are derived from the same moving point, so
// they stay coordinated for free and land together.
//
// Duration scales with how far the camera has to turn — a small correction
// should not take as long as a sweep across the room.
#define LOOK_AT_BLEND_MS_PER_DEG   55.0f
#define LOOK_AT_BLEND_MIN_MS        300
#define LOOK_AT_BLEND_MAX_MS       2200

// ---------------------------------------------------------------------------
// DRIVE GEOMETRY — change these when the hardware changes, nothing else
// ---------------------------------------------------------------------------
// Every ratio in the firmware derives from the numbers in this block.
//
// They used to be written out as FINISHED ratios in two files: the slider's
// 40 mm/rev lived in MountMotion.cpp and again inside the nominal step size
// below, and pan/tilt's 7.5:1 the same way.  Fitting a different pulley meant
// finding both, and missing one gave a mount that MOVES at one scale and
// REPORTS ITS POSITION at another — a fault that looks like a mechanical
// problem and is not.  The tooth counts were only ever in a comment.
//
// SLIDER — GT2 belt.  The GT2 profile is 2 mm between teeth by definition, so
// one motor revolution advances the carriage by teeth x 2 mm.
#define SLIDER_PULLEY_TEETH    20    // GT2 pulley on the slider motor shaft

// PAN / TILT — toothed belt reductions, driven (axis) over driver (motor).
// Both work out at 7.5:1, which is why one constant served both; they are
// separate here because they are separate pulleys and only look alike.
#define PAN_DRIVEN_TEETH      270
#define PAN_DRIVER_TEETH       36
#define TILT_DRIVEN_TEETH     120
#define TILT_DRIVER_TEETH      16

// Motors and driver settings that pair with the above.
#define MOTOR_DEG_PER_STEP_PT  0.9f  // PAN/TILT motors: 0.9 deg/full step
#define MOTOR_DEG_PER_STEP_SZ  1.8f  // SLIDER/ZOOM motors: 1.8 deg/full step
#define MICROSTEPS_PAN         256
#define MICROSTEPS_TILT        256
#define MICROSTEPS_SLIDER       32   // lower: more torque for the heavy carriage
#define MICROSTEPS_ZOOM         32
#define GEAR_RATIO_ZOOM        1.0f  // direct drive

// GT2 is a belt PROFILE, not a choice — 2 mm pitch is what makes it GT2.
constexpr float GT2_BELT_PITCH_MM = 2.0f;

// ---- derived: nothing below is edited by hand -----------------------------
constexpr float SLIDER_MM_PER_REV = SLIDER_PULLEY_TEETH * GT2_BELT_PITCH_MM;
constexpr float GEAR_RATIO_PAN    = (float)PAN_DRIVEN_TEETH  / (float)PAN_DRIVER_TEETH;
constexpr float GEAR_RATIO_TILT   = (float)TILT_DRIVEN_TEETH / (float)TILT_DRIVER_TEETH;
constexpr float SLIDER_FULL_STEPS_PER_REV = 360.0f / MOTOR_DEG_PER_STEP_SZ;

// Nominal hardware deg/µstep values — used before the first subject calibration
// refines them.
constexpr float NOMINAL_PAN_DEG_PER_STEP =
        MOTOR_DEG_PER_STEP_PT / (MICROSTEPS_PAN  * GEAR_RATIO_PAN);     // ≈ 0.000468750
constexpr float NOMINAL_TILT_DEG_PER_STEP =
        MOTOR_DEG_PER_STEP_PT / (MICROSTEPS_TILT * GEAR_RATIO_TILT);    // ≈ 0.000468750
constexpr float NOMINAL_SLIDER_MM_PER_STEP =
        SLIDER_MM_PER_REV / (MICROSTEPS_SLIDER * SLIDER_FULL_STEPS_PER_REV);  // ≈ 0.00625

// ---------------------------------------------------------------------------
// Speed preset (per axis group, 4 presets each)
// ---------------------------------------------------------------------------

struct SpeedPreset {
    uint32_t max_speed;    // physical: deg/sec (PAN/TILT/ZOOM) or mm/sec (SLIDER)
    uint32_t acceleration; // physical: deg/sec² (PAN/TILT/ZOOM) or mm/sec² (SLIDER)
};

// ---------------------------------------------------------------------------
// Callback types
// ---------------------------------------------------------------------------

using LimitsFoundCb = void (*)(Axis axis, int32_t min_steps, int32_t max_steps);
using MoveCompleteCb = void (*)();

// ---------------------------------------------------------------------------
// Mount status snapshot
// ---------------------------------------------------------------------------

struct MountStatusSnapshot {
    int32_t    pos[4];
    MountState state;
    uint8_t    flags;       // MountFlag bitmask
};

// ---------------------------------------------------------------------------
// MountMotion class
// ---------------------------------------------------------------------------

class MountMotion {
public:
    MountMotion();

    // Call once in setup()
    void begin();

    // Call every loop() — drives the state machine
    void update();

    // -----------------------------------------------------------------------
    // Motion control
    // -----------------------------------------------------------------------

    // Continuous jog.  velocities are normalised [-1000, +1000].
    // Zero on all axes = stop jogging.
    // pt_preset (1-4): speed preset for PAN/TILT axes.
    // sz_preset (1-4): speed preset for SLIDER/ZOOM axes.
    void jog(int16_t pan, int16_t tilt, int16_t slider, int16_t zoom,
             uint8_t pt_preset = 2, uint8_t sz_preset = 2);

    // Pan/tilt-only jog — drives pan and tilt exactly like jog() but leaves
    // slider and zoom completely untouched.  Used in CV-tracking mode while
    // a moveSliderTo() is running on the slider axis independently.
    void jogPanTilt(int16_t pan, int16_t tilt, uint8_t pt_preset = 2);

    // Slider/zoom-only jog — drives slider and zoom only, leaving pan/tilt
    // untouched.  Used when CV tracking owns pan/tilt and the operator wants
    // joystick control of the slider simultaneously (axis_mask = 0x0C).
    void jogSliderZoom(int16_t slider, int16_t zoom, uint8_t sz_preset = 2);

    // Move only the slider to an absolute step position using an individual
    // moveAsync() call — does NOT use the stepper group and does NOT cancel
    // any pan/tilt jog that is already running.
    void moveSliderTo(int32_t target, uint8_t sl_preset = 2);

    // Synchronised move to absolute step positions.
    // pt_preset (1-4): speed preset for PAN/TILT axes.
    // sl_preset (1-4): speed preset for SLIDER/ZOOM axes (defaults to pt_preset).
    // TEMPORARY — goto planning diagnostic (2026-08-19).  REMOVE WITH IT.
    // MountMotion cannot send packets, so it records what it decided and the
    // sketch ships it on the next loop.  See CMD_GOTO_DEBUG in protocol.h.
    struct GotoPlan {
        bool     pending;
        uint8_t  path;          // 0 = moveTo, 1 = retargetTo
        bool     sync;
        uint16_t t_move_ms;
        int32_t  target[4];
        int32_t  pos[4];
        uint16_t spd[4];
    };
    bool takeGotoPlan(GotoPlan &out);

    void moveTo(int32_t pan, int32_t tilt, int32_t slider, int32_t zoom,
                uint8_t pt_preset, uint8_t sl_preset = 0, bool sync = true);

    // Relative move — deltas are in logical step space (same frame as moveTo).
    // Converts current physical positions back to logical before adding deltas,
    // so orientation inversion is not applied twice.
    void moveRel(int32_t d_pan, int32_t d_tilt, int32_t d_slider, int32_t d_zoom,
                 uint8_t pt_preset, uint8_t sl_preset = 0);

    // Immediate halt — decelerates as fast as possible.
    void stopAll();

    // Hard stop — no deceleration.  Use for E-STOP only.
    void emergencyStop();

    // Smooth mid-move retarget — update the stepper group target without
    // stopping first.  Safe to call while STATE_MOVING_TO_POS.
    // If the mount is not moving the call is equivalent to moveTo().
    void retargetTo(int32_t pan, int32_t tilt, int32_t slider, int32_t zoom,
                    uint8_t pt_preset, uint8_t sl_preset = 0);

    // -----------------------------------------------------------------------
    // Position
    // -----------------------------------------------------------------------

    int32_t getPosition(Axis axis) const;

    // Zero an axis (sets current position as 0).
    void zeroPosition(Axis axis);

    // -----------------------------------------------------------------------
    // Limits (SLIDER and ZOOM only)
    // -----------------------------------------------------------------------

    // Set known limits (e.g. recalled from EEPROM).
    void setLimits(Axis axis, int32_t min_steps, int32_t max_steps);

    // Async limit-finding using StallGuard via DIAG1 pin.
    // Moves axis to both ends; calls cb when complete.
    // Only one axis can find limits at a time.
    void findLimits(Axis axis, LimitsFoundCb cb);
    void findHome  (Axis axis, LimitsFoundCb cb);  // move to min stop, zero, back off

    bool hasLimits(Axis axis) const;
    int32_t getMinLimit(Axis axis) const;
    int32_t getMaxLimit(Axis axis) const;

    // -----------------------------------------------------------------------
    // Configuration
    // -----------------------------------------------------------------------

    // preset: 1-4.  For GROUP_ZOOM, preset is ignored (single preset).
    void setSpeedPreset(AxisGroup group, uint8_t preset, SpeedPreset params);
    SpeedPreset getSpeedPreset(AxisGroup group, uint8_t preset) const;

    // Zoom has a single independent preset (separate from slider).
    void        setZoomPreset(SpeedPreset params);
    SpeedPreset getZoomPreset() const;

    // Orientation flags
    void setOrientation(bool pan_invert, bool tilt_invert, bool slider_invert, bool zoom_invert = false);

    // Whether this mount has a physical slider axis.
    // When false, slider commands are silently ignored.
    void setHasSlider(bool has_slider);
    bool getHasSlider() const { return _has_slider; }

    // TMC2209 run current (mA) — call after begin()
    void setRunCurrent(Axis axis, uint16_t current_ma);

    // StallGuard threshold for limit finding (0-255, higher = less sensitive)
    void setStallThreshold(Axis axis, uint8_t threshold);

    // -----------------------------------------------------------------------
    // Status
    // -----------------------------------------------------------------------

    MountStatusSnapshot getStatus() const;
    MountState          getState()  const { return _state; }
    bool                isMoving()  const;

    // Live position in physical units (pan/tilt: degrees, slider: mm).
    // Zoom has no physical unit — read raw steps from getStatus().pos[AXIS_ZOOM].
    float               positionPhys(uint8_t axis) const;
    // Bit per axis (0=pan 1=tilt 2=slider 3=zoom): stepper still in motion.
    uint8_t             movingMask() const;

    // Print TMC2209 register read-backs to Serial — call after begin()
    // (Non-const: reads live registers over UART)
    void printDriverDiagnostics();

    // -----------------------------------------------------------------------
    // v2 — Look-at tracking
    // -----------------------------------------------------------------------

    // Set the 3D world coordinates of the subject to track (mm).
    // Safe to call mid-move to switch subject smoothly.
    void setLookAtSubject(float sx, float sy, float sz, uint8_t subject_id = 0xFF);

    // Start a look-at move: slider travels from slider_start_steps to
    // slider_end_steps at sl_preset speed; pan/tilt continuously aim at the
    // current subject.  Returns false if ref not set or limits not found.
    bool startLookAtMove(int32_t slider_start_steps, int32_t slider_end_steps,
                         uint8_t sl_preset, float max_pt_deg_s);

    // Point pan/tilt at the current look-at subject from the current slider
    // position without moving the slider.  Uses a regular moveTo() so the
    // axes decelerate naturally when they arrive.  Returns false if ref not set.
    bool aimAtSubject(uint8_t pt_preset = 2);

    // Stop the look-at move (decelerates all axes).
    void stopLookAtMove();

    // -----------------------------------------------------------------------
    // v2 — Calibration support
    // -----------------------------------------------------------------------

    // Set the deg/µstep scale factors (stored in EEPROM by the .ino after calibration).
    void setDegPerStep(float pan_dps, float tilt_dps);
    void setSliderMmPerStep(float mm_per_step);
    // Rail inclination in degrees, signed; 0 is level.  The look-at maths in
    // here places the camera on the rail, so it needs the same geometry the
    // solver used — otherwise a subject solved for a climbing rail is tracked
    // as though the rail were flat, and tilt comes out very nearly constant.
    void setSliderTiltDeg(float deg) { _slider_tilt_deg = deg; }
    float getSliderTiltDeg() const   { return _slider_tilt_deg; }

    // Set the session reference: maps current step count to known angles.
    // Called every session (and after subject calibration, automatically).
    void setLookAtRef(float pan_ref_deg, float tilt_ref_deg);

    // Query reference state
    bool  isRefSet()      const { return _ref_set; }

    // Convert current step counts to world-frame angles (requires _ref_set).
    float getPanDeg()     const;
    float getTiltDeg()    const;
    float getSliderMm()   const;

    // Deg/step getters (for use by the .ino calibration solver)
    float getPanDegPerStep()    const { return _pan_deg_per_step; }
    float getTiltDegPerStep()   const { return _tilt_deg_per_step; }
    float getSliderMmPerStep()  const { return _slider_mm_per_step; }

    // Ref offset getters — world_angle = steps * dps + ref
    // Used by the calibration solver to convert motor-frame angles to world frame.
    float getPanRefDeg()  const { return _pan_ref_deg; }
    float getTiltRefDeg() const { return _tilt_ref_deg; }

    // Currently tracked subject ID (0xFF = none)
    uint8_t getLaSubjectId() const { return _la_subject_id; }
    // Dropping the subject cancels any blend with it — otherwise a half-finished
    // ease would still be running when the next subject is chosen.
    void    clearLaSubject()       { _la_subject_id = 0xFF; _la_blend_ms = 0; }
    // Look-at mode itself, not merely "a subject id is set".  The subject id
    // persists across a mode change, so it cannot stand in for the mode: a
    // subject selected before look-at was switched off would otherwise still
    // satisfy a check that meant to ask whether look-at is on.
    void    setLookAtMode(bool on)  { _look_at_mode = on; }
    bool    getLookAtMode() const   { return _look_at_mode; }

    // Persist slider-end state in flags after a look-at move arrives.
    // dir 0 = min/◀, dir 1 = max/▶.  Replaces both limit flags atomically
    // so STATUS always reflects the correct end.
    void setLookAtEndFlag(uint8_t dir) {
        if (dir == 0) { _setFlag(FLAG_AT_MIN_LIMIT); _clearFlag(FLAG_AT_MAX_LIMIT); }
        else          { _setFlag(FLAG_AT_MAX_LIMIT); _clearFlag(FLAG_AT_MIN_LIMIT); }
    }

private:
    // -----------------------------------------------------------------------
    // Internal state machine states for limit finding
    // -----------------------------------------------------------------------
    enum class LimitFindState {
        IDLE,
        MOVING_TO_MIN,
        BACKING_OFF_MIN,
        MOVING_TO_MAX,
        DONE
    };

    // -----------------------------------------------------------------------
    // Members
    // -----------------------------------------------------------------------

    // TeensyStep4 steppers + synchronized group
    // Pointers — allocated in begin() once pins are known
    TS4::Stepper*     _stepper[4];
    TS4::StepperGroup _group;

    // TMC2209 drivers — one per dedicated serial port, all address 0
    TMC2209Stepper* _tmc[4];

    // State
    MountState      _state;
    uint8_t         _flags;
    int32_t         _position[4];
    bool            _pan_invert;
    bool            _tilt_invert;
    bool            _slider_invert;
    bool            _zoom_invert;
    bool            _has_slider;

    // Speed presets — index 0 unused (presets are 1-4)
    SpeedPreset     _pt_presets[5];   // pan/tilt: 4 presets
    SpeedPreset     _sl_presets[5];   // slider:   4 presets
    SpeedPreset     _zoom_preset;     // zoom:     single preset (independent of slider)

    // Limits
    int32_t         _min_limit[4];
    int32_t         _max_limit[4];
    bool            _limits_set[4];

    // Limit finding
    LimitFindState  _lf_state;
    Axis            _lf_axis;
    LimitsFoundCb   _lf_cb;
    uint8_t         _stall_threshold[4];
    uint32_t        _lf_start_ms;
    int32_t         _lf_min_found;
    bool            _homing_only;   // true → stop after BACKING_OFF_MIN (findHome)

    // StallGuard interrupt — set by ISR on RISING edge of DIAG pin
    static volatile bool _stall_isr_fired;
    static void _diag_isr()  { _stall_isr_fired = true; }

    // Jog state
    int16_t         _jog_vel[4];
    int8_t          _jog_dir[4];    // -1, 0, +1 — direction currently passed to rotateAsync
    uint8_t         _jog_preset[4]; // preset index active when rotateAsync() was last called
    bool            _jogging;
    uint32_t        _jog_last_ms;   // millis() of the last jog() call (watchdog)

    // Smooth direction-reversal: when direction changes, the axis soft-stops
    // first, then _updateJog() starts the new direction once isMoving is false.
    int8_t          _pending_dir[4];        // queued new direction (0 = none)
    uint8_t         _pending_preset[4];
    float           _pending_spd_factor[4];

    // GOTO state — velocity-controlled P-loop (rotateAsync + overrideSpeed)
    int32_t         _goto_target[4];
    bool            _has_goto_target;
    int8_t          _goto_dir[4];         // current rotateAsync direction per axis (0=stopped)
    // An axis the operator has taken over by jogging while a goto is running.
    // _updateGoto() skips it entirely — without this it would fight the jog,
    // because its restart branch relaunches any axis whose _goto_dir is 0 while
    // the target error is large, which is exactly what a jog creates.
    bool            _goto_axis_released[4] = { false, false, false, false };
    uint32_t        _goto_max_spd_st[4];  // ceiling speed set for each axis (µsteps/s)
    uint32_t        _goto_accel_st[4];    // acceleration for each axis (µsteps/s²)
    float           _goto_decel_dist[4];  // effective P-controller decel window (µsteps),
                                          // capped to move distance so short moves start fast

    // ── v2 Look-at state ────────────────────────────────────────────────────
    float           _la_subject_x;         // 3D subject coords (mm)
    float           _la_subject_y;
    float           _la_subject_z;
    float           _la_max_steps_s[2];    // max speed per axis: [0]=pan, [1]=tilt (µsteps/s)
    int8_t          _la_pt_dir[2];         // current rotateAsync direction: PAN[0], TILT[1]
    uint32_t        _la_last_update_ms;    // timestamp of last look-at controller tick
    uint8_t         _la_subject_id;        // currently tracked subject slot (0xFF = none)
    bool            _look_at_mode = false;  // set by setLookAtMode() / EEPROM load

    // Subject-switch blend.  _la_blend_ms == 0 means no blend is running and
    // the subject is simply _la_subject_*.
    float           _la_blend_from[3] = { 0.f, 0.f, 0.f };
    uint32_t        _la_blend_start_ms = 0;
    uint32_t        _la_blend_ms       = 0;

    // The subject position to aim at RIGHT NOW — the blend evaluated at this
    // instant, or the subject itself when no blend is running.
    void     _laSubjectNow(float *sx, float *sy, float *sz) const;
    uint32_t        _la_slew_until_ms;     // apply slew-accel until this timestamp (mid-move switch)

    // Pre-aim phase: pan/tilt settle to start position before slider moves
    int32_t         _la_pre_aim_pan_tgt;   // target step for pre-aim
    int32_t         _la_pre_aim_tilt_tgt;
    int32_t         _la_stored_s_end;      // slider destination (phys steps, orientation applied)
    uint8_t         _la_stored_sl_preset;  // slider preset to use after pre-aim

    // ── v2 Angle calibration (deg/µstep, mm/µstep) ─────────────────────────
    float           _pan_deg_per_step;     // set by setDegPerStep() / EEPROM load
    float           _tilt_deg_per_step;
    float           _slider_mm_per_step;   // set by setSliderMmPerStep() / EEPROM load
    float           _slider_tilt_deg = 0.0f;  // set by setSliderTiltDeg() / EEPROM load

    // Where the camera actually is when the slider reads cx mm along the rail.
    // On a level rail this is (cx, 0) and everything below is unchanged.
    void _railWorldPos(float cx, float *wx, float *wy) const;

    // ── v2 Session reference (RAM only — cleared on power-cycle) ───────────
    float           _pan_ref_deg;          // pan angle (deg) at step count 0
    float           _tilt_ref_deg;         // tilt angle (deg) at step count 0
    bool            _ref_set;              // false until setLookAtRef() called

    // Periodic TMC UART re-config — recovers from brownout-induced register loss
    uint32_t        _tmc_refresh_ms = 0;

    // -----------------------------------------------------------------------
    // Helpers
    // -----------------------------------------------------------------------

    void     _applyPreset(uint8_t pt_preset, uint8_t sl_preset);
    // Sync scale per axis: the fraction of its PRESET speed this move wants.
    //
    // The motor's max speed is always the preset.  Synchronisation lives here
    // and is folded into overrideSpeed()'s factor instead, so a retarget never
    // has to touch a turning motor — it changes numbers and _updateGoto()
    // applies them on its next tick, which is what retargetTo() always claimed
    // to do.  Setting a new max mid-move needs rotateAsync() to take effect,
    // and rotateAsync() takes a POSITIVE max here (direction is carried by
    // overrideSpeed's sign), so calling it on an axis travelling negative would
    // fling it the other way until the next tick corrected it.
    float    _goto_spd_scale[4] = { 1.f, 1.f, 1.f, 1.f };
    GotoPlan _goto_plan{};      // TEMPORARY — see takeGotoPlan()
    int16_t  _applyOrientation(Axis axis, int16_t velocity) const;
    int32_t  _applyOrientationPos(Axis axis, int32_t target) const;
    bool     _checkStall(Axis axis);
    void     _clampToLimits(int32_t &target, Axis axis) const;
    void     _updateJog();
    void     _updateGoto();                                // velocity P-loop for position moves
    void     _updateLimitFind();
    // v2: 50 Hz look-at controller.  check_slider_arrival=false tracks the
    // subject WITHOUT the end-of-move test, for when the operator is driving
    // the slider by hand and there is no commanded destination to arrive at.
    void     _updateLookAt(bool check_slider_arrival = true);
    void     _updatePreAim();                             // v2: wait for pre-aim to settle, then start slider
    void     _driveTowardTarget(int axis, int32_t target, float max_steps_s); // v2: P-follower for one axis
    void     _setFlag(uint8_t flag)   { _flags |= flag; }
    void     _clearFlag(uint8_t flag) { _flags &= ~flag; }
};
