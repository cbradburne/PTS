"""The position grid looks the same shape on every screen.

Two screenshots of the same app, 2026-08-27: one on Windows at work, one on
macOS at home. The Windows one had "better button size and spacing"; the macOS
one looked sparse, with small buttons adrift in too much space.

Neither machine was wrong. The app runs full screen and every dimension in the
grid was a hardcoded pixel count — 130x120 buttons, 3px spacing, 24px type —
so the grid occupied a FIXED number of pixels in a window whose size varies by
machine. On the display it was tuned against it fills the row; on one with more
logical pixels the same grid sits in a bigger space and the surplus, having
nowhere else to go, comes out as gaps.

That is also why HiDPI made no difference either way: Qt6 already reports
logical pixels, so a Retina Mac and a 1080p PC differ here only in how many
logical pixels they have, which is exactly the thing the layout ignored.

Everything is now derived from the height the grid is actually given, in the
proportions it was designed at, so the reference screen is unchanged and every
other one matches it.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

from PyQt6.QtWidgets import QApplication, QSizePolicy
app = QApplication.instance() or QApplication([])

from ui.widgets.position_grid import (PositionGrid, _BTN_H_FRAC,
                                      _BTN_ASPECT, _DIAL_FRAC)
from config.position_store import PositionStore

GRID = PositionGrid(PositionStore())
GRID.show()

# The app is full screen; the grid gets the window minus the top and bottom
# bars. Scaled with the screen rather than fixed, or the reserve is a bigger
# share of a small screen than a large one and the comparison below measures
# that instead of the layout.
def bars(h):
    return round(h * 150 / 1080)


def measure(w, h):
    GRID.resize(w, h - bars(h))
    app.processEvents()
    GRID.layout().activate()
    app.processEvents()
    btn = GRID._buttons[(1, 0)]
    row = GRID._row_containers[1]
    # How much of the row the content actually spans, first button to last
    # dial. The complaint was surplus space, so this is the number that matters
    # — not how much of it is button, which is fixed by the aspect.
    d = GRID._sl_dials[1]
    fill = ((d.x() + d.width() * 2) - btn.x()) / max(1, row.width())
    return dict(row_h=GRID._row_h, w=btn.width(), h=btn.height(),
                font=GRID._font_px, radius=GRID._radius, fill=fill,
                dial=GRID._pt_dials[1].width(),
                gap=GRID._row_layouts[1].spacing())


SCREENS = [(1920, 1080), (2560, 1440), (3840, 2160), (1366, 768), (1280, 800)]

# ---- 1. nothing is a hardcoded pixel count any more -------------------------
print("1. what the sizes come from:")
src = (REPO / "pc_app/ui/widgets/position_grid.py").read_text()
assert "def _relayout(self)" in src, "the grid has no relayout pass"
assert "resizeEvent" in src, "nothing recomputes when the window size changes"
assert "border-radius: {radius}px" in src and "font-size: {font_px}px" in src, \
    "the button stylesheet has fixed metrics again, so type and corners stay\n" \
    "    the same size while the buttons around them change"
print("   radius, type and border all derived at runtime      OK")

btn = GRID._buttons[(1, 0)]
assert btn.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Expanding, \
    "the buttons are a fixed width again — the row's surplus width then has\n" \
    "    nowhere to go but the gaps, which is the original complaint"
print("   buttons share the row width rather than fixing it   OK")

# ---- 2. the reference screen is untouched ----------------------------------
# The whole point: the machine that already looked right must not change.
print("\n2. the screen it was designed on:")
ref = measure(1920, 1080)
assert abs(ref["h"] - 120) <= 3, \
    f"button height at 1920x1080 is {ref['h']}, was 120 — the reference look moved"
assert abs(ref["w"] - 130) <= 4, \
    f"button WIDTH at 1920x1080 is {ref['w']}, was 130 — without an aspect cap\n" \
    "    the buttons absorb every spare pixel and come out stretched"
assert ref["font"] == 24, f"font is {ref['font']}px at 1920x1080, was 24"
assert ref["radius"] == 22, f"radius is {ref['radius']}px at 1920x1080, was 22"
assert abs(ref["dial"] - 90) <= 8, \
    f"the dial is {ref['dial']}px at 1920x1080, was about 90"
assert 25 <= ref["gap"] <= 45, \
    f"{ref['gap']}px between buttons at 1920x1080; the reference is about 30"
print(f"   1920x1080: {ref['w']}x{ref['h']} button, {ref['gap']}px gaps, "
      f"{ref['dial']}px dial, {ref['font']}px type — as before   OK")

# ---- 3. and every other screen is the same shape ---------------------------
print("\n3. the same proportions everywhere:")
rows = []
for w, h in SCREENS:
    m = measure(w, h)
    rows.append(((w, h), m))
    print(f"   {w:>4}x{h:<5} row {m['row_h']:>3}  btn {m['w']:>3}x{m['h']:<3}  "
          f"type {m['font']:>2}  radius {m['radius']:>2}  "
          f"btn/row {m['h']/m['row_h']:.2f}  fills {m['fill']:.0%}")

for (scr, m) in rows:
    r = m["h"] / m["row_h"]
    assert abs(r - _BTN_H_FRAC) < 0.03, \
        f"{scr}: the button is {r:.2f} of its row, not {_BTN_H_FRAC:.2f} — it no\n" \
        "    longer floats in the camera's coloured band the way it was drawn"
    assert m["fill"] > 0.92, \
        f"{scr}: the row's content spans only {m['fill']:.0%} of it. The surplus\n" \
        "    has collected somewhere instead of being spread across the gaps —\n" \
        "    a 300px hole between button 10 and the dials is how that looked."
    assert abs(m["font"] / m["h"] - 24.0 / 120.0) < 0.02, \
        f"{scr}: type is {m['font']}px in a {m['h']}px button — out of proportion"
    aspect = m["w"] / m["h"]
    assert abs(aspect - _BTN_ASPECT) < 0.06, \
        f"{scr}: the button is {aspect:.2f} wide for its height, not " \
        f"{_BTN_ASPECT:.2f} — it is being stretched to fill the row"
    assert abs(m["dial"] / m["h"] - _DIAL_FRAC) < 0.08, \
        f"{scr}: the dial is {m['dial']}px beside a {m['h']}px button. Left to\n" \
        "    itself it collapses onto its own minimum and the two sit almost\n" \
        "    touching, which is how it looked before it scaled with the row."
print("   ratio, type, aspect and dials all hold              OK")

# The thing that actually went wrong: on a bigger screen of the same shape, the
# surplus came out as gaps instead of as a bigger grid. So the buttons must
# occupy the same SHARE of the row on every 16:9 screen.
# The surplus goes into the gaps, so those scale with the button too — even
# spacing, the way the reference screen has always looked.
for (scr, m) in rows:
    assert 3 <= m["gap"] <= m["w"] * 0.36, \
        f"{scr}: {m['gap']}px between {m['w']}px buttons is not the even spacing " \
        "the surplus is meant to become"
print("   surplus becomes even spacing, not one hole          OK")

# ---- 4. it can shrink as well as grow --------------------------------------
# setFixedHeight on the buttons raises the grid's own minimum height, so once
# it had been shown large it could never come back down: a 1366x768 window kept
# the row height of the 2560x1440 one before it.
print("\n4. going back down:")
measure(3840, 2160)
back = measure(1366, 768)
# Both the button height and the dial size have to be UPPER bounds. Either one
# set as a fixed size raises the grid's own minimum height and it can never come
# back down; the dial version of this bug survived the first fix.
assert back["row_h"] < 200, \
    f"after a 4K window, 1366x768 still reports a {back['row_h']}px row — the\n" \
    "    grid's minimum height has ratcheted up and it cannot shrink again"
again = measure(1920, 1080)
assert again == ref, "the same screen size no longer produces the same layout"
print(f"   4K -> 1366x768 gives {back['row_h']}px rows, and 1920 returns  OK")

# ---- 5. a small screen stays legible ---------------------------------------
print("\n5. the floor:")
tiny = measure(1024, 600)
assert tiny["h"] >= GRID._MIN_BTN_H, "the minimum button height is not honoured"
assert tiny["font"] >= 9, "type has scaled below readability"
print(f"   1024x600 clamps at {tiny['h']}px buttons, {tiny['font']}px type  OK")

print("\nALL CHECKS PASSED")
