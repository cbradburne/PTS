"""The blend sets its own speed cap, or it is not a blend.

Measured on the rig on 2026-08-24, a 25.6 degree subject switch:

    -0.2 | +8.7 +12.6 +12.8 +12.7 +14.2 +11.9 +13.9 +13.0 +12.6 +11.9 | +0.4

Snap to speed, two seconds of flat speed, snap to stop. A rectangle. The
setpoint was easing on a smoothstep the entire time and the camera never once
followed it.

The plateau is the answer: 12.4 deg/s against LOOK_AT_SLEW_BRAKE_FACTOR x
max_pt_deg_s = 0.15 x 90 = 13.5. The camera was pinned against the speed cap for
the whole move. A smoothstep across 25.6 degrees in 1408 ms needs a PEAK of 1.5
x average = 27.3 deg/s, and it was allowed 13.5 — less than half. So it
saturated immediately, ran flat out until it caught up, and stopped dead.

Two mechanisms were solving the same problem and the cap was winning. The cap
exists to stop a STEP input lurching; an eased setpoint has no step in it, so
while a blend runs the blend governs and the fixed cap does not apply.

This is also why the two previous attempts changed nothing. The blend shaped a
setpoint the camera could not follow, and the slew-cap TIMING work moved when
the cap came off when the problem was its VALUE.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent

CPP = (REPO / "firmware/teensy41_mount/MountMotion.cpp").read_text()
HDR = (REPO / "firmware/teensy41_mount/MountMotion.h").read_text()


def define(name):
    m = re.search(rf"#define {name}\s+([\d.]+)f?", HDR)
    assert m, f"{name} not found"
    return float(m.group(1))


PER_DEG = define("LOOK_AT_BLEND_MS_PER_DEG")
LO, HI  = define("LOOK_AT_BLEND_MIN_MS"), define("LOOK_AT_BLEND_MAX_MS")
HEAD    = define("LOOK_AT_BLEND_HEADROOM")
MAX_DPS = 90.0          # max_pt_deg_s, as startLookAtMove passes it

drive = CPP[CPP.index("void MountMotion::_driveTowardTarget("):]
drive = drive[:drive.index("\n}\n")]
SLOW = float(re.search(r"LOOK_AT_SLEW_BRAKE_FACTOR\s*=\s*([\d.]+)f", drive).group(1))

# ---- 1. the blend overrides the fixed cap ----------------------------------
print("1. which cap applies:")
assert "if (_laBlendActive() && _la_blend_brake > 0.0f) {" in drive, \
    "the fixed slew cap still governs during a blend — the curve gets flattened"
assert "effective_brake = _la_blend_brake;" in drive, "the blend's cap is not used"
assert "in_slew ? LOOK_AT_SLEW_BRAKE_FACTOR : LOOK_AT_BRAKE_FACTOR" in drive, \
    "the fixed caps are gone entirely — a step input has nothing limiting it"
print("   blend running -> the blend's cap                   OK")
print("   otherwise     -> the fixed caps, as before         OK")

# ---- 2. it is derived from the curve, with headroom ------------------------
print("\n2. where the blend's cap comes from:")
setter = CPP[CPP.index("void MountMotion::setLookAtSubject("):]
setter = setter[:setter.index("\n}\n")]
assert "float peak_dps = 1.5f * travel / (ms / 1000.0f);" in setter, \
    "the peak is not 1.5x the average — that is what a smoothstep peaks at"
assert "peak_dps * LOOK_AT_BLEND_HEADROOM" in setter, \
    "no headroom above the peak; pinned exactly at it the controller can only\n" \
    "    fall further behind, never catch up"
assert "constrain(" in setter and "0.0f, 1.0f)" in setter, \
    "the fraction is not clamped to a sane range"
assert HEAD > 1.0, "the headroom does not actually allow exceeding the peak"
print(f"   1.5 x average, x{HEAD} headroom, clamped 0..1        OK")

# ---- 3. the numbers, against the measurement --------------------------------
print("\n3. the rig's own switch:")
deg = 25.6
ms   = min(max(deg * PER_DEG, LO), HI)
peak = 1.5 * deg / (ms / 1000.0)
old  = SLOW * MAX_DPS
new  = min((peak * HEAD) / MAX_DPS, 1.0) * MAX_DPS
print(f"   {deg}° over {ms:.0f} ms wants a peak of {peak:.1f} deg/s")
print(f"   old cap {old:.1f} deg/s  -> measured plateau 12.4, pinned")
print(f"   new cap {new:.1f} deg/s  -> the curve fits underneath")
assert old < peak, "the old cap no longer clamps the curve; the fault has moved"
assert new > peak, "the new cap still clamps the curve — it would stay a rectangle"
assert abs(old - 13.5) < 0.1, "the old cap is not the 13.5 that was measured"
print("   the measured plateau matches the old cap           OK")

# ---- 4. and it holds across the range --------------------------------------
print("\n4. across the range of turns:")
for d in (5, 10, 25.6, 40, 60, 90):
    m = min(max(d * PER_DEG, LO), HI)
    p = 1.5 * d / (m / 1000.0)
    c = min((p * HEAD) / MAX_DPS, 1.0) * MAX_DPS
    assert c >= p, f"{d}°: cap {c:.1f} below peak {p:.1f}"
    print(f"   {d:>5}° -> blend {m:>4.0f} ms, peak {p:>5.1f} deg/s, cap {c:>5.1f}")
print("   the cap is never below the curve                   OK")

# Beyond the duration ceiling a bigger turn gets FASTER, not longer. That is a
# real consequence of LOOK_AT_BLEND_MAX_MS and worth knowing before someone
# wonders why a large switch feels brisk.
big = 1.5 * 90 / (HI / 1000.0)
assert big > 1.5 * 40 / (HI / 1000.0), "large turns no longer speed up"
print(f"   note: past {HI/PER_DEG:.0f}° the blend is capped at {HI:.0f} ms, so")
print(f"         bigger turns get faster — 90° peaks at {big:.0f} deg/s")

# ---- 5. it is cleared with the blend ---------------------------------------
print("\n5. when no blend is running:")
assert "_la_blend_brake = 0.0f;      // and the fixed caps govern again" in setter, \
    "a first selection leaves the previous switch's cap in place"
assert "_la_blend_brake = 0.0f; }" in HDR, \
    "dropping the subject leaves the cap behind"
print("   cleared on a first selection and on deselect       OK")

print("\nALL CHECKS PASSED")
