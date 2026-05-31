"""
CaptureSource — wraps OpenCV VideoCapture for USB capture cards.

Runs capture in a background thread and provides the latest frame
via get_frame() without blocking the UI.
"""
from __future__ import annotations

import threading
import logging
from typing import Optional

import sys

import cv2
import numpy as np

log = logging.getLogger(__name__)

# Pick the best capture backend for the current platform.
# CAP_DSHOW is Windows-only; using it on macOS/Linux returns isOpened()==False
# for every device index, making enumeration always return an empty list.
if sys.platform == "win32":
    _CAP_BACKEND = cv2.CAP_DSHOW
elif sys.platform == "darwin":
    _CAP_BACKEND = cv2.CAP_AVFOUNDATION
else:
    _CAP_BACKEND = cv2.CAP_V4L2


class CaptureSource:
    """
    Opens a USB capture device and continuously grabs frames.

    Usage:
        cap = CaptureSource()
        cap.open(device_index=0)
        frame = cap.get_frame()   # latest frame or None
        cap.close()
    """

    def __init__(self):
        self._cap: Optional[cv2.VideoCapture] = None
        self._frame: Optional[np.ndarray]     = None
        self._lock   = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def open(self, device_index: int = 0,
             width: int = 1280, height: int = 720) -> bool:
        self._cap = cv2.VideoCapture(device_index, _CAP_BACKEND)
        if not self._cap.isOpened():
            # Fallback: let OpenCV choose (covers unusual setups)
            self._cap = cv2.VideoCapture(device_index)
        if not self._cap.isOpened():
            log.error(f"Cannot open capture device {device_index}")
            return False

        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH,  width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # minimise latency

        self._running = True
        self._thread  = threading.Thread(target=self._grab_loop, daemon=True)
        self._thread.start()
        log.info(f"Capture opened: device {device_index}")
        return True

    def close(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        if self._cap:
            self._cap.release()
        self._cap   = None
        self._frame = None

    def get_frame(self) -> Optional[np.ndarray]:
        """Return a copy of the latest frame, or None if not ready."""
        with self._lock:
            if self._frame is None:
                return None
            return self._frame.copy()

    @property
    def is_open(self) -> bool:
        return self._cap is not None and self._cap.isOpened()

    @staticmethod
    def list_devices(max_test: int = 5) -> list[int]:
        """Return indices of available capture devices."""
        available = []
        for i in range(max_test):
            cap = cv2.VideoCapture(i, _CAP_BACKEND)
            if cap.isOpened():
                available.append(i)
            cap.release()
        return available

    # ------------------------------------------------------------------
    # Background grab loop
    # ------------------------------------------------------------------

    def _grab_loop(self) -> None:
        while self._running and self._cap and self._cap.isOpened():
            ret, frame = self._cap.read()
            if ret and frame is not None:
                # AVFoundation (macOS) can deliver BGRA frames.  CSRT and most
                # OpenCV processing expect 3-channel BGR, so strip the alpha if
                # present.  Leave grayscale frames as-is (convert in callers).
                if frame.ndim == 3 and frame.shape[2] == 4:
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                with self._lock:
                    self._frame = frame
