"""The CV window draws its overlay on the people, and its target where the loop aims.

2026-10-01, the operator's screenshot from the remote desktop: the target
crosshair and the new hold box sat right of the middle while the mount aimed
at the middle — and the blue detection boxes sat 56 px right of the people in
them.  The overlay was painted onto the picture with the label's side margin
added (the window wider than the 16:9 picture), so everything drawn moved by
that margin; clicks, mapped correctly, landed off what was drawn.  And the
crosshair sat at the vertical middle while the loop aimed a third of the way
down: the label never knew the loop's default.

WHAT THIS TEST IS PROTECTING.

  drawn on the people    in a window wider than the picture and in one taller,
                         a detection box is drawn exactly where the person is in
                         the picture, and a click on what is drawn picks them
  the target is the aim  the crosshair starts at the loop's default (centred, a
                         third of the way down), follows a Set Target click and
                         stays there; a new loop (the feed restarted) is given
                         the operator's target, not its own default
  opens in the middle    centred across the main window the first time it is
                         shown; after that, wherever the operator moved it

Run directly, or via tools/run_tests.sh with the rest.
"""
import os
import pathlib
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

import numpy as np                                              # noqa: E402
from PyQt6.QtCore import QEvent, QPointF, Qt                    # noqa: E402
from PyQt6.QtGui import QMouseEvent                             # noqa: E402
from PyQt6.QtWidgets import QApplication                        # noqa: E402

app = QApplication.instance() or QApplication([])

import cv.capture                                               # noqa: E402
from ui.cv_window import VideoLabel, CVWindow                   # noqa: E402

# The window lists capture devices as it is built; a test leaves cameras alone.
cv.capture.CaptureSource.list_devices = staticmethod(lambda max_test=5: [])

FW, FH = 1280, 720
PERSON = (320, 180, 160, 360)                 # frame px: x, y, w, h
black = np.zeros((FH, FW, 3), np.uint8)


def blue(c):
    return c.blue() > 150 and c.red() < 160


def blue_near(img, x, y, axis):
    """A detection-box pixel within a pixel of (x, y), along the given axis."""
    for d in (-1, 0, 1, 2):
        px, py = (x + d, y) if axis == "x" else (x, y + d)
        if blue(img.pixelColor(px, py)):
            return True
    return False


# ---- 1. boxes drawn on the people, whatever the window's shape ------------------
print("1. boxes drawn on the people:")
for label_size, what in (((1000, 400), "wider than the picture"),
                         ((700, 700), "taller than the picture")):
    lbl = VideoLabel()
    lbl.resize(*label_size)
    lbl.set_detection_mode(True)
    lbl.set_detections([PERSON])
    lbl.set_frame(black)
    img = lbl.pixmap().toImage()
    s = img.width() / FW                       # the picture as scaled into the label
    off_x, off_y = (lbl.width() - img.width()) // 2, (lbl.height() - img.height()) // 2
    assert (off_x > 0) == (what.startswith("wider")) and (off_y > 0) == (what.startswith("taller")), \
        f"{what}: the label came out {lbl.width()}x{lbl.height()} for a {img.width()}x" \
        f"{img.height()} picture, so this case proves nothing"
    x, y, w, h = PERSON
    mid_y, mid_x = int((y + h / 2) * s), int((x + w / 2) * s)
    assert blue_near(img, int(x * s), mid_y, "x"), \
        f"{what}: the box's left edge is not at the person's left side ({int(x * s)}, {mid_y})"
    assert blue_near(img, mid_x, int(y * s), "y"), \
        f"{what}: the box's top edge is not at the person's top ({mid_x}, {int(y * s)})"
    if off_x:
        assert not blue_near(img, int(x * s) + off_x, mid_y, "x"), \
            f"{what}: the box was drawn {off_x} px right of the person — the window's margin"
    if off_y:
        assert not blue_near(img, mid_x, int(y * s) + off_y, "y"), \
            f"{what}: the box was drawn {off_y} px below the person — the window's margin"
    # ...and a click on the box as drawn picks the person.
    fx, fy = lbl._screen_to_frame(off_x + mid_x, off_y + mid_y)
    assert lbl._hit_test(fx, fy) == PERSON, \
        f"{what}: a click on the middle of the drawn box missed the person ({fx:.0f}, {fy:.0f})"
print("   wider and taller windows: drawn on the person, and a click on it picks them   OK")

# ---- 2. the crosshair is where the loop aims ------------------------------------
# A 1080-line picture, so the default has to follow the frame (a sixth of its
# height above the middle: 180 px), not sit where a 720-line one would put it.
print("\n2. the target:")
W2, H2 = 1920, 1080
black2 = np.zeros((H2, W2, 3), np.uint8)
lbl = VideoLabel()
lbl.resize(1000, 400)
lbl.set_detection_mode(True)
lbl.set_frame(black2)
img = lbl.pixmap().toImage()
s = img.width() / W2
assert (lbl._target_cx, lbl._target_cy) == (0.0, -H2 / 6.0), \
    f"the crosshair starts at {(lbl._target_cx, lbl._target_cy)}, not the loop's default " \
    f"(0, {-H2 / 6.0:.0f}) for this picture — not where the mount aims"
tx, ty = int(W2 / 2 * s), int((H2 / 2 - H2 / 6.0) * s)
c = img.pixelColor(tx + 16, ty)
assert c.red() > 180 and c.blue() < 90, \
    f"no orange crosshair arm at ({tx + 16}, {ty}), where the loop's target is drawn: {c.name()}"
# Set Target: a click moves it, and the next frame does not put it back.
lbl.set_move_target_mode(True)
off_x = (lbl.width() - img.width()) // 2
click = QPointF(off_x + 100, 150)
lbl.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, click, click,
                                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                                Qt.KeyboardModifier.NoModifier))
moved = (lbl._target_cx, lbl._target_cy)
assert abs(moved[0] - (100 / s - W2 / 2)) < 3 and abs(moved[1] - (150 / s - H2 / 2)) < 3, \
    f"a Set Target click at ({100 / s:.0f}, {150 / s:.0f}) in the picture put the target at {moved}"
lbl.set_frame(black2)
assert (lbl._target_cx, lbl._target_cy) == moved, \
    "the next frame put the operator's target back to the default"
print("   starts at the loop's default for the picture; a Set Target click moves it, and\n"
      "   it stays   OK")


# ---- 3. a restarted feed keeps the operator's target ---------------------------
print("\n3. a new loop:")
from config.mount_config import AppConfig                       # noqa: E402


class St:
    active_pt_preset = 2


class FakeMM:
    def state(self, m):
        return St()

    def send_jog(self, *a, **k):
        pass


class NoYolo:
    available, _imgsz, model_name = False, 320, "none"

    def detect(self, frame):
        return []


win = CVWindow(1, FakeMM(), AppConfig())
win._video.set_target(50.0, 30.0)             # the operator moved it, before this loop
win._starting = True
win._on_feed_opened(True, NoYolo())
lp = win._tracking_loop
assert (lp._target_cx, lp._target_cy, lp._target_set) == (50.0, 30.0, True), \
    f"the new loop aims at {(lp._target_cx, lp._target_cy)}, not the operator's (50, 30) — " \
    "its own default, while the crosshair stayed where the operator put it"
win._stop_feed()
win.close()
print("   given the operator's target, not its own default   OK")

# ---- 4. opens centred across the main window -----------------------------------
# 2026-10-01, the operator: left to itself, Windows opened it off to one side.
print("\n4. where it opens:")
from PyQt6.QtWidgets import QWidget                             # noqa: E402

main = QWidget()
main.setGeometry(100, 50, 1600, 900)
main.show()
win2 = CVWindow(1, FakeMM(), AppConfig(), main)
win2.move(main.x(), main.y() + 40)            # the platform's choice: hard left, say
win2.show()
app.processEvents()
mid_main = main.frameGeometry().center().x()
mid_win = win2.frameGeometry().center().x()
assert abs(mid_win - mid_main) <= 2, \
    f"the CV window opened with its middle at x={mid_win}; the main window's is at {mid_main}"
assert win2.y() == main.y() + 40, f"the window's height on screen changed, to y={win2.y()}"
parked = main.x() + 10
assert abs(parked - win2.x()) > 100, "the operator's spot is too near the middle to prove anything"
win2.move(parked, win2.y())                   # the operator moves it...
win2.hide()
win2.show()                                   # ...and it is shown again
app.processEvents()
assert win2.x() == parked, \
    f"shown again, the window jumped from where the operator put it (x={parked}) to x={win2.x()}"
win2.close()
main.close()
print("   centred across the main window; after that, where the operator puts it   OK")

print("\nALL CHECKS PASSED")
