"""
NudgeOverlay — floating panel for precise incremental camera movement.

Appears on top of the position grid when the operator presses "Move".
Closed by the X button or by pressing "Move" again.

Layout:

  [X]
        ╭───────────────╮
        │   split-arc   │   [zoom]
        │   pan / tilt  │   column
        ╰───────────────╯
  ╭─────────────────────────────╮
  │  slider track, −100 … +100  │
  ╰─────────────────────────────╯

Pan and tilt are a dial of four separated arc groups with the live angles in
the hub; the slider is a track showing where the carriage actually is. Both
are drawn in widgets/nudge_controls.py, which explains why they are shaped the
way they are.

Axis colours (fixed, not camera colour):
  Tilt   — green
  Pan    — olive / yellow
  Zoom   — steel blue  (press-and-hold for continuous jog)
  Slider — purple / magenta

Step conversion:
  pan_steps_per_degree  — e.g. 17.78 for 0.9°/step × 16 µstep
  tilt_steps_per_degree — same
  slider_steps_per_mm   — depends on leadscrew; configure in Settings
  zoom jog velocities   — set as fraction of max speed (0–1000)

All conversion factors are configurable per mount in Settings.
"""
from __future__ import annotations

import math
from PyQt6.QtWidgets import (
    QFrame, QPushButton, QLabel, QSizePolicy,
    QHBoxLayout, QVBoxLayout
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont

from comms.mount_manager import MountManager
from .nudge_controls import RadialNudge, SliderTrack, ZoomColumn

# ---------------------------------------------------------------------------
# Axis colours
# ---------------------------------------------------------------------------

_TILT_BG    = "#1B5E20"; _TILT_PRESS   = "#2E7D32"; _TILT_TEXT   = "#A5D6A7"
_PAN_BG     = "#3E3000"; _PAN_PRESS    = "#5D4700"; _PAN_TEXT    = "#D4B800"
_ZOOM_BG    = "#0D1A3B"; _ZOOM_PRESS   = "#1A3080"; _ZOOM_TEXT   = "#90CAF9"
_SLIDER_BG  = "#2A0040"; _SLIDER_PRESS = "#4A1070"; _SLIDER_TEXT = "#CE93D8"
_CLOSE_BG   = "#C62828"; _CLOSE_PRESS  = "#EF5350"; _CLOSE_TEXT  = "#FFFFFF"

_BTN_BASE = """
    QPushButton {{
        background: {bg}; color: {text};
        border: 6px solid {border};
        border-radius: 10px;
        font-size: 24px; font-weight: bold;  
    }}
    QPushButton:pressed {{ background: {press}; }}
"""
#{fs}
def _style(bg, text, border, press, fs=14):
    return _BTN_BASE.format(bg=bg, text=text, border=border, press=press, fs=fs)


# ---------------------------------------------------------------------------
# NudgeOverlay
# ---------------------------------------------------------------------------

class NudgeOverlay(QFrame):
    """
    Floating nudge panel.  Parent should be the central widget.
    Call show_for(mount_id) to display for a given camera.
    """

    closed = pyqtSignal()

    # Hardware constants — edit these if microstepping or mechanics change.
    # Derived steps/deg and steps/mm are computed automatically below.
    _PAN_TILT_MICROSTEPS  = 256
    _PAN_GEAR_RATIO       = 7.5       # 270T mount / 36T motor
    _TILT_GEAR_RATIO      = 7.5       # 120T mount / 16T motor
    _PAN_TILT_STEP_ANGLE  = 0.9       # degrees per full motor step (17HM15-0904S)

    _SLIDER_MICROSTEPS    = 32
    _SLIDER_STEP_ANGLE    = 1.8       # degrees per full motor step
    _SLIDER_MM_PER_REV    = 40.0      # leadscrew pitch (mm per revolution)

    PAN_STEPS_PER_DEG   = _PAN_TILT_MICROSTEPS * _PAN_GEAR_RATIO  / _PAN_TILT_STEP_ANGLE
    TILT_STEPS_PER_DEG  = _PAN_TILT_MICROSTEPS * _TILT_GEAR_RATIO / _PAN_TILT_STEP_ANGLE
    SLIDER_STEPS_PER_MM = _SLIDER_MICROSTEPS * (360.0 / _SLIDER_STEP_ANGLE) / _SLIDER_MM_PER_REV
    ZOOM_JOG_SLOW      = 200     # velocity units (0–1000)
    ZOOM_JOG_FAST      = 700

    def __init__(self, mount_manager: MountManager, parent=None):
        super().__init__(parent)
        self._mm        = mount_manager
        self._mount_id  = 1
        self._nudge_deg_small  = 1.0    # configurable
        self._nudge_deg_large  = 10.0
        self._nudge_mm_small   = 10.0
        self._nudge_mm_large   = 100.0

        self.setStyleSheet("""
            QFrame {
                background: #111827;
                border: 6px solid #374151;
                border-radius: 14px;
            }
        """)
        self.setFixedSize(800, 800)
        self._build()
        # The hub and the carriage are drawn from whatever the mount last said.
        self._mm.position_updated.connect(self._on_position)
        self.hide()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def show_for(self, mount_id: int,
                 pan_spd: float | None = None,
                 tilt_spd: float | None = None,
                 slider_spmm: float | None = None,
                 nudge_deg_small: float = 1.0,
                 nudge_deg_large: float = 10.0,
                 nudge_mm_small: float = 10.0,
                 nudge_mm_large: float = 100.0) -> None:
        self._mount_id         = mount_id
        self._nudge_deg_small  = nudge_deg_small
        self._nudge_deg_large  = nudge_deg_large
        self._nudge_mm_small   = nudge_mm_small
        self._nudge_mm_large   = nudge_mm_large
        if pan_spd   is not None: self.PAN_STEPS_PER_DEG   = pan_spd
        if tilt_spd  is not None: self.TILT_STEPS_PER_DEG  = tilt_spd
        if slider_spmm is not None: self.SLIDER_STEPS_PER_MM = slider_spmm

        self._dial.set_steps(nudge_deg_small, nudge_deg_large)
        self._track.set_steps(nudge_mm_small, nudge_mm_large)
        # Blank until the mount answers, rather than showing the last camera's.
        self._dial.set_angles(None, None)
        self._track.set_position(None, *self._slider_range())
        self._ask_position()

        self._centre_on_parent()
        self.show()
        self.raise_()

    def set_mount(self, mount_id: int) -> None:
        """Switch the active camera while the overlay is open."""
        self._mount_id = mount_id

    def _centre_on_parent(self) -> None:
        if self.parent():
            p = self.parent()
            x = (p.width()  - self.width())  // 2
            y = (p.height() - self.height()) // 2
            self.move(x, y)

    # ------------------------------------------------------------------
    # Build layout
    # ------------------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(14)

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(48, 48)
        close_btn.setStyleSheet(_style(_CLOSE_BG, _CLOSE_TEXT, _CLOSE_PRESS,
                                       _CLOSE_PRESS, fs=16))
        close_btn.clicked.connect(self._on_close)
        root.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignLeft)

        mid = QHBoxLayout()
        mid.setSpacing(24)
        self._dial = RadialNudge()
        self._dial.nudged.connect(self._on_dial)
        mid.addWidget(self._dial, 1)

        self._zoom = ZoomColumn(self.ZOOM_JOG_FAST, self.ZOOM_JOG_SLOW)
        self._zoom.started.connect(self._zoom_jog)
        self._zoom.stopped.connect(self._zoom_stop)
        mid.addWidget(self._zoom, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(mid, 1)

        self._track = SliderTrack()
        self._track.nudged.connect(self._on_track)
        root.addWidget(self._track)

    # ------------------------------------------------------------------
    # Live readout
    # ------------------------------------------------------------------

    def _on_dial(self, axis: str, degrees: float) -> None:
        if axis == "pan":
            self._nudge_pan(degrees)
        else:
            self._nudge_tilt(degrees)
        self._ask_position()

    def _on_track(self, mm: float) -> None:
        self._nudge_slider(mm)
        self._ask_position()

    def _ask_position(self) -> None:
        """Ask once, and once more when the move should have landed.

        Positions are answered on request — nothing streams them — so the panel
        asks for what it draws rather than leaving a stale carriage on screen.
        Two requests per nudge, only while the panel is open, is a long way from
        the 5 Hz poll this replaces.
        """
        if not self.isVisible():
            return
        self._mm.request_position(self._mount_id)
        QTimer.singleShot(700, self._ask_position_once)

    def _ask_position_once(self) -> None:
        if self.isVisible():
            self._mm.request_position(self._mount_id)

    def _on_position(self, mount_id: int, pos) -> None:
        if mount_id != self._mount_id or not self.isVisible():
            return
        self._dial.set_angles(pos.pan_deg, pos.tilt_deg)
        lo, hi = self._slider_range()
        self._track.set_position(pos.slider_mm if hi > lo else None, lo, hi)

    def _slider_range(self) -> tuple[float, float]:
        """The rail's usable millimetres, or (0, 0) when it is not known yet."""
        st  = self._mm.state(self._mount_id)
        rep = getattr(st, "last_config_report", None)
        if rep is None:
            return 0.0, 0.0
        try:
            spmm = self.SLIDER_STEPS_PER_MM
            return (rep.slider_min_steps / spmm, rep.slider_max_steps / spmm)
        except Exception:
            return 0.0, 0.0

    # ------------------------------------------------------------------
    # Motion helpers
    # ------------------------------------------------------------------

    def _nudge_pan(self, degrees: float) -> None:
        delta  = int(degrees * self.PAN_STEPS_PER_DEG)
        preset = self._mm.state(self._mount_id).active_pt_preset
        self._mm.send_move_rel(self._mount_id, delta, 0, 0, 0, preset)

    def _nudge_tilt(self, degrees: float) -> None:
        delta  = int(degrees * self.TILT_STEPS_PER_DEG)
        preset = self._mm.state(self._mount_id).active_pt_preset
        self._mm.send_move_rel(self._mount_id, 0, delta, 0, 0, preset)

    def _nudge_slider(self, mm: float) -> None:
        delta  = int(mm * self.SLIDER_STEPS_PER_MM)
        preset = self._mm.state(self._mount_id).active_sl_preset
        self._mm.send_move_rel(self._mount_id, 0, 0, delta, 0, preset)

    def _zoom_jog(self, velocity: int) -> None:
        self._mm.send_jog(self._mount_id, 0, 0, 0, velocity)

    def _zoom_stop(self) -> None:
        self._mm.send_jog(self._mount_id, 0, 0, 0, 0)

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def _on_close(self) -> None:
        self.hide()
        self.closed.emit()
