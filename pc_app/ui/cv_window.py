"""
CVWindow — live camera feed with automatic person detection and tracking.

Operator workflow (YOLO available)
-----------------------------------
  1. Select capture device and click "Start Feed".
  2. People are detected automatically — numbered blue boxes appear.
  3. Click any box to start tracking that person (green box).
  4. Click a different box to switch subjects smoothly.
  5. Toggle "Set Target" to reposition the ⊕ target point in the frame.
  6. Click "Stop Tracking" or close to revert to joystick.

Fallback workflow (ultralytics not installed)
----------------------------------------------
  Steps 1–2: same.
  3. Drag a box around the subject manually.
  4. Click "Start Tracking".
  Install ultralytics for the full detection workflow:
      pip install ultralytics
"""
from __future__ import annotations

import logging
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QSlider, QSizePolicy, QFrame
)
from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot, QPoint
from PyQt6.QtGui import (
    QImage, QPixmap, QPainter, QPen, QColor, QBrush,
    QMouseEvent, QFont
)

import numpy as np
import cv2

from cv.capture import CaptureSource
from cv.tracker import TrackerKind
from cv.tracking_loop import TrackingLoop
from comms.mount_manager import MountManager
from config.mount_config import AppConfig

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# VideoLabel
# ---------------------------------------------------------------------------

class VideoLabel(QLabel):
    """
    QLabel that displays video frames with two operating modes:

    Detection mode (YOLO available):
      • Shows numbered blue boxes for every detected person.
      • Click a box to emit person_clicked(bbox).
      • Shows an orange ⊕ target crosshair.
      • In "Set Target" mode, click anywhere to reposition the crosshair
        and emit target_moved(cx, cy).

    Manual mode (YOLO unavailable):
      • Drag to draw a selection box.
      • Emits bbox_selected(bbox) on mouse release.
    """

    person_clicked = pyqtSignal(tuple)         # (x, y, w, h) in frame coords
    target_moved   = pyqtSignal(float, float)  # (cx, cy) offset from centre
    bbox_selected  = pyqtSignal(tuple)         # manual-mode draw result

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(640, 360)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setStyleSheet("background: #0A0A0A;")

        # Mode
        self._detection_mode   = False
        self._move_target_mode = False

        # Detection overlay state
        self._detections: list[tuple] = []    # (x, y, w, h) per person

        # Target crosshair — offset from frame centre in frame pixels.
        # Initialised to upper-third once frame_h is known.
        self._target_cx: float = 0.0
        self._target_cy: float = 0.0

        # Frame dimensions
        self._frame_w = 1280
        self._frame_h = 720

        # Manual-mode draw state
        self._drawing     = False
        self._start_pt    = QPoint()
        self._end_pt      = QPoint()
        self._bbox_ready: tuple | None = None

    # ------------------------------------------------------------------
    # Public setters
    # ------------------------------------------------------------------

    def set_detection_mode(self, enabled: bool) -> None:
        """Switch between detection mode (YOLO) and manual-draw mode."""
        self._detection_mode   = enabled
        self._move_target_mode = False
        self._bbox_ready       = None
        self._detections       = []
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def set_detections(self, detections: list[tuple]) -> None:
        """Update the detection overlay list (called from detections_updated signal)."""
        self._detections = detections

    def set_target(self, cx: float, cy: float) -> None:
        """Set the target crosshair position (offset from frame centre, frame px)."""
        self._target_cx = cx
        self._target_cy = cy

    def set_move_target_mode(self, enabled: bool) -> None:
        """Toggle the 'click to reposition target' mode."""
        self._move_target_mode = enabled
        self.setCursor(Qt.CursorShape.CrossCursor if enabled
                       else Qt.CursorShape.ArrowCursor)

    # Manual-mode compat
    @property
    def selected_bbox(self) -> tuple | None:
        return self._bbox_ready

    def clear_bbox(self) -> None:
        self._bbox_ready = None
        self._drawing    = False

    # ------------------------------------------------------------------
    # Frame display
    # ------------------------------------------------------------------

    def set_frame(self, frame: np.ndarray) -> None:
        self._frame_h, self._frame_w = frame.shape[:2]
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        img = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888)
        scaled = img.scaled(self.size(),
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)
        pix = QPixmap.fromImage(scaled)
        disp_w, disp_h = scaled.width(), scaled.height()

        if self._detection_mode:
            pix = self._draw_detection_overlay(pix, disp_w, disp_h)
        elif self._drawing or self._bbox_ready:
            pix = self._draw_manual_bbox(pix, disp_w, disp_h)

        self.setPixmap(pix)

    # ------------------------------------------------------------------
    # Detection-mode overlay
    # ------------------------------------------------------------------

    def _draw_detection_overlay(self, pix: QPixmap,
                                 disp_w: int, disp_h: int) -> QPixmap:
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        off_x = (self.width()  - disp_w) // 2
        off_y = (self.height() - disp_h) // 2
        sx = disp_w / self._frame_w
        sy = disp_h / self._frame_h

        # ── Detection boxes (blue, numbered) ─────────────────────────
        font = QFont()
        font.setPixelSize(13)
        font.setBold(True)
        painter.setFont(font)

        for i, (x, y, w, h) in enumerate(self._detections):
            dx = int(x * sx) + off_x
            dy = int(y * sy) + off_y
            dw = int(w * sx)
            dh = int(h * sy)

            # Box
            painter.setPen(QPen(QColor("#4FC3F7"), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(dx, dy, dw, dh)

            # Number badge
            label = str(i + 1)
            badge_w, badge_h = 20, 18
            painter.setBrush(QBrush(QColor("#4FC3F7")))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRect(dx, dy - badge_h, badge_w, badge_h)
            painter.setPen(QPen(QColor("#000000")))
            painter.drawText(dx + 2, dy - 2, label)

        # ── Target ⊕ crosshair (orange) ───────────────────────────────
        # Convert frame-centre-relative to display coordinates.
        tx = int((self._frame_w / 2 + self._target_cx) * sx) + off_x
        ty = int((self._frame_h / 2 + self._target_cy) * sy) + off_y

        pen = QPen(QColor("#FF9800"), 2)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        r = 12
        painter.drawEllipse(tx - r, ty - r, 2 * r, 2 * r)
        painter.drawLine(tx - r - 6, ty, tx + r + 6, ty)
        painter.drawLine(tx, ty - r - 6, tx, ty + r + 6)

        # Label
        painter.setPen(QPen(QColor("#FF9800")))
        font2 = QFont()
        font2.setPixelSize(10)
        painter.setFont(font2)
        painter.drawText(tx + r + 4, ty + 4, "target")

        painter.end()
        return pix

    # ------------------------------------------------------------------
    # Manual-mode box drawing
    # ------------------------------------------------------------------

    def _draw_manual_bbox(self, pix: QPixmap,
                           disp_w: int, disp_h: int) -> QPixmap:
        painter = QPainter(pix)
        pen = QPen(QColor("#4FC3F7"), 2, Qt.PenStyle.DashLine)
        painter.setPen(pen)
        off_x = (self.width()  - disp_w) // 2
        off_y = (self.height() - disp_h) // 2
        sx = self._start_pt.x() - off_x
        sy = self._start_pt.y() - off_y
        ex = self._end_pt.x()   - off_x
        ey = self._end_pt.y()   - off_y
        painter.drawRect(min(sx, ex), min(sy, ey), abs(ex - sx), abs(ey - sy))
        painter.end()
        return pix

    # ------------------------------------------------------------------
    # Mouse events
    # ------------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pt = event.position().toPoint()

        if self._detection_mode:
            if self._move_target_mode:
                # Reposition the target crosshair
                fx, fy = self._screen_to_frame(pt.x(), pt.y())
                cx = fx - self._frame_w / 2
                cy = fy - self._frame_h / 2
                self._target_cx = cx
                self._target_cy = cy
                self.target_moved.emit(cx, cy)
            else:
                # Click-to-select a person
                fx, fy = self._screen_to_frame(pt.x(), pt.y())
                clicked = self._hit_test(fx, fy)
                if clicked is not None:
                    self.person_clicked.emit(clicked)
        else:
            # Manual mode: start drawing
            self._drawing    = True
            self._start_pt   = pt
            self._end_pt     = pt
            self._bbox_ready = None

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drawing:
            self._end_pt = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if not self._detection_mode and self._drawing and \
                event.button() == Qt.MouseButton.LeftButton:
            self._drawing  = False
            self._end_pt   = event.position().toPoint()
            bbox = self._screen_to_frame_bbox()
            if bbox:
                self._bbox_ready = bbox
                self.bbox_selected.emit(bbox)
            self.update()

    # ------------------------------------------------------------------
    # Coordinate helpers
    # ------------------------------------------------------------------

    def _display_geometry(self) -> tuple[int, int, int, int, float, float]:
        """Return (off_x, off_y, disp_w, disp_h, scale_x, scale_y)."""
        scale = min(self.width() / self._frame_w, self.height() / self._frame_h)
        disp_w = int(self._frame_w * scale)
        disp_h = int(self._frame_h * scale)
        off_x  = (self.width()  - disp_w) // 2
        off_y  = (self.height() - disp_h) // 2
        return off_x, off_y, disp_w, disp_h, scale, scale

    def _screen_to_frame(self, sx: int, sy: int) -> tuple[float, float]:
        off_x, off_y, disp_w, disp_h, _, _ = self._display_geometry()
        fx = (sx - off_x) * self._frame_w / disp_w
        fy = (sy - off_y) * self._frame_h / disp_h
        return fx, fy

    def _screen_to_frame_bbox(self) -> tuple | None:
        off_x, off_y, disp_w, disp_h, _, _ = self._display_geometry()
        sx = int((self._start_pt.x() - off_x) * self._frame_w / disp_w)
        sy = int((self._start_pt.y() - off_y) * self._frame_h / disp_h)
        ex = int((self._end_pt.x()   - off_x) * self._frame_w / disp_w)
        ey = int((self._end_pt.y()   - off_y) * self._frame_h / disp_h)
        x, y = min(sx, ex), min(sy, ey)
        w, h = abs(ex - sx), abs(ey - sy)
        if w < 10 or h < 10:
            return None
        x = max(0, min(x, self._frame_w - w))
        y = max(0, min(y, self._frame_h - h))
        return (x, y, w, h)

    def _hit_test(self, fx: float, fy: float) -> tuple | None:
        """Return the detection bbox that was clicked, or None."""
        candidates = []
        for bbox in self._detections:
            x, y, w, h = bbox
            if x <= fx <= x + w and y <= fy <= y + h:
                cx = x + w / 2
                cy = y + h / 2
                dist = ((fx - cx) ** 2 + (fy - cy) ** 2) ** 0.5
                candidates.append((dist, bbox))
        if candidates:
            candidates.sort(key=lambda t: t[0])
            return candidates[0][1]
        return None


# ---------------------------------------------------------------------------
# CVWindow
# ---------------------------------------------------------------------------

class CVWindow(QWidget):
    """Emitted when tracking starts or stops: (mount_id, is_active)."""
    tracking_changed = pyqtSignal(int, bool)

    def __init__(self, mount_id: int, mount_manager: MountManager,
                 config: AppConfig, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self._mount_id = mount_id
        self._mm       = mount_manager
        self._config   = config
        self._capture  = CaptureSource()
        self._tracking_loop: TrackingLoop | None = None
        self._yolo_mode = False   # True once YOLO is confirmed available

        self.setWindowTitle("CV Tracking")
        self.resize(960, 640)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("CVWindow { background: #121212; }")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._build()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(8, 8, 8, 8)

        # ── Video ─────────────────────────────────────────────────────
        self._video = VideoLabel()
        self._video.person_clicked.connect(self._on_person_clicked)
        self._video.target_moved.connect(self._on_target_moved)
        self._video.bbox_selected.connect(self._on_bbox_selected)
        layout.addWidget(self._video)

        # ── Controls row ──────────────────────────────────────────────
        ctrl = QHBoxLayout()
        ctrl.setSpacing(8)

        # Camera selector
        ctrl.addWidget(QLabel("Camera:"))
        self._mount_combo = QComboBox()
        self._mount_combo.setFixedWidth(150)
        for mid in range(1, 6):
            self._mount_combo.addItem(self._config.mount_label(mid), mid)
        self._mount_combo.setCurrentIndex(self._mount_id - 1)
        self._mount_combo.currentIndexChanged.connect(self._on_mount_changed)
        ctrl.addWidget(self._mount_combo)

        ctrl.addWidget(self._sep())

        # Device selector
        ctrl.addWidget(QLabel("Device:"))
        self._device_combo = QComboBox()
        self._device_combo.setFixedWidth(140)
        ctrl.addWidget(self._device_combo)

        refresh_btn = QPushButton("↺")
        refresh_btn.setFixedSize(30, 30)
        refresh_btn.setToolTip("Refresh device list")
        refresh_btn.clicked.connect(self._refresh_devices)
        ctrl.addWidget(refresh_btn)

        self._feed_btn = QPushButton("Start Feed")
        self._feed_btn.setFixedHeight(36)
        self._feed_btn.setStyleSheet(
            "background:#1B5E20; color:white; border:none; border-radius:5px; padding:0 10px;")
        self._feed_btn.clicked.connect(self._toggle_feed)
        ctrl.addWidget(self._feed_btn)

        ctrl.addWidget(self._sep())

        # Gains
        ctrl.addWidget(QLabel("Pan:"))
        self._pan_gain = QSlider(Qt.Orientation.Horizontal)
        self._pan_gain.setRange(1, 100)
        self._pan_gain.setValue(20)
        self._pan_gain.setFixedWidth(70)
        self._pan_gain.setToolTip("Pan tracking gain")
        ctrl.addWidget(self._pan_gain)

        ctrl.addWidget(QLabel("Tilt:"))
        self._tilt_gain = QSlider(Qt.Orientation.Horizontal)
        self._tilt_gain.setRange(1, 100)
        self._tilt_gain.setValue(20)
        self._tilt_gain.setFixedWidth(70)
        self._tilt_gain.setToolTip("Tilt tracking gain")
        ctrl.addWidget(self._tilt_gain)

        ctrl.addWidget(self._sep())

        # Set Target toggle (detection mode only — hidden in manual mode)
        self._target_btn = QPushButton("Set Target")
        self._target_btn.setFixedHeight(36)
        self._target_btn.setCheckable(True)
        self._target_btn.setEnabled(False)
        self._target_btn.setToolTip(
            "Click to toggle target repositioning mode.\n"
            "When active, click anywhere in the video to move the ⊕ target point.\n"
            "Default: upper-third of frame.")
        self._target_btn.setStyleSheet(
            "QPushButton { background:#4A3000; color:#FF9800; border:1px solid #FF9800;"
            "  border-radius:5px; padding:0 10px; }"
            "QPushButton:checked { background:#FF9800; color:#000; }"
            "QPushButton:disabled { background:#1A1A1A; color:#444; border-color:#333; }")
        self._target_btn.toggled.connect(self._on_target_btn_toggled)
        ctrl.addWidget(self._target_btn)

        # Start Tracking (manual-mode only — shown when YOLO unavailable)
        self._start_btn = QPushButton("Start Tracking")
        self._start_btn.setFixedHeight(36)
        self._start_btn.setEnabled(False)
        self._start_btn.setVisible(False)   # shown only in manual mode
        self._start_btn.setStyleSheet(
            "background:#0D47A1; color:white; border:none; border-radius:5px; padding:0 10px;")
        self._start_btn.clicked.connect(self._start_tracking_manual)
        ctrl.addWidget(self._start_btn)

        # Stop Tracking
        self._stop_btn = QPushButton("Stop Tracking")
        self._stop_btn.setFixedHeight(36)
        self._stop_btn.setEnabled(False)
        self._stop_btn.setStyleSheet(
            "QPushButton { background:#B71C1C; color:white; border:none;"
            "  border-radius:5px; padding:0 10px; }"
            "QPushButton:disabled { background:#1A1A1A; color:#444; }")
        self._stop_btn.clicked.connect(self._stop_tracking)
        ctrl.addWidget(self._stop_btn)

        ctrl.addStretch()

        close_btn = QPushButton("Close")
        close_btn.setFixedHeight(36)
        close_btn.setStyleSheet(
            "background:#37474F; color:#CFD8DC; border:none; border-radius:5px; padding:0 14px;")
        close_btn.clicked.connect(self.close)
        ctrl.addWidget(close_btn)

        layout.addLayout(ctrl)

        # ── Status bar ────────────────────────────────────────────────
        self._status = QLabel("")
        self._status.setStyleSheet("color:#78909C; font-size:10px;")
        layout.addWidget(self._status)

        self._refresh_devices()

    @staticmethod
    def _sep() -> QFrame:
        f = QFrame()
        f.setFrameShape(QFrame.Shape.VLine)
        f.setStyleSheet("color:#333;")
        return f

    # ------------------------------------------------------------------
    # Device list
    # ------------------------------------------------------------------

    def _refresh_devices(self) -> None:
        self._device_combo.clear()
        devices = CaptureSource.list_devices()
        if devices:
            for d in devices:
                self._device_combo.addItem(f"Camera {d}", d)
            self._status.setText("Select a device and click Start Feed.")
        else:
            self._device_combo.addItem("No devices found", None)
            self._status.setText("No capture device detected. Connect one and click ↺.")

    def _on_mount_changed(self) -> None:
        self._mount_id = self._mount_combo.currentData()
        if self._tracking_loop:
            self._tracking_loop.set_mount(self._mount_id)

    # ------------------------------------------------------------------
    # Feed
    # ------------------------------------------------------------------

    def _toggle_feed(self) -> None:
        if self._capture.is_open:
            self._stop_feed()
        else:
            self._start_feed()

    def _start_feed(self) -> None:
        device = self._device_combo.currentData()
        if device is None:
            self._status.setText("No capture device found.")
            return
        if not self._capture.open(device_index=device):
            self._status.setText("Failed to open capture device.")
            return

        self._feed_btn.setText("Stop Feed")

        loop = TrackingLoop(self._mm, self._capture, self)
        loop.set_mount(self._mount_id)
        loop.frame_ready.connect(self._on_frame)
        loop.tracking_lost.connect(self._on_tracking_lost)
        loop.tracking_active.connect(self._on_tracking_active)
        loop.detections_updated.connect(self._on_detections_updated)
        loop.detector_ready.connect(self._on_detector_ready)
        self._tracking_loop = loop

        # detector_ready was emitted in __init__ — query it directly now.
        self._configure_for_detector(loop.detector_available)

    def _stop_feed(self) -> None:
        if self._tracking_loop:
            self._tracking_loop.stop_tracking()
            self._tracking_loop.shutdown()
            self._tracking_loop = None
        self._capture.close()
        self._feed_btn.setText("Start Feed")
        self._stop_btn.setEnabled(False)
        self._start_btn.setEnabled(False)
        self._start_btn.setVisible(False)
        self._target_btn.setEnabled(False)
        self._target_btn.setChecked(False)
        self._video.clear_bbox()
        self._video.set_detection_mode(False)
        self._yolo_mode = False

    def _configure_for_detector(self, available: bool) -> None:
        """Switch the UI between YOLO detection mode and manual-draw mode."""
        self._yolo_mode = available
        self._video.set_detection_mode(available)

        if available:
            self._target_btn.setEnabled(True)
            self._start_btn.setVisible(False)
            self._status.setText(
                "People detected automatically — click a person to track them.")
        else:
            self._target_btn.setEnabled(False)
            self._start_btn.setVisible(True)
            self._start_btn.setEnabled(True)
            self._status.setText(
                "YOLO unavailable (pip install ultralytics). "
                "Draw a box around the subject and click Start Tracking.")

    # ------------------------------------------------------------------
    # Tracking
    # ------------------------------------------------------------------

    @pyqtSlot(tuple)
    def _on_person_clicked(self, bbox: tuple) -> None:
        """User clicked a detection box — select that person."""
        if self._tracking_loop is None:
            return
        pg = self._pan_gain.value()  / 10.0
        tg = self._tilt_gain.value() / 10.0
        self._tracking_loop.set_gains(pg, tg)
        self._tracking_loop.select_person(bbox)

    @pyqtSlot(tuple)
    def _on_bbox_selected(self, bbox: tuple) -> None:
        """Manual mode: a box was drawn — enable the Start Tracking button."""
        self._start_btn.setEnabled(True)
        self._status.setText("Box drawn — click Start Tracking.")

    def _start_tracking_manual(self) -> None:
        """Manual-mode only: start tracking the drawn bbox."""
        bbox = self._video.selected_bbox
        if not bbox:
            self._status.setText("Draw a box around the subject first.")
            return
        if self._tracking_loop:
            pg = self._pan_gain.value()  / 10.0
            tg = self._tilt_gain.value() / 10.0
            self._tracking_loop.set_gains(pg, tg)
            ok = self._tracking_loop.start_tracking(bbox)
            if not ok:
                self._status.setText("Failed to initialise tracker.")

    def _stop_tracking(self) -> None:
        if self._tracking_loop:
            self._tracking_loop.stop_tracking()

    # ------------------------------------------------------------------
    # Target repositioning
    # ------------------------------------------------------------------

    @pyqtSlot(bool)
    def _on_target_btn_toggled(self, checked: bool) -> None:
        self._video.set_move_target_mode(checked)
        if checked:
            self._status.setText(
                "Click anywhere in the video to move the ⊕ target point. "
                "Click 'Set Target' again when done.")
        else:
            self._status.setText(
                "Target repositioned.  Click a person to track them." if self._yolo_mode
                else "Target repositioned.")

    @pyqtSlot(float, float)
    def _on_target_moved(self, cx: float, cy: float) -> None:
        if self._tracking_loop:
            self._tracking_loop.set_target(cx, cy)
        # Auto-exit set-target mode after one click for convenience
        self._target_btn.setChecked(False)

    # ------------------------------------------------------------------
    # Slots from TrackingLoop
    # ------------------------------------------------------------------

    @pyqtSlot(object)
    def _on_frame(self, frame) -> None:
        self._video.set_frame(frame)

    @pyqtSlot(list)
    def _on_detections_updated(self, detections: list) -> None:
        self._video.set_detections(detections)
        if self._yolo_mode and not (self._tracking_loop and
                                    self._tracking_loop.is_tracking):
            n = len(detections)
            if n == 0:
                self._status.setText("No people detected — waiting…")
            else:
                word = "person" if n == 1 else "people"
                self._status.setText(
                    f"{n} {word} detected — click one to start tracking.")

    @pyqtSlot(bool)
    def _on_detector_ready(self, available: bool) -> None:
        self._configure_for_detector(available)

    @pyqtSlot()
    def _on_tracking_lost(self) -> None:
        self._stop_btn.setEnabled(False)
        if self._yolo_mode:
            self._status.setText(
                "Tracking lost — click a detected person to resume.")
        else:
            self._status.setText("Tracking lost. Draw a new box and restart.")

    @pyqtSlot(bool)
    def _on_tracking_active(self, active: bool) -> None:
        self._stop_btn.setEnabled(active)
        if active:
            self._status.setText(
                "Tracking — click another person to switch, or Stop Tracking.")
        self.tracking_changed.emit(self._mount_id, active)

    # ------------------------------------------------------------------
    # Public interface (called from main window)
    # ------------------------------------------------------------------

    @property
    def is_tracking(self) -> bool:
        return (self._tracking_loop is not None
                and self._tracking_loop.is_tracking)

    @property
    def tracking_mount_id(self) -> int:
        return self._mount_id if self.is_tracking else 0

    def stop_tracking(self) -> None:
        if self._tracking_loop:
            self._tracking_loop.stop_tracking()

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self._stop_feed()
        super().closeEvent(event)
