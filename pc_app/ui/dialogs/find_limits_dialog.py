"""
FindLimitsDialog — guided wizard for finding slider and zoom limits via StallGuard.

Walks the operator through:
  1. Select axis (Slider or Zoom)
  2. Confirms the mount is clear to move
  3. Sends FIND_LIMITS command and shows live progress
  4. Displays result and stores limits
"""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QComboBox, QDialogButtonBox, QMessageBox
)
from PyQt6.QtCore import Qt, QTimer, pyqtSlot

from comms.mount_manager import MountManager
from comms.protocol import Axis


class FindLimitsDialog(QDialog):

    def __init__(self, mount_id: int, mount_manager: MountManager, parent=None,
                 has_slider: bool = True, lanc_zoom: bool = False,
                 stall_threshold_slider: int = 80, stall_threshold_zoom: int = 80):
        super().__init__(parent)
        self._mount_id   = mount_id
        self._mm         = mount_manager
        self._has_slider = has_slider
        self._lanc_zoom  = lanc_zoom
        self._stall_threshold_slider = max(1, stall_threshold_slider)
        self._stall_threshold_zoom   = max(1, stall_threshold_zoom)
        self._running    = False
        self._result: tuple[int, int] | None = None

        self.setWindowTitle(f"Find Limits — Mount {mount_id}")
        self.setMinimumWidth(380)
        self._build()

        # Listen for limits_found signal
        self._mm.limits_found.connect(self._on_limits_found)

        # Timeout watchdog (firmware has its own, but we show UI feedback)
        self._timeout_timer = QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.setInterval(630000)  # 10.5 min — covers both ends of a 3-metre slider
        self._timeout_timer.timeout.connect(self._on_timeout)

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        # Axis selector — only show axes that are physically present and motor-driven
        axis_row = QHBoxLayout()
        axis_row.addWidget(QLabel("Axis:"))
        self._axis_combo = QComboBox()
        if self._has_slider:
            self._axis_combo.addItem("Slider", Axis.SLIDER)
        if not self._lanc_zoom:
            self._axis_combo.addItem("Zoom", Axis.ZOOM)
        axis_row.addWidget(self._axis_combo)
        axis_row.addStretch()
        layout.addLayout(axis_row)

        # Warning
        warning = QLabel(
            "⚠  Ensure the axis is free to move to both ends.\n"
            "The mount will move slowly until it detects each end stop.\n"
            "Keep clear of the mechanism during the procedure."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #FFA726; font-size: 11px;")
        layout.addWidget(warning)

        # Progress bar
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)    # indeterminate
        self._progress.setTextVisible(False)
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        # Status label
        self._status_label = QLabel("")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setStyleSheet("font-size: 12px; color: #CFD8DC;")
        layout.addWidget(self._status_label)

        # If no axes are available, disable the start button and explain why
        if self._axis_combo.count() == 0:
            no_axes_label = QLabel(
                "No motor-driven axes to calibrate on this mount.\n"
                "(Slider is disabled; Zoom uses LANC serial control.)"
            )
            no_axes_label.setWordWrap(True)
            no_axes_label.setStyleSheet("color: #EF5350; font-size: 11px;")
            layout.addWidget(no_axes_label)

        # Buttons
        btn_row = QHBoxLayout()
        self._start_btn = QPushButton("Start")
        self._start_btn.setFixedHeight(44)
        self._start_btn.setStyleSheet(
            "background:#1565C0; color:white; border:none; border-radius:6px;"
            "font-size:13px; font-weight:bold;")
        self._start_btn.setEnabled(self._axis_combo.count() > 0)
        self._start_btn.clicked.connect(self._start)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self._cancel)

        btn_row.addWidget(self._start_btn)
        btn_row.addWidget(self._cancel_btn)
        layout.addLayout(btn_row)

    # ------------------------------------------------------------------
    # Logic
    # ------------------------------------------------------------------

    def _start(self) -> None:
        self._running = True
        self._start_btn.setEnabled(False)
        self._axis_combo.setEnabled(False)
        self._progress.setVisible(True)
        self._status_label.setText("Searching for limits…")

        axis: Axis = self._axis_combo.currentData()
        self._axis = axis
        from comms.protocol import Axis as _Axis
        threshold = (self._stall_threshold_slider if axis == _Axis.SLIDER
                     else self._stall_threshold_zoom)
        self._mm.send_find_limits(self._mount_id, axis, threshold)
        self._timeout_timer.start()

    def _cancel(self) -> None:
        self._timeout_timer.stop()
        if self._running:
            self._mm.send_e_stop(self._mount_id)
        self.reject()

    @pyqtSlot(int, int, int, int)
    def _on_limits_found(self, mount_id: int, axis: int,
                         min_steps: int, max_steps: int) -> None:
        if mount_id != self._mount_id or not self._running:
            return
        if Axis(axis) != self._axis:
            return

        self._timeout_timer.stop()
        self._running = False
        self._progress.setVisible(False)
        travel = max_steps - min_steps
        self._status_label.setText(
            f"Done! Travel: {travel} steps  (min={min_steps}, max={max_steps})"
        )
        self._status_label.setStyleSheet("font-size:12px; color:#4CAF50;")
        self._start_btn.setText("Close")
        self._start_btn.setEnabled(True)
        self._start_btn.clicked.disconnect()
        self._start_btn.clicked.connect(self.accept)

    def _on_timeout(self) -> None:
        self._running = False
        self._progress.setVisible(False)
        self._status_label.setText("Timed out — no response from mount.")
        self._status_label.setStyleSheet("font-size:12px; color:#EF5350;")
        self._start_btn.setText("Retry")
        self._start_btn.setEnabled(True)
        self._axis_combo.setEnabled(True)
        self._start_btn.clicked.disconnect()
        self._start_btn.clicked.connect(self._start)
