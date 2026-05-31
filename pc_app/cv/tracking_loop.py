"""
TrackingLoop — detection-first closed-loop pan/tilt controller.

Architecture
------------
Two background workers run in parallel via separate ThreadPoolExecutors:

  Detector  (slow, ~150-300 ms per frame):
    YOLOv8-nano runs every DETECT_EVERY_N ticks (~3×/s at 30 Hz), finding
    all people in the frame.  Results update the detection overlay shown in
    the CV window and periodically re-anchor the correlation tracker to
    prevent drift.

  Tracker  (fast, ~5-30 ms per frame):
    OpenCV CSRT follows the selected person on every tick for smooth,
    low-latency motion.  Briefly loses track on full 180° turns; YOLO
    re-anchors it automatically on the next detection cycle.

Operator workflow
-----------------
  • Feed opens → people detected and shown as numbered blue boxes.
  • Operator clicks any box → mount starts tracking that person immediately.
  • Operator clicks a different box → smooth switch to that person.
  • "Stop Tracking" → detection continues, mount stops.

Target point
------------
  target_cx / target_cy define where in the frame the tracked person's bbox
  centre should sit (offset from frame centre, in full-resolution pixels).
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

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

import cv2
import numpy as np

from .capture import CaptureSource
from .tracker import Tracker, TrackResult, PersonDetector, TrackerKind
from comms.mount_manager import MountManager

log = logging.getLogger(__name__)

TRACK_RATE_HZ   = 30
TRACK_INTERVAL  = 1000 // TRACK_RATE_HZ   # ms between ticks
DETECT_EVERY_N  = 10    # run YOLO every N ticks  (~3×/s at 30 Hz)
TRACK_SCALE     = 0.5   # downscale factor for CSRT (4× faster, small accuracy hit)
MAX_CSRT_MISSES = 15    # consecutive CSRT failures before tracking_lost (~0.5 s)
IOU_REANCHOR    = 0.30  # minimum IoU to accept a YOLO detection as the same person

# Head tracking: fraction of bbox height from the top used as the aim point.
# 0.12 ≈ top of head, 0.20 ≈ eye-line (good for "head and shoulders" framing).
# CSRT still tracks the whole-body bbox for stability; only the aim point shifts.
HEAD_TRACK_FRACTION = 0.15

DEFAULT_GAIN_PAN  = 2.0
DEFAULT_GAIN_TILT = 2.0
MAX_JOG_VALUE     = 1000
CURVE_EXP         = 1.5


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
                 capture: CaptureSource, parent=None):
        super().__init__(parent)
        self._mm       = mount_manager
        self._capture  = capture
        self._mount_id = 1
        self._gain_pan  = DEFAULT_GAIN_PAN
        self._gain_tilt = DEFAULT_GAIN_TILT

        # ── State ──────────────────────────────────────────────────────────
        self._detections:    list[tuple] = []    # latest YOLO person bboxes
        self._selected_bbox: tuple | None = None  # current CSRT-tracked bbox
        self._tracking    = False
        self._tick_count  = 0
        self._csrt_misses = 0

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
        self._detector = PersonDetector()
        self._tracker  = Tracker()

        # Separate executors so detection and tracking run in parallel.
        self._detect_exec = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="yolo")
        self._track_exec  = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="csrt")

        self._detect_pending: concurrent.futures.Future | None = None
        self._track_pending:  concurrent.futures.Future | None = None
        self._last_track:     TrackResult | None = None

        self._timer = QTimer(self)
        self._timer.setInterval(TRACK_INTERVAL)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

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

    def set_tracker_kind(self, kind: TrackerKind) -> None:
        """No-op — CSRT is always used.  Kept for API compatibility."""

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
        ok = self._tracker.init(small, scaled)
        if ok:
            self._selected_bbox = bbox
            self._tracking      = True
            self._csrt_misses   = 0
            self._last_track    = None
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
        self._csrt_misses   = 0
        self._tracker.stop()
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
        frame = self._capture.get_frame()
        if frame is None:
            return

        self._frame_h, self._frame_w = frame.shape[:2]
        self._tick_count += 1

        # ── YOLO detection (every DETECT_EVERY_N ticks) ───────────────────
        if (self._tick_count % DETECT_EVERY_N == 0
                and self._detect_pending is None
                and self._detector.available):
            self._detect_pending = self._detect_exec.submit(
                self._detector.detect, frame.copy())

        if self._detect_pending is not None and self._detect_pending.done():
            try:
                dets = self._detect_pending.result()
                self._detections = dets
                self.detections_updated.emit(list(dets))

                # Re-anchor CSRT when a fresh YOLO detection overlaps the
                # currently tracked bbox — prevents long-term drift.
                if self._tracking and self._selected_bbox is not None:
                    match = self._best_match(self._selected_bbox, dets)
                    if match is not None:
                        self._reinit_tracker(match, frame)
            except Exception as exc:
                log.debug(f"Detection future: {exc}")
            self._detect_pending = None

        # ── CSRT tracking (every tick) ────────────────────────────────────
        if self._tracking and self._tracker.active:
            if self._track_pending is None:
                if TRACK_SCALE < 1.0:
                    tw = int(self._frame_w * TRACK_SCALE)
                    th = int(self._frame_h * TRACK_SCALE)
                    small = cv2.resize(frame, (tw, th))
                else:
                    small = frame.copy()
                self._track_pending = self._track_exec.submit(
                    self._tracker.update, small)

            if self._track_pending is not None and self._track_pending.done():
                try:
                    raw = self._track_pending.result()
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
                except Exception as exc:
                    log.debug(f"Tracker future: {exc}")
                self._track_pending = None

        # ── Control law ───────────────────────────────────────────────────
        if self._tracking:
            result = self._last_track
            if result is not None and result.success:
                self._csrt_misses   = 0
                self._selected_bbox = result.bbox
                # Derive head position from the top of the tracked bbox rather
                # than its centre.  CSRT tracks the whole body for stability;
                # HEAD_TRACK_FRACTION brings the aim point up to eye/head level.
                if result.bbox is not None:
                    bx, by, bw, bh = result.bbox
                    head_cx = (bx + bw / 2) - self._frame_w / 2
                    head_cy = (by + bh * HEAD_TRACK_FRACTION) - self._frame_h / 2
                else:
                    head_cx, head_cy = result.cx, result.cy
                self._drive(head_cx, head_cy)
            elif result is not None and not result.success:
                # Stop the mount immediately so it doesn't drift blindly.
                self._csrt_misses += 1
                pt_preset = self._mm.state(self._mount_id).active_pt_preset
                self._mm.send_jog(self._mount_id, 0, 0, 0, 0, pt_preset=pt_preset)
                if self._csrt_misses >= MAX_CSRT_MISSES:
                    # YOLO had enough time to re-anchor and couldn't — give up.
                    self._tracking      = False
                    self._selected_bbox = None
                    self.tracking_lost.emit()

        # ── Annotate frame — selected person green bbox ───────────────────
        # Detection box overlay (blue, numbered) is drawn by VideoLabel using Qt,
        # so it stays crisp at any display scale.  The selected (CSRT) bbox is
        # drawn here with OpenCV because it updates every frame.
        display = frame
        if self._tracking and self._selected_bbox is not None:
            x, y, w, h = self._selected_bbox
            cv2.rectangle(display, (x, y), (x + w, y + h), (80, 210, 80), 2)
            cx_px = x + w // 2
            cy_px = y + h // 2
            cv2.line(display, (cx_px - 10, cy_px), (cx_px + 10, cy_px), (80, 210, 80), 1)
            cv2.line(display, (cx_px, cy_px - 10), (cx_px, cy_px + 10), (80, 210, 80), 1)

        self.frame_ready.emit(display)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _drive(self, bbox_cx: float, bbox_cy: float) -> None:
        """Power-curve P-controller: drive mount to keep subject at target."""
        err_x = bbox_cx - self._target_cx
        err_y = bbox_cy - self._target_cy

        norm_x = max(-1.0, min(1.0, err_x / (self._frame_w / 2)))
        norm_y = max(-1.0, min(1.0, err_y / (self._frame_h / 2)))

        curved_x = math.copysign(abs(norm_x) ** CURVE_EXP, norm_x)
        curved_y = math.copysign(abs(norm_y) ** CURVE_EXP, norm_y)

        pan_jog  = int(max(-MAX_JOG_VALUE, min(MAX_JOG_VALUE,
                           curved_x * MAX_JOG_VALUE * self._gain_pan)))
        tilt_jog = int(max(-MAX_JOG_VALUE, min(MAX_JOG_VALUE,
                           curved_y * MAX_JOG_VALUE * self._gain_tilt)))

        pt_preset = self._mm.state(self._mount_id).active_pt_preset
        # axis_mask=0x03: pan + tilt only — never interrupts a slider move.
        # Positive err_y means subject is below target → tilt down = negative jog.
        self._mm.send_jog(self._mount_id, pan_jog, -tilt_jog, 0, 0,
                          pt_preset=pt_preset, axis_mask=0x03)

    def _best_match(self, ref: tuple,
                    detections: list[tuple]) -> tuple | None:
        """Return the detection with highest IoU overlap with ref, or None."""
        best_iou, best = IOU_REANCHOR, None
        for det in detections:
            score = _iou(ref, det)
            if score > best_iou:
                best_iou, best = score, det
        return best

    def _reinit_tracker(self, bbox: tuple, frame: np.ndarray) -> None:
        """Re-anchor CSRT on a fresh YOLO detection bbox to correct drift."""
        x, y, w, h = bbox
        if TRACK_SCALE < 1.0:
            tw = int(self._frame_w * TRACK_SCALE)
            th = int(self._frame_h * TRACK_SCALE)
            small  = cv2.resize(frame, (tw, th))
            scaled = (int(x * TRACK_SCALE), int(y * TRACK_SCALE),
                      max(1, int(w * TRACK_SCALE)), max(1, int(h * TRACK_SCALE)))
        else:
            small, scaled = frame, bbox
        self._tracker.init(small, scaled)
        self._selected_bbox = bbox
        log.debug(f"CSRT re-anchored to YOLO detection {bbox}")
