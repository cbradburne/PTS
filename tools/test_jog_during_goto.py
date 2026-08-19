"""A jog must never orphan a goto axis mid-flight.

On the rig on 2026-08-19: pan was nudged 10 degrees with Move, the zoom control
was held before the nudge finished, and pan ran 59 degrees and was still turning
4.5 seconds later. It stopped only because the next MOVE_REL happened to re-plan
it. Releasing the zoom did nothing.

The mechanism is structural, not arithmetic:

  moveTo() starts each axis with an UNBOUNDED rotateAsync(). Nothing in the
  motor stops at the target — _updateGoto()'s P-loop watches the error and calls
  stopAsync() on arrival. That is the only code that ever parks a goto axis.

  update() calls _updateGoto() only in STATE_MOVING_TO_POS.

  jog() set _state = STATE_JOGGING for any jog at all, including a zoom-only
  one. From that moment _updateGoto() was never called again, so pan kept the
  rotation it was given and no watchdog covered it: both jog paths only stop
  axes with _jog_dir[] set, and a goto axis has _goto_dir[] instead.

jog() already had exactly this guard for STATE_LOOK_AT_MOVE — with a comment
explaining this failure — but the goto path never got it. That comment even
lists GOTO among the "self-terminating" motions, which is true only while
_updateGoto() is still being called.

The second half is ownership. Releasing an axis to the jog cannot be done by
clearing _goto_dir: _updateGoto()'s restart branch relaunches any axis whose
_goto_dir is 0 while the target error is large, which is what a jog creates. It
would drive the axis back as fast as the operator jogged it away.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent

CPP = (REPO / "firmware/teensy41_mount/MountMotion.cpp").read_text()
HDR = (REPO / "firmware/teensy41_mount/MountMotion.h").read_text()


def func(name: str) -> str:
    i = CPP.index(f"void MountMotion::{name}(")
    j = CPP.index("\n}", i)
    return CPP[i:j]


JOG, UPDATE, UPD = func("jog"), func("_updateGoto"), func("update")

# ---- 1. the structural fact that makes this a bug ---------------------------
print("1. why a stolen state strands the axis:")
assert re.search(r"if \(_state == STATE_MOVING_TO_POS\) \{\s*\n\s*_updateGoto\(\);", UPD), \
    "_updateGoto() is no longer gated on STATE_MOVING_TO_POS — re-read this test"
print("   _updateGoto() runs only in STATE_MOVING_TO_POS      OK")

MOVETO = func("moveTo")
assert "rotateAsync()" in MOVETO, "moveTo no longer starts axes with rotateAsync"
assert "stopAsync" in UPDATE, "_updateGoto no longer parks arrived axes"
print("   moveTo starts unbounded; only _updateGoto parks it  OK")

# ---- 2. a zoom-only jog must not take the state -----------------------------
print("\n2. zoom-only jog while a goto is running:")
assert "goto_zoom_only" in JOG, "the goto guard is gone — a zoom nudge will orphan the move"
m = re.search(r"bool goto_zoom_only = \(_state == STATE_MOVING_TO_POS &&\s*\n\s*"
              r"pan == 0 && tilt == 0 && slider == 0\);", JOG)
assert m, "the zoom-only condition changed shape"
print("   recognised as zoom-only                            OK")

# It must join the look-at early-return, which leaves _state alone.
assert re.search(r"if \(_state == STATE_LOOK_AT_MOVE \|\| _state == STATE_LOOK_AT_PRE_AIM \|\|\s*\n\s*"
                 r"goto_zoom_only\) \{", JOG), \
    "the goto case does not share the look-at early-return"
# and the assignment that caused this must come AFTER that return
guard_at  = JOG.index("goto_zoom_only) {")
assign_at = JOG.index("_state       = STATE_JOGGING;")
assert guard_at < assign_at, "_state = STATE_JOGGING is no longer after the guard"
print("   returns before _state = STATE_JOGGING              OK")

# ---- 3. the zoom axis is handed over, not fought -----------------------------
print("\n3. ownership of the zoom axis:")
assert "_goto_axis_released[4]" in HDR, "the released-axis flag is gone"
assert "if (goto_zoom_only && zoom != 0) _goto_axis_released[AXIS_ZOOM] = true;" in JOG, \
    "a zoom jog no longer takes the axis from the goto"
assert "if (_goto_axis_released[i]) continue;" in UPDATE, \
    "_updateGoto does not skip released axes — it will fight the jog"

# The skip has to come BEFORE the restart branch, or the restart fires first.
skip_at    = UPDATE.index("_goto_axis_released[i]")
restart_at = UPDATE.index("if (_goto_dir[i] == 0) {")
assert skip_at < restart_at, \
    "the released check sits after the restart branch — the axis is relaunched anyway"
print("   released axes skipped before the restart branch    OK")

# A released axis must not keep the move alive forever.
assert UPDATE.index("_goto_axis_released[i]") < UPDATE.index("any_active = true"), \
    "a released axis still counts as active — the goto would never complete"
print("   a released axis does not keep the goto active      OK")

# ---- 4. a jog that DOES take over must cancel, not orphan -------------------
print("\n4. jogging pan/tilt/slider during a goto:")
assert "if (_goto_dir[i] != 0 && _jog_vel[i] == 0) _stepper[i]->stopAsync();" in JOG, \
    "axes the goto left turning are not stopped when a jog takes over"
assert re.search(r"if \(_state == STATE_MOVING_TO_POS\) \{[\s\S]{0,400}?_has_goto_target = false;", JOG), \
    "the goto is not cancelled when a jog takes over"
cancel_at = JOG.index("if (_goto_dir[i] != 0 && _jog_vel[i] == 0)")
assert cancel_at < assign_at, "the cancel runs after _state changes; it must run before"
print("   orphaned axes stopped, goto cancelled, before the")
print("   state changes                                     OK")

# ---- 5. the flag must not leak into the next move ---------------------------
print("\n5. the released flag is cleared again:")
starts = CPP.count("if (_jog_dir[i] == 0) _goto_axis_released[i] = false;")
assert starts == 2, f"expected moveTo and retargetTo to re-claim axes, found {starts}"
print("   moveTo and retargetTo re-claim un-jogged axes      OK")
stops = CPP.count("for (int i = 0; i < 4; i++) _goto_axis_released[i] = false;")
assert stops == 2, f"expected stopAll and emergencyStop to clear, found {stops}"
print("   stopAll and emergencyStop clear every axis         OK")

# ---- 6. the failure, simulated ----------------------------------------------
# The essential loop: an axis under rotateAsync only stops if the P-loop gets to
# look at it. Model both policies and show what the operator saw.
print("\n6. what each policy does to the pan axis:")

STEP_HZ, SPD, TARGET = 1000.0, 53333.0, 21333.0   # from the 19:44:54 plan


def run(guard: bool, jog_from=0.2, jog_to=3.0, secs=4.5):
    """Pan's position over time. The zoom is held from jog_from to jog_to —
    starting DURING the 0.4 s move, which is the case the operator hit.
    Returns (final position in steps, whether it ever parked)."""
    pos, state, dt, t = 0.0, "MOVING_TO_POS", 1.0 / STEP_HZ, 0.0
    while t < secs:
        if jog_from <= t < jog_to:
            # A zoom-only jog packet arrives. Old behaviour took the state for
            # it; the guard leaves it alone.
            if not guard:
                state = "JOGGING"
        elif state == "JOGGING":
            state = "IDLE"                 # stick released — jog ends, and the
                                           # goto is NOT resumed by anything
        if state == "MOVING_TO_POS":
            err = TARGET - pos
            if abs(err) <= 10:
                return pos, True           # _updateGoto() parks it on arrival
            pos += SPD * dt
        else:
            pos += SPD * dt                # nobody is steering; it just turns
        t += dt
    return pos, False


for guard, label in ((False, "before"), (True, "after ")):
    pos, parked = run(guard)
    deg = pos * 0.00046875
    print(f"   {label}: {'parked at target' if parked else 'STILL TURNING'}"
          f" — {pos:8.0f} steps ({deg:5.1f} deg), target {TARGET*0.00046875:.1f} deg")

pos_before, parked_before = run(False)
pos_after,  parked_after  = run(True)
assert not parked_before, "the simulation no longer reproduces the fault"
assert parked_after, "the guard does not let the goto finish"
assert pos_before > TARGET * 2, "the runaway should be far past the target"
print("   the guard is what stops it                        OK")

print("\nALL CHECKS PASSED")
