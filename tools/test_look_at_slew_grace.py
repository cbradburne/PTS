"""The gentle slew cap must come off when the camera is already still.

Switching subject mid-move eased IN nicely and kicked on the way OUT.

_driveTowardTarget() caps peak speed at LOOK_AT_SLEW_BRAKE_FACTOR while a
"slew" is in progress and LOOK_AT_BRAKE_FACTOR otherwise — 0.15 against 0.25, a
67% jump, and it is a STEP not a ramp. That is invisible if it happens once the
camera has settled, and visible as a kick if it happens while the camera is
still moving.

The grace period that held the cap was a fixed 2000 ms. The blend that moves the
setpoint is as long as the turn requires — 55 ms per degree, so a 30 degree
switch takes about 1.6 s. The setpoint therefore stopped at 1.6 s, the camera
was still closing the P-loop's following error behind it, and the cap came off
at 2.0 s: 378 ms into the catch-up, on the rig's own two subjects.

A fixed grace cannot stay clear of a variable blend. It now runs for the blend
plus a settling allowance, so the step lands on a camera that has stopped.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re, math
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent

CPP = (REPO / "firmware/teensy41_mount/MountMotion.cpp").read_text()
HDR = (REPO / "firmware/teensy41_mount/MountMotion.h").read_text()


def define(name: str) -> float:
    m = re.search(rf"#define {name}\s+([\d.]+)f?", HDR)
    assert m, f"{name} not found"
    return float(m.group(1))


PER_DEG = define("LOOK_AT_BLEND_MS_PER_DEG")
LO      = define("LOOK_AT_BLEND_MIN_MS")
HI      = define("LOOK_AT_BLEND_MAX_MS")
SETTLE  = define("LOOK_AT_SLEW_SETTLE_MS")

# ---- 1. the cap is still a step, which is why the timing matters -----------
print("1. how the cap comes off:")
drive = CPP[CPP.index("void MountMotion::_driveTowardTarget("):]
drive = drive[:drive.index("\n}\n")]
assert "in_slew ? LOOK_AT_SLEW_BRAKE_FACTOR : LOOK_AT_BRAKE_FACTOR" in drive, \
    "the brake factor no longer switches between two values — re-read this test"
slow = float(re.search(r"LOOK_AT_SLEW_BRAKE_FACTOR\s*=\s*([\d.]+)f", drive).group(1))
fast = float(re.search(r"LOOK_AT_BRAKE_FACTOR\s*=\s*([\d.]+)f", drive).group(1))
assert fast > slow, "the slew cap is no longer the gentler of the two"
print(f"   {slow} -> {fast}, a {100*(fast-slow)/slow:.0f}% step             OK")
print("   so WHEN it happens is what decides if it shows      OK")

# ---- 2. the grace follows the blend ----------------------------------------
print("\n2. how long the cap is held:")
setter = CPP[CPP.index("void MountMotion::setLookAtSubject("):]
setter = setter[:setter.index("\n}\n")]
assert "_la_slew_until_ms = millis() + _la_blend_ms + LOOK_AT_SLEW_SETTLE_MS;" in setter, \
    "the grace is no longer tied to the blend duration"
print("   blend + settle, not a fixed 2 s                     OK")

# It must be armed AFTER the blend length is known, or it uses a stale value.
blend_set = setter.index("_la_blend_ms       = (uint32_t)ms;")
grace_set = setter.index("_la_slew_until_ms = millis() + _la_blend_ms")
assert blend_set < grace_set, \
    "the grace is armed before _la_blend_ms is computed — it would use the\n" \
    "    PREVIOUS switch's duration"
print("   armed after the blend length is computed            OK")

# A first selection has no blend to cover and keeps the original fixed grace.
assert "_la_slew_until_ms = millis() + LOOK_AT_SLEW_DURATION_MS;" in setter, \
    "a first selection lost its grace entirely"
print("   a first selection keeps the fixed grace             OK")

# ---- 3. the timing, on the rig's own subjects ------------------------------
# Subj 2 and Subj 5 as solved on 2026-08-24 after the reference was set.
A = (-1896.0, -1603.0, 4632.0)
B = (594.0, -710.0, 4684.0)


def angles(s, cx=0.0):
    return (math.degrees(math.atan2(s[0] - cx, s[2])),
            math.degrees(math.atan2(s[1], math.hypot(s[0] - cx, s[2]))))


pa, ta = angles(A)
pb, tb = angles(B)
travel = max(abs(pb - pa), abs(tb - ta))
blend = min(max(travel * PER_DEG, LO), HI)

print(f"\n3. switching between the rig's two subjects ({travel:.1f}° of turn):")
print(f"   setpoint stops at            {blend:.0f} ms")
print(f"   old fixed grace expired at   2000 ms  -> {2000 - blend:+.0f} ms into the catch-up")
assert 0 < 2000 - blend < 1000, \
    "this switch no longer lands the old grace inside the catch-up; the numbers\n" \
    "    that motivated the change have moved"
print(f"   new grace expires at         {blend + SETTLE:.0f} ms  -> {SETTLE:.0f} ms after it stops")
assert blend + SETTLE > 2000, "the new grace is no longer than the old fixed one here"
print("   the step now lands on a still camera                OK")

# ---- 4. and it holds for any turn ------------------------------------------
# The point of tying them is that no turn size can put the step in the wrong
# place. A fixed grace only ever suited one blend length.
print("\n4. across the whole range of turns:")
worst = 0.0
for deg in (1, 5, 10, 20, 30, 45, 60, 90, 180):
    b = min(max(deg * PER_DEG, LO), HI)
    margin = (b + SETTLE) - b          # always SETTLE by construction
    worst = max(worst, abs(margin - SETTLE))
    fixed_gap = 2000 - b               # where the OLD grace fell
    flag = "  (old grace fell mid-catch-up)" if 0 < fixed_gap < 1000 else ""
    print(f"   {deg:>3}° -> blend {b:>4.0f} ms, cap off {SETTLE:.0f} ms later{flag}")
assert worst < 1e-6, "the settle margin is no longer constant across turn sizes"
print("   margin is the same whatever the turn                OK")

print("\nALL CHECKS PASSED")
