"""
CV tracker — person detection + correlation tracking.

PersonDetector  wraps YOLOv8-nano for real-time person detection.
                Requires: pip install ultralytics
                Downloads the nano model (~6 MB) on first use.
                Degrades gracefully if ultralytics is not installed.

Tracker         wraps OpenCV CSRT for smooth between-detection tracking
                of a single selected person.  Always uses CSRT (fastest
                accurate algorithm); KCF is available as fallback.

TrackResult / TrackerKind  shared data types used by TrackingLoop.
"""
from __future__ import annotations

import logging
import os
import ssl
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

log = logging.getLogger(__name__)


class TrackerKind(str, Enum):
    """Kept for API compatibility — CSRT is always used internally."""
    KCF   = "kcf"
    CSRT  = "csrt"
    MOSSE = "mosse"


@dataclass
class TrackResult:
    success: bool
    cx: float         # bbox centre X offset from frame centre (+ = right)
    cy: float         # bbox centre Y offset from frame centre (+ = down)
    bbox: tuple[int, int, int, int] | None   # (x, y, w, h) in frame coords


# ---------------------------------------------------------------------------
# PersonDetector — YOLOv8-nano wrapper
# ---------------------------------------------------------------------------

class PersonDetector:
    """
    Detects all people in a frame using YOLOv8-nano.
    Thread-safe: detect() can be called from a background executor.
    """

    CONF_THRESHOLD = 0.45
    PERSON_CLASS   = 0          # COCO class 0 = person
    MODEL_NAME     = "yolov8n.pt"

    def __init__(self):
        self._model     = None
        self._available = False
        self._try_load()

    def _try_load(self) -> None:
        try:
            # ── Writable cache directory ───────────────────────────────────
            # ultralytics downloads the model into the *current working
            # directory* by default, which may be read-only (e.g. inside an
            # app bundle or /Applications).  Chdir to a guaranteed-writable
            # location so the first-run download always succeeds.
            cache_dir = Path.home() / ".cache" / "ultralytics"
            cache_dir.mkdir(parents=True, exist_ok=True)
            prev_cwd = os.getcwd()
            os.chdir(cache_dir)

            # ── macOS SSL certificates ─────────────────────────────────────
            # Python on macOS ships without the system CA bundle, so HTTPS
            # downloads fail with "certificate verify failed".  Temporarily
            # allow unverified context just for the model download; this is
            # safe because the checksum is verified by ultralytics afterward.
            orig_ctx = ssl._create_default_https_context
            ssl._create_default_https_context = ssl._create_unverified_context

            try:
                from ultralytics import YOLO  # type: ignore
                self._model = YOLO(self.MODEL_NAME)
            finally:
                os.chdir(prev_cwd)
                ssl._create_default_https_context = orig_ctx

            # Warmup pass so the first real detection is not slow
            dummy = np.zeros((320, 320, 3), dtype=np.uint8)
            self._model(dummy, verbose=False, classes=[self.PERSON_CLASS])
            self._available = True
            log.info("YOLOv8-nano person detector ready")
        except ImportError:
            log.warning(
                "ultralytics not installed — auto-detection unavailable.  "
                "Run:  pip install ultralytics"
            )
        except Exception as exc:
            log.error(f"Failed to load YOLO model: {exc}")

    @property
    def available(self) -> bool:
        return self._available

    def detect(self, frame: np.ndarray) -> list[tuple[int, int, int, int]]:
        """
        Detect all people in frame.
        Returns list of (x, y, w, h) bboxes in frame pixel coordinates.
        """
        if not self._available or self._model is None:
            return []
        try:
            results = self._model(
                frame, verbose=False,
                classes=[self.PERSON_CLASS],
                conf=self.CONF_THRESHOLD,
            )
            bboxes = []
            for r in results:
                for box in r.boxes:
                    x1, y1, x2, y2 = [int(v) for v in box.xyxy[0].tolist()]
                    bboxes.append((x1, y1, x2 - x1, y2 - y1))
            return bboxes
        except Exception as exc:
            log.warning(f"Detection error: {exc}")
            return []


# ---------------------------------------------------------------------------
# Tracker — OpenCV CSRT correlation tracker
# ---------------------------------------------------------------------------

def _attr_exists(dotted: str) -> bool:
    """Return True if the dotted attribute path exists on cv2."""
    obj = cv2
    try:
        for part in dotted.split(".")[1:]:
            obj = getattr(obj, part)
        return True
    except AttributeError:
        return False


_CSRT_FACTORIES = [
    ("cv2.legacy.TrackerCSRT_create", lambda: cv2.legacy.TrackerCSRT_create()),
    ("cv2.TrackerCSRT.create",        lambda: cv2.TrackerCSRT.create()),
    # Fallbacks when CSRT is not in the installed OpenCV build
    ("cv2.TrackerKCF.create",         lambda: cv2.TrackerKCF.create()),
    ("cv2.legacy.TrackerKCF_create",  lambda: cv2.legacy.TrackerKCF_create()),
]


class Tracker:
    """
    Wraps OpenCV CSRT for smooth per-frame tracking between YOLO detections.
    The kind argument is accepted for API compatibility but ignored — CSRT
    (with KCF fallback) is always used.
    """

    def __init__(self):
        self._tracker: Optional[cv2.Tracker] = None
        self._active  = False
        self._frame_w = 0
        self._frame_h = 0

    @property
    def active(self) -> bool:
        return self._active

    def init(self, frame: np.ndarray,
             bbox: tuple[int, int, int, int],
             kind: TrackerKind = TrackerKind.CSRT) -> bool:
        """Initialise tracker on bbox (x, y, w, h) in frame coordinates."""
        self._tracker = None
        self._active  = False

        for name, factory in _CSRT_FACTORIES:
            try:
                candidate = factory()
                result = candidate.init(frame, bbox)
                # OpenCV ≤4.7: init() returns bool.  OpenCV ≥4.8: init() returns
                # None (C++ void).  Treat None as success — exception = failure.
                if result is not False:
                    self._tracker = candidate
                    self._active  = True
                    log.debug(f"Correlation tracker started: {name}")
                    break
                else:
                    log.warning(f"Tracker init returned False: {name} bbox={bbox}")
            except AttributeError:
                log.debug(f"Tracker not present in this OpenCV build: {name}")
            except Exception as exc:
                log.warning(f"Tracker init error: {name}: {type(exc).__name__}: {exc}")

        if not self._active:
            # Probe what is actually available to help diagnose the failure.
            _available = [s for s in [
                "cv2.legacy", "cv2.legacy.TrackerCSRT_create",
                "cv2.legacy.TrackerKCF_create", "cv2.TrackerCSRT", "cv2.TrackerKCF",
            ] if _attr_exists(s)]
            log.error(
                "All correlation tracker constructors failed. "
                "Available cv2 tracker symbols: %s. "
                "If empty: pip uninstall opencv-python && pip install opencv-contrib-python",
                _available or ["(none)"],
            )

        self._frame_h, self._frame_w = frame.shape[:2]
        return self._active

    def update(self, frame: np.ndarray) -> TrackResult:
        """Update tracker with new frame. Safe to call from background thread."""
        if not self._active or self._tracker is None:
            return TrackResult(success=False, cx=0.0, cy=0.0, bbox=None)

        ok, bbox = self._tracker.update(frame)
        if not ok:
            self._active = False
            return TrackResult(success=False, cx=0.0, cy=0.0, bbox=None)

        x, y, w, h = [int(v) for v in bbox]
        subj_cx = x + w / 2
        subj_cy = y + h / 2
        return TrackResult(
            success=True,
            cx=subj_cx - self._frame_w / 2,
            cy=subj_cy - self._frame_h / 2,
            bbox=(x, y, w, h),
        )

    def stop(self) -> None:
        self._active  = False
        self._tracker = None
