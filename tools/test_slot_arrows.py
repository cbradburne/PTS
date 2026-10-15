"""Slots 9 and 10 recall, and only recall, when look-at is off.

(In look-at mode they are ◀/▶: a look-at move with a subject, a slide without
one — tools/test_look_at_slide.py.  Neither is ever a jog.)

Those two slots are the SAME QPushButton as the look-at ◀/▶ arrows. Qt fires
`pressed` on mouse-down and `released` + `clicked` on mouse-up, so one press ran
the arrow handler and the recall handler both.

With no look-at subject selected the arrow handler used to fall back to a raw
slider jog at `vel = 1000 * direction` — full deflection, because a button has
no analogue value to send. On the rig on 2026-08-19 that put the slider at full
speed for as long as the button was held: 0.5 → 4.4 → 0.5 mm inside a single
recall, with no joystick involved.

The movement was the visible half. The damaging half was that it left the mount
in a different state each time the recall arrived — jogging or not, depending on
how long the button was held — and the firmware chooses moveTo() or
retargetTo() on exactly that state. Two presses of one slot could take different
code paths, which is what made the zoom fault look like it could not be a goto.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))
import re

SRC = (REPO / "pc_app/ui/main_window.py").read_text()
GRID = (REPO / "pc_app/ui/widgets/position_grid.py").read_text()


def code_only(text: str) -> str:
    """Strip comments and docstrings, so prose about the old behaviour is not
    mistaken for the old behaviour.  The comments here deliberately quote the
    jog that was removed, and a test that cannot tell an explanation from an
    instruction would fail on its own documentation."""
    out, in_doc = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('"""') or stripped.endswith('"""'):
            if stripped.count('"""') == 1:
                in_doc = not in_doc
            continue
        if in_doc or stripped.startswith("#"):
            continue
        out.append(line.split("  # ")[0])
    return "\n".join(out)

# ---- 1. the collision that makes this matter still exists -------------------
# If the arrows ever stop sharing a widget with the slot buttons, this whole
# file is describing a problem that no longer exists and should be revisited
# rather than left asserting something meaningless.
print("1. the buttons are still shared:")
assert "btn.pressed.connect(self._make_arrow_press" in GRID, \
    "arrows no longer bound to the slot buttons — revisit this test"
assert "btn.clicked.connect(self._make_click_handler" in GRID, \
    "slot recall no longer bound to the same button"
m = re.search(r"if not self\._has_slider\[mount_id\] or slot not in \((\d+), (\d+)\)", GRID)
assert m, "the arrow slot guard changed shape"
print(f"   slots {int(m.group(1))+1} and {int(m.group(2))+1} are also the ◀/▶ arrows  OK")

# ---- 2. no raw-jog fallback, and no arrow at all when look-at is off ---------
print("\n2. arrow press:")
body = code_only(SRC[SRC.index("def _on_slider_jog_start"):SRC.index("def _on_slider_jog_stop")])
assert "1000 * direction" not in body, \
    "the full-deflection slider jog fallback is back — slot 9/10 will jog on recall"
assert "send_jog" not in body, \
    "_on_slider_jog_start sends a jog again; it must only start moves to a rail end"
print("   sends no jog at all                                OK")

# With look-at off the press must not reach the arrow handler at all: since
# 2026-10-15 a press with no subject starts a slide, so "no subject" can no
# longer stand for "look-at is off".  The grid decides, from what it shows.
press = code_only(GRID[GRID.index("def _make_arrow_press"):GRID.index("def _make_arrow_release")])
assert "if not self._look_at_mode[mount_id]:" in press and \
       press.index("if not self._look_at_mode[mount_id]:") < press.index("slider_jog_started.emit"), \
    "slot 9/10 presses reach the arrow handler with look-at off — a recall would also slide"
print("   with look-at off, slots 9/10 only recall           OK")

# In look-at mode: a subject → a look-at move; none → a slide.
assert "send_start_look_at_move" in body, "the look-at move was removed too"
assert re.search(r"if subj < 0:", body) and "send_start_slide" in body, \
    "with no subject the arrows do not slide"
print("   a subject → look-at move; none → a slide            OK")

# ---- 3. the release handler stops sending a pointless packet ----------------
stop = code_only(SRC[SRC.index("def _on_slider_jog_stop"):])
stop = stop[:stop.index("\n    @")] if "\n    @" in stop else stop[:2000]
assert "send_jog" not in stop, \
    "the release still sends a zero jog; nothing was started, so nothing needs stopping"
print("\n3. arrow release sends nothing either                 OK")

# ---- 4. the docstrings must not describe the old behaviour ------------------
# A comment that says the opposite of the code is worse than no comment: this
# one would send the next reader looking for a jog that is not there.
assert "otherwise fall back to raw slider jog" not in SRC, \
    "a docstring still promises the raw-jog fallback"
print("4. no docstring still promises the old fallback       OK")

print("\nALL CHECKS PASSED")
