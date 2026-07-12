"""
NudgeOverlay — floating panel for precise incremental camera movement.

Appears on top of the position grid when the operator presses "Move".
Closed by the X button or by pressing "Move" again.

Layout (matches reference screenshot):

  [X]      [Tilt +10°]                   [Z++]
           [Tilt  +1°]                   [Z+]
  [Pan-10] [Pan -1°]   [Pan +1°] [Pan+10°]  [Z-]
           [Tilt  -1°]                   [Z--]
           [Tilt -10°]

  [Sl-100mm][Sl-10mm]        [Sl+10mm][Sl+100mm]

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
    QFrame, QGridLayout, QPushButton, QLabel, QSizePolicy
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont

from comms.mount_manager import MountManager

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
# Hold button (for zoom — jogs while held)
# ---------------------------------------------------------------------------

class _HoldButton(QPushButton):
    """Sends jog commands while held; sends stop on release."""

    def __init__(self, label: str, on_start, on_stop, rate_ms: int = 50,
                 parent=None):
        super().__init__(label, parent)
        self._start = on_start
        self._stop  = on_stop
        self._timer = QTimer(self)
        self._timer.setInterval(rate_ms)
        self._timer.timeout.connect(on_start)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._start()
            self._timer.start()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self._timer.stop()
        self._stop()
        super().mouseReleaseEvent(event)


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

        # Refresh button labels
        self._lbl_tilt_u_s.setText(f"+{nudge_deg_small:.0f}°")
        self._lbl_tilt_u_l.setText(f"+{nudge_deg_large:.0f}°")
        self._lbl_tilt_d_s.setText(f"−{nudge_deg_small:.0f}°")
        self._lbl_tilt_d_l.setText(f"−{nudge_deg_large:.0f}°")
        self._lbl_pan_l_s.setText(f"−{nudge_deg_small:.0f}°")
        self._lbl_pan_l_l.setText(f"−{nudge_deg_large:.0f}°")
        self._lbl_pan_r_s.setText(f"+{nudge_deg_small:.0f}°")
        self._lbl_pan_r_l.setText(f"+{nudge_deg_large:.0f}°")
        self._lbl_sl_l_s.setText(f"−{nudge_mm_small:.0f}mm")
        self._lbl_sl_l_l.setText(f"−{nudge_mm_large:.0f}mm")
        self._lbl_sl_r_s.setText(f"+{nudge_mm_small:.0f}mm")
        self._lbl_sl_r_l.setText(f"+{nudge_mm_large:.0f}mm")

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
        grid = QGridLayout(self)
        grid.setSpacing(8)
        grid.setContentsMargins(14, 14, 14, 14)

        BIG = 100    # px — large buttons
        SML = 100    # px — small buttons

        # ---- Close button ----
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(48, 48)
        close_btn.setStyleSheet(_style(_CLOSE_BG, _CLOSE_TEXT, _CLOSE_PRESS, _CLOSE_PRESS, fs=16))
        close_btn.clicked.connect(self._on_close)
        grid.addWidget(close_btn, 0, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        # ---- Tilt (green, vertical column = col 2) ----
        self._lbl_tilt_u_l = self._tilt_btn(f"+{self._nudge_deg_large:.0f}°", BIG,
                                              lambda: self._nudge_tilt(+self._nudge_deg_large))
        self._lbl_tilt_u_s = self._tilt_btn(f"+{self._nudge_deg_small:.0f}°", SML,
                                              lambda: self._nudge_tilt(+self._nudge_deg_small))
        self._lbl_tilt_d_s = self._tilt_btn(f"−{self._nudge_deg_small:.0f}°", SML,
                                              lambda: self._nudge_tilt(-self._nudge_deg_small))
        self._lbl_tilt_d_l = self._tilt_btn(f"−{self._nudge_deg_large:.0f}°", BIG,
                                              lambda: self._nudge_tilt(-self._nudge_deg_large))

        grid.addWidget(self._lbl_tilt_u_l, 0, 2)
        grid.addWidget(self._lbl_tilt_u_s, 1, 2)
        grid.addWidget(self._lbl_tilt_d_s, 3, 2)
        grid.addWidget(self._lbl_tilt_d_l, 4, 2)

        # ---- Pan (olive, horizontal row = row 2) ----
        self._lbl_pan_l_l = self._pan_btn(f"−{self._nudge_deg_large:.0f}°", BIG,
                                           lambda: self._nudge_pan(-self._nudge_deg_large))
        self._lbl_pan_l_s = self._pan_btn(f"−{self._nudge_deg_small:.0f}°", SML,
                                           lambda: self._nudge_pan(-self._nudge_deg_small))
        self._lbl_pan_r_s = self._pan_btn(f"+{self._nudge_deg_small:.0f}°", SML,
                                           lambda: self._nudge_pan(+self._nudge_deg_small))
        self._lbl_pan_r_l = self._pan_btn(f"+{self._nudge_deg_large:.0f}°", BIG,
                                           lambda: self._nudge_pan(+self._nudge_deg_large))

        grid.addWidget(self._lbl_pan_l_l, 2, 0)
        grid.addWidget(self._lbl_pan_l_s, 2, 1)
        grid.addWidget(self._lbl_pan_r_s, 2, 3)
        grid.addWidget(self._lbl_pan_r_l, 2, 4)

        # ---- Zoom (blue, press-and-hold, col 5) ----
        z_pp = _HoldButton("Z++", lambda: self._zoom_jog(+self.ZOOM_JOG_FAST),
                           self._zoom_stop)
        z_p  = _HoldButton("Z+",  lambda: self._zoom_jog(+self.ZOOM_JOG_SLOW),
                           self._zoom_stop)
        z_m  = _HoldButton("Z−",  lambda: self._zoom_jog(-self.ZOOM_JOG_SLOW),
                           self._zoom_stop)
        z_mm = _HoldButton("Z−−", lambda: self._zoom_jog(-self.ZOOM_JOG_FAST),
                           self._zoom_stop)
        for btn in (z_pp, z_p, z_m, z_mm):
            btn.setFixedSize(72, SML)
            btn.setStyleSheet(_style(_ZOOM_BG, _ZOOM_TEXT, _ZOOM_PRESS, _ZOOM_PRESS))
        grid.addWidget(z_pp, 0, 5)
        grid.addWidget(z_p,  1, 5)
        grid.addWidget(z_m,  3, 5)
        grid.addWidget(z_mm, 4, 5)

        # ---- Slider (purple, row 5) ----
        self._lbl_sl_l_l = self._slider_btn(f"−{self._nudge_mm_large:.0f}mm", 120,
                                             lambda: self._nudge_slider(-self._nudge_mm_large))
        self._lbl_sl_l_s = self._slider_btn(f"−{self._nudge_mm_small:.0f}mm", 120,
                                             lambda: self._nudge_slider(-self._nudge_mm_small))
        self._lbl_sl_r_s = self._slider_btn(f"+{self._nudge_mm_small:.0f}mm", 120,
                                             lambda: self._nudge_slider(+self._nudge_mm_small))
        self._lbl_sl_r_l = self._slider_btn(f"+{self._nudge_mm_large:.0f}mm", 120,
                                             lambda: self._nudge_slider(+self._nudge_mm_large))

        grid.addWidget(self._lbl_sl_l_l, 5, 0)
        grid.addWidget(self._lbl_sl_l_s, 5, 1)
        grid.addWidget(self._lbl_sl_r_s, 5, 3)
        grid.addWidget(self._lbl_sl_r_l, 5, 4)

        # Column spacer between tilt and zoom
        grid.setColumnMinimumWidth(2, SML + 8)
        grid.setColumnStretch(2, 1)

    # ------------------------------------------------------------------
    # Button factories
    # ------------------------------------------------------------------

    def _tilt_btn(self, label: str, size: int, handler) -> QPushButton:
        btn = QPushButton(label)
        btn.setFixedSize(size, size)
        btn.setStyleSheet(_style(_TILT_BG, _TILT_TEXT, _TILT_PRESS, _TILT_PRESS))
        btn.clicked.connect(handler)
        return btn

    def _pan_btn(self, label: str, size: int, handler) -> QPushButton:
        btn = QPushButton(label)
        btn.setFixedSize(size, size)
        btn.setStyleSheet(_style(_PAN_BG, _PAN_TEXT, _PAN_PRESS, _PAN_PRESS))
        btn.clicked.connect(handler)
        return btn

    def _slider_btn(self, label: str, size: int, handler) -> QPushButton:
        btn = QPushButton(label)
        btn.setFixedSize(size, 60)
        btn.setStyleSheet(_style(_SLIDER_BG, _SLIDER_TEXT, _SLIDER_PRESS, _SLIDER_PRESS))
        btn.clicked.connect(handler)
        return btn

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
