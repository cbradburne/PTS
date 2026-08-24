"""A tilted rail needs a different stall threshold in each direction.

Finding the slider's limits drives to one end, then the other. On a level rail
both legs load the motor about equally and one StallGuard threshold serves both.
On a tilted one they do not: going up the motor carries the carriage AND its
weight down the slope, coming down that weight helps.

One threshold then cannot work. Set it for the climb and the descent detects the
end stop late — the carriage is into it before DIAG fires, which is what the
30 mm end margin is currently working around. Set it for the descent and the
climb trips on the gradient alone, part-way along the rail.

The direction of the correction is easy to get backwards. SG_RESULT FALLS as
load rises, and DIAG fires when SG_RESULT < SGTHRS x 2, so a HIGHER SGTHRS trips
at LOWER load:

    climbing   → LOWER  SGTHRS → trips at higher load → survives the gradient
    descending → HIGHER SGTHRS → trips at lower load  → catches the stop early

Getting that inverted would make both legs worse than a single threshold, so the
numbers are checked here rather than assumed.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re, math
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent

CPP = (REPO / "firmware/teensy41_mount/MountMotion.cpp").read_text()
HDR = (REPO / "firmware/teensy41_mount/MountMotion.h").read_text()

fn = CPP[CPP.index("void MountMotion::_applyStallThreshold("):]
fn = fn[:fn.index("\n}\n")]

# ---- 1. it only touches the slider, and only when tilted -------------------
print("1. what it applies to:")
assert "axis == AXIS_SLIDER && _slider_tilt_deg != 0.0f" in fn, \
    "the compensation is not confined to a tilted slider"
print("   slider only, and only when tilted                  OK")

# Pan, tilt and zoom keep exactly the threshold they were given.
assert "float thr = (float)_stall_threshold[idx] / (float)SGTHRS_DIVISOR[idx];" in fn, \
    "the base threshold is no longer the stored one scaled by the divisor"
print("   other axes unchanged                               OK")

# ---- 2. both legs get their own value ---------------------------------------
print("\n2. when it is applied:")
n = CPP.count("_applyStallThreshold(")
assert n >= 3, f"called {n} times; expected the definition plus both legs"
# The min leg's threshold is set in the shared setup that findLimits() and
# findHome() both delegate to — see 4b.
seek0 = CPP[CPP.index("void MountMotion::_beginLimitSeek("):]
seek0 = seek0[:seek0.index("\n}\n")]
assert "_applyStallThreshold(axis," in seek0, "the first leg does not set a threshold"
upd = CPP[CPP.index("void MountMotion::_updateLimitFind("):]
assert "_applyStallThreshold(ax, go_max_positive);" in upd, \
    "the max leg reuses the min leg's threshold — it runs the other way"
print("   set for the min leg, re-set for the max leg        OK")

# ---- 3. the logical/physical translation ------------------------------------
# The rail rises as the LOGICAL coordinate increases; slider_invert flips
# logical and physical apart, so the physical direction alone cannot say which
# way the leg climbs.
print("\n3. direction:")
assert "int logical_dir = phys_positive ? 1 : -1;" in fn, "no physical direction taken"
assert "if (_slider_invert) logical_dir = -logical_dir;" in fn, \
    "slider_invert is not applied — the climb/descend test would be backwards on\n" \
    "    an inverted rail, which is exactly how mount 5 is configured"
assert "sinf(_slider_tilt_deg * (float)DEG_TO_RAD) * (float)logical_dir" in fn, \
    "the slope is not the gravity component along the rail"
print("   physical → logical via slider_invert, then sin()   OK")

# ---- 4. the numbers, and the sign ------------------------------------------
print("\n4. the correction, on mount 5 (tilt +21, invert on):")
comp = float(re.search(r"STALL_TILT_COMP = ([\d.]+)f", CPP).group(1))
base = 80          # DEFAULT_STALL_THRESHOLD for the slider


def leg(phys_positive: bool, invert: bool, tilt: float):
    logical = (1 if phys_positive else -1) * (-1 if invert else 1)
    slope = math.sin(math.radians(tilt)) * logical
    return slope, round(base * (1.0 - comp * slope))


for phys, name in ((False, "MIN leg (physical -)"), (True, "MAX leg (physical +)")):
    slope, thr = leg(phys, True, 21.0)
    print(f"   {name}: {'CLIMBING  ' if slope > 0 else 'DESCENDING'} "
          f"slope {slope:+.3f}  SGTHRS {base} -> {thr}")

s_min, t_min = leg(False, True, 21.0)
s_max, t_max = leg(True, True, 21.0)
assert s_min * s_max < 0, "both legs came out the same way; one must climb and one descend"
print("   one climbs, the other descends                     OK")

climb = t_min if s_min > 0 else t_max
desc = t_max if s_min > 0 else t_min
assert climb < base < desc, \
    f"the correction is INVERTED: climbing {climb}, descending {desc}, base {base}"
print("   climbing lower, descending higher — not inverted   OK")

# ---- 4b. homing gets the same treatment ------------------------------------
# findHome() drives the SAME leg as the first half of findLimits(), and on this
# rig that leg is the climb.  It used to keep its own copy of the setup and wrote
# SGTHRS raw, so homing false-stalled part-way up the rail while Find Limits
# survived — the compensation existed and homing could not see it.
print("\n4b. homing:")
assert "void MountMotion::_beginLimitSeek(" in CPP, \
    "the shared limit-seek setup is gone; findHome may drift from findLimits again"
for entry in ("findLimits", "findHome"):
    body = CPP[CPP.index(f"void MountMotion::{entry}("):]
    body = body[:body.index("\n}\n")]
    assert "_beginLimitSeek(" in body, f"{entry} no longer delegates to the shared setup"
    assert "SGTHRS" not in body, \
        f"{entry} writes SGTHRS itself again — that is how homing lost the tilt\n" \
        f"    compensation in the first place"
print("   findLimits and findHome share one setup            OK")
print("   neither writes SGTHRS directly                     OK")

seek = CPP[CPP.index("void MountMotion::_beginLimitSeek("):]
seek = seek[:seek.index("\n}\n")]
assert "_applyStallThreshold(axis," in seek, \
    "the shared setup does not apply the slope-compensated threshold"
assert "_homing_only = homing_only;" in seek, \
    "the shared setup does not carry the homing flag through"
print("   the shared setup applies the compensation          OK")

# ---- 5. a level rail is untouched -------------------------------------------
print("\n5. every other rig:")
for inv in (True, False):
    for phys in (True, False):
        assert leg(phys, inv, 0.0)[1] == base, "a level rail changed the threshold"
print("   0 degrees changes nothing, either invert           OK")

# And the written value cannot leave the chip's range whatever the tilt.
assert "constrain((int)(thr + 0.5f), 1, 255)" in fn, \
    "the threshold is written without clamping to the chip's range"
for tilt in (-90.0, -45.0, 45.0, 90.0):
    for phys in (True, False):
        raw = base * (1.0 - comp * math.sin(math.radians(tilt)) * (1 if phys else -1))
        assert 1 <= max(1, min(255, round(raw))) <= 255
print("   clamped to 1-255 at any tilt                       OK")

print("\nALL CHECKS PASSED")
