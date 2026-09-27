"""Camera Control — Advanced: its Cam 1-5 row sits exactly under the main window's.

2026-09-27, the operator's screenshot (a 1920x1080 screen): the Advanced panel
opens straight below the main window's Cam 1-5 buttons, and its own row did
not match them — 12 px between buttons against the main row's 6, 58 px high
against 60, smaller type, and centred on the Camera Control window rather than
under the row, so Cam 1 sat about 32 px left of the Cam 1 above it.

WHAT THIS TEST IS PROTECTING.

  drawn like the main row      px(60) high, at least px(150) wide, the main
                               row's gap, its type and padding — before any
                               main window is measured, and at another scale
  lined up with the real one   each button the size of the one above it (a
                               long camera name makes its button wider), the
                               same gaps, and the dialog moved sideways until
                               the rows share their edges — the main window
                               found through Camera Control, its parent
  only sideways, only on       where it opens vertically is untouched, and a
  screen                       row near the screen's edge does not push it off
  the real row                 the row above is MainWindow's own
                               _build_cam_selector, measured by its own
                               cam_selector_rects()

A 1920x1080 offscreen screen, as on the rig: the default one is 800x800, where
every dialog would be held on screen and nothing here would be tested.

Run directly, or via tools/run_tests.sh with the rest.
"""
import json
import os
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
_cfg = pathlib.Path(tempfile.mkdtemp()) / "screen.json"
_cfg.write_text(json.dumps({"screens": [{
    "name": "rig", "x": 0, "y": 0, "width": 1920, "height": 1080,
    "logicalDpi": 96, "logicalBaseDpi": 96, "dpr": 1}]}))
os.environ["QT_QPA_PLATFORM"] = f"offscreen:configfile={_cfg}"
sys.path.insert(0, str(REPO / "pc_app"))

from PyQt6.QtCore import QPoint, QRect
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialog, QWidget

app = QApplication.instance() or QApplication([])
scr = app.primaryScreen().availableGeometry()
assert (scr.width(), scr.height()) == (1920, 1080), f"the test screen is {scr}, not 1920x1080"

import ui.ui_scale as S
S.init_ui_scale()                                  # 1080 high: scale 1.0
import ui.main_window as mw
from ui.dialogs.camera_advanced_dialog import CameraAdvancedDialog


class FakeMM:
    def __init__(self):
        class _Sig:
            def connect(self, *a): pass
        self.cam_status_received = _Sig()
    def state(self, m): return type("St", (), {"cam_wb": None, "cam_tint": None, "cam_adv": {}})()
    def cam_ble_state(self, m): return True
    def __getattr__(self, name):
        if name.startswith("send_cam_"):
            return lambda *a, **k: None
        raise AttributeError(name)


def on_screen(w) -> QRect:
    return QRect(w.mapToGlobal(QPoint(0, 0)), w.size())


def main_window(row_x=None, gap=10, height=64):
    """A main window whose Cam 1-5 row is MainWindow's own — then given a gap
    and a height of its own, so the dialog has to copy them, not happen to
    share them."""
    m = QWidget()
    m.setGeometry(0, 0, 1920, 1080)
    m._config = type("C", (), {"mount_label": lambda self, i:
                               "Balcony Wide Shot" if i == 2 else f"Cam {i}"})()
    m._active_mount = 1
    m._make_cam_select = lambda mid: (lambda *a: None)
    row = mw.MainWindow._build_cam_selector(m)
    row.setParent(m)
    row.layout().setSpacing(gap)
    for b in m._cam_btns:
        b.setFixedHeight(height)
    row.adjustSize()
    row.move((1920 - row.width()) // 2 if row_x is None else row_x, 6)
    m.cam_selector_rects = lambda: mw.MainWindow.cam_selector_rects(m)
    m.show()
    QTest.qWait(30)
    return m


def advanced(main, at=(237, 72)):
    """Opened as the app opens it: from Camera Control, parented to it, and
    placed wherever the window manager puts it."""
    cc = QDialog(main)
    cc.setGeometry(300, 300, 900, 500)
    cc.show()
    d = CameraAdvancedDialog(FakeMM(), cc, mount_id=1)
    d.move(*at)
    d.show()
    QTest.qWait(80)                 # its line-up runs once it has been shown
    return d


# ---- 1. drawn like the main row, before anything is measured ------------------
print("1. with no main window to measure:")
d0 = CameraAdvancedDialog(FakeMM(), None, mount_id=1)
d0.show(); QTest.qWait(50)
b0 = [d0._cam_btns[i] for i in range(1, 6)]
assert all(b.height() == S.px(60) for b in b0), \
    f"the buttons are {[b.height() for b in b0]} high, not the main row's {S.px(60)}"
assert all(b.width() >= S.px(150) for b in b0), "a button is narrower than the main row's minimum"
g0 = [on_screen(b0[i + 1]).left() - on_screen(b0[i]).right() - 1 for i in range(4)]
assert g0 == [S.px(4) + 2 * S.px(0)] * 4, \
    f"the gaps are {g0}, not the main row's {S.px(4) + 2 * S.px(0)} (px(4) between " \
    "containers that each add px(0) a side)"
css = b0[0].styleSheet()
assert "font-size: 28px" in css and "padding: 6px 14px" in css and "border-radius: 20px" in css, \
    f"the buttons are not styled as the main row's _cam_btn_style: {css}"
d0.close()
S.init_ui_scale(1440)
d1 = CameraAdvancedDialog(FakeMM(), None, mount_id=1)
assert d1._cam_btns[1].height() == S.px(60) == 80 and "font-size: 37px" in d1._cam_btns[1].styleSheet(), \
    "on a bigger screen the picker does not scale with the main row"
d1.close()
S.init_ui_scale()
print(f"   {S.px(60)} px high, {S.px(4) + 2 * S.px(0)} px apart, 28 px type, and scaled with the main row   OK")

# ---- 2. lined up under the real row ------------------------------------------
print("\n2. opened under the main window's row:")
m = main_window()
rects = m.cam_selector_rects()
assert rects[1].width() > S.px(150) + 20, \
    "the long name did not widen its button — the case this has to copy is not being made"
main_gaps = {rects[i + 1].left() - rects[i].right() - 1 for i in range(4)}
d = advanced(m)
got = [on_screen(d._cam_btns[i]) for i in range(1, 6)]
for i, (a, b) in enumerate(zip(got, rects), start=1):
    assert abs(a.left() - b.left()) <= 1 and abs(a.right() - b.right()) <= 1, \
        f"Cam {i} spans {a.left()}-{a.right()} under a Cam {i} at {b.left()}-{b.right()}"
    assert a.height() == b.height(), f"Cam {i} is {a.height()} high under one {b.height()} high"
print(f"   every button edge within 1 px of the one above; gaps {sorted(main_gaps)} px as above;\n"
      f"   Cam 2 {rects[1].width()} px wide for its name, as above   OK")
assert d.y() == 72, f"the dialog moved vertically, to y={d.y()} — only sideways was asked for"
print("   moved sideways only                                         OK")
d.close()

# ---- 3. never pushed off the screen -------------------------------------------
print("\n3. a row at the screen's edge:")
m2 = main_window(row_x=0)
d2 = advanced(m2, at=(400, 72))
fg = d2.frameGeometry()
assert fg.left() >= scr.left() and fg.right() <= scr.right(), \
    f"lining up pushed the dialog off the screen: {fg} on {scr}"
print(f"   held on screen at x={fg.left()} rather than lined up off it   OK")
d2.close()

print("\nALL CHECKS PASSED")
