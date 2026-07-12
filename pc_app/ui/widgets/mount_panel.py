"""
MountPanel — per-mount status and speed controls.

Shows:
  - Connection indicator
  - Motion state label
  - Slider and zoom travel bars (0-100%)
  - Pan/Tilt speed dial
  - Slider speed dial
  - "Set Position" button
"""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QProgressBar,
    QFrame, QSizePolicy
)
from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtGui import QColor

from .speed_dial import SpeedDial
from comms.mount_manager import MountManager
from comms.protocol import MountState

_STATE_LABELS = {
    MountState.IDLE:           "● IDLE",
    MountState.JOGGING:        "● JOGGING",
    MountState.MOVING_TO_POS:  "● MOVING",
    MountState.FINDING_LIMITS: "● FINDING LIMITS",
    MountState.ERROR:          "● ERROR",
}

_STATE_COLOURS = {
    MountState.IDLE:           "#4CAF50",
    MountState.JOGGING:        "#4FC3F7",
    MountState.MOVING_TO_POS:  "#FFA726",
    MountState.FINDING_LIMITS: "#AB47BC",
    MountState.ERROR:          "#EF5350",
}


class MountPanel(QWidget):
    """
    Controls and status for one mount.

    Signals:
        set_position_clicked()    — user pressed "Set Position"
        pan_tilt_preset_changed(int)
        slider_preset_changed(int)
    """

    set_position_clicked   = pyqtSignal()
    pan_tilt_preset_changed = pyqtSignal(int)
    slider_preset_changed   = pyqtSignal(int)

    def __init__(self, mount_id: int, parent=None):
        super().__init__(parent)
        self._mount_id   = mount_id
        self._slider_min = 0
        self._slider_max = 1
        self._zoom_min   = 0
        self._zoom_max   = 1
        self._build()

    # ------------------------------------------------------------------
    # Public — update from mount state
    # ------------------------------------------------------------------

    def update_status(self, state: MountState, flags: int,
                      connected: bool) -> None: # Removed pos_slider, pos_zoom args
        """Update the connection and status indicators."""
        # Connection indicator
        self._conn_indicator.set_status(connected)
        
        # Update status text/color based on state/flags
        # ... existing logic to update labels ...

        # Limit bars: We can no longer update these via STATUS packets
        # because the position data is missing. 
        # These should only be updated when a STATE_REPORT arrives.

    def set_limits(self, slider_min: int, slider_max: int,
                   zoom_min: int, zoom_max: int) -> None:
        self._slider_min = slider_min
        self._slider_max = slider_max
        self._zoom_min   = zoom_min
        self._zoom_max   = zoom_max

    @property
    def pan_tilt_preset(self) -> int:
        return self._pt_dial.preset

    @pan_tilt_preset.setter
    def pan_tilt_preset(self, v: int) -> None:
        self._pt_dial.preset = v

    @property
    def slider_preset(self) -> int:
        return self._sl_dial.preset

    @slider_preset.setter
    def slider_preset(self, v: int) -> None:
        self._sl_dial.preset = v

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Connection + state
        self._conn_label  = QLabel("○ Disconnected")
        self._conn_label.setStyleSheet("color: #EF5350; font-size: 10px;")
        self._state_label = QLabel("● IDLE")
        self._state_label.setStyleSheet("color: #4CAF50; font-size: 11px;")
        layout.addWidget(self._conn_label)
        layout.addWidget(self._state_label)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #333;")
        layout.addWidget(sep)

        # Slider bar
        layout.addWidget(self._make_bar_row("Slide:", "slider"))
        layout.addWidget(self._make_bar_row("Zoom:", "zoom"))

        # Speed dials — Slider left, Pan/Tilt right
        dials_row = QHBoxLayout()
        self._pt_dial = SpeedDial("Pan/Tilt")
        self._sl_dial = SpeedDial("Slider")
        self._pt_dial.preset_changed.connect(self.pan_tilt_preset_changed)
        self._sl_dial.preset_changed.connect(self.slider_preset_changed)
        dials_row.addWidget(self._sl_dial)    # Slider  — left
        dials_row.addWidget(self._pt_dial)    # Pan/Tilt — right
        layout.addLayout(dials_row)

        # Set Position button
        self._set_btn = QPushButton("Set Position")
        self._set_btn.setFixedHeight(44)
        self._set_btn.setStyleSheet("""
            QPushButton {
                background: #1565C0; color: white; border: none;
                border-radius: 6px; font-size: 13px; font-weight: bold;
            }
            QPushButton:pressed { background: #0D47A1; }
        """)
        self._set_btn.clicked.connect(self.set_position_clicked)
        layout.addWidget(self._set_btn)

        layout.addStretch()

    def _make_bar_row(self, label_text: str, kind: str) -> QWidget:
        row = QWidget()
        hl  = QHBoxLayout(row)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(4)

        lbl = QLabel(label_text)
        lbl.setFixedWidth(36)
        lbl.setStyleSheet("color: #90A4AE; font-size: 10px;")

        bar = QProgressBar()
        bar.setRange(0, 1000)
        bar.setValue(0)
        bar.setFixedHeight(8)
        bar.setTextVisible(False)
        bar.setStyleSheet("""
            QProgressBar { background: #1E1E1E; border: 1px solid #333; border-radius: 3px; }
            QProgressBar::chunk { background: #4FC3F7; border-radius: 3px; }
        """)

        hl.addWidget(lbl)
        hl.addWidget(bar)

        if kind == "slider":
            self._slider_bar = bar
        else:
            self._zoom_bar = bar

        return row

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _update_bar(bar: QProgressBar, pos: int, min_v: int, max_v: int) -> None:
        span = max_v - min_v
        if span <= 0:
            bar.setValue(0)
            return
        pct = int(((pos - min_v) / span) * 1000)
        bar.setValue(max(0, min(1000, pct)))
