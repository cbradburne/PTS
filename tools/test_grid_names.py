"""A position's name shows on its button whether or not a point is stored there.

The operator, 2026-10-08: "please keep the set name visible even if there's no
location set, or even if the mount is off.  I don't like the surprise of seeing
the wrong name appear when I've set a location with a set name.  I'd rather
keep the name visible so I know name I'm storing the point to."

The grid showed a slot's name only once the mount reported a position stored
there, and a mount that is off reports none, so a named slot read "4" until a
point was stored in it — and then a name appeared.  Renaming an empty slot
opened on a blank box, whatever its name was.

This builds the real PositionGrid and reads what its buttons say:
  1. a named slot shows its name: empty, stored, and on a mount that is off;
  2. a slot with no name shows its number, and look-at's ◀/▶ stay arrows;
  3. renaming offers the name it has, stored position or not.

Run directly, or via tools/run_tests.sh.
"""
import os, sys, pathlib
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

try:
    from PyQt6.QtWidgets import QApplication
except ImportError:
    print("PyQt6 not installed: the grid needs it, skipping.")
    print("\nALL CHECKS PASSED")
    sys.exit(0)
app = QApplication.instance() or QApplication([])

from config.position_store import PositionStore
from ui import virtual_keyboard
from ui.widgets.position_grid import PositionGrid, MODE_LABEL_EDIT

store = PositionStore()
grid = PositionGrid(store)


def text(mid, slot):
    return grid._buttons[(mid, slot)].text()


# ---- 1. a named slot shows its name ----------------------------------------------
print("1. a named slot shows its name:")
store.set_label(2, 0, "Lectern")
store.set_label(2, 3, "Font")                        # named, nothing stored yet
grid.set_mount_connected(2, True)
grid.update_slot_masks(2, 0b0001, 0)                 # STATUS: only position 1 stored
assert text(2, 3) == "Font", f"an empty named slot reads {text(2, 3)!r}, not its name"
print("   named, nothing stored there: its name                         OK")

grid.update_slot_masks(2, 0b1001, 0)                 # a point stored at "Font"
assert (text(2, 0), text(2, 3)) == ("Lectern", "Font"), (text(2, 0), text(2, 3))
print("   stored there: the same name                                   OK")

# Off: what main_window does when a mount stops answering — and then a STATUS
# the hub relays anyway, which redraws the row while it is off.
grid.update_slot_masks(2, 0, 0)
grid.set_mount_connected(2, False)
grid.update_slot_masks(2, 0, 0)
assert (text(2, 0), text(2, 3)) == ("Lectern", "Font"), \
    f"a mount that is off shows {(text(2, 0), text(2, 3))}, not its names"
print("   the mount off: still its names                                OK")

# A name arriving from the hub (another device renamed it) on an empty slot.
store.set_label(3, 6, "Band")
for s in range(10):                                  # as _reload_names_ui does
    grid.refresh_button(3, s)
assert text(3, 6) == "Band", f"a name from the hub reads {text(3, 6)!r} on an empty slot"
print("   a name from another device, on an empty slot: shown           OK")

# ---- 2. what has no name -----------------------------------------------------------
print("\n2. what has no name:")
assert text(2, 4) == "5" and text(3, 0) == "1", (text(2, 4), text(3, 0))
print("   a slot with no name shows its number                          OK")

grid.set_mount_connected(4, True)
grid.set_has_slider(4, True)
grid.set_look_at_mode(4, True)
store.set_label(4, 1, "Altar")
grid.refresh_row_labels(4)
assert (text(4, 1), text(4, 8), text(4, 9)) == ("Altar", "◀", "▶"), \
    (text(4, 1), text(4, 8), text(4, 9))
grid.update_slot_masks(4, 0, 0)                      # no subject stored at "Altar"
assert (text(4, 1), text(4, 8), text(4, 9)) == ("Altar", "◀", "▶"), \
    (text(4, 1), text(4, 8), text(4, 9))
print("   look-at: subjects keep their names, 9 and 10 stay ◀ and ▶     OK")

# ---- 3. renaming ---------------------------------------------------------------------
print("\n3. renaming offers the name it has:")
offered = []


def fake_get_text(parent, title, prompt, text=""):
    offered.append(text)
    return "", False                                 # Cancel: change nothing


virtual_keyboard.get_text = fake_get_text
grid.set_mount_connected(2, True)
grid.update_slot_masks(2, 0b0001, 0)                 # "Font" empty again
grid.set_mode(MODE_LABEL_EDIT)
grid._buttons[(2, 3)].click()                        # empty, named "Font"
grid._buttons[(2, 0)].click()                        # stored, named "Lectern"
assert offered == ["Font", "Lectern"], f"the rename box offered {offered}"
print("   empty or stored, the box opens on the name the button shows    OK")

print("\nALL CHECKS PASSED")
