"""The motor may not accelerate faster than the curve it is following asks for.

_driveTowardTarget() derives its acceleration from the braking geometry alone:

    acc = v_max_braking * max_steps_s / (2 * KP_STEPS)

which knows nothing about the trajectory. It comes out at 469 deg/s^2, and x3
for the slew boost, 1406. A 90 degree switch's smoothstep peaks at 47. So the
motor was allowed to change speed thirty times faster than anything was asking
it to, and at 1406 deg/s^2 a single 20 ms control tick permits a 28 deg/s step
— the whole plateau, between two ticks. The controller only recomputes at 50
Hz, so its output is a staircase, and that acceleration executes every step of
it essentially instantly.

A FLAT limit is the obvious fix and the wrong one. What a smoothstep demands is
6 x travel / duration^2, and the SMALL turns are greedy, because their duration
is short:

     5 deg over  300 ms -> 333 deg/s^2
    90 deg over 3388 ms ->  47 deg/s^2

Any flat number low enough to help a 90 degree switch clips a 5 degree one —
reintroducing at the small end the exact clipping that was just removed at the
large end (see test_look_at_speed_ceiling.py).

So it is derived per switch from the curve itself, with headroom, and taken
only when it is the SMALLER of the two. It cannot clip, because it is computed
from the very trajectory it bounds.

The payoff is at the end of the move. acc also sets the sqrt braking curve, so
a gentler acc means braking starts further out:

    90 deg switch:  0.80 deg of runway -> 8.44 deg,  17 ms of decel -> 424 ms

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent

HDR = (REPO / "firmware/teensy41_mount/MountMotion.h").read_text()
CPP = (REPO / "firmware/teensy41_mount/MountMotion.cpp").read_text()


def define(name):
    m = re.search(rf"#define {name}\s+([\d.]+)f?", HDR)
    assert m, f"{name} not found"
    return float(m.group(1))


def code_only(text):
    return "\n".join(l if l.find("//") < 0 else l[:l.find("//")]
                     for l in text.splitlines())



def clear_la_body(hdr: str) -> str:
    """The body of clearLaSubject(), whatever fields it happens to clear.

    This used to be matched as the literal "_la_blend_brake = 0.0f; }" — which
    broke the moment another field was added before the brace. Every piece of
    state a switch sets has to be dropped here, and the test should say that
    rather than pin the punctuation."""
    s = hdr[hdr.index("void    clearLaSubject()"):]
    return s[:s.index("}") + 1]


# ---- 1. it is derived from the curve, not typed in -------------------------
print("1. where the limit comes from:")
setter = code_only(CPP[CPP.index("void MountMotion::setLookAtSubject("):])
setter = setter[:setter.index("\n}\n")]
assert "_la_blend_accel = LOOK_AT_ACCEL_HEADROOM * 6.0f * travel" in setter, \
    "the acceleration limit is not derived from the blend's own curve; a fixed\n" \
    "    number here clips whichever end of the range it was not chosen for"
assert "(ms / 1000.0f) * (ms / 1000.0f)" in setter, \
    "the limit is not divided by duration SQUARED — that is not an acceleration"
HEAD = define("LOOK_AT_ACCEL_HEADROOM")
assert HEAD > 1.0, "no headroom: the controller could never correct an error"
print(f"   6 x travel / duration^2, x{HEAD} headroom              OK")

# ---- 2. and only ever taken when it is the smaller ------------------------
print("\n2. how it is applied:")
drive = code_only(CPP[CPP.index("void MountMotion::_driveTowardTarget("):])
drive = drive[:drive.index("\n}\n")]
assert "if (_la_blend_accel > 0.0f && dps_axis > 0.0f) {" in drive, \
    "the limit is applied unconditionally; outside a switch there is no curve\n" \
    "    to derive it from and the fixed geometry must govern"
assert "if (acc > acc_limit) acc = acc_limit;" in drive, \
    "the limit is not taken as a minimum — it could RAISE the acceleration"
assert "_la_blend_accel / dps_axis" in drive, \
    "deg/s^2 is not converted to steps/s^2; the units would be out by 2000x"
print("   only during a switch, and only when smaller           OK")

# It must reach the braking curve, not just the stepper: v_sqrt assumes
# deceleration == acc, and a motor held below what the curve assumed overshoots.
assert drive.index("if (acc > acc_limit)") < drive.index("float v_sqrt"), \
    "the limit is applied after v_sqrt is computed, so the braking curve still\n" \
    "    assumes the old acceleration and the camera overshoots every target"
assert drive.index("if (acc > acc_limit)") < drive.index("float stepper_acc"), \
    "the stepper's acceleration is taken before the limit is applied"
print("   bounds the braking curve and the stepper together     OK")

# ---- 3. cleared with the blend ---------------------------------------------
print("\n3. when no switch is running:")
assert "_la_blend_accel = 0.0f;" in setter, \
    "a first selection leaves the previous switch's acceleration limit in place"
assert "_la_blend_accel = 0.0f;" in clear_la_body(HDR), \
    "dropping the subject leaves the limit behind"
print("   cleared on first selection and on deselect            OK")

# ---- 4. the numbers across the range ---------------------------------------
print("\n4. what it actually does:")
PER, LO, HI = (define("LOOK_AT_BLEND_MS_PER_DEG"),
               define("LOOK_AT_BLEND_MIN_MS"), define("LOOK_AT_BLEND_MAX_MS"))
FILL = define("LOOK_AT_BLEND_FILL")
KP = define("LOOK_AT_KP_STEPS")
VMAX = define("AXIS_MAX_STEPS_S")
DPS = 0.9 / (256 * (270 / 36))
CEIL = VMAX * DPS
GEOM = (VMAX * VMAX) / (2 * KP) * DPS          # unbounded, at cap fraction 1.0


def duration_s(d):
    return max(min(max(d * PER, LO), HI), 1.5 * d * 1000.0 / (CEIL * FILL)) / 1000.0


rows = []
for d in (5, 10, 25, 40, 72, 90, 120):
    t = duration_s(d)
    need = 6 * d / (t * t)
    used = min(GEOM, need * HEAD)
    peak = 1.5 * d / t
    rows.append((d, need, used, peak * peak / (2 * used), peak / used * 1000))
    print(f"   {d:5.0f}° curve wants {need:5.0f}, uses {used:5.0f} deg/s^2 -> "
          f"brake {rows[-1][3]:5.2f}° over {rows[-1][4]:4.0f} ms")

# Nothing may be clipped: the limit is always at or above what the curve needs.
for d, need, used, _, _ in rows:
    assert used >= need, \
        f"{d}° is clipped — curve needs {need:.0f} deg/s^2 and only {used:.0f} allowed"
print("   no turn is clipped                                    OK")

# Small turns must be untouched: they were never the complaint.
small = [r for r in rows if r[0] == 5][0]
assert abs(small[2] - GEOM) < 1.0, \
    "a 5° turn no longer runs on the unbounded geometry; the small end has been\n" \
    "    slowed down to fix a fault that only ever affected the large end"
print("   5° still runs on the fixed geometry, unchanged        OK")

# And the big turns must actually gain a usable ease-out. 17 ms was the fault.
big = [r for r in rows if r[0] == 90][0]
assert big[4] > 300, \
    f"a 90° switch still decelerates in {big[4]:.0f} ms; the whole point is that\n" \
    "    the arrival stops happening inside a control tick or two"
print(f"   90° decelerates over {big[4]:.0f} ms, was 17            OK")

print("\nALL CHECKS PASSED")
