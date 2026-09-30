"""CV tracking aims at the head — not at a point down the box.

2026-09-29, the operator's recording: the framing dropped whenever the speaker
stepped out from behind the lectern, and rose again when they went back.  The aim
point was 15% of the box's HEIGHT from its top, and the box runs head to waist
behind a lectern and head to floor beside it — so the aim sat on the face in
one and on the chin in the other, and the mount followed it down.

The detector is now YOLOv8's pose model, which places each person's face and
shoulders; the loop aims at the head it finds and carries it with the tracked
box between detections.

WHAT THIS TEST IS PROTECTING.

  the head, found            the face points that were seen; failing those, eye
                             level above the shoulders; failing both, nothing
  the head, aimed at         the loop drives the head to the target, and the box
                             growing to the floor (legs out from behind a
                             lectern) does not move the aim point at all
  carried, and kept          between detections the head moves with the tracked
                             box; a detection that places no head leaves the
                             last one in place; a new person starts afresh
  without the pose model     15% down the box, exactly as before
  an old update is dropped   a tracker update started before a re-anchor, for a
                             box the tracker has since left, never drives
  off the person, caught     a tracker settled on the background, still reporting
                             success, while YOLO sees the person elsewhere:
                             re-anchored on them if they are near where last seen,
                             dropped if not; YOLO seeing nobody counts for nothing

Run directly, or via tools/run_tests.sh with the rest.
"""
import os
import pathlib
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

import numpy as np                                              # noqa: E402
from PyQt6.QtWidgets import QApplication                        # noqa: E402

app = QApplication.instance() or QApplication([])

import cv.tracking_loop as tl                                   # noqa: E402
from cv.tracker import (Person, TrackResult, head_from_keypoints,  # noqa: E402
                        EYES_ABOVE_SHOULDERS, KP_MIN_CONF)

# ---- 1. where the head is, from 17 keypoints -----------------------------------
print("1. the head, from the keypoints:")
xy = np.zeros((17, 2))
cf = np.zeros(17)
xy[0], xy[1], xy[2] = (100, 50), (96, 46), (104, 46)      # nose, eyes
cf[0] = cf[1] = cf[2] = 0.9
xy[3], cf[3] = (80, 48), KP_MIN_CONF - 0.1                # an ear, not trusted
xy[5], xy[6] = (80, 90), (120, 90)                        # shoulders
cf[5] = cf[6] = 0.9
(hx, hy), src = head_from_keypoints(xy, cf)
assert src == "face" and abs(hx - 100) < 1e-6 and abs(hy - 142 / 3) < 1e-6, (hx, hy, src)
cf[:5] = 0.1                                              # turned to the screen
res = head_from_keypoints(xy, cf)
assert res[1] == "shoulders" and res[0] is not None, \
    f"with the face unseen, no head was placed from the shoulders: {res}"
(hx, hy), src = res
assert hx == 100 and abs(hy - (90 - EYES_ABOVE_SHOULDERS * 40)) < 1e-6, \
    f"with the face unseen the head is not placed above the shoulders: {(hx, hy, src)}"
cf[6] = 0.1                                               # one shoulder hidden too
assert head_from_keypoints(xy, cf) == (None, ""), "a head was invented from one shoulder"
print("   the face seen; above the shoulders without it; nothing without both   OK")


# ---- the loop, with fakes --------------------------------------------------------
class St:
    active_pt_preset = 1


class FakeMM:
    def __init__(self):
        self.jogs = []

    def state(self, m):
        return St()

    def send_jog(self, mid, pan, tilt, *a, **k):
        self.jogs.append((pan, tilt))


class FakeCapture:
    def __init__(self):
        self.frame = np.zeros((720, 1280, 3), np.uint8)

    def get_frame(self):
        return self.frame.copy()


class FakeTracker:
    """Moves the anchored box by `step` each update, as a person walking —
    and, like MOSSE, can hand back a box `grow` bigger around the same centre
    (it rounds the box up to a size its FFT likes)."""
    def __init__(self):
        self.active, self.box, self.step, self.kind_name = False, None, (0, 0), "fake"
        self.fail = False
        self.grow = (0, 0)

    def init(self, frame, bbox, kind=None):
        self.box, self.active = list(bbox), True
        return True

    def update(self, frame):
        if self.fail:
            self.active = False
            return TrackResult(False, 0, 0, None)
        self.box[0] += self.step[0]
        self.box[1] += self.step[1]
        x, y, w, h = self.box
        gw, gh = self.grow
        return TrackResult(True, 0, 0, (x - gw // 2, y - gh // 2, w + gw, h + gh))

    def stop(self):
        self.active = False


class PoseDetector:
    available, _imgsz, model_name = True, 320, "fake-pose"

    def __init__(self):
        self.people = []

    def detect_people(self, frame):
        return list(self.people)


tl.TRACK_SCALE = 1.0                  # boxes in the tests' own pixels
tl.DETECT_MIN_GAP_S = 1e9             # detections only when the test says
tl.JOG_INTERVAL_S = 0.0               # every drive jogs: where, not how often
                                      # (test_cv_timing checks the rate)


def new_loop(detector):
    loop = tl.TrackingLoop(FakeMM(), FakeCapture(), detector=detector)
    loop._timer.stop()
    loop._tracker = FakeTracker()
    return loop


def detect(loop, people):
    """Hand the loop a detection result as if YOLO had just finished."""
    loop._detector.people = people
    loop._last_detect_submit = 0.0
    loop._people = people
    loop._detections = [p.bbox for p in people]


def tick(loop, n=1):
    for _ in range(n):
        loop._tick_body()
        f = loop._track_pending
        if f is not None:
            f.result()               # the worker's update, finished before the next tick


# ---- 2. the aim point is the head, however long the box --------------------------
print("\n2. aimed at the head:")
det = PoseDetector()
loop = new_loop(det)
behind = Person((600, 200, 80, 150), head=(640, 222), head_from="face")   # head to waist
detect(loop, [behind])
loop.select_person(behind.bbox)
tick(loop, 3)
assert loop._aim_px == (640, 222), f"the aim is not on the head: {loop._aim_px}"
# Stepping out: the same head, the box now runs to the floor (twice as tall).
beside = Person((600, 200, 80, 300), head=(640, 222), head_from="face")
detect(loop, [beside])
loop._reinit_tracker(beside, loop._capture.get_frame())
tick(loop, 3)
assert loop._aim_px == (640, 222), \
    f"the box grew to the floor and the aim moved with it, to {loop._aim_px} — the lectern drop"
old_way = (int(600 + 40), int(200 + 300 * tl.HEAD_TRACK_FRACTION))
# Hands up: the box's top rises 50 px above the head, which stays where it was.
hands_up = Person((600, 150, 80, 350), head=(640, 222), head_from="face")
detect(loop, [hands_up])
loop._reinit_tracker(hands_up, loop._capture.get_frame())
tick(loop, 3)
assert loop._aim_px == (640, 222), \
    f"raised hands lifted the box and the aim went with it, to {loop._aim_px}"
# The tracker hands back a bigger box around the same centre — as MOSSE did in
# the replay of the operator's recording, a 514 px box coming back 540 tall with
# its top 13 px higher, which put the first version's aim 13 px off the head.
loop._tracker.grow = (4, 26)
detect(loop, [beside])
loop._reinit_tracker(beside, loop._capture.get_frame())
tick(loop, 3)
assert loop._aim_px == (640, 222), \
    f"the tracker grew its box around the same centre and the aim moved to {loop._aim_px}"
loop._tracker.grow = (0, 0)
print(f"   head at (640, 222) behind the lectern, beside it, with hands up, and with the\n"
      f"   tracker resizing its box; aiming down the box would have moved from\n"
      f"   (640, 222) to {old_way}   OK")

# ---- 3. carried with the box, kept without a head, fresh for a new person --------
print("\n3. carried and kept:")
detect(loop, [beside])                      # hands down again: head 22 px below the top
loop._reinit_tracker(beside, loop._capture.get_frame())
tick(loop, 2)
loop._tracker.step = (5, 2)                 # walking right and slightly down
tick(loop, 4)
x, y, w, h = loop._selected_bbox
assert loop._aim_px == (int(x + w / 2), int(y + 22)), \
    f"the head did not move with the box: aim {loop._aim_px}, box {loop._selected_bbox}"
loop._tracker.step = (0, 0)
no_head = Person(loop._selected_bbox, head=None)          # a detection with no head
loop._reinit_tracker(no_head, loop._capture.get_frame())
tick(loop, 2)
x, y, w, h = loop._selected_bbox
assert loop._aim_px == (int(x + w / 2), int(y + 22)), \
    "a detection that placed no head threw the measured one away"
other = Person((200, 300, 60, 120), head=None)
detect(loop, [other])
loop.select_person(other.bbox)
tick(loop, 2)
assert loop._aim_px == (230, int(300 + 120 * tl.HEAD_TRACK_FRACTION)), \
    f"a newly selected person kept the last one's head: {loop._aim_px}"
print("   moves with the box; a headless detection keeps it; a new person starts afresh   OK")

# ---- 4. the plain model: down the box, as before ---------------------------------
print("\n4. without the pose model:")


class PlainDetector:
    available, _imgsz, model_name = True, 320, "fake-plain"

    def detect(self, frame):
        return [(600, 200, 80, 300)]


loop2 = new_loop(PlainDetector())
people = loop2._detect_fn(None)
assert people == [Person((600, 200, 80, 300))] and people[0].head is None, people
detect(loop2, people)
loop2.select_person((600, 200, 80, 300))
tick(loop2, 2)
assert loop2._aim_px == (640, int(200 + 300 * tl.HEAD_TRACK_FRACTION)), loop2._aim_px
print("   15% down the box, as it always was   OK")

# ---- 5. an update for a box the tracker has left does not drive ------------------
print("\n5. an old update after a re-anchor:")
loop3 = new_loop(PoseDetector())
p = Person((600, 200, 80, 150), head=(640, 222), head_from="face")
detect(loop3, [p])
loop3.select_person(p.bbox)
loop3._tick_body()                           # an update starts for the first anchor...
pending = loop3._track_pending
assert pending is not None
far = Person((100, 100, 80, 150), head=(140, 122), head_from="face")
loop3._reinit_tracker(far, loop3._capture.get_frame())   # ...and the tracker moves on
pending.result()
loop3._mm.jogs.clear()
before = loop3._aim_px
loop3._tick_body()                           # the old update is collected here
assert loop3._aim_px == before and not loop3._mm.jogs, \
    f"an update for the box the tracker had left moved the aim to {loop3._aim_px}"
print("   dropped, not driven   OK")

# ---- 5b. a detection of an older frame is caught up --------------------------
# YOLO answers about a frame 50-250 ms old.  In the operator's recording the
# person had moved 35 px by then, and a tracker re-anchored where YOLO saw them
# stayed on the wrong spot until the next detection.
print("\n5b. a detection of an older frame:")
import threading                                                # noqa: E402


class SlowPose(PoseDetector):
    def __init__(self):
        super().__init__()
        self.go = threading.Event()

    def detect_people(self, frame):
        self.go.wait(5)
        return list(self.people)


slow = SlowPose()
loop4 = new_loop(slow)
p4 = Person((600, 200, 80, 150), head=(640, 222), head_from="face")
detect(loop4, [p4])
loop4.select_person(p4.bbox)
tick(loop4, 2)
slow.people = [p4]                           # YOLO sees them where they are NOW...
tl.DETECT_MIN_GAP_S = 0.0
loop4._last_detect_submit = 0.0
tick(loop4)                                  # ...and starts on this frame
tl.DETECT_MIN_GAP_S = 1e9
assert loop4._detect_pending is not None, "no detection started"
loop4._tracker.step = (10, 5)                # they walk on while it runs
tick(loop4, 3)
loop4._tracker.step = (0, 0)
tick(loop4, 2)
before = loop4._aim_px
assert before != (640, 222), "the tracker did not follow them, so this proves nothing"
slow.go.set()
loop4._detect_pending.result()
tick(loop4, 2)                               # the old frame's detection lands
assert abs(loop4._aim_px[0] - before[0]) <= 1 and abs(loop4._aim_px[1] - before[1]) <= 1, \
    f"a detection of an older frame pulled the aim back from {before} to {loop4._aim_px} — " \
    "where they were when YOLO started, not where they are"
print(f"   they moved from (640, 222) to {before} while YOLO ran; its answer kept them there   OK")

# ---- 6. a tracker settled on the background is caught ------------------------
# The replay of the operator's recording: the tracker sat on a door frame and
# the lectern, reporting success, for 11 s while the speaker walked away — YOLO
# seeing them every 0.2 s, a little further off, never where the tracker was.
# The old loop did the same at the same moment.
print("\n6. a tracker on the background:")


def yolo_round(lp, people):
    """One YOLO result through the loop's own path: started, answered, used."""
    lp._detector.people = list(people)
    tl.DETECT_MIN_GAP_S = 0.0
    lp._last_detect_submit = 0.0
    tick(lp)
    tl.DETECT_MIN_GAP_S = 1e9
    if lp._detect_pending is not None:
        lp._detect_pending.result()
        tick(lp)


loop5 = new_loop(PoseDetector())
p5 = Person((600, 200, 80, 300), head=(640, 222), head_from="face")
detect(loop5, [p5])
loop5.select_person(p5.bbox)
tick(loop5, 2)
assert loop5._aim_px == (640, 222), loop5._aim_px
# YOLO misses them for a while — it did, 16 times running, in the replay.
for _ in range(4):
    yolo_round(loop5, [])
assert loop5._tracker.active and loop5._aim_px == (640, 222), \
    "detections that saw nobody at all dropped a tracker that was on the person"
# They walk 100 px left; the tracker stays where it was, on the wall.
walked = Person((500, 200, 80, 300), head=(540, 222), head_from="face")
yolo_round(loop5, [walked])
tick(loop5)
assert loop5._aim_px == (640, 222) and loop5._tracker.active, \
    f"one detection away from the tracker, and it was acted on: aim {loop5._aim_px}"
yolo_round(loop5, [walked])
tick(loop5, 2)
assert loop5._aim_px == (540, 222) and loop5._tracker.active, \
    "seen twice near where they were last seen, none where the tracker was, and the " \
    f"tracker was not moved onto them: aim {loop5._aim_px}, " \
    f"tracker {'on' if loop5._tracker.active else 'dropped'}"
# On they go, and the tracker settles again.  Looked for near where YOLO saw
# them last (540) — where they were first picked (640) is too far by now.
further = Person((390, 200, 80, 300), head=(430, 222), head_from="face")
yolo_round(loop5, [further])
yolo_round(loop5, [further])
tick(loop5, 2)
assert loop5._aim_px == (430, 222) and loop5._tracker.active, \
    "looked for near where they were first picked, not where YOLO last saw them: " \
    f"aim {loop5._aim_px}"
# ...and then nowhere near where they were last seen: dropped, then stopped.
gone = Person((100, 200, 80, 300), head=(140, 222), head_from="face")
yolo_round(loop5, [gone])
yolo_round(loop5, [gone])
assert not loop5._tracker.active, \
    "seen twice, far from where the tracked person was last seen, and the tracker " \
    "was not dropped — kept on the wall, or moved onto whoever that was"
assert loop5._stats.stale_drives == 0, \
    "an update the tracker made before it was dropped drove the mount"
loop5._mm.jogs.clear()
tick(loop5, 3)
assert not loop5._mm.jogs, f"the mount was driven after the tracker was dropped: {loop5._mm.jogs}"
loop5._lost_since -= tl.LOST_STOP_S
tick(loop5, 2)
assert loop5._mm.jogs == [(0, 0)], \
    f"dropped for {tl.LOST_STOP_S} s and not stopped exactly once: {loop5._mm.jogs}"
assert (loop5._stats.found_near, loop5._stats.dropped) == (2, 1), \
    f"counted {loop5._stats.found_near} found nearby and {loop5._stats.dropped} " \
    "dropped; it was two and one"
print("   nobody seen: kept; seen twice nearby: re-anchored on them, twice, as they\n"
      "   walked on; seen twice far off: dropped, then one stop   OK")

# ---- 6b. a tracker that cannot hold them: the mount follows YOLO ---------------
# In the same replay a quarter of the tracker's restarts failed on the very
# frame they started on — MOSSE finding too little to lock onto in a dim, soft
# picture.  The mount was fed only by the tracker, so each of those was a halt:
# YOLO re-anchored on the speaker every 0.2 s, overlapping 0.8-0.99, and after
# 0.35 s without a tracker result the mount was told to stop anyway.
print("\n6b. a tracker that cannot hold them:")
loop6 = new_loop(PoseDetector())
p6 = Person((600, 200, 80, 300), head=(640, 222), head_from="face")
detect(loop6, [p6])
loop6.select_person(p6.bbox)
tick(loop6, 2)
loop6._tracker.fail = True                   # it holds no one from here on
tick(loop6, 2)
assert not loop6._tracker.active, "the fake tracker did not lose them, so this proves nothing"
loop6._mm.jogs.clear()
for k in (1, 2, 3):
    tick(loop6)                              # its last failure, collected
    assert loop6._lost_since, "the tracker's failure was not taken as a loss"
    loop6._lost_since -= 0.3                 # 0.3 s since it lost them
    moved = Person((600 + 10 * k, 200, 80, 300), head=(640 + 10 * k, 222), head_from="face")
    sent = len(loop6._mm.jogs)
    yolo_round(loop6, [moved])
    assert loop6._aim_px == (640 + 10 * k, 222) and len(loop6._mm.jogs) > sent, \
        f"YOLO found them at ({640 + 10 * k}, 222) and the mount was not driven there: " \
        f"aim {loop6._aim_px}, jogs {loop6._mm.jogs}"
assert (0, 0) not in loop6._mm.jogs and not loop6._tracker.active, \
    "the mount was stopped though YOLO found them every round, 0.3 s apart: " \
    f"{loop6._mm.jogs}"
print("   driven on each detection, and never stopped while YOLO keeps finding them   OK")

for lp in (loop, loop2, loop3, loop4, loop5, loop6):
    lp.shutdown()

# ---- 7. the detector: the pose model first, the plain one if it cannot -----------
# The concert PC downloads the pose model the first time the feed starts; offline
# that fails, and tracking must still work as it did.
print("\n7. which model, and what it reports:")
import types                                                    # noqa: E402
import cv.tracker as trk                                        # noqa: E402


class _Arr:
    def __init__(self, a):
        self.a = np.asarray(a, dtype=float)

    def cpu(self):
        return self

    def numpy(self):
        return self.a


class _Box:
    def __init__(self, xyxy):
        self.xyxy = [types.SimpleNamespace(tolist=lambda v=xyxy: list(v))]


class _Result:
    def __init__(self, boxes, kxy=None, kconf=None):
        self.boxes = [_Box(b) for b in boxes]
        self.keypoints = (types.SimpleNamespace(xy=_Arr(kxy), conf=_Arr(kconf))
                          if kxy is not None else None)


asked = []
missing = set()
result = []


class FakeYOLO:
    def __init__(self, name):
        asked.append(name)
        if name in missing:
            raise FileNotFoundError(f"{name} could not be downloaded")
        self.name = name

    def __call__(self, frame, **kw):
        return result


sys.modules["ultralytics"] = types.SimpleNamespace(YOLO=FakeYOLO)
d = trk.PersonDetector(imgsz=320)
assert asked[0] == trk.PersonDetector.POSE_MODEL and d.has_pose and d.available, \
    f"the pose model was not the first asked for, or not used: {asked}"
kxy = np.zeros((2, 17, 2)); kcf = np.zeros((2, 17))
kxy[0, 0], kcf[0, 0] = (140, 60), 0.9                       # first person: a nose
kxy[1, 5], kxy[1, 6] = (410, 200), (450, 200)                # second: shoulders only
kcf[1, 5] = kcf[1, 6] = 0.9
result[:] = [_Result([(100, 40, 180, 300), (400, 150, 460, 400)], kxy, kcf)]
ppl = d.detect_people(np.zeros((720, 1280, 3), np.uint8))
assert [p.bbox for p in ppl] == [(100, 40, 80, 260), (400, 150, 60, 250)], ppl
assert ppl[0].head == (140.0, 60.0) and ppl[0].head_from == "face", ppl[0]
assert ppl[1].head_from == "shoulders" and ppl[1].head[0] == 430.0, ppl[1]
assert d.detect(np.zeros((720, 1280, 3), np.uint8)) == [p.bbox for p in ppl], \
    "detect() no longer gives the same boxes the CV window draws"

asked.clear()
missing.add(trk.PersonDetector.POSE_MODEL)
result[:] = [_Result([(100, 40, 180, 300)])]
d2 = trk.PersonDetector(imgsz=320)
assert asked == [trk.PersonDetector.POSE_MODEL, trk.PersonDetector.MODEL_NAME] \
    and d2.available and not d2.has_pose and d2.model_name == trk.PersonDetector.MODEL_NAME, \
    f"with the pose model unobtainable the plain one was not used: {asked}"
ppl2 = d2.detect_people(np.zeros((720, 1280, 3), np.uint8))
assert ppl2 == [Person((100, 40, 80, 260))], ppl2
print("   the pose model first; offline, the plain one; heads from the face or\n"
      "   the shoulders; detect() still the CV window's boxes   OK")

print("\nALL CHECKS PASSED")
