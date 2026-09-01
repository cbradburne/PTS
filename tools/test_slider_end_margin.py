"""The slider's limits are held back from each stall by a per-mount setting.

The rail is tilted. That needed more motor current and a less twitchy
StallGuard threshold to stop the carriage stalling part-way along a move — and
a less sensitive stall detector notices the end stop LATER. By the time
limit-find records the far end, the carriage is already into the stop, so the
recorded position is not where travel safely ends. Every later goto to that
limit drives back to the same place and grinds.

Holding the usable end back by 30 mm costs 30 mm of travel and means a move to
the limit stops short of the stop rather than against it.

It applied to the FAR end only, on the reasoning that the near end is position
0 — the home stall itself — so there was nothing to hold back from. That was
wrong, and it showed: a look-at run parked hard against the stop at one end and
stopped about 32 mm short at the other. Late detection is late at both ends, so
the position recorded at each is already inside its stop, and home is no more a
safe place to drive to than the far limit is. Both ends take the inset now.

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
# It is a SETTING now, not a constant: the right value is found by trying one
# and listening, and as a constant every attempt cost a board removal, a
# re-home, a ref 0/0 and a recalibration. The constant that remains is the
# default and the compile-time sanity check.
print("1. the margin:")
PROTO = (REPO / "firmware/shared/protocol.h").read_text()
m = re.search(r"#define SLIDER_END_MARGIN_MM_DEFAULT\s+(\d+)", PROTO)
assert m, "the slider margin default is no longer stated in mm"
mm = int(m.group(1))
assert mm == 30, f"the slider margin default is {mm} mm, expected 30"
print(f"   {mm} mm default, written in mm not steps           OK")

# One home for the number. A copy in MountMotion.cpp would be free to drift
# from the one the EEPROM default and the app both use.
assert "SLIDER_SAFETY_MARGIN_MM" not in CPP, \
    "MountMotion.cpp has its own copy of the margin again — it and " \
    "protocol.h's\n    default would drift, and only the rig would notice"
assert "SLIDER_END_MARGIN_MM_DEFAULT / NOMINAL_SLIDER_MM_PER_STEP" in CPP, \
    "the default is no longer derived from the drive geometry"
print("   one definition, converted via the drive geometry   OK")

# And the limit find reads the SETTING, not the constant.
lf_use = CPP[CPP.index("const int32_t margin ="):]
lf_use = lf_use[:lf_use.index(";", lf_use.index("LIMIT_SAFETY_MARGIN"))]
assert "_slider_end_margin_mm" in lf_use and "AXIS_SLIDER" in lf_use, \
    "the limit find still uses the compiled constant for the slider, so the\n" \
    "    setting would do nothing"
print("   and the limit find uses the setting                OK")

step_mm = 40.0 / (32 * 200)          # SLIDER_MM_PER_REV / (µsteps × full steps)
steps = int(mm / step_mm)
print(f"   = {steps} steps at {step_mm*1000:.2f} µm/step")

# ---- 2. it applies to BOTH ends --------------------------------------------
# Asserted as a property rather than as source text: this section used to match
# the exact setLimits() line, which made a deliberate behaviour change look like
# a broken test and said nothing about what the limits actually became.
print("\n2. which end it applies to:")
seg = CPP[CPP.index("// Position was zeroed at the min/home stall"):]
seg = seg[:seg.index("_lf_state = LimitFindState::IDLE;")]
calls = re.findall(r"setLimits\(ax,\s*([^,]+?),\s*\n?\s*([^)]+?)\);", seg, re.S)
assert calls, "setLimits is no longer called after the far stall"
for lo, hi in calls:
    lo, hi = re.sub(r"\s+", " ", lo).strip(), re.sub(r"\s+", " ", hi).strip()
    for end, expr in (("near", lo), ("far", hi)):
        assert "inset" in expr or "LIMIT_SAFETY_MARGIN" in expr, \
            f"the {end} limit takes no margin: setLimits(ax, {lo}, {hi}).\n" \
            "    An end left at the raw stall is an end a recall drives into."
    assert "max_found" in lo or "max_found" in hi, \
        f"neither limit comes from the stall that was found: ({lo}, {hi})"
print(f"   both ends, in {len(calls)} orientation(s)                       OK")

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
