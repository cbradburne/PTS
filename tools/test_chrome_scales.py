"""The top and bottom bars scale like the grid between them.

The position grid was made resolution-independent first; the chrome around it
was left as it was — 60px buttons and 28px type, right on the 1920x1080 screen
it was drawn against and progressively smaller on anything with more logical
pixels. On a 4K display the grid would have scaled and the bars would not,
which is worse than both being wrong together.

The bars are simpler than the grid: the window is full screen and never
changes size, so the scale is read once at build time from the screen's height
rather than recomputed on resize. Re-applying forty stylesheets to handle a
case that cannot happen is cost without benefit.

The transform is applied to the STYLE HELPERS rather than at each
setStyleSheet() call, because those run again on every state change — active
and inactive, armed and idle — and one missed would resize a button the moment
it was pressed.

The arithmetic now lives in ui/ui_scale.py, because the Move panel needs the
same number and main_window is what builds it. main_window keeps the private
aliases, so everything below still reads MW._px.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

from PyQt6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])

import ui.main_window as MW
import ui.ui_scale as SCALE

SRC = (REPO / "pc_app/ui/main_window.py").read_text()
SCALE_SRC = (REPO / "pc_app/ui/ui_scale.py").read_text()
LINES = SRC.splitlines()

STYLES = [MW._cam_btn_style, MW._clear_btn_style, MW._action_btn_style,
          MW._edit_btn_style, MW._move_btn_style, MW._run_btn_style,
          MW._run_cam_active_style, MW._clear_mode_btn_style,
          MW._clear_cam_btn_style, MW._set_btn_style]


def sample_sheets():
    """One sheet from every helper, in both of its states where it has two."""
    out = []
    for fn in STYLES:
        n = fn.__wrapped__.__code__.co_argcount
        for arg in ((1, True), (1, False)) if n == 2 else ((True,), (False,)) if n == 1 else ((),):
            try:
                out.append(fn(*arg))
            except Exception:
                pass
    return out


# ---- 0. the call the app actually makes ------------------------------------
# Every other check here passes an explicit height, which skips the screen
# lookup entirely — so the one path the app takes was the one path untested,
# and it shipped with QApplication unimported. NameError at startup, on a
# machine that had already pulled.
print("0. _init_ui_scale() with no argument:")
s0 = MW._init_ui_scale()
assert isinstance(s0, float) and 0.65 <= s0 <= 3.0, \
    f"reading the scale from the screen returned {s0!r}"
assert MW._px(60) > 0, "the scale is unusable after reading from the screen"
print(f"   reads the screen and returns {s0:.2f}                   OK")

# The module must carry everything that path needs, not rely on the caller
# having imported it. This test imports QApplication itself, which is exactly
# how the missing import stayed hidden.
imports = SCALE_SRC[:SCALE_SRC.index("def init_ui_scale")]
assert "QApplication" in imports, \
    "ui_scale does not IMPORT QApplication, though init_ui_scale() calls it.\n" \
    "    Matching anywhere in the file would pass on the call itself, which is\n" \
    "    the thing that raised NameError."
print("   QApplication imported by the module itself           OK")

# The aliases in main_window have to be the live functions, not a snapshot: a
# `from ui_scale import _SCALE` anywhere would freeze at whatever it was when
# the import ran, and every scaled size would come out at 1.0 forever.
MW._init_ui_scale(2160)
assert MW._px(60) == 120, \
    f"main_window's _px gives {MW._px(60)} at 2160 — its alias is not live"
print("   main_window's aliases track the shared number        OK")

# ---- 1. the reference screen is untouched ----------------------------------
print("1. the screen it was designed on:")
MW._init_ui_scale(1080)
assert SCALE.scale() == 1.0, f"scale at 1080 is {SCALE.scale()}, must be exactly 1"
assert MW._px(60) == 60 and MW._px(28) == 28, "the reference sizes have moved"
sizes = set(int(x) for s in sample_sheets() for x in re.findall(r"font-size: ?(\d+)px", s))
assert 28 in sizes, f"the bar type is no longer 28px at 1080: {sorted(sizes)}"
print("   1920x1080: 60px buttons, 28px type — exactly as before   OK")

# ---- 2. and everything moves together elsewhere ----------------------------
print("\n2. other screens:")
for h, want_btn in ((1440, 80), (2160, 120), (768, 43)):
    MW._init_ui_scale(h)
    assert MW._px(60) == want_btn, \
        f"a {h}px screen gives a {MW._px(60)}px button, expected {want_btn}"
    fonts = set(int(x) for s in sample_sheets()
                for x in re.findall(r"font-size: ?(\d+)px", s))
    scaled = round(28 * h / 1080)
    assert scaled in fonts, \
        f"a {h}px screen has fonts {sorted(fonts)}; 28px should have become {scaled}"
    print(f"   {h:>4}px screen: {MW._px(60):>3}px buttons, {scaled:>2}px type")
print("   button and type scale together                       OK")

# ---- 3. nothing is left hardcoded ------------------------------------------
print("\n3. what is left in pixels:")
for pat, what in ((r"setFixedHeight\(\d", "setFixedHeight"),
                  (r"setFixedWidth\(\d", "setFixedWidth"),
                  (r"setMinimumWidth\(\d", "setMinimumWidth"),
                  (r"setSpacing\(\d", "setSpacing"),
                  (r"setContentsMargins\(\d", "setContentsMargins")):
    n = len(re.findall(pat, SRC))
    assert n == 0, f"{n} {what}() calls still take a raw pixel count"
print("   no raw sizes, spacings or margins                    OK")

# Every stylesheet carrying a px must go through _qss(), directly or via a
# decorated helper. A missed one changes size the moment its state changes.
missed = []
for i, line in enumerate(LINES, 1):
    if "setStyleSheet(" not in line:
        continue
    blob = "\n".join(LINES[i - 1:i + 8])
    if not re.search(r"\d+px", blob):
        continue
    if "_qss(" in blob or "_style(" in line:
        continue
    missed.append(i)
assert not missed, \
    f"stylesheets at lines {missed} carry pixel sizes without _qss(); they would\n" \
    "    keep their original size and jump when the widget changes state"
print("   every stylesheet with a px goes through _qss()       OK")

# ---- 4. the helpers are wrapped, not the call sites -------------------------
print("\n4. where the transform is applied:")
for fn in STYLES:
    assert hasattr(fn, "__wrapped__"), \
        f"{fn.__name__} is not decorated, so the state it returns is unscaled"
print(f"   all {len(STYLES)} style helpers decorated               OK")

# ---- 5. it is read before anything is built --------------------------------
print("\n5. when the scale is read:")
build = SRC[SRC.index("    def _build(self) -> None:"):]
build = build[:build.index("\n    def ", 1)]
assert "_init_ui_scale()" in build, "the scale is never initialised from the screen"
assert build.index("_init_ui_scale()") < build.index("QWidget()"), \
    "the scale is read after widgets are made, so the first ones built use 1.0"
print("   first thing in _build(), before any widget exists    OK")

# ---- 6. and it cannot run away ---------------------------------------------
print("\n6. the clamps:")
MW._init_ui_scale(200)
assert SCALE.scale() >= 0.65, "a tiny screen scales below usability"
lo = MW._px(60)
MW._init_ui_scale(8000)
assert SCALE.scale() <= 3.0, "a wall-sized display scales without limit"
print(f"   200px screen -> {lo}px buttons, 8000px -> {MW._px(60)}px    OK")

MW._init_ui_scale(1080)   # leave it as the rest of the suite expects
print("\nALL CHECKS PASSED")
