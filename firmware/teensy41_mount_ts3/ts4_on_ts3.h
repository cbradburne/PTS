#pragma once
/*
 * ts4_on_ts3.h — TeensyStep4's interface, as MountMotion uses it, on TeensyStep 3.
 *
 * The TeensyStep 3 test build of the mount (firmware/teensy41_mount_ts3).  Its
 * MountMotion.cpp is the production file unchanged and its MountMotion.h differs
 * only in including this instead of <teensystep4.h>, so what changes on the bench
 * is the library underneath, not the motion code.  tools/test_teensy_ts3.py keeps
 * it that way and checks the mapping below.
 *
 * TeensyStep4 here means OUR copy, libraries/TeensyStep4: 0.0.3 plus our fixes.
 * What each call did there, and what it does here:
 *
 *   setMaxSpeed(v)      The speed — SIGNED, the sign is the direction — that the
 *                       next rotateAsync() or moveAsync() starts with.  Kept here,
 *                       as TeensyStep4 kept it.  TeensyStep 3's own maximum is a
 *                       limit that acts at once, also on a motor already moving, so
 *                       it is only ever set at the start of a move.
 *   setAcceleration(a)  Kept until the next start, limited to v²/2 as TeensyStep4
 *                       limited it (minus TeensyStep4's int32 overflow above
 *                       46,340 steps/s, which was a bug and not a limit).
 *   rotateAsync()       Rotate at the kept speed, ramping from the current one.
 *   overrideSpeed(f)    While rotating: rotate at f × the speed the rotation
 *                       started with, truncated to whole steps/s as TeensyStep4
 *                       stored it.  A rotation that comes out at 0 has stopped.
 *                       Ignored unless the motor is rotating, as in TeensyStep4.
 *   stopAsync()         A rotation: see TS3_SOFT_STOP.  A move to a target:
 *                       decelerate at that move's own rate (both libraries).
 *   emergencyStop()     Stand still at once.
 *   setTargetAbs(t) + moveAsync()   moveAbsAsync(t) at |speed|.
 *   isMoving            A member that TeensyStep4's interrupt wrote; here an
 *                       object that asks TeensyStep 3 each time it is read.
 *   setPosition(p)      setPositionCounter(p).
 *   begin(), cleanupTimer(), StepperGroup
 *                       Nothing to do: TeensyStep 3 takes and frees its timers
 *                       itself, and MountMotion builds its group but never moves
 *                       it.  begin() attaches the error report instead.
 *
 * Left as TeensyStep 3 makes them, on purpose (README.md):
 *   - below the pull-in speed a motor starts, stops and reverses without a ramp:
 *     TeensyStep 3's default, 100 steps/s.  TeensyStep4 started every move at
 *     200 steps/s;
 *   - the STEP pulse and the DIR setup time are TeensyStep 3's defaults, 5 µs
 *     each, made by the timer rather than waited out inside the interrupt.  Ours
 *     were cut from 8 and 5 µs to 1 µs because the waiting starved the interrupt;
 *   - the step interrupts run at priority 48, ahead of the serial ports and the
 *     StallGuard pins.
 */
#include <Arduino.h>
#include <TeensyStep.h>

// How stopAsync() stops a ROTATING motor (the jog, the goto loop, the look-at
// drive — everything but a move to a target).
//   0, the default: at once, as our TeensyStep4 does.  Its rotation interrupt
//     stops dead the moment the target speed is 0, so the deceleration
//     MountMotion sets just before a stopAsync() is never used.  A joystick
//     hides it — the PC app and the displays ease the speed down before they send
//     the stop — but a soft limit or the jog watchdog stops from full speed.
//     Kept so that the first bench test changes the library and nothing else.
//   1: decelerate at the acceleration set before it, as MountMotion's comments
//     intend ("Soft stop — ... decelerate gently").  A soft-limit stop then runs
//     past the limit by v²/2a: try it with the limits in mind.
//     TS3_SOFT_STOP=1 tools/build.sh flash teensyts3
#ifndef TS3_SOFT_STOP
#define TS3_SOFT_STOP 0
#endif

namespace TS4
{
    namespace detail
    {
        // TeensyStep 3 reports every warning and error through one function.  It
        // can be called with the interrupts off — MountMotion wraps many of its
        // calls in noInterrupts() — so it only counts; reportErrors() prints.
        struct Reports
        {
            volatile uint32_t errors = 0, warnings = 0;
            volatile int lastError = 0, lastWarning = 0;
            uint32_t printedErrors = 0, printedWarnings = 0, lastPrintMs = 0;
        };
        inline Reports& reports()
        {
            static Reports r;
            return r;
        }
        inline void onReport(TeensyStep::errorCode code)
        {
            Reports& r = reports();
            if (TeensyStep::isError(code))
            {
                r.errors      = r.errors + 1;
                r.lastError   = (int)code;
            }
            else if (TeensyStep::isWarning(code))
            {
                r.warnings    = r.warnings + 1;
                r.lastWarning = (int)code;
            }
        }
    }

    // TeensyStep4 started its timers here.  TeensyStep 3 needs nothing; its
    // reports are attached instead.
    inline void begin() { TeensyStep::attachErrFunc(detail::onReport); }

    // Prints what TeensyStep 3 has reported since the last time, at most once a
    // second: from loop(), where printing is safe.
    inline void reportErrors(Print& out)
    {
        detail::Reports& r = detail::reports();
        uint32_t e = r.errors, w = r.warnings;
        if (e == r.printedErrors && w == r.printedWarnings) return;
        uint32_t now = millis();
        if (now - r.lastPrintMs < 1000) return;
        r.lastPrintMs = now;
        if (e != r.printedErrors)
            out.printf("[TS3] %lu error(s), last: %s\n", (unsigned long)(e - r.printedErrors),
                       TeensyStep::describe((TeensyStep::errorCode)r.lastError));
        if (w != r.printedWarnings)
            out.printf("[TS3] %lu warning(s), last: %s\n", (unsigned long)(w - r.printedWarnings),
                       TeensyStep::describe((TeensyStep::errorCode)r.lastWarning));
        r.printedErrors   = e;
        r.printedWarnings = w;
    }

    class Stepper
    {
        // First, so that it is built before isMoving, which refers to it.
        TeensyStep::Stepper motor;

     public:
        Stepper(int stepPin, int dirPin)
            : motor(stepPin, dirPin), isMoving{motor}
        {}

        // TeensyStep4's public member, read as a bool: true while the motor moves.
        struct MovingFlag
        {
            const TeensyStep::Stepper& m;
            operator bool() const { return m.isMoving(); }
        } isMoving;

        Stepper& setMaxSpeed(int32_t speed)
        {
            vMax = speed > vMaxMax ? vMaxMax : speed < -vMaxMax ? -vMaxMax : speed;
            return *this;
        }

        Stepper& setAcceleration(uint32_t a)
        {
            int64_t limit = (int64_t)vMax * vMax / 2;
            acc = (int64_t)a < limit ? a : (uint32_t)limit;
            return *this;
        }

        void rotateAsync(int32_t v = 0)
        {
            int32_t base = v == 0 ? vMax : v;
            if (base == 0)
            {
                // TeensyStep4: nothing for a motor standing still, a stop for one
                // that moves, at the acceleration set last.
                if (motor.isMoving()) stopRotation(true);
                return;
            }
            mode    = Mode::rotate;
            rotBase = base;
            motor.setMaxSpeed((float)vMaxMax); // a limit here; the speed is the command's
            motor.setAcceleration((float)acc);
            command((float)base);
        }

        void overrideSpeed(float factor)
        {
            if (mode != Mode::rotate || !motor.isMoving()) return;
            int32_t v = (int32_t)(rotBase * factor); // TeensyStep4 kept it in whole steps/s
            if (v == 0)
            {
                // At the acceleration the rotation started with: TeensyStep4's
                // override changed the speed and nothing else.
                stopRotation(false);
                return;
            }
            command((float)v);
        }

        void stopAsync()
        {
            if (mode == Mode::rotate)
            {
                stopRotation(true); // at the acceleration set last, as TeensyStep4
                return;
            }
            mode    = Mode::stopping;
            lastCmd = 0.0f;
            motor.stopAsync(); // a move to a target: at its own deceleration, as TeensyStep4
        }

        void emergencyStop()
        {
            mode    = Mode::idle;
            lastCmd = 0.0f;
            motor.emergencyStop();
        }

        void setTargetAbs(int32_t pos) { target = pos; }

        void moveAsync()
        {
            mode    = Mode::target;
            lastCmd = 0.0f;
            motor.setMaxSpeed((float)(vMax < 0 ? -vMax : vMax));
            motor.setAcceleration((float)acc);
            motor.moveAbsAsync(target);
        }

        int32_t getPosition() const { return motor.getPosition(); }
        void setPosition(int32_t p) { motor.setPositionCounter(p); }
        void cleanupTimer() {}

     private:
        // TeensyStep4's limits and defaults (stepper.h), kept so that a setting
        // means here what it meant there.
        static constexpr int32_t vMaxMax = 100'000;

        enum class Mode : uint8_t { idle, rotate, target, stopping };

        Mode mode       = Mode::idle;
        int32_t vMax    = 1'000; // TeensyStep4's vMaxDefault
        uint32_t acc    = 1'000; // TeensyStep4's aDefault
        int32_t rotBase = 0;     // the speed the current rotation started with
        int32_t target  = 0;
        float lastCmd   = 0.0f;  // the speed last commanded, so that a loop that
                                 // sends the same one every pass costs nothing

        void command(float speed)
        {
            if (speed == lastCmd && motor.isMoving()) return;
            lastCmd = speed;
            motor.rotateAsync(speed);
        }

        // atSetAcceleration: decelerate at the acceleration set last (a stop),
        // or at the one the rotation runs at (an override that comes out at 0).
        void stopRotation(bool atSetAcceleration)
        {
            lastCmd = 0.0f;
#if TS3_SOFT_STOP
            mode = Mode::stopping;
            if (atSetAcceleration) motor.setAcceleration((float)acc);
            motor.stopAsync();
#else
            (void)atSetAcceleration;
            mode = Mode::stopping;
            motor.emergencyStop();
#endif
        }
    };

    // MountMotion builds one and adds its four motors, but never moves it.
    class StepperGroup
    {
     public:
        void add(Stepper&) {}
    };
}
