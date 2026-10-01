"""
TrackingLoop — detection-first closed-loop pan/tilt controller.

Architecture
------------
Two background workers run in parallel via separate ThreadPoolExecutors:

  Detector  (slow, ~50 ms per frame on the concert PC at 320 px):
    YOLOv8-nano — the pose model where it loads — runs at most every
    DETECT_MIN_GAP_S, finding all people in the frame and, with the pose
    model, where each one's head is.  Results update the detection overlay
    shown in the CV window and re-anchor the correlation tracker to prevent
    drift — and drive the mount themselves, so a tracker that cannot hold
    the person leaves it following at YOLO's rate rather than halting.

  Tracker  (fast, a few ms per frame):
    A correlation tracker (MOSSE by default) follows the selected person on
    every tick for smooth, low-latency motion.  When it loses them the mount
    is sent nothing — never driven on the last position — and stopped if
    YOLO has not found them again within LOST_STOP_S.  A tracker that has
    wandered onto the background instead (it reports success there for as
    long as it likes) is caught by YOLO seeing the person somewhere else —
    see UNCONFIRMED_ROUNDS.

The aim point is the HEAD: where the pose model put it, carried along with
the tracked box between detections.  Not a point down the box — the box grows
to the floor when a lectern stops hiding the legs, and up to the hands when
they are raised.  Without the pose model, 15% down the box as before.

Operator workflow
-----------------
  • Feed opens → people detected and shown as numbered blue boxes.
  • Operator clicks any box → mount starts tracking that person immediately.
  • Operator clicks a different box → smooth switch to that person.
  • "Stop Tracking" → detection continues, mount stops.

Target point
------------
  target_cx / target_cy define where in the frame the tracked person's aim
  point should sit (offset from frame centre, in full-resolution pixels).
  Default: upper-third  (cy = -frame_h / 6, cx = 0 = horizontally centred).
  Operator can drag the ⊕ crosshair in the CV window to reposition it.

Fallback (YOLO unavailable)
---------------------------
  If ultralytics is not installed, detector_available is False and the CV
  window falls back to the original draw-a-box manual workflow.
"""
from __future__ import annotations

import concurrent.futures
import logging
import math
import os
import sys
import time

from PyQt6.QtCore import QObject, QTimer, Qt, pyqtSignal

import cv2
import numpy as np

from .capture import CaptureSource
from .tracker import Tracker, TrackResult, PersonDetector, TrackerKind, Person
from comms.mount_manager import MountManager
from config.mount_config import AxisGroupPresets

log = logging.getLogger(__name__)

TRACK_RATE_HZ   = 30
TRACK_INTERVAL  = 1000 // TRACK_RATE_HZ   # ms between ticks
# The mount is jogged at most this often — the joystick's rate (JOG_RATE_HZ in
# motion/command_dispatcher.py, "responsiveness vs radio congestion"), and
# about what the concert PC's loop actually sent at its 21 ticks/s.  The
# tracker runs every tick; each jog carries the freshest aim.  Faster jogs buy
# little: the Teensy ramps every speed change at its acceleration anyway.
JOG_RATE_HZ     = 20
JOG_INTERVAL_S  = 1.0 / JOG_RATE_HZ
# YOLO again as soon as this long after the last one started.  It was every
# 10th tick: at the concert PC's 21 ticks/s, one result every 0.47 s for a
# ~50 ms job, with three cores idle.  Five a second re-anchors the tracker two
# and a half times as often, for about half a core more.
DETECT_MIN_GAP_S = 0.2
TRACK_SCALE     = 0.5   # downscale factor for CSRT (4× faster, small accuracy hit)
# A moment's loss is not a reason to stop.  The correlation tracker drops the
# person a few times every ten seconds (the concert PC's own counts), and YOLO
# finds them again within one DETECT_MIN_GAP_S.  So for LOST_STOP_S the mount is
# sent nothing and carries on as it was going — the Teensy's jog watchdog
# (JOG_WATCHDOG_MS, 500 ms) would stop it anyway — and only then is it told to
# stop.  Stopping at once turned every loss into a stop-start.  Nothing is
# ever computed from the old position: the old code went on DRIVING toward it,
# for as long as the loss lasted.
LOST_STOP_S     = 0.35
# How long the person may stay lost, the mount stopped and YOLO looking for
# them near where they were, before tracking is given up (tracking_lost).
MAX_LOST_S      = 2.0
IOU_REANCHOR    = 0.30  # minimum IoU to accept a YOLO detection as the same person
# The tracker has to keep being confirmed by YOLO.  A correlation tracker can
# settle on the background — the speaker's box, run to the floor beside the
# lectern, takes in a door frame and the lectern's edges — and go on
# reporting success there while the speaker walks away: in the replay of the
# operator's recording, 11 s on the wall, with YOLO seeing them every 0.2 s a
# little further off.  (The old loop did the same, at the same moment.)
#
# So when UNCONFIRMED_ROUNDS detections in a row see people but none where
# the tracker is, the person is looked for near where YOLO last saw them —
# within REACQUIRE_WIDTHS of their width, head to head — and re-anchored on;
# failing that, the tracker is dropped and the lost path takes over.  A
# detection that sees NOBODY does not count toward that: YOLO at 320 px
# missed the speaker 16 times running in the same replay with the tracker
# squarely on them.  The risk taken: someone else within that distance, seen
# twice while the tracked person was not, is taken for them.
UNCONFIRMED_ROUNDS = 2
REACQUIRE_WIDTHS   = 1.5
# ...but nobody, for long enough, does.  2026-10-01 at the hall: the operator
# walked out through a door and the tracker stayed on a sliver of it — with
# the frame empty there was no one to see elsewhere, so nothing ever
# challenged it, and the mount held on the door until the operator stopped
# it.  Live, YOLO confirms the tracked person on all but a handful of its
# ~47 results every ten seconds while they are in view, so this long without
# one means they have gone: the tracker is dropped — the mount stops, YOLO
# looks for them near where it last saw them for MAX_LOST_S, then tracking
# is given up.
UNSEEN_DROP_S      = 3.0

# The aim point without the pose model: this fraction of the box's height
# from its top.  0.12 ≈ top of head, 0.20 ≈ eye-line.  A fraction of the
# HEIGHT slides down the body as the box grows — face behind a lectern, chin
# beside it — which is why the pose model's head is used whenever there is one.
HEAD_TRACK_FRACTION = 0.15

# One line in comms.log every CV_TIMING_REPORT_S while the feed runs: where
# the time goes, and how often the tracker loses the person.
#
# Added 2026-09-25.  On the production PC (a 4-core 7th-gen i5) the video was
# smooth but the box moved only about every 0.8 s, and the camera went
# move-stop-move.  Slow ticks, a slow display, slow YOLO, or a tracker that
# keeps losing the person between detections could each do that, and each
# wants a different fix.  This measures; it changes nothing.
CV_TIMING_REPORT_S = 10.0


def _timed(fn, arg, stats, bucket: str):
    """Run fn(arg) on a worker thread and record its duration in ms.

    The bucket is looked up when the job ENDS, so a job that straddles a
    report lands in the window it finished in."""
    t0 = time.perf_counter()
    try:
        return fn(arg)
    finally:
        getattr(stats, bucket).append((time.perf_counter() - t0) * 1000.0)


class _CvStats:
    """One report window.  The two workers only append durations to their
    lists; every other field is written on the Qt thread, in _tick."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.t0             = time.monotonic()
        self.cpu0           = time.process_time()
        self.ticks          = 0
        self.no_frame       = 0      # ticks with nothing from the camera yet
        self.tick_ms: list  = []     # the whole tick, display included
        self.show_ms: list  = []     # handing the frame to the display
        self.detect_ms: list = []    # YOLO, measured on its worker
        self.track_ms: list  = []    # tracker update, measured on its worker
        self.detect_results = 0
        self.detect_gaps: list = []  # seconds between YOLO results
        self.track_ok       = 0
        self.track_fail     = 0
        self.tracking_ticks = 0      # ticks with a person selected
        self.lost_ticks     = 0      # ...of which the tracker had lost them
        self.stale_drives   = 0      # ...and the mount was driven anyway, on
                                     # the last position the tracker gave
        self.reanchors      = 0
        self.found_near     = 0      # ...of which near where YOLO last saw them
        self.dropped        = 0      # trackers dropped: people seen, none there
        self.dropped_unseen = 0      # ...and: nobody confirmed for UNSEEN_DROP_S
        self.aim_err: list  = []     # px from the aim point to the target, per
                                     # fresh position
        self.crossings      = 0      # times the aim crossed the target (an
                                     # overshoot, or the person turning back)
        self.drive_calls    = 0      # fresh positions the drive saw...
        self.held           = 0      # ...with both axes holding (HOLD_FRACTION)
        self.jogs           = 0      # jogs sent to the mount (see JOG_RATE_HZ)
        self.stops          = 0      # stop commands sent for a lost track
        self.gave_up        = 0      # tracks abandoned (tracking_lost)


def _ms(v: float) -> str:
    return f"{v:.1f}" if v < 10 else f"{v:.0f}"


def _avg_max(v: list) -> str:
    if not v:
        return "none"
    return f"{_ms(sum(v) / len(v))} ms avg / {_ms(max(v))} max"


DEFAULT_GAIN_PAN  = 2.0      # the CV window's sliders, 20 = 2.0
DEFAULT_GAIN_TILT = 2.0
MAX_JOG_VALUE     = 1000

# How hard the mount is driven toward the target: a SPEED in deg/s, for an
# error of half the frame's width, per unit of gain — at the default 2.0, 10
# deg/s at half a frame and 0.55 deg/s at 40 px.  Proportional to the error,
# so the last few pixels close the way the first did.
#
# It was error^1.5, as a share of the preset's top speed — and the Teensy then
# puts every jog through the joystick's own curve, 0.3x + 0.7x^3
# (MountMotion.cpp jogExpo), soft in the middle for a thumb.  Together, a speed
# that died away near the target.  The operator, 2026-10-01: the mount kept up
# with them walking, but "the last bit" took seconds.  Simulated, it never
# arrived: a 40 px error was still more than 8 px out after 30 s, at
# 0.09 deg/s.  This does it in about 2 s at the operator's framing.
#
# Faster is not free.  The loop sees the person 0.15-0.25 s late (capture,
# YOLO, radio), and the right speed for an error in PIXELS depends on the zoom,
# which the PC cannot see: simulated with a 0.25 s delay, this rate holds
# steady up to about 150 px per degree — a picture 8.5 deg wide: full length
# from about 25 m, head and shoulders from about 7 — and overshoots on
# tighter ones.  The
# sliders are the operator's trim: up for wide shots, down if a tight one
# swings past.
TRACK_SPEED_DPS   = 5.0
# Nothing is sent for an error this small (a fraction of the frame's width,
# 5 px at 1280): a few pixels of pose jitter are not a move.
DEADBAND_FRACTION = 0.004
# Never ask for a speed the mount could not stop from within the error left,
# at the preset's acceleration, allowing BRAKE_DELAY_S of loop delay.  Without
# the zoom, the distance left is reckoned as if the shot were tight
# (BRAKE_PX_PER_DEG at 1280 px wide): on wider ones it slows early, which
# costs a large swing a little time and stops it sailing past.
BRAKE_PX_PER_DEG  = 250.0
BRAKE_DELAY_S     = 0.2
# A still subject holds the shot still.  The mount moves only when the aim
# point leaves a box around the target — HOLD_FRACTION of the frame's width
# and of its height either side, each axis deciding for itself — and then
# brings it back to the target, the middle of the box, until within
# ARRIVE_FRACTION, where it stops and holds again.
#
# 2026-10-01, the operator, on the proportional drive: "quite jittery".  The
# CV TIMING line had the aim a median 7-11 px from the target, but crossing it
# 4-12 times every ten seconds: the mount answering every few pixels of pose
# jitter, sway and tracker wobble.  Their idea: "a point where any small
# movements don't move the mount, but when a subject leaves 'an area', the
# mount starts to move and puts the subject back to the centre".  The CV
# window draws the box and sets its size (its Hold slider); 0 follows every
# movement, as before.
HOLD_FRACTION     = 0.06
ARRIVE_FRACTION   = 0.015
# The Teensy's joystick curve (MountMotion.cpp JOG_EXPO_STRENGTH), undone
# here so that the mount makes the speed asked for.  tools/test_cv_pace.py
# reads the firmware's value and fails if the two part.
JOG_EXPO_STRENGTH = 0.7


def _cbrt(v: float) -> float:
    return math.copysign(abs(v) ** (1.0 / 3.0), v)


def jog_expo(x: float) -> float:
    """The Teensy's joystick curve: a jog's share of full stick (0-1) to the
    share of the preset's top speed it runs at."""
    return (1.0 - JOG_EXPO_STRENGTH) * x + JOG_EXPO_STRENGTH * x ** 3


def jog_for_speed(s: float) -> float:
    """The share of full stick the Teensy turns into speed share s (0-1)."""
    if s <= 0.0:
        return 0.0
    if s >= 1.0:
        return 1.0
    a, b = JOG_EXPO_STRENGTH, 1.0 - JOG_EXPO_STRENGTH
    if a <= 0.0:
        return s
    # a x^3 + b x = s has one real root (the curve only rises): Cardano.
    p, q = b / a, -s / a
    r = math.sqrt((q / 2.0) ** 2 + (p / 3.0) ** 3)
    return _cbrt(-q / 2.0 + r) + _cbrt(-q / 2.0 - r)


def axis_jog(err_px: float, gain: float, frame_w: int,
             vmax: float, accel: float) -> int:
    """One axis's jog for an error of err_px: the speed TRACK_SPEED_DPS sets,
    no more than the mount can stop from (BRAKE_*) or the preset can do
    (vmax deg/s, accel deg/s^2), as the stick share that makes it.

    Both axes are measured against the frame's WIDTH: pixels are square, so a
    pixel of tilt is as many degrees as a pixel of pan.  Tilt was measured
    against the height, 1.8x as hard for the same error."""
    d_px = abs(err_px) - DEADBAND_FRACTION * frame_w
    if d_px <= 0.0 or vmax <= 0.0:
        return 0
    v = gain * TRACK_SPEED_DPS * d_px / (frame_w / 2.0)
    if accel > 0.0:
        d_deg = d_px / (BRAKE_PX_PER_DEG * frame_w / 1280.0)
        ad = accel * BRAKE_DELAY_S
        v = min(v, math.sqrt(2.0 * accel * d_deg + ad * ad) - ad)
    s = min(1.0, v / vmax)
    return int(math.copysign(round(MAX_JOG_VALUE * jog_for_speed(s)), err_px))


def _default_pt_preset(mount_id: int, preset: int) -> tuple[float, float]:
    """The pan/tilt presets the firmware ships (deg/s, deg/s^2), until the CV
    window hands over the mount's configured ones."""
    sp = AxisGroupPresets().get(preset if preset in (1, 2, 3, 4) else 2)
    return float(sp.max_speed), float(sp.accel)


def _iou(a: tuple, b: tuple) -> float:
    """Intersection-over-Union of two (x, y, w, h) bboxes."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(ax, bx);  iy = max(ay, by)
    iw = max(0, min(ax + aw, bx + bw) - ix)
    ih = max(0, min(ay + ah, by + bh) - iy)
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _head_or_top(p: Person) -> tuple[float, float]:
    """Where a detected person's head is: the pose model's, or the top centre
    of their box — which a lectern does not move."""
    if p.head is not None:
        return p.head
    x, y, w, h = p.bbox
    return (x + w / 2, y)


class TrackingLoop(QObject):
    """
    Signals
    -------
    frame_ready(np.ndarray)    annotated frame for display (BGR, 3-channel)
    tracking_lost()            CSRT failed and YOLO could not re-acquire
    tracking_active(bool)      a person was selected / deselected
    detections_updated(list)   latest list of (x, y, w, h) person bboxes
    detector_ready(bool)       True once YOLO is available
    """

    frame_ready        = pyqtSignal(object)   # np.ndarray
    tracking_lost      = pyqtSignal()
    tracking_active    = pyqtSignal(bool)
    detections_updated = pyqtSignal(list)
    detector_ready     = pyqtSignal(bool)

    def __init__(self, mount_manager: MountManager,
                 capture: CaptureSource, parent=None,
                 tracker_kind: str = "mosse", detect_size: int = 416,
                 detector=None):
        # `detector`: a PersonDetector already loaded.  Loading one imports
        # torch and runs a warm-up pass, seconds on the M710q; the CV window
        # does that on a worker thread so its spinner can turn, then hands the
        # result in here.  None loads one now, as before.
        super().__init__(parent)
        self._mm       = mount_manager
        self._capture  = capture
        self._mount_id = 1
        # Perf settings (see AppConfig.cv_tracker / cv_detect_size, and
        # tools/cv_benchmark.py for measuring a given machine).
        try:
            self._tracker_kind = TrackerKind(str(tracker_kind).lower())
        except ValueError:
            log.warning("Unknown cv_tracker %r — using MOSSE", tracker_kind)
            self._tracker_kind = TrackerKind.MOSSE
        self._gain_pan  = DEFAULT_GAIN_PAN
        self._gain_tilt = DEFAULT_GAIN_TILT
        # (mount, preset) -> (top speed deg/s, acceleration deg/s^2): what a
        # jog at that preset can do — see set_pt_preset_speeds().
        self._pt_preset_speed = _default_pt_preset
        # The hold box (HOLD_FRACTION), and whether each axis — pan, tilt — is
        # on its way back to the target rather than holding.
        self._hold      = HOLD_FRACTION
        self._moving    = [True, True]
        self._sent_zero = False       # the stop for this hold has been sent

        # ── State ──────────────────────────────────────────────────────────
        self._detections:    list[tuple] = []    # latest YOLO person bboxes
        self._people:        list[Person] = []   # ...the same, with their heads
        self._selected_bbox: tuple | None = None  # current CSRT-tracked bbox
        self._tracking    = False
        self._tick_count  = 0
        # Where the head sits in the box, in pixels: across from its centre,
        # down from its top.  Measured on the last detection that placed it,
        # and carried with the box between detections.  From the TOP because
        # a box grows down when legs come out from behind a lectern, and the
        # top stays at the head.  None: aim down the box instead.
        self._head_off: tuple[float, float] | None = None
        # Half the height of the box the tracker was last started on.  The
        # tracker's own box is not that box: MOSSE rounds it up to a size its
        # FFT likes, keeping the centre (a 514 px box came back 540 tall, its
        # top 13 px higher).  So the top is rebuilt from the tracker's centre
        # and this, not read off the tracker's box.
        self._anchor_half_h: float | None = None
        self._aim_px:   tuple[int, int] | None = None   # drawn on the frame
        self._lost_since   = 0.0      # when the person was lost; 0 = not lost
        self._lost_stopped = False    # the stop for this loss has been sent
        self._track_gen    = 0        # bumped on every tracker (re)start
        # The detection YOLO last confirmed the tracked person with, and how
        # many since have seen people but none where the tracker is — see
        # UNCONFIRMED_ROUNDS.
        self._last_seen: Person | None = None
        self._unconfirmed  = 0
        self._confirmed_at = 0.0      # when YOLO last confirmed it — UNSEEN_DROP_S
        self._aim_side = (0, 0)       # which side of the target, by axis — crossings

        # Target point — offset from frame centre (full-res pixels).
        # Upper-third default is applied when the operator first selects a
        # person (frame_h is known at that point).
        self._target_cx:  float = 0.0
        self._target_cy:  float = 0.0
        self._target_set: bool  = False   # True once manually positioned

        # Frame dimensions (updated each tick)
        self._frame_w = 1280
        self._frame_h = 720

        # ── Workers ────────────────────────────────────────────────────────
        self._detector = (detector if detector is not None
                          else PersonDetector(imgsz=detect_size))
        self._tracker  = Tracker()

        # Separate executors so detection and tracking run in parallel.
        self._detect_exec = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="yolo")
        self._track_exec  = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="csrt")

        self._detect_pending: concurrent.futures.Future | None = None
        self._track_pending:  concurrent.futures.Future | None = None
        self._track_pending_gen = 0   # the tracker start that update belongs to
        self._last_track:     TrackResult | None = None
        self._last_detect_submit = 0.0
        # The tracker's box when the running detection started (None if it
        # was not tracking).  A detection describes that frame, not the one
        # it is applied to — see _catch_up().
        self._detect_ref_box: tuple | None = None
        # The pose-aware call where the detector has one; a detector with only
        # detect() (the plain model's shape, and the tests' fakes) gives boxes
        # without heads, and the loop aims down the box.
        self._detect_fn = (self._detector.detect_people
                           if hasattr(self._detector, "detect_people")
                           else lambda f: [Person(tuple(b)) for b in self._detector.detect(f)])

        # CV TIMING — see CV_TIMING_REPORT_S.
        self._stats            = _CvStats()
        self._context_logged   = False
        self._last_detect_t    = 0.0
        self._frames_at_report = getattr(capture, "frames_grabbed", None)

        # PRECISE, or Windows runs it at 21 ticks/s, not 30.  Qt hands a coarse
        # timer of 20 ms-20 s to the Windows message timer, which fires only on
        # the system clock's 15.6 ms ticks: 33 ms becomes 47 — the concert PC's
        # CV TIMING read 21.0-21.3 ticks/s, every session.  A precise one goes
        # to the 1 ms multimedia timer (qeventdispatcher_win.cpp, Qt 6.8 and
        # dev alike).  Elsewhere coarse timers already keep time.
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(TRACK_INTERVAL)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._jog_due = 0.0           # when the next jog may go — see JOG_RATE_HZ

        self.detector_ready.emit(self._detector.available)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_tracking(self) -> bool:
        return self._tracking

    @property
    def detector_available(self) -> bool:
        return self._detector.available

    @property
    def target(self) -> tuple[float, float]:
        """Current target point (cx, cy) offset from frame centre."""
        return (self._target_cx, self._target_cy)

    def set_mount(self, mount_id: int) -> None:
        self._mount_id = mount_id

    def set_gains(self, pan: float, tilt: float) -> None:
        self._gain_pan  = pan
        self._gain_tilt = tilt

    def set_hold(self, fraction: float) -> None:
        """The hold box's half-size, a fraction of the frame's width and
        height — see HOLD_FRACTION.  0 follows every movement."""
        self._hold = max(0.0, float(fraction))

    def set_pt_preset_speeds(self, fn) -> None:
        """fn(mount_id, preset) -> (top speed deg/s, acceleration deg/s^2):
        the mount's configured pan/tilt presets.  The drive asks for SPEEDS,
        so it has to know what the preset in each jog will run at."""
        self._pt_preset_speed = fn

    def set_tracker_kind(self, kind: TrackerKind) -> None:
        """Change the correlation tracker; takes effect on the next re-anchor."""
        self._tracker_kind = kind

    def set_target(self, cx: float, cy: float) -> None:
        """Reposition the target point (offset from frame centre, full-res px)."""
        self._target_cx  = cx
        self._target_cy  = cy
        self._target_set = True

    def select_person(self, bbox: tuple[int, int, int, int]) -> None:
        """
        Start tracking, or switch to, the person with this bbox.
        Called when the operator clicks a detection box in the CV window.
        Switching while already tracking gives a smooth subject transition —
        the P-controller simply sees a new (large) error and ramps toward it.
        """
        frame = self._capture.get_frame()
        if frame is None:
            return

        self._frame_h, self._frame_w = frame.shape[:2]

        # Apply upper-third default on first selection if not manually set.
        if not self._target_set:
            self._target_cy = -self._frame_h / 6.0
            self._target_cx = 0.0

        # Initialise CSRT on the selected bbox (downscaled for speed).
        x, y, w, h = bbox
        if TRACK_SCALE < 1.0:
            tw = int(self._frame_w * TRACK_SCALE)
            th = int(self._frame_h * TRACK_SCALE)
            small  = cv2.resize(frame, (tw, th))
            scaled = (int(x * TRACK_SCALE), int(y * TRACK_SCALE),
                      max(1, int(w * TRACK_SCALE)), max(1, int(h * TRACK_SCALE)))
        else:
            small  = frame
            scaled = bbox

        was_tracking = self._tracking
        ok = self._tracker.init(small, scaled, self._tracker_kind)
        self._track_gen += 1
        if ok:
            self._selected_bbox = bbox
            self._tracking      = True
            self._lost_since    = 0.0
            self._lost_stopped  = False
            self._last_track    = None
            # A new person: their head, not the last one's.
            self._head_off      = None
            self._anchor_half_h = h / 2
            clicked = self._person_for(bbox)
            self._take_head(clicked)
            self._last_seen     = clicked if clicked is not None else Person(tuple(bbox))
            self._unconfirmed   = 0
            self._confirmed_at  = time.monotonic()
            self._aim_side      = (0, 0)
            # A new pick is centred straight away, box or no box.
            self._moving        = [True, True]
            self._sent_zero     = False
            if not was_tracking:
                self.tracking_active.emit(True)
        else:
            log.warning("Failed to initialise correlation tracker on selected person")

    def start_tracking(self, bbox: tuple[int, int, int, int]) -> bool:
        """
        Legacy API used by the manual-mode (YOLO unavailable) workflow.
        Identical to select_person() — kept so CVWindow works in both modes.
        """
        self.select_person(bbox)
        return self._tracking

    def stop_tracking(self) -> None:
        """Stop following the selected person.  Detection continues."""
        self._tracking      = False
        self._selected_bbox = None
        self._lost_since    = 0.0
        self._aim_px        = None
        self._tracker.stop()
        self._track_gen    += 1
        pt_preset = self._mm.state(self._mount_id).active_pt_preset
        self._mm.send_jog(self._mount_id, 0, 0, 0, 0, pt_preset=pt_preset)
        self.tracking_active.emit(False)

    def shutdown(self) -> None:
        """Stop timer and background threads.  Call before discarding."""
        self._tracking = False
        self._timer.stop()
        self._detect_exec.shutdown(wait=False, cancel_futures=True)
        self._track_exec.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------
    # Tick — runs on the Qt main thread, must stay non-blocking
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        t0 = time.perf_counter()
        self._tick_body()
        st = self._stats
        st.ticks += 1
        st.tick_ms.append((time.perf_counter() - t0) * 1000.0)
        if time.monotonic() - st.t0 >= CV_TIMING_REPORT_S:
            self._report_timing()

    def _tick_body(self) -> None:
        st = self._stats
        frame = self._capture.get_frame()
        if frame is None:
            st.no_frame += 1
            return

        self._frame_h, self._frame_w = frame.shape[:2]
        self._tick_count += 1
        if not self._context_logged:
            self._log_context()

        # ── YOLO detection (at most every DETECT_MIN_GAP_S) ───────────────
        now = time.monotonic()
        if (self._detect_pending is None and self._detector.available
                and now - self._last_detect_submit >= DETECT_MIN_GAP_S):
            self._last_detect_submit = now
            self._detect_ref_box = (self._selected_bbox
                                    if self._tracking and self._tracker.active else None)
            self._detect_pending = self._detect_exec.submit(
                _timed, self._detect_fn, frame.copy(), st, "detect_ms")

        if self._detect_pending is not None and self._detect_pending.done():
            st.detect_results += 1
            if self._last_detect_t:
                st.detect_gaps.append(now - self._last_detect_t)
            self._last_detect_t = now
            try:
                people = self._detect_pending.result()
                self._people = people
                dets = [p.bbox for p in people]
                self._detections = dets
                self.detections_updated.emit(list(dets))

                # Re-anchor the tracker on a fresh detection of the tracked
                # person — it prevents drift, it brings back a person the
                # tracker lost, and it re-measures where their head is.
                # Caught up first: the detection is of an older frame.
                if self._tracking and self._selected_bbox is not None:
                    people = self._catch_up()
                    match = self._person_for(self._selected_bbox, people)
                    if match is None and people:
                        # People, but none where the tracker is — see
                        # UNCONFIRMED_ROUNDS.
                        self._unconfirmed += 1
                        if self._unconfirmed >= UNCONFIRMED_ROUNDS:
                            match = self._near_last_seen(people)
                            if match is not None:
                                st.found_near += 1
                            elif self._tracker.active:
                                st.dropped += 1
                                self._tracker.stop()
                                self._track_gen += 1
                    if (match is None and self._tracker.active
                            and now - self._confirmed_at >= UNSEEN_DROP_S):
                        # Not confirmed for UNSEEN_DROP_S, whatever YOLO saw.
                        st.dropped_unseen += 1
                        self._tracker.stop()
                        self._track_gen += 1
                    if match is not None:
                        self._reinit_tracker(match, frame)
                        # A detection is a fresh position in its own right:
                        # drive on it.  Fed only by the tracker, the mount
                        # halted whenever a restarted tracker could not hold
                        # the person — in the replay, a quarter of restarts
                        # failed on the very frame they started on, MOSSE
                        # finding too little to lock onto in a dim, soft
                        # picture — though YOLO had them every 0.2 s.
                        self._drive_on(match.bbox)
            except Exception as exc:
                log.debug(f"Detection future: {exc}")
            self._detect_pending = None

        # ── Tracker: collect the last update, then start the next ─────────
        # Collected whether or not the tracker is still active.  A tracker
        # that loses the person switches itself off with that very update, and
        # its failure is the one result that must be read: left unread, the
        # last SUCCESS stayed in force and the mount was driven on it — 84% of
        # one ten-second window on the concert PC, 2026-09-28.
        #
        # Collected BEFORE the next update starts, so there is one update per
        # tick.  Started only on the tick after a collect, it ran every other
        # tick at best: 1-10 updates a second against 21 ticks.
        fresh: TrackResult | None = None
        if self._track_pending is not None and self._track_pending.done():
            try:
                raw = self._track_pending.result()
                if self._track_pending_gen != self._track_gen:
                    raw = None       # for a box the tracker has since left
                if raw is not None:
                    if raw.success:
                        st.track_ok += 1
                    else:
                        st.track_fail += 1
                    # Scale bbox coordinates back to full resolution.
                    if TRACK_SCALE < 1.0 and raw.bbox is not None:
                        inv = 1.0 / TRACK_SCALE
                        bx, by, bw, bh = raw.bbox
                        raw = TrackResult(
                            success=raw.success,
                            cx=raw.cx * inv,
                            cy=raw.cy * inv,
                            bbox=(int(bx * inv), int(by * inv),
                                  max(1, int(bw * inv)), max(1, int(bh * inv))),
                        )
                    self._last_track = raw
                    fresh = raw
            except Exception as exc:
                log.debug(f"Tracker future: {exc}")
            self._track_pending = None

        if self._tracking and self._tracker.active and self._track_pending is None:
            if TRACK_SCALE < 1.0:
                tw = int(self._frame_w * TRACK_SCALE)
                th = int(self._frame_h * TRACK_SCALE)
                small = cv2.resize(frame, (tw, th))
            else:
                small = frame.copy()
            self._track_pending_gen = self._track_gen
            self._track_pending = self._track_exec.submit(
                _timed, self._tracker.update, small, st, "track_ms")

        # ── Control law ───────────────────────────────────────────────────
        if self._tracking:
            st.tracking_ticks += 1
            if not self._tracker.active:
                st.lost_ticks += 1
            if fresh is not None and fresh.success and fresh.bbox is not None:
                if not self._tracker.active:
                    st.stale_drives += 1     # cannot happen now; kept as the check
                self._selected_bbox = fresh.bbox
                self._drive_on(fresh.bbox)
            elif (fresh is not None and not fresh.success) or not self._tracker.active:
                # Lost the person.  Nothing is sent from the last position; YOLO
                # looks for them near where they were.  After LOST_STOP_S the
                # mount is stopped, once; after MAX_LOST_S tracking is given up.
                if not self._lost_since:
                    self._lost_since = now
                if not self._lost_stopped and now - self._lost_since >= LOST_STOP_S:
                    self._lost_stopped = True
                    st.stops += 1
                    pt_preset = self._mm.state(self._mount_id).active_pt_preset
                    self._mm.send_jog(self._mount_id, 0, 0, 0, 0, pt_preset=pt_preset)
                if now - self._lost_since >= MAX_LOST_S:
                    self._tracking      = False
                    self._selected_bbox = None
                    self._aim_px        = None
                    self._lost_since    = 0.0
                    st.gave_up += 1
                    self.tracking_lost.emit()

        # ── Annotate frame — selected person green bbox ───────────────────
        # Detection box overlay (blue, numbered) is drawn by VideoLabel using Qt,
        # so it stays crisp at any display scale.  The selected (CSRT) bbox is
        # drawn here with OpenCV because it updates every frame — and with it
        # the point the camera is actually aimed at, the head, rather than the
        # middle of the box.
        display = frame
        if self._tracking and self._selected_bbox is not None:
            x, y, w, h = self._selected_bbox
            cv2.rectangle(display, (x, y), (x + w, y + h), (80, 210, 80), 2)
            if self._aim_px is not None:
                ax, ay = self._aim_px
                cv2.line(display, (ax - 10, ay), (ax + 10, ay), (80, 210, 80), 2)
                cv2.line(display, (ax, ay - 10), (ax, ay + 10), (80, 210, 80), 2)

        # The display slot is connected directly on this thread, so the
        # emit IS the display: scaling, overlay and paint.
        t_show = time.perf_counter()
        self.frame_ready.emit(display)
        st.show_ms.append((time.perf_counter() - t_show) * 1000.0)

    def _log_context(self) -> None:
        """Once per feed: what the timings below were measured with."""
        self._context_logged = True
        torch = sys.modules.get("torch")
        threads = torch.get_num_threads() if torch is not None else "not loaded"
        log.info("CV TIMING context: frame %dx%d, tracker %s requested, "
                 "YOLO %s at %s px (%s), torch threads %s, %s CPU cores",
                 self._frame_w, self._frame_h, self._tracker_kind.value,
                 "available" if self._detector.available else "unavailable",
                 getattr(self._detector, "_imgsz", "?"),
                 getattr(self._detector, "model_name", "?") or "?",
                 threads, os.cpu_count())

    def _report_timing(self) -> None:
        """The CV TIMING line — see CV_TIMING_REPORT_S."""
        st   = self._stats
        secs = max(1e-6, time.monotonic() - st.t0)
        cpu  = (time.process_time() - st.cpu0) / secs * 100.0

        parts = [f"{st.ticks / secs:.1f} ticks/s (target {TRACK_RATE_HZ})"
                 + (f", {st.no_frame} with no frame yet" if st.no_frame else "")]
        tick = f"tick {_avg_max(st.tick_ms)}"
        if st.show_ms:
            tick += f", of which display {_avg_max(st.show_ms)}"
        parts.append(tick)

        grabbed = getattr(self._capture, "frames_grabbed", None)
        if grabbed is not None and self._frames_at_report is not None:
            parts.append(f"camera {(grabbed - self._frames_at_report) / secs:.1f} fps")
        self._frames_at_report = grabbed

        if self._detector.available:
            yolo = (f"YOLO {st.detect_results / secs:.1f} results/s, "
                    f"{_avg_max(st.detect_ms)} each")
            if st.detect_gaps:
                yolo += (f", a result every {sum(st.detect_gaps) / len(st.detect_gaps):.2f} s"
                         f" (longest {max(st.detect_gaps):.2f})")
            parts.append(yolo)
        else:
            parts.append("YOLO unavailable")

        if st.tracking_ticks:
            name = self._tracker.kind_name or self._tracker_kind.value
            parts.append(f"tracker {name} {(st.track_ok + st.track_fail) / secs:.1f}/s, "
                         f"{_avg_max(st.track_ms)}, {st.track_ok} ok / "
                         f"{st.track_fail} lost the person")
            parts.append(f"lost {100.0 * st.lost_ticks / st.tracking_ticks:.0f}% "
                         f"of tracking time, driving on a stale position for "
                         f"{100.0 * st.stale_drives / st.tracking_ticks:.0f}%, "
                         f"{st.reanchors} re-anchors ({st.found_near} near where "
                         f"YOLO last saw them), {st.dropped} trackers dropped as "
                         f"off the person, {st.dropped_unseen} as out of sight, "
                         f"{st.stops} stop commands, "
                         f"{st.gave_up} tracks given up")
            parts.append(f"mount jogged {st.jogs / secs:.1f}/s")
            if st.aim_err:
                e = sorted(st.aim_err)
                parts.append(f"aim off target: median {e[len(e) // 2]:.0f} px, 90% within "
                             f"{e[min(len(e) - 1, int(0.9 * len(e)))]:.0f} px, crossed it "
                             f"{st.crossings} times")
            if st.drive_calls:
                parts.append(f"held still {100.0 * st.held / st.drive_calls:.0f}% "
                             f"(hold box {100.0 * self._hold:.0f}%)")
        else:
            parts.append("not tracking")

        parts.append(f"CPU {cpu:.0f}% of one core ({os.cpu_count()} cores)")
        log.info("CV TIMING %.0f s: %s", secs, " | ".join(parts))
        st.reset()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _drive_on(self, bbox: tuple) -> None:
        """Drive the mount at the head in bbox — a fresh position of the
        tracked person, from the tracker or from a detection — ending any
        loss."""
        self._lost_since   = 0.0
        self._lost_stopped = False
        ax, ay = self._aim(bbox)
        self._aim_px = (int(ax), int(ay))
        self._note_aim(ax - self._frame_w / 2 - self._target_cx,
                       ay - self._frame_h / 2 - self._target_cy)
        self._drive(ax - self._frame_w / 2, ay - self._frame_h / 2)

    def _note_aim(self, ex: float, ey: float) -> None:
        """For the CV TIMING line: how far the aim point is from the target,
        and each time it crosses to the other side — on either axis, clear of
        twice the deadband so pose jitter does not count.  The line can show
        how the mount closes on a person, where neither the jogs nor the
        mount's position are logged."""
        st = self._stats
        st.aim_err.append(math.hypot(ex, ey))
        clear = 2.0 * DEADBAND_FRACTION * self._frame_w
        side = tuple(0 if abs(e) <= clear else (1 if e > 0 else -1) for e in (ex, ey))
        for was, now in zip(self._aim_side, side):
            if was and now and was != now:
                st.crossings += 1
        self._aim_side = tuple(n or w for w, n in zip(self._aim_side, side))

    def _drive(self, bbox_cx: float, bbox_cy: float) -> None:
        """Drive the mount to bring the aim point onto the target: each axis
        at a speed proportional to its error (see TRACK_SPEED_DPS), within
        what the active preset can do and stop from — or holding still while
        the aim stays in the hold box (see HOLD_FRACTION).  CV's jogs only —
        the joystick's go their own way, curve and all."""
        ex, ey = bbox_cx - self._target_cx, bbox_cy - self._target_cy
        # Hold or move, each axis for itself, decided on every fresh position
        # (sent below at the jog rate).  A box no bigger than the arrival
        # margin is no box: follow every movement.
        st = self._stats
        for i, (e, size) in enumerate(((ex, self._frame_w), (ey, self._frame_h))):
            if self._hold <= ARRIVE_FRACTION:
                self._moving[i] = True
            elif not self._moving[i] and abs(e) > self._hold * size:
                self._moving[i] = True
            elif self._moving[i] and abs(e) <= ARRIVE_FRACTION * size:
                self._moving[i] = False
        st.drive_calls += 1
        if not any(self._moving):
            st.held += 1

        pt_preset = self._mm.state(self._mount_id).active_pt_preset
        vmax, accel = self._pt_preset_speed(self._mount_id, pt_preset)
        pan_jog  = axis_jog(ex, self._gain_pan, self._frame_w, vmax, accel) \
            if self._moving[0] else 0
        tilt_jog = axis_jog(ey, self._gain_tilt, self._frame_w, vmax, accel) \
            if self._moving[1] else 0
        # Holding: one stop, then nothing — the radio carries no stream of
        # zeros while the shot is still.
        if not pan_jog and not tilt_jog and self._sent_zero:
            return

        # At most JOG_RATE_HZ.  The schedule moves on one interval per jog, so
        # at 30 ticks/s the jogs land on alternate ticks and on consecutive
        # ones by turns — 20 a second on average.  After a pause (a loss, or
        # the first jog) it restarts from now rather than catching up.
        now = time.monotonic()
        if now < self._jog_due:
            return
        self._jog_due += JOG_INTERVAL_S
        if self._jog_due < now:
            self._jog_due = now + JOG_INTERVAL_S
        st.jogs += 1
        self._sent_zero = not pan_jog and not tilt_jog

        # axis_mask=0x03: pan + tilt only — never interrupts a slider move.
        # Positive err_y means subject is below target → tilt down = negative jog.
        self._mm.send_jog(self._mount_id, pan_jog, -tilt_jog, 0, 0,
                          pt_preset=pt_preset, axis_mask=0x03)

    def _person_for(self, ref: tuple, people: list[Person] | None = None) -> Person | None:
        """The detection (of the latest, or of `people`) that is the person
        in ref — the highest IoU, and at least IOU_REANCHOR — or None."""
        best_iou, best = IOU_REANCHOR, None
        for p in (self._people if people is None else people):
            score = _iou(ref, p.bbox)
            if score > best_iou:
                best_iou, best = score, p
        return best

    def _near_last_seen(self, people: list[Person]) -> Person | None:
        """Of `people`, the one nearest where YOLO last saw the tracked
        person — head to head, or the box's top centre where there is no
        head — if within REACQUIRE_WIDTHS of that person's width; else None."""
        seen = self._last_seen
        if seen is None:
            return None
        sx, sy = _head_or_top(seen)
        best, best_d = None, REACQUIRE_WIDTHS * seen.bbox[2]
        for p in people:
            px, py = _head_or_top(p)
            d = math.hypot(px - sx, py - sy)
            if d <= best_d:
                best, best_d = p, d
        return best

    def _catch_up(self) -> list[Person]:
        """The latest detections, moved on by how far the tracked person has
        moved since the detection's frame.

        YOLO runs on a frame that is 50-250 ms old by the time its answer is
        used, and the tracker is re-anchored on the CURRENT frame.  Moving —
        the camera following them, if nothing else — the person is no longer
        where the detection says: in the operator's recording, 35 px, and the
        tracker, started on the wrong spot, stayed on it until the next
        detection.  The tracker saw the frames in between, so its own motion
        since the detection started is the correction.  Nothing to go on when
        it was not tracking then, or is not now: the detections as they are."""
        ref, cur = self._detect_ref_box, self._selected_bbox
        if ref is None or cur is None or not self._tracker.active:
            return self._people
        dx = (cur[0] + cur[2] / 2) - (ref[0] + ref[2] / 2)
        dy = (cur[1] + cur[3] / 2) - (ref[1] + ref[3] / 2)
        if not dx and not dy:
            return self._people
        moved = []
        for p in self._people:
            x, y, w, h = p.bbox
            head = (p.head[0] + dx, p.head[1] + dy) if p.head is not None else None
            moved.append(Person((int(round(x + dx)), int(round(y + dy)), w, h), head, p.head_from))
        return moved

    def _take_head(self, person: Person | None) -> None:
        """Measure where the head sits in the box, from a detection that
        placed it.  One that did not — the plain model, or neither face nor
        shoulders seen — leaves the last measurement in place."""
        if person is None or person.head is None:
            return
        x, y, w, h = person.bbox
        hx, hy = person.head
        self._head_off = (hx - (x + w / 2), hy - y)

    def _aim(self, bbox: tuple) -> tuple[float, float]:
        """Where to aim, in frame pixels: the head, carried with the box — or,
        with no head measured, HEAD_TRACK_FRACTION down the box.

        bbox is the tracker's box.  Its centre moves with the person; its top
        is rebuilt from that centre and the height of the box it was started
        on (see _anchor_half_h), because the tracker resizes its own."""
        x, y, w, h = bbox
        half = self._anchor_half_h if self._anchor_half_h is not None else h / 2
        cx, top = x + w / 2, y + h / 2 - half
        if self._head_off is not None:
            dx, dy = self._head_off
            return (cx + dx, top + dy)
        return (cx, top + 2 * half * HEAD_TRACK_FRACTION)

    def _reinit_tracker(self, person: Person, frame: np.ndarray) -> None:
        """Re-anchor the tracker on a fresh detection of the tracked person,
        and take where their head is from it."""
        x, y, w, h = person.bbox
        if TRACK_SCALE < 1.0:
            tw = int(self._frame_w * TRACK_SCALE)
            th = int(self._frame_h * TRACK_SCALE)
            small  = cv2.resize(frame, (tw, th))
            scaled = (int(x * TRACK_SCALE), int(y * TRACK_SCALE),
                      max(1, int(w * TRACK_SCALE)), max(1, int(h * TRACK_SCALE)))
        else:
            small, scaled = frame, person.bbox
        self._tracker.init(small, scaled, self._tracker_kind)
        self._track_gen += 1
        self._selected_bbox = person.bbox
        self._anchor_half_h = h / 2
        self._take_head(person)
        self._last_seen   = person
        self._unconfirmed = 0
        self._confirmed_at = time.monotonic()
        self._stats.reanchors += 1
        log.debug(f"tracker re-anchored to YOLO detection {person.bbox}")
