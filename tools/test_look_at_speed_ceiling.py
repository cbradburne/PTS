"""The look-at controller must be told a speed the axis can actually reach.

Measured on the rig on 2026-08-26. Three subject switches of 72, 92 and 81
degrees, whose computed speed caps were 64, 81 and 71 deg/s. All three
plateaued at 45, then stopped inside a single probe sample.

The cause is one line in TeensyStep4 (stepper.h):

    vMax = constrain(speed, -vMaxMax, vMaxMax);      // vMaxMax = 100'000

It clamps silently. It does not fail and it does not report. The look-at
controller was passing 90 deg/s, which is 192,000 steps/s — a rate the library
will never issue — so every number derived from it was computed against a speed
that cannot happen:

  the speed caps all sat ABOVE the real ceiling, so none of them ever bound;
  three different caps produced one identical plateau, which is the tell

  the sqrt braking curve took its deceleration from the same fiction, so the
  camera held the clamp until it was under a degree from target and then
  stopped in 15-19 ms — less than one 20 ms control tick

That last number is the whole complaint. "One speed, then the next, with no
transition out" is what a sub-tick stop looks like, and no amount of tuning the
caps could ever have reached it, because the caps were not what ended the move.
Arriving was.

The second half is that a smoothstep peaks at 1.5x its average. Asking for a
peak above the ceiling does not slow the curve down, it CLIPS it — the camera
saturates and the shape collapses back into the rectangle the blend exists to
remove. LOOK_AT_BLEND_MAX_MS was forcing exactly that for turns over about 58
degrees. So the ceiling now sets a FLOOR on the blend's duration: a big turn
takes longer rather than going faster.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent

HDR = (REPO / "firmware/teensy41_mount/MountMotion.h").read_text()
CPP = (REPO / "firmware/teensy41_mount/MountMotion.cpp").read_text()
INO = (REPO / "firmware/teensy41_mount/teensy41_mount.ino").read_text()


def define(name, text=HDR):
    m = re.search(rf"#define {name}\s+([\d.]+)f?", text)
    assert m, f"{name} not found"
    return float(m.group(1))


# ---- 1. the clamp is real, and is what the header claims -------------------
print("1. the library's ceiling:")
VMAXMAX = define("AXIS_MAX_STEPS_S")
# libraries/, not the installed Arduino copy: build.sh compiles against the
# vendored one, so that is the only copy whose value reaches the rig.
lib = REPO / "libraries/TeensyStep4/src/stepper.h"
if lib.exists():
    src = lib.read_text()
    m = re.search(r"vMaxMax\s*=\s*([\d']+)", src)
    assert m, "vMaxMax is gone from TeensyStep4 — re-read this test"
    actual = float(m.group(1).replace("'", ""))
    assert actual == VMAXMAX, \
        f"the library clamps at {actual:,.0f} steps/s, the header says {VMAXMAX:,.0f}"
    cpp = (lib.parent / "stepper.cpp").read_text()
    assert "constrain(speed, -vMaxMax, vMaxMax)" in cpp, \
        "setMaxSpeed no longer clamps — the premise of this whole file changed"
    print(f"   TeensyStep4 clamps at {actual:,.0f} steps/s, silently    OK")
else:
    raise AssertionError("libraries/TeensyStep4 is missing — the build would fail too")

# ---- 2. the controller is told that, not 90 --------------------------------
print("\n2. what the controller is told:")
assert "AXIS_MAX_STEPS_S * NOMINAL_PAN_DEG_PER_STEP" in HDR, \
    "LOOK_AT_MAX_DEG_S is no longer derived from the clamp and the drive\n" \
    "    geometry — a hand-typed number here goes stale the moment either moves"
DPS = 0.9 / (256 * (270 / 36))
CEILING = VMAXMAX * DPS
print(f"   {VMAXMAX:,.0f} steps/s x {DPS:.8f} deg/step = {CEILING:.3f} deg/s   OK")

call = INO[INO.index("mount.startLookAtMove("):]
call = call[:call.index(";") + 1]
assert "90.0f" not in call, \
    "startLookAtMove is passed 90 deg/s again — the axis cannot do it, and\n" \
    "    every cap and braking distance derived from it becomes a fiction"
assert "LOOK_AT_MAX_DEG_S" in call, "the real ceiling is not passed"
print("   startLookAtMove gets the real ceiling, not 90            OK")

# ---- 3. the blend fits underneath it ---------------------------------------
print("\n3. the blend's duration:")
setter = CPP[CPP.index("void MountMotion::setLookAtSubject("):]
setter = setter[:setter.index("\n}\n")]
assert "LOOK_AT_BLEND_FILL" in setter, \
    "the blend no longer sizes itself against the ceiling; a big turn will be\n" \
    "    clipped back into a rectangle"
fit = setter.index("fit_ms")
clamp = setter.index("LOOK_AT_BLEND_MAX_MS")
assert clamp < fit, \
    "the fit runs BEFORE the MAX_MS clamp, so the clamp overrides it again and\n" \
    "    nothing changes for exactly the turns that need it"
print("   the fit is applied after the MAX_MS clamp                OK")

FILL = define("LOOK_AT_BLEND_FILL")
assert 0.0 < FILL < 1.0, "the fill fraction leaves no headroom under the ceiling"
PER = define("LOOK_AT_BLEND_MS_PER_DEG")
LO, HI = define("LOOK_AT_BLEND_MIN_MS"), define("LOOK_AT_BLEND_MAX_MS")


def blend_ms(deg):
    ms = min(max(deg * PER, LO), HI)
    return max(ms, 1.5 * deg * 1000.0 / (CEILING * FILL))


# ---- 4. against the rig's own three switches -------------------------------
print("\n4. the switches that were measured:")
for deg in (72.3, 91.7, 80.6):
    was = min(max(deg * PER, LO), HI)
    now = blend_ms(deg)
    peak_was, peak_now = 1.5 * deg / (was / 1000), 1.5 * deg / (now / 1000)
    assert peak_was > CEILING, \
        f"{deg}° no longer over-asks; the measurement this is built on has moved"
    assert peak_now <= CEILING * FILL + 0.01, \
        f"{deg}° still asks {peak_now:.1f} deg/s of a {CEILING:.1f} axis"
    print(f"   {deg:5.1f}° {was:.0f}->{now:.0f} ms, "
          f"peak {peak_was:.1f}->{peak_now:.1f} deg/s (ceiling {CEILING:.1f})")
print("   every one now fits under the ceiling                     OK")

# ---- 5. and small turns are left alone -------------------------------------
# The turns that already fit were not the complaint and must not change feel.
print("\n5. turns that already fitted:")
for deg in (5, 10, 25, 40, 55):
    was = min(max(deg * PER, LO), HI)
    assert abs(blend_ms(deg) - was) < 1.0, \
        f"{deg}° changed duration; it was already under the ceiling"
print("   5-55° unchanged                                          OK")

# The crossover should be where the old ceiling started over-asking, not an
# arbitrary place — otherwise some band of turns is still being clipped.
cross = next(d for d in range(5, 180) if blend_ms(d) > min(max(d*PER, LO), HI) + 1)
assert 1.5 * cross / (HI / 1000) > CEILING * FILL, \
    "turns just below the crossover still ask for more than the axis can give"
print(f"   longer only above {cross}°, which is where clipping began  OK")

print("\nALL CHECKS PASSED")
