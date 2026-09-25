"""The CV TIMING line says where the tracking loop's time goes — accurately.

2026-09-25: on the production PC the video was smooth but the box around the
person moved only about every 0.8 s, and the camera went move-stop-move.  The
line added to cv/tracking_loop.py measures every stage once per report window
so the cause can be read from comms.log rather than guessed.  It must not
change what the loop does, and its counts have to be true — a timing line that
miscounts sends the fix to the wrong place.

WHAT THIS TEST IS PROTECTING.

  the wrapper is invisible   _timed returns what the job returns, re-raises what
                             it raises, and files the duration in the window the
                             job ended in
  the numbers are real       tick rate, camera rate and YOLO time match what the
                             fakes were set up to do
  the counts are true        stop commands and tracks given up equal what the
                             mount was actually sent and what the loop signalled
  stale driving is seen      a tick that drives the mount while the tracker has
                             lost the person is counted as such
  once, then every window    the context line once per feed, a report per window
  the camera rate is real    the capture thread counts frames the device gave,
                             not reads that failed

Run directly, or via tools/run_tests.sh with the rest.
"""
import io
import logging
import os
import pathlib
import re
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

import numpy as np                                              # noqa: E402
from PyQt6.QtWidgets import QApplication                        # noqa: E402
from PyQt6.QtCore import QTimer                                 # noqa: E402

app = QApplication.instance() or QApplication([])

import cv.tracking_loop as tl                                   # noqa: E402
from cv.tracker import TrackResult                              # noqa: E402

BUF = io.StringIO()
_h = logging.StreamHandler(BUF)
_h.setFormatter(logging.Formatter("%(message)s"))
tl.log.addHandler(_h)
tl.log.setLevel(logging.INFO)
tl.log.propagate = False

DETECT_S = 0.04          # the fake YOLO's time per detection
FRAME_HZ = 30            # the fake camera's frame rate


class FakeDetector:
    available = True
    _imgsz = 416

    def __init__(self, imgsz=416):
        pass

    def detect(self, frame):
        time.sleep(DETECT_S)
        return [(500, 200, 120, 300)]


class FakeCapture:
    def __init__(self):
        self.frames_grabbed = 0
        self._frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        self._run = True
        threading.Thread(target=self._grab, daemon=True).start()

    def _grab(self):
        while self._run:
            self.frames_grabbed += 1
            time.sleep(1 / FRAME_HZ)

    def get_frame(self):
        return self._frame.copy()


class FakeTracker:
    """Succeeds until told to fail; `active` behaves as the real one does."""
    kind_name = "fake-mosse"

    def __init__(self):
        self.active = False
        self.fail = False
        # The real tracker switches itself off on a failure.  Section 3 turns
        # that off, to reach the stop-and-give-up path whose counts it checks.
        self.off_on_fail = True

    def init(self, frame, bbox, kind=None):
        self.active = True
        return True

    def update(self, frame):
        if self.fail:
            if self.off_on_fail:
                self.active = False
            return TrackResult(False, 0.0, 0.0, None)
        return TrackResult(True, 0.0, 0.0, (250, 100, 60, 150))

    def stop(self):
        self.active = False


class St:
    active_pt_preset = 1


class FakeMM:
    def __init__(self):
        self.jogs = []

    def state(self, m):
        return St()

    def send_jog(self, mid, pan, tilt, *a, **k):
        self.jogs.append((pan, tilt))


def run(seconds):
    QTimer.singleShot(int(seconds * 1000), app.quit)
    app.exec()


def reports():
    return [l for l in BUF.getvalue().splitlines() if l.startswith("CV TIMING ") and
            not l.startswith("CV TIMING context")]


def num(pattern, line):
    m = re.search(pattern, line)
    assert m, f"no {pattern!r} in:\n  {line}"
    return float(m.group(1))


tl.PersonDetector = FakeDetector

# ---- 1. the wrapper is invisible ---------------------------------------------
print("1. _timed:")
stats = tl._CvStats()
assert tl._timed(lambda x: x * 2, 21, stats, "detect_ms") == 42
assert len(stats.detect_ms) == 1 and stats.detect_ms[0] >= 0
try:
    tl._timed(lambda x: 1 / 0, 0, stats, "track_ms")
    raise AssertionError("_timed swallowed the job's exception")
except ZeroDivisionError:
    pass
assert len(stats.track_ms) == 1, "a job that raised was not timed"


def slow_then_swap(x):
    stats.reset()                    # a report lands while the job runs
    return x


tl._timed(slow_then_swap, 1, stats, "detect_ms")
assert len(stats.detect_ms) == 1, \
    "a job that straddled a report was filed in the window it started in"
print("   returns, re-raises, and files in the window it ended in   OK")

# ---- 2. the numbers are real -----------------------------------------------------
print("\n2. a tracking run:")
tl.CV_TIMING_REPORT_S = 1.0
mm, cap = FakeMM(), FakeCapture()
loop = tl.TrackingLoop(mm, cap, tracker_kind="mosse", detect_size=416)
loop._tracker = FakeTracker()
loop.select_person((500, 200, 120, 300))
run(2.4)
lines = reports()
assert len(lines) >= 2, f"expected a report per 1 s window:\n{BUF.getvalue()}"
ctx = [l for l in BUF.getvalue().splitlines() if l.startswith("CV TIMING context")]
assert len(ctx) == 1 and "frame 1280x720" in ctx[0] and "416 px" in ctx[0], ctx
line = lines[-1]
print("   " + line.replace(" | ", "\n      | "))
ticks = num(r"([\d.]+) ticks/s", line)
assert 10 <= ticks <= 35, f"{ticks} ticks/s from a 30 Hz timer"
fps = num(r"camera ([\d.]+) fps", line)
assert 15 <= fps <= 35, f"camera {fps} fps from a {FRAME_HZ} fps source"
yolo_ms = num(r"YOLO [\d.]+ results/s, ([\d.]+) ms avg", line)
assert DETECT_S * 1000 * 0.8 <= yolo_ms <= DETECT_S * 1000 + 60, \
    f"YOLO reported {yolo_ms} ms for a {DETECT_S * 1000:.0f} ms job"
assert "a result every" in line
assert re.search(r"tracker fake-mosse [\d.]+/s, .* \d+ ok / 0 lost the person", line), line
assert "lost 0% of tracking time, driving on a stale position for 0%" in line, line
print("   rates and times match what the fakes were set to do   OK")

# ---- 3. the counts are true ------------------------------------------------------
print("\n3. a person lost for good:")
BUF.truncate(0); BUF.seek(0)
mm.jogs.clear()
lost = []
loop.tracking_lost.connect(lambda: lost.append(1))
loop._tracker.fail = True
loop._tracker.off_on_fail = False            # keep reporting the failures
loop._detector.detect = lambda frame: []     # nothing to re-anchor on
run(1.3)
tl.CV_TIMING_REPORT_S = 0.0                  # flush the window now
loop._tick()
text = BUF.getvalue()
stops = sum(int(n) for n in re.findall(r"(\d+) stop commands", text))
given_up = sum(int(n) for n in re.findall(r"(\d+) tracks given up", text))
zero_jogs = sum(1 for j in mm.jogs if j == (0, 0))
assert stops == zero_jogs and stops > 0, \
    f"the line counted {stops} stop commands; the mount was sent {zero_jogs}"
assert given_up == len(lost) == 1, \
    f"the line counted {given_up} tracks given up; the loop signalled {len(lost)}"
print(f"   {stops} stop commands and {given_up} track given up, as sent   OK")

# ---- 4. driving on a stale position is counted -------------------------------
print("\n4. stale driving:")
tl.CV_TIMING_REPORT_S = 1000.0
loop._stats.reset()
loop._tracking = True
loop._tracker.active = False                 # lost the person...
loop._last_track = TrackResult(True, 5.0, 5.0, (250, 100, 60, 150))  # ...last good
loop._tick()
assert loop._stats.lost_ticks == 1 and loop._stats.stale_drives == 1, \
    "a tick that drove the mount on the last good position after the tracker " \
    "lost the person was not counted as stale"
loop._tracker.active = True
loop._tick()
assert loop._stats.stale_drives == 1, "a tick with a live tracker was counted as stale"
print("   counted when the tracker is lost, not when it is live   OK")

# ---- 5. what it says when there is nothing to say ------------------------
print("\n5. idle and without YOLO:")
loop.stop_tracking()
loop._stats.reset()                          # a window with no tracking in it
BUF.truncate(0); BUF.seek(0)
tl.CV_TIMING_REPORT_S = 0.0
loop._detector.available = False
loop._tick()
text = BUF.getvalue()
assert "not tracking" in text and "YOLO unavailable" in text, text
loop.shutdown()
cap._run = False
print("   'not tracking' and 'YOLO unavailable'   OK")

# ---- 6. the camera rate counts the device's real frames ---------------------
print("\n6. the capture thread's count:")
from cv.capture import CaptureSource                           # noqa: E402


class FakeDevice:
    """Five good frames with a dropped read among them, then stop."""
    def __init__(self, cs):
        self.cs, self.reads = cs, 0

    def isOpened(self):
        return True

    def read(self):
        self.reads += 1
        if self.reads >= 6:
            self.cs._running = False
        if self.reads == 3:
            return False, None                 # a dropped frame
        return True, np.zeros((4, 4, 3), dtype=np.uint8)


cs = CaptureSource()
cs._running = True
cs._cap = FakeDevice(cs)
cs._grab_loop()
assert cs.frames_grabbed == 5, \
    f"the grab loop counted {cs.frames_grabbed} frames from 5 good reads and 1 dropped"
print("   good frames counted, a dropped read not   OK")

print("\nALL CHECKS PASSED")
