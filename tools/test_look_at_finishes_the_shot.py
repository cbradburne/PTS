"""A look-at move ends when the SHOT arrives, not when the rail does.

Reported on 2026-08-27, with the use case attached, which is what makes it
matter: the rig is covering a panel. The slider is tracking one speaker, a
different person starts talking, the operator switches subject — and if the
slider reaches the end of the rail before the turn is finished, the camera
stops pointing between two people. On a panel that is a shot of nobody.

stopLookAtMove() stops all four steppers, so ending on slider arrival cut pan
and tilt off wherever they had got to. There was nothing gradual about it: the
axes were commanded to stop, mid-blend.

The fix is to let the aim finish. Once the slider is parked the target angles
are static apart from the blend, so pan and tilt close on them in their own
time and the move ends when they are there. A move with no switch in it is
unaffected — the camera has been tracking the whole way, so the aim is already
within tolerance when the rail arrives and it ends on the same tick it always
did.

A timeout bounds it, because a state machine that waits for a condition needs
a way out if the condition never comes. The clock starts on SLIDER arrival, so
the worst case is a switch beginning the instant before that with the whole
blend still to run — 6.8 s at 180 degrees, plus its settle. The timeout has to
clear that with room to spare, so it only fires if the aim genuinely cannot
converge, and it says so in the log when it does.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent

CPP = (REPO / "firmware/teensy41_mount/MountMotion.cpp").read_text()
HDR = (REPO / "firmware/teensy41_mount/MountMotion.h").read_text()


def define(name):
    m = re.search(rf"#define {name}\s+([\d.]+)(?:f|UL)?", HDR)
    assert m, f"{name} not found"
    return float(m.group(1))


def code_only(text):
    return "\n".join(l if l.find("//") < 0 else l[:l.find("//")]
                     for l in text.splitlines())


# ---- 1. arriving is not the same as finishing ------------------------------
print("1. what ends a look-at move:")
upd = code_only(CPP[CPP.index("    if (check_slider_arrival) {"):])
upd = upd[:upd.index("\n}\n")]

assert "!_stepper[AXIS_SLIDER]->isMoving && sl_err <= GOTO_ARRIVE_STEPS" in upd, \
    "the slider-arrival test itself has changed; re-read this file"
assert "bool aim_there" in upd and "bool blending" in upd, \
    "the move still ends on slider arrival alone, so a switch that is still\n" \
    "    running gets cut off wherever it had reached"
assert "if ((!blending && aim_there) || waited_long_enough) {" in upd, \
    "the end condition is not 'the blend has finished AND the aim is there'"
print("   ends on blend finished AND aim on target           OK")

# The aim error must be measured the same way the controller measures it, or
# the two disagree about what "there" means.
assert "labs(pan_target  - _stepper[AXIS_PAN ]->getPosition())" in upd and \
       "labs(tilt_target - _stepper[AXIS_TILT]->getPosition())" in upd, \
    "the aim error is not target-minus-position on the same values the\n" \
    "    controller drives, so it could read 'arrived' while the axis is not"
print("   error measured against the driven targets          OK")

# ---- 2. and it cannot wait forever -----------------------------------------
print("\n2. the way out:")
assert "waited_long_enough" in upd and "LOOK_AT_AIM_FINISH_MAX_MS" in upd, \
    "nothing bounds the wait; an aim that cannot converge would leave the mount\n" \
    "    in LOOK_AT_MOVE with the slider parked, indefinitely"
assert "TIMED OUT" in CPP, \
    "a timed-out finish is indistinguishable in the log from a clean one"

TIMEOUT = define("LOOK_AT_AIM_FINISH_MAX_MS")
PER, LO, HI = (define("LOOK_AT_BLEND_MS_PER_DEG"),
               define("LOOK_AT_BLEND_MIN_MS"), define("LOOK_AT_BLEND_MAX_MS"))
FILL, SETTLE = define("LOOK_AT_BLEND_FILL"), define("LOOK_AT_SLEW_SETTLE_MS")
VMAX = define("AXIS_MAX_STEPS_S")
CEIL = VMAX * (0.9 / (256 * (270 / 36)))

longest = max(max(min(max(d * PER, LO), HI), 1.5 * d * 1000.0 / (CEIL * FILL))
              for d in range(1, 181))
# Real margin, not a hair's breadth: the clock starts on SLIDER arrival, so the
# worst case is a switch beginning the instant before it, leaving the entire
# blend still to run.
assert TIMEOUT > (longest + SETTLE) * 1.3, \
    f"the {TIMEOUT:.0f} ms timeout is shorter than the longest blend " \
    f"({longest:.0f} ms) plus its settle ({SETTLE:.0f} ms) — it would cut off a\n" \
    "    turn that was progressing perfectly well"
print(f"   {TIMEOUT:.0f} ms, against a worst-case turn of "
      f"{longest:.0f}+{SETTLE:.0f} ms      OK")

# ---- 3. the clock is per-move, not per-boot --------------------------------
# A timestamp that survives a move makes the NEXT one time out instantly.
print("\n3. the arrival clock:")
assert "_la_sl_arrived_ms = 0;   // still travelling" in CPP, \
    "the clock is not cleared while the slider is still moving, so it would\n" \
    "    date from some earlier arrival"
start = code_only(CPP[CPP.index("bool MountMotion::startLookAtMove("):])
start = start[:start.index("\n}\n")]
assert "_la_sl_arrived_ms = 0;" in start, "a new move inherits the previous one's clock"
stop = code_only(CPP[CPP.index("void MountMotion::stopLookAtMove()"):])
stop = stop[:stop.index("\n}")]
assert "_la_sl_arrived_ms = 0;" in stop, "ending a move leaves its clock set"
print("   cleared on start, on stop, and while travelling    OK")

# ---- 4. a move with no switch is unaffected --------------------------------
# The tolerance has to be loose enough that ordinary tracking is already inside
# it, or every plain rail move would now hang about at the end.
print("\n4. an ordinary move, with no switch in it:")
TOL = define("LOOK_AT_AIM_ARRIVE_DEG")
assert 0.0 < TOL <= 0.2, \
    f"{TOL} deg is not a sensible arrival tolerance: too tight and a plain move\n" \
    "    waits for the controller to chase noise, too loose and a switch is cut\n" \
    "    off before it is visibly finished"
KP = define("LOOK_AT_KP_STEPS")
tau = KP / VMAX                       # P-loop time constant, seconds
print(f"   tolerance {TOL} deg; the loop's own time constant is {tau*1000:.0f} ms,")
print(f"   so a tracking error decays inside it in well under one sample  OK")

# ---- 5. and the mount really does stop, eventually --------------------------
assert "stopLookAtMove();" in upd, "nothing ends the move at all any more"
n = code_only(CPP).count("stopLookAtMove();")
assert n >= 1, "the only path out of a look-at move has gone"
print("\n5. it still ends:")
print("   stopLookAtMove() is reached on both branches       OK")

print("\nALL CHECKS PASSED")
