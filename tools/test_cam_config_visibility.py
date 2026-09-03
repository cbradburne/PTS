"""The camera tab only asks questions the hardware has answers to.

A mount with no slider has no slider StallGuard threshold to set. A zoom driven
over LANC is a serial command to the camera, not a stepper — it has no speed, no
acceleration, and nothing for StallGuard to measure. Leaving those controls on
screen invites a value that will never be used and implies the axis exists.

The slider SPEED presets already followed the slider checkbox; the slider
threshold did not, and neither of the zoom controls followed the LANC checkbox.

Driven rather than read: the dialog is built, the checkboxes are toggled through
all four combinations, and the widgets are asked whether they are hidden. A rule
written into the source and never exercised is how the slider threshold came to
be the odd one out.

isHidden(), not isVisible(): these live on a QTabWidget page that is not the
current one, so isVisible() is False for every one of them regardless.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

from PyQt6.QtWidgets import QApplication, QGroupBox
from PyQt6.QtCore import QObject, pyqtSignal
app = QApplication.instance() or QApplication([])

from config.mount_config import load_config
from ui.dialogs.config_dialog import ConfigDialog


class _Any:
    """Callable and connectable — enough of a MountManager for a dialog."""
    def __call__(self, *a, **k):  return None
    def connect(self, *a, **k):   return None
    def disconnect(self, *a, **k): return None
    def emit(self, *a, **k):      return None


class _St:
    last_config_report = None
    active_pt_preset = 2; active_sl_preset = 2
    slider_min = None; slider_max = None; connected = False


class _MM(QObject):
    limits_found           = pyqtSignal(int, int, int, int)
    config_report_received = pyqtSignal(int, object)
    mount_status_updated   = pyqtSignal(int)
    def state(self, m):    return _St()
    def mount_table(self): return [b"\x00" * 6] * 5
    def sat_names(self):   return {}
    def __getattr__(self, n): return _Any()


mm = _MM(); mm.mount_route = [0] * 5
DLG = ConfigDialog(load_config(), mm, _MM())
app.processEvents()


def box_of(w):
    """The group box this widget actually lives in — not the first one with a
    matching title, of which there are five, one per camera tab."""
    while w is not None and not isinstance(w, QGroupBox):
        w = w.parent()
    assert w is not None, "widget is not inside a QGroupBox"
    return w


def shown(w):
    return not w.isHidden()


# ---- every camera tab, every combination -----------------------------------
WANT = {
    # (has_slider, lanc_zoom): (sl speeds, zoom preset, sg box, sg slider, sg zoom)
    (True,  False): (True,  True,  True,  True,  True),
    (True,  True):  (True,  False, True,  True,  False),
    (False, False): (False, True,  True,  False, True),
    (False, True):  (False, False, False, False, False),
}
NAMES = ("slider speeds", "zoom preset", "StallGuard box",
         "StallGuard slider", "StallGuard zoom")

print("1. what each camera tab shows:")
print(f"   {'slider':>6} {'lanc':>5} | " +
      " ".join(f"{n:>17}" for n in NAMES))
for mount_id in range(1, 6):
    key       = f"m{mount_id}"
    slider_cb = getattr(DLG, f"_{key}_has_slider")
    lanc_cb   = getattr(DLG, f"_{key}_lanc_zoom")
    sg_slider = getattr(DLG, f"_{key}_sg_slider")
    sg_zoom   = getattr(DLG, f"_{key}_sg_zoom")
    sl_box    = box_of(getattr(DLG, f"_{key}_sl_rows")[0][0])
    zm_box    = box_of(getattr(DLG, f"_{key}_zm_spd"))
    sg_box    = box_of(sg_slider)

    for (has_slider, lanc), want in WANT.items():
        slider_cb.setChecked(has_slider)
        lanc_cb.setChecked(lanc)
        app.processEvents()
        got = (shown(sl_box), shown(zm_box), shown(sg_box),
               shown(sg_slider), shown(sg_zoom))
        for name, g, w in zip(NAMES, got, want):
            assert g == w, (
                f"cam{mount_id} with has_slider={has_slider}, lanc_zoom={lanc}: "
                f"{name} is {'shown' if g else 'hidden'},\n"
                f"    expected {'shown' if w else 'hidden'}. Full row {got} vs {want}.")
        if mount_id == 1:
            print(f"   {str(has_slider):>6} {str(lanc):>5} | " +
                  " ".join(f"{('shown' if g else 'hidden'):>17}" for g in got))
print("   all 5 tabs, all 4 combinations                    OK")

# ---- and the box goes when it has nothing left in it ------------------------
# A group box titled "StallGuard Thresholds" with both rows hidden is a heading
# over an empty space, which reads as a bug rather than as an absence.
print("\n2. the empty case:")
assert WANT[(False, True)][2] is False, "the test no longer checks the empty box"
print("   no slider and a LANC zoom: the box goes entirely   OK")

# ---- the ordering trap that is already in this file -------------------------
# setVisible on a widget with no parent yet does not "hide it later" — it shows
# it as a TOP-LEVEL WINDOW. Five camera tabs made that a burst of stray windows
# during construction, which on macOS take the operator to another Space. Every
# visibility call has to come after the widget is in a layout.
print("\n3. built before hidden:")
SRC = (REPO / "pc_app/ui/dialogs/config_dialog.py").read_text()
for box, marker in (("sl_box", "sl_box.setVisible(mc.has_slider)"),
                    ("zm_box", "zm_box.setVisible(not mc.lanc_zoom)")):
    add = SRC.index(f"layout.addWidget({box})")
    assert SRC.index(marker) > add, \
        f"{box} is hidden before it is added to a layout — that shows it as a\n" \
        "    top-level window instead, which is the stray-window bug"
assert SRC.index("layout.addWidget(sg_box)") < SRC.index("_sg_rows()"), \
    "the StallGuard box is hidden before it is added to a layout"
print("   every setVisible comes after its addWidget         OK")

print("\nALL CHECKS PASSED")
