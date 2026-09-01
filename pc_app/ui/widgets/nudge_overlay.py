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

Pan and tilt are a dial of four separated arc groups, with a legend in the hub
naming which way each axis lies; the slider is a track of four tap zones.
Neither shows a position — the mount answers when asked but volunteers
nothing, so a live readout would mean polling for it. Both are drawn in
widgets/nudge_controls.py, which explains why they are shaped the way they are.

Axis colours (fixed, not camera colour):
  Tilt   — green
  Pan    — olive / yellow
  Zoom   — steel blue  (press-and-hold for continuous jog)
  Slider — purple / magenta

Sizing: the panel was 800x800, which is right on the 1920x1080 screen it was
drawn against and progressively smaller on anything with more logical pixels —
the same fault the bars had, on the one part of the UI that had not been fixed.
It now takes its side from ui_scale like the chrome, and everything inside is a
fraction of that side, the way PositionGrid scales from the height it is given.
See _apply_scale().

Step conversion:
  pan_steps_per_degree  — e.g. 17.78 for 0.9°/step × 16 µstep
  tilt_steps_per_degree — same
  slider_steps_per_mm   — from the GT2 pulley and belt pitch; see below
  zoom jog velocities   — set as fraction of max speed (0–1000)

All conversion factors are configurable per mount in Settings.
"""
from __future__ import annotations

import logging
import math
from PyQt6.QtWidgets import (
    QFrame, QPushButton, QLabel, QSizePolicy, QWidget,
    QHBoxLayout, QVBoxLayout
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont

from comms.mount_manager import MountManager
from ..ui_scale import px as _px
from .nudge_controls import (RadialNudge, SliderTrack, ZoomColumn,
                             _R_OUT_1 as DIAL_OUTER_FRAC)

# ---------------------------------------------------------------------------
# The panel, and everything in it, as a fraction of one side
# ---------------------------------------------------------------------------
# The reference is the 800px square the panel was drawn as, so at 1920x1080
# every number below comes out exactly where it was.  The dial, the track and
# the zoom column already size their own arcs, type and pen widths from the
# rectangle they are given, so scaling the panel scales them with it; these are
# the pieces that were still raw pixel counts.

_PANEL_REF   = 800          # side, on the screen it was drawn against
_PANEL_MIN   = 360          # below this the dial stops being a target
_PANEL_INSET = 24           # clearance kept between panel and parent edges

_MARGIN_F   =  18 / 800     # panel padding
_GAP_F      =  14 / 800     # close button -> dial -> track
_COL_GAP_F  =  24 / 800     # dial -> zoom column
_CLOSE_F    =  48 / 800     # close button, square
_TRACK_H_F  =  76 / 800     # slider track height
_ZOOM_W_F   =  96 / 800
_ZOOM_H_F   = 300 / 800
_BORDER_F   =   6 / 800     # panel frame
_RADIUS_F   =  14 / 800

_CLOSE_BG   = "#C62828"; _CLOSE_PRESS  = "#EF5350"; _CLOSE_TEXT  = "#FFFFFF"

log = logging.getLogger(__name__)


def _close_style(size: int) -> str:
    """The close button, scaled from its own side rather than the screen's —
    it is part of the panel, and the panel is what changes size."""
    f = lambda frac, lo=1: max(lo, round(size * frac))
    return f"""
        QPushButton {{
            background: {_CLOSE_BG}; color: {_CLOSE_TEXT};
            border: {f(6 / 48)}px solid {_CLOSE_PRESS};
            border-radius: {f(10 / 48, 2)}px;
            font-size: {f(24 / 48, 8)}px; font-weight: bold;
        }}
        QPushButton:pressed {{ background: {_CLOSE_PRESS}; }}
    """


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
    # 20-tooth GT2 pulley on a 2 mm belt = 40 mm per motor revolution. This said
    # "leadscrew pitch", which is the right number attached to the wrong part:
    # the firmware derives the same 40 from SLIDER_PULLEY_TEETH x
    # GT2_BELT_PITCH_MM, and anyone changing the pulley would have found nothing
    # here to change. tools/test_slider_scale.py holds the two together.
    _SLIDER_MM_PER_REV    = 40.0      # 20T GT2 pulley x 2.0 mm pitch

    PAN_STEPS_PER_DEG   = _PAN_TILT_MICROSTEPS * _PAN_GEAR_RATIO  / _PAN_TILT_STEP_ANGLE
    TILT_STEPS_PER_DEG  = _PAN_TILT_MICROSTEPS * _TILT_GEAR_RATIO / _PAN_TILT_STEP_ANGLE
    SLIDER_STEPS_PER_MM = _SLIDER_MICROSTEPS * (360.0 / _SLIDER_STEP_ANGLE) / _SLIDER_MM_PER_REV
    ZOOM_JOG_SLOW      = 200     # velocity units (0–1000)
    ZOOM_JOG_FAST      = 700

    def __init__(self, mount_manager: MountManager, parent=None):
        super().__init__(parent)
        self._mm        = mount_manager
        self._mount_id  = 1
        self._nudge_deg_fine   = 0.2    # configurable
        self._nudge_deg_small  = 1.0
        self._nudge_deg_large  = 10.0
        self._nudge_mm_small   = 10.0
        self._nudge_mm_large   = 100.0

        # Limits can land after the panel is already open — on a cold start the
        # operator can beat the first STATUS packet to it. Without this the run
        # zones would stay grey until the panel was closed and reopened.
        self._mm.mount_status_updated.connect(self._on_mount_status)

        self._side = 0          # set by _apply_scale(), which runs before show
        self._build()
        self._apply_scale(_px(_PANEL_REF))
        self.hide()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def show_for(self, mount_id: int,
                 pan_spd: float | None = None,
                 tilt_spd: float | None = None,
                 slider_spmm: float | None = None,
                 nudge_deg_fine: float = 0.2,
                 nudge_deg_small: float = 1.0,
                 nudge_deg_large: float = 10.0,
                 nudge_mm_small: float = 10.0,
                 nudge_mm_large: float = 100.0) -> None:
        self._mount_id         = mount_id
        self._nudge_deg_fine   = nudge_deg_fine
        self._nudge_deg_small  = nudge_deg_small
        self._nudge_deg_large  = nudge_deg_large
        self._nudge_mm_small   = nudge_mm_small
        self._nudge_mm_large   = nudge_mm_large
        if pan_spd   is not None: self.PAN_STEPS_PER_DEG   = pan_spd
        if tilt_spd  is not None: self.TILT_STEPS_PER_DEG  = tilt_spd
        if slider_spmm is not None: self.SLIDER_STEPS_PER_MM = slider_spmm

        self._dial.set_steps(nudge_deg_fine, nudge_deg_small, nudge_deg_large)
        self._track.set_steps(nudge_mm_small, nudge_mm_large)
        self._sync_runs()
        self._sync_track()

        self._centre_on_parent()
        self.show()
        self.raise_()

    def set_mount(self, mount_id: int) -> None:
        """Switch the active camera while the overlay is open."""
        self._mount_id = mount_id
        self._sync_runs()

    def _centre_on_parent(self) -> None:
        """Size the panel for this screen, then put it in the middle.

        Sizing lives here because this is the one place that knows the parent,
        and main_window calls it again on resize.  The screen's height sets the
        side; the parent only ever clamps it, so a short window cannot end up
        with a panel hanging off both ends.
        """
        side = _px(_PANEL_REF)
        p = self.parent()
        if p is not None:
            room = min(p.width(), p.height()) - _px(_PANEL_INSET) * 2
            side = min(side, room)
        self._apply_scale(max(_PANEL_MIN, int(side)))
        if p is not None:
            self.move((p.width()  - self.width())  // 2,
                      (p.height() - self.height()) // 2)

    # ------------------------------------------------------------------
    # Scale
    # ------------------------------------------------------------------

    def _apply_scale(self, side: int) -> None:
        """Resize the panel and everything measured in pixels inside it.

        Every child that draws itself — dial, track, zoom column — already
        works from the rectangle the layout hands it, so they need nothing from
        here beyond the room to grow into.  What is set below is the handful of
        sizes Qt will not derive on its own.
        """
        if side == self._side:
            return
        self._side = side
        f = lambda frac, lo=1: max(lo, round(side * frac))

        self.setStyleSheet(f"""
            QFrame {{
                background: #111827;
                border: {f(_BORDER_F)}px solid #374151;
                border-radius: {f(_RADIUS_F, 2)}px;
            }}
        """)
        self.setFixedSize(side, side)

        m = f(_MARGIN_F)
        self._root.setContentsMargins(m, m, m, m)
        self._root.setSpacing(f(_GAP_F))
        self._mid.setSpacing(f(_COL_GAP_F))
        self._bottom.setSpacing(f(_COL_GAP_F))

        c = f(_CLOSE_F)
        self._close_btn.setFixedSize(c, c)
        self._close_btn.setStyleSheet(_close_style(c))

        self._track.setMinimumHeight(f(_TRACK_H_F))
        self._zoom.set_metrics(f(_ZOOM_W_F), f(_ZOOM_H_F))
        self._zoom_gutter.setFixedWidth(f(_ZOOM_W_F))

        # The track is measured off the dial, so the dial has to have its new
        # geometry first. Without this the sync runs against whatever the dial
        # was before the resize and the track keeps a width from another size
        # of panel — which is exactly what it did.
        self._root.activate()
        self._sync_track()

    # ------------------------------------------------------------------
    # Build layout
    # ------------------------------------------------------------------

    def _build(self) -> None:
        # No sizes here: margins, spacings and the close button are all set by
        # _apply_scale(), which runs before the panel is ever shown.
        self._root = QVBoxLayout(self)

        self._close_btn = QPushButton("✕")
        self._close_btn.clicked.connect(self._on_close)
        self._root.addWidget(self._close_btn, 0, Qt.AlignmentFlag.AlignLeft)

        self._mid = QHBoxLayout()
        self._dial = RadialNudge()
        self._dial.nudged.connect(self._on_dial)
        self._mid.addWidget(self._dial, 1)

        self._zoom = ZoomColumn(self.ZOOM_JOG_FAST, self.ZOOM_JOG_SLOW)
        self._zoom.started.connect(self._zoom_jog)
        self._zoom.stopped.connect(self._zoom_stop)
        self._mid.addWidget(self._zoom, 0, Qt.AlignmentFlag.AlignVCenter)
        self._root.addLayout(self._mid, 1)

        # The track lives in the dial's own column, not the panel's, and is
        # matched to the diameter of the outer arc ring — see _sync_track().
        # Anything else leaves it wider than the control it belongs to.
        self._track = SliderTrack()
        self._track.nudged.connect(self._on_track)
        self._track.run.connect(self._on_run)
        self._bottom = QHBoxLayout()
        self._bottom.addStretch(1)
        self._bottom.addWidget(self._track)
        self._bottom.addStretch(1)
        self._zoom_gutter = QWidget()
        self._bottom.addWidget(self._zoom_gutter)
        self._root.addLayout(self._bottom)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_track()

    def _sync_track(self) -> None:
        """Match the track's width to the dial's outer ring.

        The dial paints inside the largest circle that fits, so its drawn width
        is min(w, h) x the outer radius — not the widget's width, which is
        whatever the layout handed it. Measuring the widget instead would leave
        the track wider than the control it sits under.
        """
        d = getattr(self, "_dial", None)
        if d is None:
            return
        self._track.setFixedWidth(
            max(200, int(min(d.width(), d.height()) * DIAL_OUTER_FRAC)))

    def _on_dial(self, axis: str, degrees: float) -> None:
        if axis == "pan":
            self._nudge_pan(degrees)
        else:
            self._nudge_tilt(degrees)

    def _on_track(self, mm: float) -> None:
        self._nudge_slider(mm)

    # ------------------------------------------------------------------
    # Run to the end of the rail
    # ------------------------------------------------------------------

    def _slider_span(self) -> int | None:
        """Steps from one calibrated end of the rail to the other, or None.

        None means this mount has not found its limits, and a run must not be
        sent: the Teensy clamps a slider move to its limits only once they are
        set (MountMotion::_clampToLimits), so on an uncalibrated axis the same
        command is a move with nothing to stop it.
        """
        st = self._mm.state(self._mount_id)
        lo, hi = getattr(st, "slider_min", None), getattr(st, "slider_max", None)
        if lo is None or hi is None or hi <= lo:
            return None
        return int(hi - lo)

    def _sync_runs(self) -> None:
        self._track.set_runs_enabled(self._slider_span() is not None)

    def _on_mount_status(self, mount_id: int) -> None:
        # Not gated on isVisible(): a hidden panel costs nothing to keep right,
        # and a panel parented to a window that has not been shown yet reports
        # itself hidden even after show() — which is exactly the cold start
        # this is here to cover.
        if mount_id == self._mount_id:
            self._sync_runs()

    def _on_run(self, direction: int) -> None:
        """Send the carriage to one end of the rail.

        A relative move of the full rail length: from anywhere on the rail that
        always overshoots the end, and the Teensy stops it exactly there. It
        avoids asking the mount where it is — the unsolicited position
        broadcast was removed on purpose, and a goto would need all four axes
        in absolute terms, which would drag pan and tilt along with it.
        """
        span = self._slider_span()
        if span is None:
            log.warning("Mount %d: slider run ignored — no calibrated limits",
                        self._mount_id)
            self._sync_runs()
            return
        preset = self._mm.state(self._mount_id).active_sl_preset
        self._mm.send_move_rel(self._mount_id, 0, 0,
                               int(direction) * span, 0, preset)

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
