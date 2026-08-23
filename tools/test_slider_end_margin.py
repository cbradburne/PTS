"""The slider's far limit is held 30 mm back from where the stall was found.

The rail is tilted. That needed more motor current and a less twitchy
StallGuard threshold to stop the carriage stalling part-way along a move — and
a less sensitive stall detector notices the end stop LATER. By the time
limit-find records the far end, the carriage is already into the stop, so the
recorded position is not where travel safely ends. Every later goto to that
limit drives back to the same place and grinds.

Holding the usable far end back by 30 mm costs 30 mm of travel and means a move
to the limit stops short of the stop rather than against it.

This is a workaround for late detection, not a fix for it. The honest fix is a
stall threshold that trips at the stop with the current the tilt demands, or a
physical switch — either would let the margin go back to a millimetre or two.
Recorded here so that whoever gets to that knows this number is a symptom.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent

CPP = (REPO / "firmware/teensy41_mount/MountMotion.cpp").read_text()
HDR = (REPO / "firmware/teensy41_mount/MountMotion.h").read_text()

# ---- 1. the margin, in millimetres -----------------------------------------
print("1. the margin:")
m = re.search(r"SLIDER_SAFETY_MARGIN_MM = (\d+);", CPP)
assert m, "the slider margin is no longer stated in mm"
mm = int(m.group(1))
assert mm == 30, f"the slider margin is {mm} mm, expected 30"
print(f"   {mm} mm, written in mm not steps                    OK")

# Converted from the drive geometry rather than hardcoded, so a pulley change
# does not silently turn 30 mm into some other distance.
assert "SLIDER_SAFETY_MARGIN_MM / NOMINAL_SLIDER_MM_PER_STEP" in CPP, \
    "the margin is no longer derived from the drive geometry"
print("   converted via NOMINAL_SLIDER_MM_PER_STEP           OK")

step_mm = 40.0 / (32 * 200)          # SLIDER_MM_PER_REV / (µsteps × full steps)
steps = int(mm / step_mm)
print(f"   = {steps} steps at {step_mm*1000:.2f} µm/step")

# ---- 2. it is held back from the FAR end only ------------------------------
# The near end is position 0 — the home stall itself — so there is nothing to
# hold back from. Applying it there would move home away from home.
print("\n2. which end it applies to:")
seg = CPP[CPP.index("// Position was zeroed at the min/home stall"):]
seg = seg[:seg.index("_lf_state = LimitFindState::IDLE;")]
assert "setLimits(ax, 0,\n                              max_found - LIMIT_BACK_OFF[idx] - LIMIT_SAFETY_MARGIN[idx]);" in seg, \
    "the far limit no longer subtracts the margin"
assert "setLimits(ax, 0," in seg, "the near limit is no longer the home stall at 0"
print("   far end only; near end stays at the home stall     OK")

# ---- 3. the margin must exceed the jog back-off ----------------------------
# Otherwise the soft limit a JOG respects would sit further out than the limit
# a GOTO drives to, and jogging could reach past where a recall is allowed.
print("\n3. against the jog back-off:")
m = re.search(r"LIMIT_BACK_OFF\[4\]\s*=\s*\{\s*\d+,\s*\d+,\s*(\d+),", CPP)
assert m, "LIMIT_BACK_OFF changed shape"
back_off = int(m.group(1))
assert steps > back_off, \
    f"margin {steps} steps is not clear of the jog back-off {back_off}"
print(f"   {steps} steps vs {back_off} back-off ({back_off*step_mm:.1f} mm)         OK")
assert "static_assert(LIMIT_SAFETY_MARGIN[AXIS_SLIDER] > LIMIT_BACK_OFF[AXIS_SLIDER]" in CPP, \
    "nothing enforces that relationship at compile time"
print("   and a static_assert holds it there                 OK")

# ---- 4. nothing else moved -------------------------------------------------
print("\n4. the other axes:")
m = re.search(r"LIMIT_SAFETY_MARGIN\[4\] = \{\s*\n?\s*(\d+), (\d+),", CPP)
assert m and m.group(1) == "0" and m.group(2) == "0", \
    "pan/tilt gained a safety margin; they have no end stops to find"
assert re.search(r"NOMINAL_SLIDER_MM_PER_STEP\),\s*\n\s*100\s*\n?\s*\};", CPP), \
    "the zoom margin changed; this was a slider-only request"
print("   pan/tilt still 0, zoom still 100                   OK")

print("\nALL CHECKS PASSED")
