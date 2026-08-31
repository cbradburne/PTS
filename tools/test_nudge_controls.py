"""The Move panel's dial hits the arc you aimed at, and nothing when you miss.

The panel was a cross of square buttons. It is now a dial of four separated arc
groups with the live angles in the hub, a track for the slider showing where the
carriage is, and a matching column for zoom.

The gaps are the feature, so they are what this file is mostly about. A slip
near a diagonal has to land on NOTHING — nudging pan when you meant tilt is a
shot on air going the wrong way, and a dial with no dead space between the axes
would make that easier than the square buttons did, not harder.

Everything here drives the widgets the way a finger does: synthesise a press at
a real coordinate and check what comes out. Geometry that is only asserted
against the constants that produced it proves nothing.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, math
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QPointF, QSize, Qt
from PyQt6.QtGui import QMouseEvent
app = QApplication.instance() or QApplication([])

from ui.widgets.nudge_controls import (RadialNudge, SliderTrack, ZoomColumn,
                                       _R_HUB, _R_IN_0, _R_IN_1,
                                       _R_OUT_0, _R_OUT_1, _HALF_SPAN)

SIZE = 400
HALF = SIZE / 2.0


def press(widget, x, y):
    ev = QMouseEvent(QMouseEvent.Type.MouseButtonPress, QPointF(x, y),
                     Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier)
    widget.mousePressEvent(ev)


def at(clock_deg, r_frac):
    """A point on the dial, degrees clockwise from twelve."""
    t = math.radians(clock_deg)
    return HALF + HALF * r_frac * math.sin(t), HALF - HALF * r_frac * math.cos(t)


DIAL = RadialNudge(); DIAL.resize(SIZE, SIZE); DIAL.set_steps(1, 10)
FIRED = []
DIAL.nudged.connect(lambda a, d: FIRED.append((a, d)))


def tap(clock_deg, r_frac):
    FIRED.clear()
    press(DIAL, *at(clock_deg, r_frac))
    return list(FIRED)


R_IN  = (_R_IN_0 + _R_IN_1) / 2
R_OUT = (_R_OUT_0 + _R_OUT_1) / 2

# ---- 1. each arc does what it is labelled --------------------------------
print("1. the eight arcs:")
CASES = [(0,   R_IN,  "tilt",  +1.0), (0,   R_OUT, "tilt", +10.0),
         (180, R_IN,  "tilt",  -1.0), (180, R_OUT, "tilt", -10.0),
         (90,  R_IN,  "pan",   +1.0), (90,  R_OUT, "pan",  +10.0),
         (270, R_IN,  "pan",   -1.0), (270, R_OUT, "pan",  -10.0)]
for deg, r, axis, want in CASES:
    got = tap(deg, r)
    assert got == [(axis, want)], \
        f"{deg}° at r={r:.2f} gave {got}, expected {axis} {want:+g}"
    ring = "inner" if r == R_IN else "outer"
    print(f"   {deg:>3}° {ring:<5} -> {axis} {want:+6.1f}°   OK")

# Up is tilt UP and right is pan RIGHT, not some other pairing that happens to
# be self-consistent.
assert tap(0, R_OUT) == [("tilt", +10.0)], "up is not tilt up"
assert tap(90, R_OUT) == [("pan", +10.0)], "right is not pan right"
print("   up is tilt+, right is pan+                        OK")

# ---- 2. and the gaps do nothing ------------------------------------------
print("\n2. where a slip lands:")
for diag in (45, 135, 225, 315):
    for r in (R_IN, R_OUT):
        assert tap(diag, r) == [], \
            f"a press at {diag}° fired {FIRED} — the diagonals must be dead, or " \
            "a slip\n    between the axes moves the wrong one"
print("   all four diagonals are dead                       OK")

# Just inside the arc still works, just outside it does not. This is the edge
# that matters: it is where a hurried press actually lands.
edge_in  = tap(_HALF_SPAN - 2, R_OUT)
edge_out = tap(_HALF_SPAN + 2, R_OUT)
assert edge_in == [("tilt", 10.0)], f"2° inside the arc edge fired {edge_in}"
assert edge_out == [], f"2° outside the arc edge fired {edge_out}"
print(f"   the edge is where it is drawn, +/-{_HALF_SPAN:.0f}°            OK")

# The hub is a readout, not a button, and the ring gap is a gap.
assert tap(0, _R_HUB * 0.5) == [], "the hub fires — it is a readout, not a target"
assert tap(0, (_R_IN_1 + _R_OUT_0) / 2) == [], "the gap between the rings fires"
assert tap(0, 0.99) == [], "outside the outer ring fires"
print("   hub, inter-ring gap and outside all dead          OK")

# ---- 3. the track ---------------------------------------------------------
print("\n3. the slider track:")
TRACK = SliderTrack(); TRACK.resize(600, 90); TRACK.set_steps(10, 100)
MM = []
TRACK.nudged.connect(MM.append)
for frac, want in ((0.10, -100.0), (0.35, -10.0), (0.65, +10.0), (0.90, +100.0)):
    MM.clear()
    press(TRACK, 600 * frac, 45)
    assert MM == [want], f"a press {frac:.0%} across gave {MM}, expected {want:+g}"
print("   −100 / −10 / +10 / +100, left to right            OK")

# The carriage is drawn only when the mount has actually said where it is.
TRACK.set_position(None, 0, 900)
assert TRACK._mm is None, "an unknown position is being treated as a number"
TRACK.set_position(412, 0, 900)
assert TRACK._mm == 412, "a known position is not kept"
print("   carriage only once a position has been reported   OK")

# ---- 4. zoom --------------------------------------------------------------
print("\n4. the zoom column:")
ZOOM = ZoomColumn(700, 200); ZOOM.resize(96, 300)
VEL = []
ZOOM.started.connect(VEL.append)
STOPPED = []
ZOOM.stopped.connect(lambda: STOPPED.append(True))
for frac, want in ((0.10, +700), (0.35, +200), (0.65, -200), (0.90, -700)):
    VEL.clear()
    press(ZOOM, 48, 300 * frac)
    assert VEL == [want], f"a press {frac:.0%} down gave {VEL}, expected {want:+d}"
    ZOOM.mouseReleaseEvent(None)
assert len(STOPPED) == 4, "releasing does not stop the jog every time"
print("   fast/slow both ways, and every release stops it   OK")

# A widget given an alignment in a layout is sized from its sizeHint, and a
# bare QWidget has none — this one landed at zero height and vanished with no
# error at all.
hint = ZOOM.sizeHint()
assert isinstance(hint, QSize) and hint.height() > 100 and hint.width() > 40, \
    f"ZoomColumn.sizeHint() is {hint}; a layout that aligns it will size it " \
    "from this,\n    and a hintless widget silently collapses to nothing"
print(f"   sizeHint {hint.width()}x{hint.height()}, so it cannot collapse       OK")

# ---- 5. the panel wires them to the mount ---------------------------------
print("\n5. what the panel does with them:")
SRC = (REPO / "pc_app/ui/widgets/nudge_overlay.py").read_text()
assert "self._dial.nudged.connect(self._on_dial)" in SRC, "the dial is not wired"
assert "self._track.nudged.connect(self._on_track)" in SRC, "the track is not wired"
assert "self._zoom.started.connect(self._zoom_jog)" in SRC and \
       "self._zoom.stopped.connect(self._zoom_stop)" in SRC, "zoom is not wired"
assert "self._mm.position_updated.connect(self._on_position)" in SRC, \
    "nothing feeds the hub or the carriage, so both stay blank forever"
print("   dial, track, zoom and the position feed all wired OK")

# Positions are answered on request, so the panel has to ask — but only while
# it is open, or this becomes the polling that was deliberately removed.
assert "def request_position" in (REPO / "pc_app/comms/mount_manager.py").read_text(), \
    "there is no way to ask for a position"
ask = SRC[SRC.index("    def _ask_position(self)"):]
ask = ask[:ask.index("\n    def ", 1)]
assert "if not self.isVisible():" in ask, \
    "the panel asks for positions while closed; that is a background poll with " \
    "no\n    one looking at the answer"
print("   asks only while the panel is open                 OK")

print("\nALL CHECKS PASSED")
