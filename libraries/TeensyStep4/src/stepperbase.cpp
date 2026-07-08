#include "Arduino.h"

#pragma push_macro("abs")
#undef abs

#include "stepperbase.h"
#include <algorithm>

namespace TS4
{
    StepperBase::StepperBase(int _stepPin, int _dirPin)
        : s(0), v(0), v_sqr(0), stepPin(_stepPin), dirPin(_dirPin),
          stpTimer(nullptr)   // must be explicit: new startRotate() guards on (stpTimer == nullptr)
                              // and heap memory is NOT zeroed on reset, only on cold power-on
    {
        pinMode(stepPin, OUTPUT);
        pinMode(dirPin, OUTPUT);

        // setMaxSpeed(vMaxDefault);
    }

    void StepperBase::startRotate(int32_t _v_tgt, uint32_t a)
    {
        // If target velocity is zero and motor is already stopped, nothing to do.
        // Without this guard, stopAsync() on an idle motor allocates a timer that
        // rotISR() immediately stops (v_tgt==0 path), but isMoving=true is set
        // AFTER rotISR() returns, leaving isMoving=true with no running timer.
        // The next startRotate() call then hits the early-return and never starts.
        if (_v_tgt == 0 && !isMoving) {
            return;
        }

        v_tgt = _v_tgt;
        v_tgt_orig = v_tgt;
        v_tgt_sqr = (int64_t)signum(v_tgt) * v_tgt * v_tgt;
        vDir = (int32_t)signum(v_tgt_sqr - v_sqr);
        twoA = 2 * a;

        // If we are already moving, just update the target and exit.
        if (isMoving && stpTimer != nullptr) {
            return;
        }

        noInterrupts();
        // Clean up a timer that stepISR() stopped-but-didn't-return (GOTO completion).
        // rotISR() now does its own full inline cleanup so timerPendingReturn
        // is only set by stepISR(); handle it here before allocating.
        if (timerPendingReturn && stpTimer != nullptr) {
            stpTimer->stop();
            TimerFactory::returnTimer(stpTimer);
            stpTimer = nullptr;
            timerPendingReturn = false;
        }

        if (stpTimer == nullptr) {
            stpTimer = TimerFactory::makeTimer();
            if (stpTimer != nullptr) {
                stpTimer->setPulseParams(8, stepPin);
                stpTimer->attachCallbacks([this] { rotISR(); }, [this] { resetISR(); });
                v_sqr = vDir * 200 * 200;
                mode = mode_t::rotate;
                stpTimer->start();
                isMoving = true;
            }
        }
        interrupts();
    }




    void StepperBase::startMoveTo(int32_t _s_tgt, int32_t v_e, uint32_t v_tgt, uint32_t a)
    {
        s          = 0;
        int32_t ds = std::abs(_s_tgt - pos);
        s_tgt      = ds;

        dir = signum(_s_tgt - pos);
        digitalWriteFast(dirPin, dir > 0 ? HIGH : LOW);
        delayMicroseconds(5);

        twoA = 2 * a;
        // v_sqr      = (int64_t) v * v;
        v_sqr     = 0;
        v         = 0;
        v_tgt_orig = v_tgt;
        v_tgt_sqr = (int64_t)v_tgt * v_tgt;

        int64_t accLength = (v_tgt_sqr - v_sqr) / twoA + 1;
        if (accLength >= ds / 2) accLength = ds / 2;

        accEnd   = accLength - 1;
        decStart = s_tgt - accLength;

        // SerialUSB1.printf("TimerAddr: %p\n", &stpTimer);
        // SerialUSB1.printf("a: %6d   twoA:  %6d\n", a, twoA);
        // SerialUSB1.printf("v0:%6d   v_tgt: %6d\n", v, v_tgt);
        // SerialUSB1.printf("s: %6d   s_tgt: %6d\n", s, s_tgt);
        // SerialUSB1.printf("aE:%6d   dS:    %6d %d\n\n", accEnd, decStart, accLength);

        noInterrupts(); // <--- LOCK
        if (!isMoving)
        {
            if (stpTimer != nullptr) {
                stpTimer->stop();
                TimerFactory::returnTimer(stpTimer);
            }

            stpTimer = TimerFactory::makeTimer();
            if (stpTimer != nullptr) {
                stpTimer->attachCallbacks([this] { stepISR(); }, [this] { resetISR(); });
                stpTimer->setPulseParams(8, stepPin);
                isMoving = true;
                v_sqr    = 200 * 200;
                mode     = mode_t::target;
                stpTimer->start();
            }
        }
        interrupts(); // <--- UNLOCK
    }

    // void StepperBase::rotateAsync()
    // {
    //     rotateAsync(vMax);
    // }

    void StepperBase::startStopping(int32_t v_end, uint32_t a)
    {
        // if (!isMoving) return;
        if (mode == mode_t::rotate){
            mode = mode_t::stopping;
            startRotate(v_end, a);
            mode = mode_t::stopping;
        } else {
            mode = mode_t::stopping;
        }
        // SerialUSB1.println("stoprot");
        // SerialUSB1.flush();
    }

    void StepperBase::emergencyStop()
    {
        noInterrupts(); // <--- LOCK
        if (stpTimer != nullptr) {
            stpTimer->stop();
            TimerFactory::returnTimer(stpTimer);
            stpTimer = nullptr;
        }
        isMoving = false;
        v_sqr    = 0;
        interrupts(); // <--- UNLOCK
    }

    void StepperBase::overrideSpeed(float factor)
    {
        if (mode == mode_t::rotate)
        {
            noInterrupts();
            v_tgt =v_tgt_orig * factor;
            v_tgt_sqr = (int64_t)signum(v_tgt) * v_tgt * v_tgt;
            vDir      = (int32_t)signum(v_tgt_sqr - v_sqr);
            interrupts();

            // Serial.print(v_tgt_sqr);
            // Serial.printf("  %d \n", v_tgt);
        }
    }

    void StepperBase::cleanupTimer() 
    {
        if (timerPendingReturn) {
            noInterrupts();
            if (stpTimer != nullptr) {
                stpTimer->stop();
                TimerFactory::returnTimer(stpTimer);
                stpTimer = nullptr;
            }
            isMoving = false;
            timerPendingReturn = false;
            interrupts();
        }
    }
}

#pragma pop_macro("abs")