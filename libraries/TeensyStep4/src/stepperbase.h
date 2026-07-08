#pragma once

#include "Arduino.h"
#pragma push_macro("abs")
#undef abs

#include "timers/interfaces.h"
#include "timers/timerfactory.h"
#include <algorithm>
#include <cstdint>
#include <string>

namespace TS4
{
    class StepperBase
    {
     public:
        std::string name;
        bool isMoving = false;
        void emergencyStop();
        void overrideSpeed(float factor);
        void cleanupTimer();

     protected:
        StepperBase(const int stepPin, const int dirPin);

        void startMoveTo(int32_t s_tgt, int32_t v_e, uint32_t v_max, uint32_t a);
        void startRotate(int32_t v_max, uint32_t a);
        void startStopping(int32_t va_end, uint32_t a);


        inline void setDir(int d);
        int32_t dir;
        int32_t vDir;

        volatile int32_t pos = 0;
        volatile int32_t target;

        int32_t s_tgt;
        int32_t v_tgt, v_tgt_orig;
        int64_t v_tgt_sqr;

        int32_t twoA;
        int32_t decStart, accEnd;

        volatile int32_t s;
        volatile int32_t v;
        volatile int64_t v_sqr;

        inline void doStep();

        const int stepPin, dirPin;

        ITimer* stpTimer;
        inline void stepISR();
        inline void rotISR();
        inline void resetISR();

        enum class mode_t {
            target,
            rotate,
            stopping,
        } mode = mode_t::target;

        // Bresenham:
        StepperBase* next = nullptr; // linked list of steppers, maintained from outside
        int32_t A, B;                // Bresenham parameters (https://en.wikipedia.org/wiki/Bresenham)

        friend class StepperGroupBase;

        volatile bool timerPendingReturn = false;
    };

    //========================================================================================================
    // Inline implementation
    //========================================================================================================

    void StepperBase::doStep()
    {
        digitalWriteFast(stepPin, HIGH);
        s += 1;
        pos += dir;

        StepperBase* stepper = next;
        while (stepper != nullptr) // move slave motors if required
        {
            if (stepper->B >= 0)
            {
                digitalWriteFast(stepper->stepPin, HIGH);
                stepper->pos += stepper->dir;
                stepper->B -= this->A;
            }
            stepper->B += stepper->A;
            stepper = stepper->next;
        }
    }

    void StepperBase::stepISR()
    {
        if (mode == mode_t::stopping){
            mode = mode_t::target;
            if (s < accEnd)                                     // still accelerating
            {
                accEnd = decStart = 0;                          // start deceleration
                s_tgt             = 2 * s;                      // we need the same way to decelerate as we traveled so far
            } else if (s < decStart)                            // constant speed phase
            {
                decStart = 0;                                   // start deceleration
                s_tgt    = s + accEnd;                          // normal deceleration distance  ds = distance to end
            }
        }

        if (s < accEnd) // accelerating
        {
            v = signum(v_sqr) * sqrtf(std::abs(v_sqr));
            v_sqr += twoA;
            stpTimer->updateFrequency(std::abs(v));
            doStep();
        } else if (s < decStart) // constant speed
        {
            v = std::min(sqrtf(v_sqr), sqrtf(v_tgt_sqr));
            stpTimer->updateFrequency(v);
            doStep();
        } else if (s < s_tgt) // decelerating
        {
            v_sqr -= twoA;
            v = signum(v_sqr) * sqrtf(std::abs(v_sqr));
            stpTimer->updateFrequency(std::abs(v));
            doStep();
        } else // target reached
        {
            if (stpTimer != nullptr) {
                stpTimer->stop();
            }
            
            // DO NOT call returnTimer or set stpTimer = nullptr here!
            isMoving = false;
            timerPendingReturn = true; // Mark for cleanup in the main loop

            auto *cur = this;
            while (cur != nullptr) 
            {
                auto *tmp = cur->next;
                cur->next = nullptr;
                cur = tmp;
            }
        }
        //    isMoving = false;
        //}
    }

    void StepperBase::rotISR() {
        if (v_tgt == 0) {
            // Full cleanup inline.  returnTimer() only sets isFree=true —
            // safe to call from an ISR on single-core ARM.  Doing it here
            // avoids the timerPendingReturn deferred path which introduced
            // mode/isMoving state-machine edge cases that prevented restart.
            v_sqr = 0;
            if (stpTimer != nullptr) {
                stpTimer->stop();
                TimerFactory::returnTimer(stpTimer);
                stpTimer = nullptr;
            }
            isMoving = false;
            timerPendingReturn = false;
            return;
        }
        mode = mode_t::rotate;
        int32_t v_abs;

        // Use explicit int64_t comparison — std::abs has no guaranteed int64_t
        // overload in the included headers, so it silently truncates to int32_t,
        // overflowing during direction reversals (v_sqr≈-200M, v_tgt_sqr≈4B).
        int64_t _diff = v_sqr - v_tgt_sqr;
        if (_diff > (int64_t)twoA || _diff < -(int64_t)twoA) // target speed not yet reached
        {
            v_sqr += vDir * twoA;

            // Only update DIR pin (and wait for it to settle) when direction
            // actually changes.  Calling delayMicroseconds(5) on every step
            // blocks all ISRs for 5 µs each time — with 4 motors at full speed
            // that consumes ~50% of CPU time in busy-wait and causes step-pulse
            // jitter on every axis.
            int32_t new_dir = signum(v_sqr);
            if (new_dir != dir) {
                dir = new_dir;
                digitalWriteFast(dirPin, dir > 0 ? HIGH : LOW);
                delayMicroseconds(5);
            }

            v_abs = sqrtf(std::abs(v_sqr));
            stpTimer->updateFrequency(v_abs);
            doStep();
        } else
        {
            // At target speed — direction is stable, no DIR write or delay needed.
            if (v_tgt != 0)
            {
                v_abs = sqrtf(std::abs(v_sqr));
                stpTimer->updateFrequency(v_abs);
                doStep();
            } else
            {
                stpTimer->stop();
                // DO NOT set stpTimer = nullptr here!
                // DO NOT call returnTimer here!
                isMoving = false; 
                timerPendingReturn = true; // Mark it for cleanup in the main loop
            }

            /*
            {
                //SerialUSB.printf("rotISR %s stopped\n", name.c_str());
                stpTimer->stop();
                TimerFactory::returnTimer(stpTimer);
                stpTimer = nullptr;
                v_sqr = 0;

                auto *cur = this;
                while (cur != nullptr) // hack, remove slave motors
                {
                    auto *tmp = cur->next;
                    cur->next = nullptr;
                    cur = tmp;
                }
                isMoving = false;
            }
            */
        }

        // {
        //     //      Serial.println("rot0");
        //     int32_t v_abs;
        //     if (vDir * (v_tgt_sqr - v_sqr) > twoA)
        //     {
        //         v_sqr += vDir * twoA;

        //         digitalWriteFast(dirPin, signum(v_sqr) > 0 ? HIGH : LOW);
        //         delayMicroseconds(5);

        //         v_abs = sqrtf(std::abs(v_sqr));
        //         stpTimer->updateFrequency(v_abs);
        //         doStep();
        //     } else
        //     {
        //         //      Serial.println("rot2");
        //         v_abs = sqrtf(std::abs(v_tgt_sqr));
        //         v = signum(v_tgt_sqr) * v_abs;

        //         if (mode != mode_t::stopping)
        //         {
        //             stpTimer->updateFrequency(v_abs);
        //             doStep();
        //         } else
        //         {
        //             stpTimer->stop();
        //             TimerFactory::returnTimer(stpTimer);
        //             stpTimer = nullptr;
        //             isMoving = false;
        //             v = 0;
        //             v_sqr = 0;
        //             //       Serial.println("stopped");
        //         }
        //     }

        //     pos += signum(v);
    }

    void StepperBase::resetISR()
    {
        // Serial.println("r");
        // Serial.flush();
        StepperBase* stepper = this;
        while (stepper != nullptr)
        {
            digitalWriteFast(stepper->stepPin, LOW);
            stepper = stepper->next;
        }
    }
}
#pragma pop_macro("abs")
