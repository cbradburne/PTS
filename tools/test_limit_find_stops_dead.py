"""Finding a limit stops the axis dead; it does not decelerate into the stop.

A stall during a limit find IS the end of travel. There is nowhere left to
decelerate into, so every step of a ramp after the stall is a step driven into
the end stop — and on the rig you can hear it.

Both legs called stopAsync(), which ramps down at LIMIT_FIND_ACCEL. This file
works out how far that actually carries the carriage, and fails if the stall
path can ramp at all.

What it does NOT do is move the recorded limit, and this file said otherwise
when it was written. getPosition() and setPosition(0) are on the line after the
stop call, so the old stopAsync() recorded the stall point too — the ramp came
after the number was taken. Both ends land where the stall was, before and
after. Taking the reading at a standstill is still the right way round, it is
just not worth any millimetres of LIMIT_SAFETY_MARGIN: that margin covers the
stall being NOTICED late, which this change does not touch.

A TIMEOUT is the opposite case and must still ramp: nothing was hit, the axis
is out in the middle of the rail, and halting it dead there would be worse than
the ramp. The two share a branch, so both are checked.

Run directly, or via tools/run_tests.sh with the rest.
"""
import pathlib, re

REPO = pathlib.Path(__file__).resolve().parent.parent
CPP  = (REPO / "firmware/teensy41_mount/MountMotion.cpp").read_text()
HDR  = (REPO / "firmware/teensy41_mount/MountMotion.h").read_text()
LIB  = (REPO / "libraries/TeensyStep4/src/stepperbase.cpp").read_text()


def resolve(*sources):
    """Every numeric #define and constexpr in these files, evaluated.

    Resolved rather than copied, so the millimetres below follow a pulley or
    microstep change instead of quietly describing the old hardware — which is
    exactly why the end margin is written in mm and converted in the
    firmware rather than pasted in as steps.
    """
    raw = {}
    for src in sources:
        for name, expr in re.findall(r"#define\s+([A-Z_][A-Z0-9_]*)\s+([^\n/]+)", src):
            raw.setdefault(name, expr.strip())
        for name, expr in re.findall(
                r"(?:static\s+)?constexpr\s+\w+\s+([A-Za-z_]\w*)\s*=\s*([^;]+);", src):
            raw.setdefault(name, expr.strip())
    out = {}
    for _ in range(len(raw) + 1):          # iterate until the graph settles
        for name, expr in raw.items():
            if name in out:
                continue
            try:
                out[name] = eval(re.sub(r"\bf\b|[fF](?=[^\w.]|$)", "", expr), {}, out)
            except Exception:
                pass
    return out


def const(consts, name):
    assert name in consts and isinstance(consts[name], (int, float)), \
        f"{name} is gone, or no longer resolves to a number"
    return float(consts[name])


PROTO = (REPO / "firmware/shared/protocol.h").read_text()
CONSTS = resolve(PROTO, HDR, CPP)

# ---- 1. how far a ramp would carry it --------------------------------------
print("1. what decelerating at the end stop costs:")
speed = const(CONSTS, "LIMIT_FIND_SPEED")
accel = const(CONSTS, "LIMIT_FIND_ACCEL")
mm_per_step = const(CONSTS, "NOMINAL_SLIDER_MM_PER_STEP")

ramp_steps = speed * speed / (2.0 * accel)
ramp_mm    = ramp_steps * mm_per_step
print(f"   {speed:.0f} steps/s at {accel:.0f} steps/s²"
      f" -> {ramp_steps:.0f} steps = {ramp_mm:.1f} mm")

# Not a theoretical concern: state the size so a future speed or accel change
# that makes it worse is visible here rather than only audible on the rig.
assert ramp_mm > 1.0, \
    f"the ramp is only {ramp_mm:.2f} mm — if that is genuinely true this test has\n" \
    "    lost its point, but check LIMIT_FIND_SPEED and LIMIT_FIND_ACCEL first"
margin_mm = const(CONSTS, "SLIDER_END_MARGIN_MM_DEFAULT")
print(f"   against a {margin_mm:.0f} mm far-end safety margin      "
      f"({100 * ramp_mm / margin_mm:.0f}% of it)")

# ---- 2. so a stall stops dead ----------------------------------------------
print("\n2. what happens on a stall:")
lf = CPP[CPP.index("void MountMotion::_updateLimitFind()"):]
lf = lf[:lf.index("\n// ----", lf.index("switch (_lf_state)"))]

for leg in ("MOVING_TO_MIN", "MOVING_TO_MAX"):
    blk = lf[lf.index(f"case LimitFindState::{leg}:"):]
    blk = blk[:blk.index("break;")]
    assert "emergencyStop();" in blk, \
        f"the {leg} leg does not stop dead on a stall — it ramps {ramp_mm:.1f} mm\n" \
        "    into the end stop it has just found"
    assert re.search(r"if \(stalled\)\s+_stepper\[idx\]->emergencyStop\(\);", blk), \
        f"the {leg} leg's dead stop is not the one taken on a stall"
    assert re.search(r"else\s+_stepper\[idx\]->stopAsync\(\);", blk), \
        f"the {leg} leg slams to a halt on a TIMEOUT too — nothing was hit there,\n" \
        "    and the axis is somewhere out in the middle of the rail"
    print(f"   {leg:<13} dead on a stall, ramped on a timeout  OK")

# ---- 3. the reading is taken with the axis stopped -------------------------
# Not because the recorded position would otherwise be wrong — it would not, the
# read is the very next line — but because a reading taken while an axis is
# still moving depends on how long that next line takes, and setPosition() is a
# bare `pos = p` racing an ISR that is also writing it.
print("\n3. when the position is read:")
assert "void setPosition(int32_t p) { pos = p; }" in \
       (REPO / "libraries/TeensyStep4/src/Stepper.h").read_text(), \
    "setPosition is no longer a bare write — re-check whether it is ISR-safe now"
mn = lf[lf.index("case LimitFindState::MOVING_TO_MIN:"):]
mn = mn[:mn.index("break;")]
assert mn.index("emergencyStop();") < mn.index("setPosition(0)"), \
    "home is recorded before the axis is stopped, so the ISR keeps stepping past it"
mx = lf[lf.index("case LimitFindState::MOVING_TO_MAX:"):]
mx = mx[:mx.index("break;")]
assert mx.index("emergencyStop();") < mx.index("getPosition()"), \
    "the far limit is read while the carriage is still moving"
print("   min zeroed and max read at a standstill           OK")
print("   (this changes no millimetres — see the docstring)  OK")

# ---- 4. _checkStall is still only asked once ------------------------------
# It CLEARS _stall_isr_fired, so calling it on the timeout path would eat a
# stall flag that the next leg is entitled to see.
print("\n4. the stall test's side effect:")
assert "bool stalled = !timed_out && stall_settled && _checkStall(ax);" in lf, \
    "_checkStall() is no longer short-circuited — it clears _stall_isr_fired, so\n" \
    "    calling it on a timeout throws away a flag the next leg needs"
chk = CPP[CPP.index("bool MountMotion::_checkStall("):]
chk = chk[:chk.index("\n}")]
assert "_stall_isr_fired = false;" in chk, \
    "_checkStall no longer clears the flag — if that is deliberate the guard above\n" \
    "    can be relaxed, but check it is not now latching a stale stall instead"
print("   called once per tick, only when it can be true    OK")

# ---- 5. and stopping one axis dead does not stop the others ----------------
# This is what makes it legal to call on a single axis mid-run: emergencyStop()
# returns THIS stepper's timer to the pool. A shared or global timer would take
# the whole rig down with it.
print("\n5. emergencyStop() is per-axis:")
es = LIB[LIB.index("void StepperBase::emergencyStop()"):]
es = es[:es.index("\n    }")]
assert "stpTimer" in es and "returnTimer(stpTimer)" in es, \
    "emergencyStop no longer returns this stepper's own timer"
assert "stpTimer = nullptr;" in es, \
    "the timer pointer is not cleared, so the next move cannot allocate one"
assert not re.search(r"for\s*\(", es), \
    "emergencyStop loops over something — if it now touches every axis, calling it\n" \
    "    for one limit find would halt the other three mid-move"
print("   stops this stepper's timer and no one else's      OK")

print("\nALL CHECKS PASSED")
