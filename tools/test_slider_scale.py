"""Both ends of the rail are inset, and the app's millimetres are the mount's.

TWO ENDS. A look-at run to one end parked hard against the stop while the other
stopped about 32 mm short. The safety margin was applied to the far end only,
on the reasoning that the near end IS the datum — position 0 is the home stall.
But the reason the far end needs it applies just as much at home: a stall is
noticed late, so the position recorded at each end is already inside its stop,
and driving back to it drives back into the stop. Same inset both ends now.

MILLIMETRES. Find-limits reported a step count, which is a number nobody can
check against the rail in front of them. The app converts with the same
steps-per-mm the Move panel uses, so the readout cannot disagree with the
control — and that number is checked here against the firmware's own geometry,
because the two derive it from different descriptions of the same hardware and
have no other reason to stay equal.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

CPP = (REPO / "firmware/teensy41_mount/MountMotion.cpp").read_text()
HDR = (REPO / "firmware/teensy41_mount/MountMotion.h").read_text()
MW  = (REPO / "pc_app/ui/main_window.py").read_text()


def resolve(*sources):
    """Every numeric #define and constexpr in these files, evaluated."""
    raw = {}
    for src in sources:
        for name, expr in re.findall(r"#define\s+([A-Z_][A-Z0-9_]*)\s+([^\n/]+)", src):
            raw.setdefault(name, expr.strip())
        for name, expr in re.findall(
                r"(?:static\s+)?constexpr\s+\w+\s+([A-Za-z_]\w*)\s*=\s*([^;]+);", src):
            raw.setdefault(name, expr.strip())
    out = {}
    for _ in range(len(raw) + 1):
        for name, expr in raw.items():
            if name in out:
                continue
            try:
                out[name] = eval(re.sub(r"[fF](?=[^\w.]|$)", "", expr), {}, out)
            except Exception:
                pass
    return out


C = resolve(HDR, CPP)

# ---- 1. the two ends match ---------------------------------------------------
print("1. where the usable rail ends:")
lf = CPP[CPP.index("case LimitFindState::MOVING_TO_MAX:"):]
lf = lf[:lf.index("break;")]
assert "const int32_t inset = LIMIT_BACK_OFF[idx] + LIMIT_SAFETY_MARGIN[idx];" in lf, \
    "the inset is no longer computed once for both ends"

calls = re.findall(r"setLimits\(ax,\s*([^,]+?),\s*\n?\s*([^)]+?)\);", lf, re.S)
assert calls, "setLimits is no longer called from the MOVING_TO_MAX leg"
for lo, hi in calls:
    lo, hi = re.sub(r"\s+", " ", lo).strip(), re.sub(r"\s+", " ", hi).strip()
    assert "inset" in lo and "inset" in hi, \
        f"one end takes no inset: setLimits(ax, {lo}, {hi}).\n" \
        "    The end left at the raw stall parks hard against the stop, and the " \
        "other\n    stops short — which is the asymmetry measured on the rig."
print(f"   both ends inset, in {len(calls)} orientation(s)             OK")

back_off = C["LIMIT_BACK_OFF"][2] if isinstance(C.get("LIMIT_BACK_OFF"), list) else None
if back_off is None:                       # array literal did not evaluate
    back_off = int(re.search(r"LIMIT_BACK_OFF\[4\]\s*=\s*\{[^}]*?,\s*[^,]*,\s*(\d+)",
                             CPP).group(1))
mm_per_step = C["NOMINAL_SLIDER_MM_PER_STEP"]
margin_mm   = C["SLIDER_SAFETY_MARGIN_MM"]
inset_mm    = back_off * mm_per_step + margin_mm
print(f"   {inset_mm:.1f} mm at each end "
      f"({back_off} steps back-off + {margin_mm:.0f} mm margin)")

# The operator measured "approx 32 mm" at the far end. That is this number, and
# it is the value now applied at both — so it is worth failing if it drifts far
# enough to be a different rail.
assert 25.0 < inset_mm < 40.0, \
    f"the inset is {inset_mm:.1f} mm. That is not wrong in itself, but it was " \
    "31.9 mm\n    when the rig was measured — check the margin change was " \
    "intended before\n    widening this."
print("   matches the 31.9 mm measured on the rig           OK")

# ---- 2. the app's millimetres are the mount's ------------------------------
# The PC derives steps/mm from "32 microsteps, 1.8 deg/step, 40 mm/rev"; the
# firmware derives 40 mm/rev from a 20-tooth GT2 pulley on a 2 mm belt. Same
# hardware, two descriptions, and nothing but this holding them equal.
print("\n2. steps per millimetre, both sides:")
from ui.widgets.nudge_overlay import NudgeOverlay
pc_spmm = NudgeOverlay.SLIDER_STEPS_PER_MM
fw_spmm = 1.0 / mm_per_step
assert abs(pc_spmm - fw_spmm) < 1e-6, (
    f"the PC app works in {pc_spmm:g} steps/mm and the firmware in {fw_spmm:g}.\n"
    f"    Every millimetre the operator asks for would be off by "
    f"{100 * abs(pc_spmm - fw_spmm) / fw_spmm:.1f}%, and the\n"
    "    limits readout would disagree with the rail it describes.")
print(f"   PC {pc_spmm:g} = firmware {fw_spmm:g} steps/mm            OK")

assert C["SLIDER_MM_PER_REV"] == NudgeOverlay._SLIDER_MM_PER_REV, \
    f"mm per revolution: firmware {C['SLIDER_MM_PER_REV']}, " \
    f"PC {NudgeOverlay._SLIDER_MM_PER_REV}"
ovsrc = (REPO / "pc_app/ui/widgets/nudge_overlay.py").read_text()
decl = next(l for l in ovsrc.splitlines()
            if "_SLIDER_MM_PER_REV" in l and "=" in l and "self." not in l)
assert "GT2" in decl or "pulley" in decl.lower(), \
    f"the 40 mm/rev is not attributed to the pulley: {decl.strip()!r}\n" \
    "    It said 'leadscrew pitch' — the right number on the wrong part, so a " \
    "pulley\n    change would have found nothing here to update."
print(f"   {C['SLIDER_PULLEY_TEETH']:.0f}T pulley x {C['GT2_BELT_PITCH_MM']:.0f} mm "
      f"= {C['SLIDER_MM_PER_REV']:.0f} mm/rev, named as such   OK")

# ---- 3. what find-limits actually says --------------------------------------
print("\n3. the message after a limit find:")
handler = MW[MW.index("    def _on_limits_found("):]
handler = handler[:handler.index("\n    # ---")]
assert "SLIDER_STEPS_PER_MM" in handler, \
    "the slider limits are reported without converting to millimetres"
assert "NudgeOverlay.SLIDER_STEPS_PER_MM" in handler, \
    "the conversion is a copy rather than the one the Move panel uses — the " \
    "readout\n    and the control can then disagree about what a millimetre is"
assert "mm of travel" in handler, "the usable travel is not reported"

# Zoom is a lens ring; millimetres would be a fiction.
zoom_branch = handler[handler.index("Axis.ZOOM"):]
assert "steps" in zoom_branch.split("save_config")[0], \
    "zoom limits are being reported in millimetres, which they are not"
print("   slider in mm with its travel, zoom in steps       OK")

# 240000 steps is a 1.5 m rail. If the units were ever swapped the number would
# be absurd rather than subtly wrong, so check the arithmetic reads sensibly.
lo_mm, hi_mm = 5100 / pc_spmm, 240000 / pc_spmm
assert 30 < lo_mm < 35 and 1400 < hi_mm < 1600, \
    f"a 5100 -> 240000 step rail reads as {lo_mm:.0f} -> {hi_mm:.0f} mm"
print(f"   5100 -> 240000 steps reads {lo_mm:.0f} -> {hi_mm:.0f} mm         OK")

# ---- 4. the line the operator actually reads --------------------------------
# The status bar at the bottom of the window was converted first, and it was the
# wrong surface: the result of a limit find is read off the green line in the
# Find Limits dialog, which is where the operator is already looking when it
# finishes. Driven here rather than asserted from source, because "is it in
# millimetres" is a question about what the label says.
print("\n4. the Find Limits dialog:")
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QObject, pyqtSignal
app = QApplication.instance() or QApplication([])

from comms.protocol import Axis
from ui.dialogs.find_limits_dialog import FindLimitsDialog


class _MM(QObject):
    limits_found = pyqtSignal(int, int, int, int)
    def send_find_limits(self, *a): pass


def result_for(axis, lo, hi):
    mm = _MM()
    dlg = FindLimitsDialog(1, mm, axis)
    dlg._running = True
    mm.limits_found.emit(1, int(axis), lo, hi)
    app.processEvents()
    return dlg._result.text()


line = result_for(Axis.SLIDER, 5100, 240000)
assert "steps" not in line, \
    f"the dialog still reports the rail in steps: {line!r}"
for want in ("1468 mm", "32 mm", "1500 mm"):
    assert want in line, f"expected {want!r} in the result line, got {line!r}"
print(f"   {line}   OK")

zoom = result_for(Axis.ZOOM, 0, -18000)
assert "mm" not in zoom, \
    f"the zoom result claims millimetres, which a lens ring has none of: {zoom!r}"
print(f"   zoom stays in steps                               OK")

# Both surfaces, one conversion. If either grew its own the two could disagree
# about the same rail on the same screen.
DLG = (REPO / "pc_app/ui/dialogs/find_limits_dialog.py").read_text()
for name, src in (("the dialog", DLG), ("main_window", MW)):
    assert "NudgeOverlay.SLIDER_STEPS_PER_MM" in src, \
        f"{name} converts with a number of its own rather than the one the Move " \
        "panel\n    sends nudges with"
print("   dialog and status bar share one steps/mm           OK")

print("\nALL CHECKS PASSED")
