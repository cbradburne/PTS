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
MAX_DPS = 100000.0 * (0.9 / (256 * (270 / 36)))   # what the axis can reach:
# AXIS_MAX_STEPS_S x deg/step = 46.875.  This was 90 -- the value the .ino
# used to pass -- and 90 is a speed TeensyStep4 clamps away silently, so every
# number below was being checked against a fiction.  See
# test_look_at_speed_ceiling.py.

drive = CPP[CPP.index("void MountMotion::_driveTowardTarget("):]
drive = drive[:drive.index("\n}\n")]
SLOW = float(re.search(r"LOOK_AT_SLEW_BRAKE_FACTOR\s*=\s*([\d.]+)f", drive).group(1))

# ---- 1. the blend overrides the fixed cap ----------------------------------
print("1. which cap applies:")
assert "if (_la_blend_ms != 0 && _la_blend_brake > 0.0f) {" in drive, \
    "the fixed slew cap still governs during a blend — the curve gets flattened"
assert "effective_brake = _la_blend_brake;                  // still blending" in drive, \
    "the blend's cap is not used while the blend runs"
assert "in_slew ? LOOK_AT_SLEW_BRAKE_FACTOR : LOOK_AT_BRAKE_FACTOR" in drive, \
    "the fixed caps are gone entirely — a step input has nothing limiting it"
print("   blend running -> the blend's cap                   OK")
print("   no blend      -> the fixed caps, as before         OK")

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
assert abs(old - 13.5) < 0.2, \
    "the slew cap is no longer the 13.5 deg/s the 2026-08-24 plateau matched;\n"\
    "    the brake FRACTIONS must move whenever max_pt_deg_s does, or the\n"\
    "    absolute speed they have always meant changes underneath them"
print("   the measured plateau matches the old cap           OK")

# ---- 4. and it holds across the range --------------------------------------
print("\n4. across the range of turns:")
FILL = define("LOOK_AT_BLEND_FILL")


def blend_ms(d):
    """The duration the firmware actually picks, including the fit under the
    ceiling. Without the fit this loop checks a rule the code stopped using."""
    return max(min(max(d * PER_DEG, LO), HI),
               1.5 * d * 1000.0 / (MAX_DPS * FILL))


for d in (5, 10, 25.6, 40, 60, 90, 120):
    m = blend_ms(d)
    p = 1.5 * d / (m / 1000)
    c = min((p * HEAD) / MAX_DPS, 1.0) * MAX_DPS
    assert c >= p, f"{d}\u00b0: cap {c:.1f} below peak {p:.1f}"
    assert p <= MAX_DPS, \
        f"{d}\u00b0 asks {p:.1f} deg/s of an axis that tops out at {MAX_DPS:.1f};\n" \
        "    the curve is clipped, not slowed, and comes back as a rectangle"
    print(f"   {d:>5}\u00b0 -> blend {m:>4.0f} ms, peak {p:>5.1f} deg/s, cap {c:>5.1f}")
print("   the cap is never below the curve                   OK")
print("   and no turn asks for more than the axis can give   OK")

# Past the MAX_MS ceiling a bigger turn now takes LONGER. It used to get
# faster, which is precisely how the rectangle came back for large switches:
# the duration was capped, so the curve's peak rose until it was above what
# the axis could deliver and got clipped flat.
big, mid = blend_ms(90), blend_ms(40)
assert big > mid, "large turns no longer take longer"
assert 1.5 * 90 / (big / 1000) <= MAX_DPS, "a 90\u00b0 turn still over-asks"
print(f"   note: past {HI * MAX_DPS * FILL / 1500:.0f}\u00b0 the blend grows with the turn,")
print(f"         so the peak stays at {1.5 * 90 / (big / 1000):.0f} deg/s instead of climbing")

# ---- 4b. and it hands back gradually, not on a cliff -----------------------
# Capping the blend correctly is only half of it. Reverting to the fixed cap the
# instant the blend ends was a 62% drop in one control tick, at exactly the
# moment the setpoint stopped and while the camera was still closing its
# following error — the same step, moved to the other end of the move.
print("\n4b. the handover at the end of the blend:")
drive_all = CPP[CPP.index("void MountMotion::_driveTowardTarget("):]
drive_all = drive_all[:drive_all.index("\n}\n")]
assert "u * u * (3.0f - 2.0f * u)" in drive_all, \
    "the cap no longer ramps on a smoothstep; it steps back to the fixed value"
assert "LOOK_AT_SLEW_SETTLE_MS" in drive_all, \
    "the handover is not spread over the settle window"
assert "_laBlendActive()" not in drive_all, \
    "the cap is gated on the blend WINDOW again — that reverts it on the instant\n" \
    "    the blend ends, which is the cliff"
assert "(int32_t)(now_ms - blend_end) < 0" in drive_all, \
    "the blend/settle boundary is not compared in a wrap-safe way"
print("   smoothstepped across the settle window              OK")

# Worst single-tick change in the ALLOWED speed, at the 50 Hz control rate.
TICK = 20
def cap_at(ms, blend, fixed, settle):
    if ms < 0:      return blend
    if ms >= settle: return fixed
    u = ms / settle
    return blend + (fixed - blend) * (u * u * (3 - 2 * u))

SETTLE = define("LOOK_AT_SLEW_SETTLE_MS")
blend_frac = min((1.5 * 25.6 / (min(max(25.6*PER_DEG, LO), HI)/1000.0) * HEAD) / MAX_DPS, 1.0)
step_before = abs(blend_frac - SLOW) * MAX_DPS
step_after  = max(abs(cap_at(t + TICK, blend_frac, SLOW, SETTLE)
                      - cap_at(t, blend_frac, SLOW, SETTLE)) * MAX_DPS
                  for t in range(0, int(SETTLE), TICK))
print(f"   one-tick change: {step_before:.1f} deg/s -> {step_after:.2f} deg/s")
assert step_after < 2.0, \
    f"the cap still moves {step_after:.1f} deg/s in a single tick"
assert step_after < step_before / 10, "the handover is barely gentler than the cliff"
print("   no cliff left in the allowed speed                  OK")

# It must work in BOTH directions: a very small turn blends slowly enough that
# its cap sits BELOW the fixed one, and the ramp then goes up.
tiny = min((1.5 * 1.0 / (LO/1000.0) * HEAD) / MAX_DPS, 1.0)
assert tiny < SLOW, "a 1 degree turn no longer caps below the fixed slew value"
up = [cap_at(t, tiny, SLOW, SETTLE) for t in (0, int(SETTLE//2), int(SETTLE))]
assert up[0] < up[1] < up[2], "the ramp does not rise when the blend cap is lower"
print("   ramps up as readily as down                         OK")

# ---- 5. it is cleared with the blend ---------------------------------------
print("\n5. when no blend is running:")
assert "_la_blend_brake = 0.0f;      // and the fixed caps govern again" in setter, \
    "a first selection leaves the previous switch's cap in place"
assert "_la_blend_brake = 0.0f; }" in HDR, \
    "dropping the subject leaves the cap behind"
print("   cleared on a first selection and on deselect       OK")

print("\nALL CHECKS PASSED")
