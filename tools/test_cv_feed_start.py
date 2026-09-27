"""Start Feed in the CV window: a spinner while it starts, and the right colours.

Between pressing Start Feed and the picture appearing, the capture device
opens and person detection loads — seconds on the M710q.  Both used to run
on the Qt thread, so the window simply froze.  A spinner cannot turn on a
frozen thread, so the real subject here is that the slow work now happens
somewhere else:

  1. Start Feed returns at once, and the spinner keeps turning while a
     deliberately slow fake device opens and a slow fake detector loads.
  2. The spinner stays until the first PICTURE, not merely until the device
     opened — the picture is what the operator is waiting for.
  3. The button: grey "Starting…" (and pressing it does nothing), red
     "Stop Feed" while running, green "Start Feed" when stopped.
  4. A device that will not open: spinner gone, button back, said so.
  5. A device that opens and sends nothing: the spinner gives up and says so,
     rather than spinning for ever.
  6. Closing the window mid-start: the start-up thread closes the device
     itself when its open returns — nothing left running behind a closed
     window.
  7. The spinner really is drawn: pixels of its colour on the video area.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))
import threading, time

import numpy as np
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QObject, pyqtSignal
app = QApplication.instance() or QApplication([])

import ui.cv_window as cw
from config.mount_config import AppConfig

OPEN_S, LOAD_S = 0.6, 0.6


class FakeCapture:
    """Slow to open, then a picture after `first_after` seconds (None: never)."""
    first_after = 0.3
    will_open = True
    closes = []

    def __init__(self):
        self.frames_grabbed = 0
        self._open = False
        self._opened_at = None

    @staticmethod
    def list_devices(max_test=5):
        return [0]

    def open(self, device_index=0, width=1280, height=720):
        time.sleep(OPEN_S)
        if not self.will_open:
            return False
        self._open = True
        self._opened_at = time.monotonic()
        return True

    def close(self):
        self._open = False
        FakeCapture.closes.append(threading.current_thread().name)

    @property
    def is_open(self):
        return self._open

    def get_frame(self):
        if not self._open or self.first_after is None:
            return None
        if time.monotonic() - self._opened_at < self.first_after:
            return None
        self.frames_grabbed += 1
        return np.zeros((720, 1280, 3), dtype=np.uint8)


class FakeDetector:
    available = True
    _imgsz = 416
    loads = 0

    def __init__(self, imgsz=416):
        time.sleep(LOAD_S)
        FakeDetector.loads += 1

    def detect(self, frame):
        return []


class _State:
    """Any mount-state field the tracking loop reads: a plausible preset."""
    def __getattr__(self, name):
        return 2


class FakeMM(QObject):
    def state(self, mount_id):
        return _State()

    def __getattr__(self, name):
        return lambda *a, **k: None


cw.CaptureSource = FakeCapture
cw.PersonDetector = FakeDetector


def pump(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


def window():
    w = cw.CVWindow(1, FakeMM(), AppConfig())
    w.resize(960, 640)
    w.show()
    app.processEvents()
    return w


def btn(w):
    return (w._feed_btn.text(), w._feed_btn.isEnabled(), w._feed_btn.styleSheet())


# ---- 1-3. the normal start ----------------------------------------------------
print("1. Start Feed with a slow device and a slow detector:")
w = window()
assert btn(w)[0] == "Start Feed" and "#1B5E20" in btn(w)[2], btn(w)
t0 = time.monotonic()
w._feed_btn.click()
took = time.monotonic() - t0
assert took < 0.15, f"Start Feed held the Qt thread for {took:.2f}s"
text, enabled, style = btn(w)
assert text == "Starting…" and not enabled, (text, enabled)
assert w._video.busy, "no spinner while starting"
a0 = w._video._busy_angle
pump(0.5)                                    # mid-open: the fake is still asleep
assert w._video._busy_angle != a0, "the spinner did not turn while the device opened"
w._feed_btn.click()                          # disabled: must do nothing
assert w._starting, "pressing Starting… changed something"
print(f"   returned in {took * 1000:.0f} ms; spinner turning while it opens; "
      f"'Starting…' greyed and inert")

pump(OPEN_S + LOAD_S)                        # open + detector done
text, enabled, style = btn(w)
assert text == "Stop Feed" and enabled and "#B71C1C" in style, (text, enabled, style)
print("2. device open: 'Stop Feed', red", end="")
pump(0.6)                                    # the first picture arrives
assert not w._video.busy, "the spinner outlived the first picture"
assert w._video.pixmap() is not None and not w._video.pixmap().isNull()
print(" — spinner gone with the first picture")

w._feed_btn.click()
text, enabled, style = btn(w)
assert text == "Start Feed" and enabled and "#1B5E20" in style, (text, enabled, style)
print("3. stopped: 'Start Feed', green")
w.close()
pump(0.2)

# ---- 4. a device that will not open ------------------------------------------
FakeCapture.will_open = False
w = window()
w._feed_btn.click()
pump(OPEN_S + 0.3)
assert not w._video.busy and btn(w)[0] == "Start Feed" and btn(w)[1]
assert "Failed to open" in w._status.text(), w._status.text()
print("4. a device that will not open: spinner gone, 'Start Feed' back, said so")
w.close()
pump(0.2)
FakeCapture.will_open = True

# ---- 5. open, but no picture ----------------------------------------------------
FakeCapture.first_after = None
cw.CVWindow.FIRST_FRAME_WAIT_MS = 400
w = window()
w._feed_btn.click()
pump(OPEN_S + LOAD_S + 0.2)
assert w._video.busy, "gave up before the wait was up"
pump(0.6)
assert not w._video.busy, "spinning for ever at a device that sends nothing"
assert "no picture" in w._status.text(), w._status.text()
print("5. open but silent: the spinner gives up after the wait, and says why")
w.close()
pump(0.2)
FakeCapture.first_after = 0.3

# ---- 6. closed mid-start ----------------------------------------------------------
FakeCapture.closes.clear()
loads = FakeDetector.loads
w = window()
w._feed_btn.click()
pump(0.1)
w.close()                                    # while the device is still opening
pump(OPEN_S + LOAD_S + 0.4)
assert FakeCapture.closes == ["cv-start"], FakeCapture.closes
assert FakeDetector.loads == loads, "loaded a detector for a window that had closed"
print("6. closed mid-start: the start-up thread closed the device itself, "
      "and skipped the detector")

# ---- 7. the spinner is drawn ------------------------------------------------------
w = window()
w._video.set_busy("Starting the feed…")
pump(0.1)
img = w._video.grab().toImage()
blue = 0
for x in range(img.width() // 2 - 40, img.width() // 2 + 40):
    for y in range(img.height() // 2 - 40, img.height() // 2 + 40):
        c = img.pixelColor(x, y)
        if c.blue() > 200 and c.green() > 150 and c.red() < 140:
            blue += 1
assert blue > 50, f"only {blue} spinner-coloured pixels in the middle of the video"
w._video.set_busy(None)
w.close()
print(f"7. drawn: {blue} spinner-blue pixels in the middle of the video area")

print("\nALL CHECKS PASSED")
