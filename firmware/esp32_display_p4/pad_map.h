#pragma once
/*
 * pad_map.h — a controller's sticks to a mount jog, the PC app's way.
 *
 * The same law as pc_app/motion/joystick.py + command_dispatcher.py, so a
 * controller feels the same on the display as on the PC:
 *
 *   right stick X → pan          right stick Y → tilt   (up = tilt up)
 *   left stick X  → slider       left stick Y  → zoom   (up = zoom in)
 *
 *   deadzone 0.08, rescaled so motion starts from 0 at its edge
 *   EMA smoothing, alpha 0.6 at 20 Hz
 *   ×1000 truncated, compared at that resolution
 *   circular-to-square per stick, so 45° full deflection reaches ±1000 on both
 *   a jog every tick while anything is non-zero, one zero jog when it stops
 *
 * One difference: the result is clamped to ±1000.  A pad whose stick reaches
 * the corner of its square (most cheap pads do) would otherwise come out at
 * ±1414 after circular-to-square; the touch joystick can never ask for more
 * than 1000, and neither does this.
 *
 * In doubles, as Python does it, so host/test_pad.py can hold it to the PC
 * app's own functions to the count; twenty ticks a second costs nothing.
 *
 * Plain C++, compiled on the Mac by host/test_pad.py.
 */
#include <stdint.h>
#include <stdlib.h>
#include <math.h>
#include "pad_reports.h"

#define PAD_DEADZONE      0.08
#define PAD_SMOOTH_ALPHA  0.6
#define PAD_JOG_RATE_HZ   20
#define PAD_JOG_FULL      1000

enum { PAD_PAN = 0, PAD_TILT, PAD_SLIDER, PAD_ZOOM };

static inline double pad_apply_deadzone(double v, double dz) {
    if (fabs(v) < dz) return 0.0;
    double sign = v > 0 ? 1.0 : -1.0;
    return sign * (fabs(v) - dz) / (1.0 - dz);
}

// pc_app circ_to_sq(): Python's round() halves to even, as lrint does.
static inline void pad_circ_to_sq(int32_t *x, int32_t *y) {
    if (*x == 0 && *y == 0) return;
    double mag  = hypot((double)*x, (double)*y);
    double maxc = (double)(abs(*x) > abs(*y) ? abs(*x) : abs(*y));
    *x = (int32_t)lrint((double)*x / maxc * mag);
    *y = (int32_t)lrint((double)*y / maxc * mag);
}

struct PadJogMapper {
    double  s[4]    = {};     // smoothed pan, tilt, slider, zoom
    int32_t last[4] = {};     // the previous tick's ×1000 values, before circ_to_sq
    bool    last_zero = true;

    void reset() { *this = PadJogMapper(); }

    // One tick at PAD_JOG_RATE_HZ.  True when a jog should go out now, in out[].
    bool tick(const PadInput &in, int16_t out[4]) {
        const double raw[4] = {
             pad_apply_deadzone(in.rx, PAD_DEADZONE),    // pan
            -pad_apply_deadzone(in.ry, PAD_DEADZONE),    // tilt: stick up is tilt up
             pad_apply_deadzone(in.lx, PAD_DEADZONE),    // slider
            -pad_apply_deadzone(in.ly, PAD_DEADZONE),    // zoom: stick up is zoom in
        };
        const double a = PAD_SMOOTH_ALPHA, b = 1.0 - a;
        int32_t v[4];
        bool changed = false;
        for (int i = 0; i < 4; i++) {
            s[i] = a * raw[i] + b * s[i];
            v[i] = (int32_t)(s[i] * PAD_JOG_FULL);      // Python int(): towards zero
            if (v[i] != last[i]) changed = true;
            last[i] = v[i];
        }
        pad_circ_to_sq(&v[PAD_PAN], &v[PAD_TILT]);
        pad_circ_to_sq(&v[PAD_SLIDER], &v[PAD_ZOOM]);
        bool is_zero = !v[0] && !v[1] && !v[2] && !v[3];
        bool send = !( !changed && is_zero && last_zero );
        last_zero = is_zero;
        for (int i = 0; i < 4; i++) {
            int32_t c = v[i] > PAD_JOG_FULL ? PAD_JOG_FULL : v[i] < -PAD_JOG_FULL ? -PAD_JOG_FULL : v[i];
            out[i] = (int16_t)c;
        }
        return send;
    }

    bool moving() const { return !last_zero; }
};
