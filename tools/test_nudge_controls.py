"""The Move panel's dial hits the arc you aimed at, and nothing when you miss.

The panel was a cross of square buttons. It is now a dial of four separated arc
groups, a track for the slider, and a matching column for zoom. Nothing shows a
live position: the mount answers CMD_GET_POSITION but volunteers nothing — the
unsolicited broadcast was removed on purpose — so a readout would have meant
the panel polling for it, which is the traffic that removal was for.

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

from PyQt6.QtWidgets import QApplication, QWidget
from PyQt6.QtCore import QPointF, QSize, Qt
from PyQt6.QtGui import QMouseEvent
app = QApplication.instance() or QApplication([])

from ui.widgets.nudge_controls import (RadialNudge, SliderTrack, ZoomColumn,
                                       _R_HUB, _RINGS, _R_OUT_1, _HALF_SPAN,
                                       _RUN_W)

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


DIAL = RadialNudge(); DIAL.resize(SIZE, SIZE); DIAL.set_steps(0.2, 1, 10)
FIRED = []
DIAL.nudged.connect(lambda a, d: FIRED.append((a, d)))


def tap(clock_deg, r_frac):
    FIRED.clear()
    press(DIAL, *at(clock_deg, r_frac))
    return list(FIRED)


R_FINE, R_IN, R_OUT = [(a + b) / 2 for a, b in _RINGS]

# ---- 1. each arc does what it is labelled --------------------------------
# Three rings now: the step grows with the radius, fine nearest the hub. The
# fine ring is the one that has to be checked hardest — it is the shortest arc
# and the longest label, so it is where a layout mistake would land first.
print("1. the twelve arcs:")
NAMED = (("fine", R_FINE, 0.2), ("small", R_IN, 1.0), ("large", R_OUT, 10.0))
CASES = [(clock, r, axis, sign * step, name)
         for clock, axis, sign in ((0, "tilt", +1), (180, "tilt", -1),
                                   (90, "pan", +1), (270, "pan", -1))
         for name, r, step in NAMED]
for deg, r, axis, want, ring in CASES:
    got = tap(deg, r)
    assert got == [(axis, want)], \
        f"{deg}° on the {ring} ring gave {got}, expected {axis} {want:+g}"
    print(f"   {deg:>3}° {ring:<5} -> {axis:<4} {want:+6.1f}°   OK")

# Up is tilt UP and right is pan RIGHT, not some other pairing that happens to
# be self-consistent.
assert tap(0, R_OUT) == [("tilt", +10.0)], "up is not tilt up"
assert tap(90, R_OUT) == [("pan", +10.0)], "right is not pan right"
print("   up is tilt+, right is pan+                        OK")

# The rings run fine -> small -> large outwards. Reversed, every muscle-memory
# press on the outside would be the smallest step instead of the biggest.
assert R_FINE < R_IN < R_OUT, "the rings are no longer ordered by radius"
assert tap(0, R_FINE)[0][1] < tap(0, R_IN)[0][1] < tap(0, R_OUT)[0][1], \
    "the step does not grow with the radius"
print("   step grows outwards: 0.2 -> 1 -> 10               OK")

# ---- 2. and the gaps do nothing ------------------------------------------
print("\n2. where a slip lands:")
for diag in (45, 135, 225, 315):
    for r in (R_FINE, R_IN, R_OUT):
        assert tap(diag, r) == [], \
            f"a press at {diag}° fired {FIRED} — the diagonals must be dead, or " \
            "a slip\n    between the axes moves the wrong one"
print("   all four diagonals are dead, on all three rings   OK")

# Just inside the arc still works, just outside it does not. This is the edge
# that matters: it is where a hurried press actually lands.
edge_in  = tap(_HALF_SPAN - 2, R_OUT)
edge_out = tap(_HALF_SPAN + 2, R_OUT)
assert edge_in == [("tilt", 10.0)], f"2° inside the arc edge fired {edge_in}"
assert edge_out == [], f"2° outside the arc edge fired {edge_out}"
print(f"   the edge is where it is drawn, +/-{_HALF_SPAN:.0f}°            OK")

# The hub is a legend, not a button, and every gap between rings is a gap.
# With three rings there are two of them, and both separate steps that differ
# by 5x or more — landing on the wrong side is not a near miss.
assert tap(0, _R_HUB * 0.5) == [], "the hub fires — it is a legend, not a target"
for (_, inner_top), (outer_bottom, _) in zip(_RINGS, _RINGS[1:]):
    mid = (inner_top + outer_bottom) / 2
    assert tap(0, mid) == [], f"the gap at r={mid:.3f} fires"
assert tap(0, (_R_HUB + _RINGS[0][0]) / 2) == [], "the hub-to-fine gap fires"
assert tap(0, 0.99) == [], "outside the outer ring fires"
print("   hub, both inter-ring gaps and outside all dead    OK")

# ---- 3. the track ---------------------------------------------------------
print("\n3. the slider track:")
TRACK = SliderTrack(); TRACK.resize(600, 90); TRACK.set_steps(10, 100)
MM, RUNS = [], []
TRACK.nudged.connect(MM.append)
TRACK.run.connect(RUNS.append)

# Six zones on a 600px track: pad = h/10 = 9 either end, and 582px divided
# into 4 nudge units plus 2 run units of 0.75 -> 105.8px per unit. Worked out
# here rather than read back from _zones(), so the arithmetic is checked and
# not just echoed.
UNIT = (600 - 2 * 9) / (4 + 2 * _RUN_W)
EDGES = [9]
for width in (_RUN_W, 1, 1, 1, 1, _RUN_W):
    EDGES.append(EDGES[-1] + UNIT * width)
CENTRES = [(EDGES[i] + EDGES[i + 1]) / 2 for i in range(6)]


def tap_track(x):
    MM.clear(); RUNS.clear()
    press(TRACK, x, 45)
    return list(MM), list(RUNS)


for x, want in zip(CENTRES[1:5], (-100.0, -10.0, +10.0, +100.0)):
    mm, runs = tap_track(x)
    assert mm == [want] and runs == [], \
        f"a press at x={x:.0f} gave {mm}/{runs}, expected a {want:+g}mm nudge"
print("   −100 / −10 / +10 / +100 across the middle         OK")

# The two ends run the carriage the whole way. They emit a DIRECTION, not a
# distance: the panel turns it into a move as long as the rail, which the
# Teensy clamps at the end — nothing here needs to know where the rail is.
for x, want in ((CENTRES[0], -1), (CENTRES[-1], +1)):
    mm, runs = tap_track(x)
    assert runs == [want] and mm == [], \
        f"a press at x={x:.0f} gave {mm}/{runs}, expected a run of {want:+d}"
assert TRACK._zones().__next__()[4] == "<<", "the left run zone is not labelled <<"
assert list(TRACK._zones())[-1][4] == ">>", "the right run zone is not labelled >>"
print("   << and >> at the ends, run left and run right     OK")

# A run is a move with nothing to stop it unless the Teensy has limits set, so
# when the mount has none the zones must be inert as well as grey. Drawn dead
# and still live would be the worst of the three states.
TRACK.set_runs_enabled(False)
for x in (CENTRES[0], CENTRES[-1]):
    mm, runs = tap_track(x)
    assert runs == [] and mm == [], \
        f"a run fired with limits unknown: {runs}"
mm, runs = tap_track(CENTRES[2])
assert mm == [-10.0], "greying the runs disabled the nudges too"
TRACK.set_runs_enabled(True)
mm, runs = tap_track(CENTRES[0])
assert runs == [-1], "the runs never came back"
print("   dead while the limits are unknown, live after     OK")

# And the whole rail greys out on a mount that has no slider at all. Two
# separate questions with two separate answers: "is there a rail" and "has it
# been calibrated". A mount with no slider must not accept a nudge either —
# there is no axis to send it to.
TRACK.set_enabled(False)
for x in (CENTRES[0], CENTRES[2], CENTRES[-1]):
    mm, runs = tap_track(x)
    assert mm == [] and runs == [], \
        f"a mount with no slider still moved something: {mm}/{runs}"
TRACK.set_enabled(True)
mm, runs = tap_track(CENTRES[2])
assert mm == [-10.0], "the track never comes back when a slider appears"
print("   a mount with no slider: the whole rail is dead     OK")

# No position readout anywhere. The mount answers CMD_GET_POSITION but
# volunteers nothing — the unsolicited broadcast was removed deliberately — so
# anything drawing a live position would have to poll for it, which is the
# traffic that removal was for.
CTRL = (REPO / "pc_app/ui/widgets/nudge_controls.py").read_text()
for gone in ("set_position", "set_angles", "_min_mm", "self._pan_deg"):
    assert gone not in CTRL, \
        f"{gone} is back — the panel is claiming a position again, and the only\n" \
        "    way to keep one current is to poll for it"
assert "request_position" not in (REPO / "pc_app/comms/mount_manager.py").read_text(), \
    "request_position survives with no caller"
print("   nothing draws a live position                     OK")

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

# The captions name the axis; the arrows only give a direction, and up-is-in is
# a convention rather than something the control states. They also inset the
# body, so the four zones must still land where a finger expects them — checked
# by the presses above, which run against the widget's full height.
assert "ZOOM IN" in CTRL and "ZOOM OUT" in CTRL, \
    "the zoom column no longer says which way is which"
VEL.clear()
press(ZOOM, 48, 3)                       # in the top caption, above the body
press(ZOOM, 48, 297)                     # in the bottom caption
assert VEL == [], \
    f"pressing a caption fired {VEL}; the labels are text, not a fifth and " \
    "sixth button"
print("   IN/OUT captions, and neither is pressable          OK")

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
assert "self._track.run.connect(self._on_run)" in SRC, "the run zones are not wired"
assert "position_updated" not in SRC, \
    "the panel subscribes to positions again but displays none"
print("   dial, track, zoom and the runs all wired          OK")

# ---- 6. the track is the width of the dial it sits under ------------------
# The dial paints inside the largest circle that FITS, so its drawn width is
# min(w, h) x the outer radius — not the widget width, which is whatever the
# layout handed it. Measuring the widget leaves the track visibly wider than
# the control it belongs to.
print("\n6. the track under the dial:")
from PyQt6.QtCore import QObject, pyqtSignal
from ui.widgets.nudge_overlay import NudgeOverlay
class _St:
    active_pt_preset = 2; active_sl_preset = 2; last_config_report = None
    slider_min = 1000; slider_max = 241000        # a calibrated 1.5m rail
    has_slider = True
class _MM(QObject):
    position_updated     = pyqtSignal(int, object)
    mount_status_updated = pyqtSignal(int)
    def __init__(self):
        super().__init__()
        self.sent = []
        self.st = _St()
    def state(self, m): return self.st
    def send_move_rel(self, *a): self.sent.append(("move_rel",) + a)
    def send_jog(self, *a): self.sent.append(("jog",) + a)

import ui.ui_scale as SCALE
from ui.widgets.nudge_overlay import (NudgeOverlay, _PANEL_REF, _PANEL_MIN,
                                      _TRACK_H_F, _ZOOM_W_F, _CLOSE_F)

SCALE.init_ui_scale(1080)


def panel(parent_w=1920, parent_h=918, mm=None):
    """A panel in a parent the size of the central widget on that screen."""
    p = QWidget(); p.resize(parent_w, parent_h)
    ov = NudgeOverlay(mm or _MM(), parent=p)
    ov.show_for(1, nudge_deg_fine=0.2, nudge_deg_small=1, nudge_deg_large=10,
                nudge_mm_small=10, nudge_mm_large=100)
    app.processEvents(); ov.layout().activate(); app.processEvents()
    ov._parent_keepalive = p          # or Python frees it and Qt follows
    return ov


OV = panel()
d, tr = OV._dial, OV._track
circle = min(d.width(), d.height()) * _R_OUT_1
assert abs(tr.width() - circle) <= 2, \
    f"the track is {tr.width()}px against a {circle:.0f}px dial circle"
assert abs((tr.x() + tr.width() / 2) - (d.x() + d.width() / 2)) <= 2, \
    "the track is not centred on the dial — it is centred on the panel, which " \
    "the\n    zoom column pushes off to one side"
print(f"   {tr.width()}px wide against a {circle:.0f}px circle, same centre   OK")

# ---- 7. and the whole panel scales with the screen ------------------------
# The panel was a fixed 800x800 — right on the 1920x1080 it was drawn against
# and progressively smaller on anything with more logical pixels, which is the
# fault the grid and the bars had already been fixed for. Three machines side
# by side is where it showed.
print("\n7. the panel on other screens:")


def ratios(ov):
    dl = ov._dial
    side = ov.width()
    return {"circle": min(dl.width(), dl.height()) * _R_OUT_1 / side,
            "track_w": ov._track.width() / side,
            "track_h": ov._track.height() / side,
            "zoom_w":  ov._zoom.width() / side,
            "close":   ov._close_btn.width() / side}


REF = ratios(OV)
assert OV.width() == OV.height() == _PANEL_REF, \
    f"at 1080 the panel is {OV.width()}x{OV.height()}, not the {_PANEL_REF} it " \
    "was\n    approved at — this change was meant to leave that screen alone"
print(f"   1080: {_PANEL_REF}px square, unchanged                 OK")

for h, want in ((1440, 1067), (2160, 1600), (768, 569)):
    SCALE.init_ui_scale(h)
    ov = panel(parent_w=int(h * 16 / 9), parent_h=int(h * 0.85))
    assert ov.width() == want, \
        f"a {h}px screen gives a {ov.width()}px panel, expected {want}"
    got = ratios(ov)
    for k, v in REF.items():
        assert abs(got[k] - v) < 0.006, \
            f"at {h}px the {k} is {got[k]:.4f} of the panel, {v:.4f} at 1080 —\n" \
            f"    something inside is still a fixed pixel count"
    print(f"   {h:>4}px screen: {ov.width():>4}px panel, "
          f"{ov._track.width():>4}px track, {ov._zoom.width():>3}px zoom")
print("   everything inside keeps its proportion               OK")

# The proportions being right is not the same as the control still working.
# Drive the dial at its NEW size, at coordinates a finger would land on.
SCALE.init_ui_scale(2160)
big = panel(parent_w=3840, parent_h=1836)
hits = []
big._dial.nudged.connect(lambda a, dg: hits.append((a, dg)))
bw, bh = big._dial.width(), big._dial.height()
bhalf = min(bw, bh) / 2.0
for clock, r_frac, want in ((0, R_OUT, ("tilt", 10.0)), (90, R_IN, ("pan", 1.0)),
                            (270, R_OUT, ("pan", -10.0)), (45, R_OUT, None)):
    hits.clear()
    t = math.radians(clock)
    press(big._dial, bw / 2 + bhalf * r_frac * math.sin(t),
                     bh / 2 - bhalf * r_frac * math.cos(t))
    assert hits == ([want] if want else []), \
        f"on a 2160px screen, {clock}° gave {hits}, expected {want}"
print("   the arcs and the dead diagonals still hit at 2x      OK")

# A panel taller than the window it floats over is worse than a small one:
# the close button goes off the top. The screen sets the size, the parent only
# ever clamps it.
SCALE.init_ui_scale(2160)
squashed = panel(parent_w=3840, parent_h=700)
assert squashed.height() <= 700, \
    f"the panel is {squashed.height()}px tall inside a 700px parent"
assert squashed.height() >= _PANEL_MIN, "the clamp crushed the panel to nothing"
assert squashed.y() >= 0 and squashed.x() >= 0, \
    "the panel is positioned off the top or left edge of its parent"
print(f"   clamped to {squashed.height()}px in a 700px window, still on-screen  OK")

# Leave the scale where the rest of the suite expects it.
SCALE.init_ui_scale(1080)

# ---- 8. what a run-to-the-end actually sends ------------------------------
# The panel turns a direction into a relative move one full rail long. The
# Teensy clamps a slider move to its limits, so overshooting IS the mechanism —
# but only once those limits are set. MountMotion::_clampToLimits returns
# untouched when they are not, which makes the same command a move with nothing
# to stop it. That is why this is gated on the mount, not on the widget.
print("\n8. running to the end of the rail:")
mmgr = _MM()
ov = panel(mm=mmgr)
span = _St.slider_max - _St.slider_min

mmgr.sent.clear(); ov._on_run(+1)
assert mmgr.sent == [("move_rel", 1, 0, 0, span, 0, 2)], \
    f"a run right sent {mmgr.sent}; expected a +{span} step slider move alone"
mmgr.sent.clear(); ov._on_run(-1)
assert mmgr.sent == [("move_rel", 1, 0, 0, -span, 0, 2)], \
    f"a run left sent {mmgr.sent}; expected a -{span} step slider move alone"
print(f"   +/-{span} steps on the slider, pan and tilt at 0  OK")

# Uncalibrated: nothing goes out, and the zones say so before they are pressed.
mmgr.st.slider_min = mmgr.st.slider_max = None
ov._sync_runs()
assert ov._track._runs_enabled is False, \
    "the run zones stay live with no limits — pressing one sends a move that " \
    "the\n    Teensy has nothing to clamp"
mmgr.sent.clear(); ov._on_run(+1)
assert mmgr.sent == [], f"a run went out with no limits set: {mmgr.sent}"
print("   nothing sent, and drawn dead, with no limits      OK")

# And it comes back on its own when the limits arrive — the operator can open
# the panel before the first STATUS packet lands.
mmgr.st.slider_min, mmgr.st.slider_max = 0, 100000
mmgr.mount_status_updated.emit(1)
assert ov._track._runs_enabled is True, \
    "limits arrived while the panel was open and the run zones stayed grey"
print("   live again the moment the limits arrive           OK")

# ---- 9. and the whole track follows "has slider" ---------------------------
# The panel keeps its shape either way: greyed, not hidden, so the dial and the
# zoom column do not move about depending on which camera is selected.
print("\n9. a mount with no slider:")
mmgr.st.has_slider = False
ov._sync_runs()
assert ov._track._enabled is False, \
    "the track stays live on a mount with no slider — every press would go to an\n" \
    "    axis that is not there"
mmgr.sent.clear()
press(ov._track, ov._track.width() * 0.5, ov._track.height() * 0.5)
assert mmgr.sent == [], f"a nudge went out to a mount with no slider: {mmgr.sent}"
print("   greyed, and nothing goes out                      OK")

mmgr.st.has_slider = True
ov._sync_runs()
assert ov._track._enabled is True, "the track never returns when a slider does"
print("   and back when a slider does have one              OK")

# Greyed rather than hidden: the main window greys its slider dial the same way,
# and a panel that changes shape per camera is harder to use than one that does
# not.
assert "set_enabled" in (REPO / "pc_app/ui/widgets/nudge_controls.py").read_text()
assert "setVisible" not in SRC, \
    "the track is being hidden rather than greyed — the panel then changes shape\n" \
    "    depending on which camera is selected"
print("   greyed, not hidden, so the panel keeps its shape   OK")

# Nothing inside the panel may carry a raw pixel count any more — that is the
# whole point, and a new setFixedSize(48, 48) would undo it silently.
OVSRC = (REPO / "pc_app/ui/widgets/nudge_overlay.py").read_text()
build = OVSRC[OVSRC.index("    def _build(self)"):]
build = build[:build.index("\n    def ", 1)]
for pat in ("setFixedSize(", "setFixedWidth(", "setSpacing(",
            "setContentsMargins(", "px"):
    assert pat not in build, \
        f"_build() sets {pat!r} — sizes belong in _apply_scale(), which runs " \
        "again\n    whenever the panel changes size"
print("   _build() sets no sizes at all                        OK")

print("\nALL CHECKS PASSED")
