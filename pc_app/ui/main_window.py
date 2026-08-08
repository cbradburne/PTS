"""
MainWindow — operator interface.

Layout (matches reference screenshots):

  ┌─────────────────────────────────────────────────────────────────────┐
  │ [Move|Edit]  [Cam1][Cam2][Cam3][Cam4][Cam5]  [status]  [SET][ESTOP]│
  ├─────────────────────────────────────────────────────────────────────┤
  │                                                           Pan Slide  │
  │  [1][2][3][4][5][6][7][8][9][10]  ◎   ◎    ← CAM 1 (green)        │
  │  [1][2][3][4][5][6][7][8][9][10]  ◎   ◎    ← CAM 2 (blue)         │
  │  [1][2][3][4][5][6][7][8][9][10]  ◎   ◎    ← CAM 3 (olive)        │
  │  [1][2][3][4][5][6][7][8][9][10]  ◎   ◎    ← CAM 4 (teal)         │
  │  [1][2][3][4][5][6][7][8][9][10]  ◎   ◎    ← CAM 5 (purple)       │
  ├─────────────────────────────────────────────────────────────────────┤
  │ [⚙ Config]  [CV Track]  [Find Limits]     [status label]   [Exit]  │
  └─────────────────────────────────────────────────────────────────────┘

MODE — MOVE:  position buttons recall saved positions
MODE — EDIT:  position buttons store current camera position to that slot
              top bar shows camera selector (Cam1–5, active = coloured border)
"""
from __future__ import annotations

import logging
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QSizePolicy, QFrame, QInputDialog,
    QDialog, QDialogButtonBox, QMessageBox
)
from PyQt6.QtCore import Qt, pyqtSlot, QTimer
from PyQt6.QtGui import QFont, QColor, QPainter, QPaintEvent

from .widgets.position_grid import (
    PositionGrid, MODE_MOVE, MODE_LABEL_EDIT, MODE_SET, MODE_CLEAR, CAM_COLORS
)
from .widgets.estop_button import EStopButton
from .widgets.nudge_overlay import NudgeOverlay
from .dialogs.config_dialog import ConfigDialog
from . import virtual_keyboard
from comms.bridge import Bridge
from comms.mount_manager import MountManager
from comms.protocol import (TARGET_SLOT_LA_MIN, TARGET_SLOT_LA_MAX)
from comms.protocol import MountState, MountFlag, Axis
from config.mount_config import AppConfig, save_config
from config.position_store import PositionStore
from motion.joystick import JoystickHandler
from motion.command_dispatcher import CommandDispatcher

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _accent_hex(mount_id: int) -> str:
    return CAM_COLORS[mount_id]["accent"].name()

def _cam_btn_style(mount_id: int, active: bool) -> str:
    accent = _accent_hex(mount_id)
    bg     = CAM_COLORS[mount_id]["btn_bg"]
    text   = CAM_COLORS[mount_id]["btn_text"]
    border = accent if active else "#333333"
    bw     = 5 #if active else 2
    return f"""
        QPushButton {{
            background: {bg}; color: {text};
            border: {bw}px solid {border};
            border-radius: 20px; font-size: 28px; font-weight: bold;
            padding: 6px 14px;
        }}
    """

def _clear_btn_style(mount_id: int) -> str:
    accent = _accent_hex(mount_id)
    bg     = CAM_COLORS[mount_id]["btn_bg"]
    text   = CAM_COLORS[mount_id]["btn_text"]
    return f"""
        QPushButton {{
            background: {bg}; color: {text};
            border: 4px solid {accent};
            border-radius: 20px; font-size: 22px;
            padding: 4px 10px;
        }}
        QPushButton:pressed {{ background: #111; }}
    """

def _action_btn_style() -> str:
    return """
        QPushButton {
            background: #1E2A35; color: #90A4AE;
            border: 4px solid #37474F; border-radius: 20px;
            font-size: 28px; padding: 6px 14px;
        }
        QPushButton:pressed { background: #131C24; }
    """

def _edit_btn_style(active: bool) -> str:
    """Edit button: RED when active (label-edit mode), neutral when inactive."""
    if active:
        return """QPushButton {
            background: #B71C1C; color: #FFFFFF;
            border: 6px solid #FF1744; border-radius: 20px;
            font-size: 28px; font-weight: bold; padding: 6px 18px;
        }
        QPushButton:pressed { background: #7F0000; }"""
    return """QPushButton {
        background: #1E3A5F; color: #90CAF9;
        border: 6px solid #2962FF; border-radius: 20px;
        font-size: 28px; font-weight: bold; padding: 6px 18px;
    }
    QPushButton:pressed { background: #0D1A2B; }"""

def _move_btn_style(active: bool) -> str:
    """Style for the Move (nudge overlay) button."""
    if active:
        return """QPushButton {
            background: #827717; color: #FFF9C4;
            border: 6px solid #F9A825; border-radius: 20px;
            font-size: 28px; font-weight: bold; padding: 6px 18px;
        }"""
    return """QPushButton {
        background: #33691E; color: #DCEDC8;
        border: 6px solid #689F38; border-radius: 20px;
        font-size: 28px; font-weight: bold; padding: 6px 18px;
    }"""

def _run_btn_style(active: bool) -> str:
    """Run toggle button: green when run mode is active."""
    if active:
        return """QPushButton {
            background: #1A3A1A; color: #A5D6A7;
            border: 6px solid #4CAF50; border-radius: 20px;
            font-size: 28px; font-weight: bold; padding: 6px 18px;
        }
        QPushButton:pressed { background: #0D2410; }"""
    return """QPushButton {
        background: #1E2A35; color: #90A4AE;
        border: 4px solid #37474F; border-radius: 20px;
        font-size: 28px; padding: 6px 18px;
    }
    QPushButton:pressed { background: #131C24; }"""

def _run_cam_active_style(mount_id: int) -> str:
    """Camera button style when that camera's run sequence is active (amber border)."""
    bg = CAM_COLORS[mount_id]["btn_bg"]
    return f"""
        QPushButton {{
            background: {bg}; color: #FFF9C4;
            border: 5px solid #FF9800;
            border-radius: 20px; font-size: 28px; font-weight: bold;
            padding: 6px 14px;
        }}
        QPushButton:pressed {{ background: #111; }}
    """

def _clear_mode_btn_style(active: bool) -> str:
    """Clear toggle button: orange when active."""
    if active:
        return """QPushButton {
            background: #3E2000; color: #FFC107;
            border: 6px solid #FF6F00; border-radius: 20px;
            font-size: 28px; font-weight: bold; padding: 6px 18px;
        }
        QPushButton:pressed { background: #1A0D00; }"""
    return """QPushButton {
        background: #2A2A2A; color: #90A4AE;
        border: 6px solid #455A64; border-radius: 20px;
        font-size: 28px; font-weight: bold; padding: 6px 18px;
    }
    QPushButton:pressed { background: #1A1A1A; }"""

def _clear_cam_btn_style(mount_id: int) -> str:
    """Cam button style when clear mode is active — orange tint."""
    bg = CAM_COLORS[mount_id]["btn_bg"]
    return f"""
        QPushButton {{
            background: {bg}; color: #FFC107;
            border: 5px solid #FF6F00;
            border-radius: 20px; font-size: 24px; font-weight: bold;
            padding: 6px 14px;
        }}
        QPushButton:pressed {{ background: #111; }}
    """

def _set_btn_style(armed: bool) -> str:
    """SET button: bright amber/pulsing when armed (waiting for position tap)."""
    if armed:
        return """QPushButton {
            background: #E65100; color: #FFF9C4;
            border: 6px solid #FF9800; border-radius: 20px;
            font-size: 28px; font-weight: bold; padding: 6px 18px;
        }
        QPushButton:pressed { background: #BF360C; }"""
    return """QPushButton {
        background: #2A2A2A; color: #90A4AE;
        border: 6px solid #455A64; border-radius: 20px;
        font-size: 28px; font-weight: bold; padding: 6px 18px;
    }
    QPushButton:pressed { background: #1A1A1A; }"""


# ---------------------------------------------------------------------------
# Cam-button inactive overlay  (same concept as PositionGrid row overlay)
# ---------------------------------------------------------------------------

class _CamBtnOverlay(QWidget):
    """Semi-transparent screen over a cam selector button when mount is offline."""
    _FILL   = QColor(0, 0, 0, 150)
    _RADIUS = 20          # must match border-radius in _cam_btn_style

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setVisible(True)           # disconnected on startup

    def paintEvent(self, event: QPaintEvent) -> None:   # type: ignore[override]
        from PyQt6.QtGui import QPainterPath
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(self.rect().toRectF(), self._RADIUS, self._RADIUS)
        p.setClipPath(path)
        p.fillRect(self.rect(), self._FILL)


class _CamBtnContainer(QWidget):
    """Wraps one cam QPushButton with a floating _CamBtnOverlay child."""

    def __init__(self, btn: QPushButton, parent=None) -> None:
        super().__init__(parent)
        vl = QVBoxLayout(self)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)
        vl.addWidget(btn)
        self._overlay = _CamBtnOverlay(self)
        self._overlay.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._overlay.setGeometry(self.rect())
        self._overlay.raise_()

    def set_connected(self, connected: bool) -> None:
        self._overlay.setVisible(not connected)


class MainWindow(QMainWindow):

    def __init__(self, config: AppConfig, bridge: Bridge,
                 mount_manager: MountManager,
                 position_store: PositionStore,
                 joystick: JoystickHandler):
        super().__init__()
        self._config  = config
        self._bridge  = bridge
        self._mm      = mount_manager
        self._store   = position_store
        self._joy     = joystick

        self._dispatcher   = CommandDispatcher(joystick, mount_manager, self)
        self._active_mount = 1
        self._mode         = MODE_MOVE
        self._edit_active  = False   # Edit button (label-edit) toggle
        self._set_armed    = False   # SET button armed (waiting for position tap)
        self._clear_mode   = False   # Clear button toggled

        # Run-sequence state — one entry per mount
        self._run_mode  = False
        self._run_states: dict[int, dict] = {
            m: {'active': False, 'slots': [], 'target_idx': 0, 'goto_ignore': 0}
            for m in range(1, 6)
        }

        # target_slot is now tracked by the Teensy and broadcast in every STATUS packet,
        # so all connected clients (PC, phone, tablet) flash in sync automatically.

        self.setWindowTitle("Camera Mount Controller")
        self._build()
        self._connect_signals()
        self._connect_bridge()
        #self.showMaximized()
        self.showFullScreen()

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _build(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        # ---- Top bar ----
        root.addWidget(self._build_top_bar())

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color:#222;")
        root.addWidget(sep)

        # ---- Position grid ----
        self._grid = PositionGrid(self._store)
        root.addWidget(self._grid, stretch=1)

        # ---- Nudge overlay (child of central widget, floats above grid) ----
        self._nudge_overlay = NudgeOverlay(self._mm, parent=central)
        self._nudge_overlay.closed.connect(self._on_nudge_closed)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet("color:#222;")
        root.addWidget(sep2)

        # ---- Bottom bar ----
        root.addWidget(self._build_bottom_bar())

    def _build_top_bar(self) -> QWidget:
        # Three-column grid: left and right both stretch=1 so the centre
        # column (cam buttons) is always at the true horizontal midpoint.
        container = QWidget()
        grid = QGridLayout(container)
        grid.setContentsMargins(4, 4, 4, 4)
        grid.setSpacing(0)
        grid.setColumnStretch(0, 1)  # left  — expands to fill half the slack
        grid.setColumnStretch(1, 0)  # centre — natural width, perfectly centred
        grid.setColumnStretch(2, 1)  # right  — expands to fill half the slack

        A = Qt.AlignmentFlag

        # ---- Left: Move + Edit ----
        left = QWidget()
        left_hl = QHBoxLayout(left)
        left_hl.setContentsMargins(0, 0, 0, 0)
        left_hl.setSpacing(6)

        self._move_btn = QPushButton("Move")
        self._move_btn.setFixedHeight(60)
        self._move_btn.setMinimumWidth(80)
        self._move_btn.setStyleSheet(_move_btn_style(False))
        self._move_btn.clicked.connect(self._toggle_nudge)
        left_hl.addWidget(self._move_btn)

        self._mode_btn = QPushButton("Edit")
        self._mode_btn.setFixedHeight(60)
        self._mode_btn.setMinimumWidth(80)
        self._mode_btn.setStyleSheet(_edit_btn_style(False))
        self._mode_btn.clicked.connect(self._toggle_edit)
        left_hl.addWidget(self._mode_btn)

        left_hl.addStretch()   # push buttons to the left edge

        grid.addWidget(left, 0, 0, A.AlignVCenter | A.AlignLeft)

        # ---- Centre: cam selector ----
        self._cam_btns_widget = self._build_cam_selector()
        grid.addWidget(self._cam_btns_widget, 0, 1, A.AlignVCenter | A.AlignHCenter)

        # ---- Right: status label + Clear + SET ----
        right = QWidget()
        right_hl = QHBoxLayout(right)
        right_hl.setContentsMargins(0, 0, 0, 0)
        right_hl.setSpacing(6)

        right_hl.addStretch()  # push buttons to the right edge

        self._conn_label = QLabel("Not connected")
        self._conn_label.setStyleSheet("color:#555; font-size:10px;")
        right_hl.addWidget(self._conn_label)

        right_hl.addSpacing(8)

        self._clear_mode_btn = QPushButton("Clear")
        self._clear_mode_btn.setFixedHeight(60)
        self._clear_mode_btn.setMinimumWidth(100)
        self._clear_mode_btn.setStyleSheet(_clear_mode_btn_style(False))
        self._clear_mode_btn.clicked.connect(self._toggle_clear_mode)
        right_hl.addWidget(self._clear_mode_btn)

        self._set_btn = QPushButton("SET")
        self._set_btn.setFixedHeight(60)
        self._set_btn.setMinimumWidth(80)
        self._set_btn.setStyleSheet(_set_btn_style(False))
        self._set_btn.clicked.connect(self._on_set_btn)
        right_hl.addWidget(self._set_btn)

        grid.addWidget(right, 0, 2, A.AlignVCenter | A.AlignRight)

        return container

    def _build_cam_selector(self) -> QWidget:
        w  = QWidget()
        hl = QHBoxLayout(w)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(4)
        self._cam_btns: list[QPushButton] = []
        self._cam_containers: dict[int, _CamBtnContainer] = {}
        for mid in range(1, 6):
            btn = QPushButton(self._config.mount_label(mid))
            btn.setFixedHeight(60)
            btn.setMinimumWidth(150)
            btn.setStyleSheet(_cam_btn_style(mid, mid == self._active_mount))
            btn.clicked.connect(self._make_cam_select(mid))
            self._cam_btns.append(btn)
            container = _CamBtnContainer(btn)
            self._cam_containers[mid] = container
            hl.addWidget(container)
        return w

    def _build_bottom_bar(self) -> QWidget:
        # Three-column grid so Run is always perfectly centred.
        container = QWidget()
        grid = QGridLayout(container)
        grid.setContentsMargins(4, 4, 4, 4)
        grid.setSpacing(0)
        grid.setColumnStretch(0, 1)  # left
        grid.setColumnStretch(1, 0)  # centre — Run button
        grid.setColumnStretch(2, 1)  # right

        A = Qt.AlignmentFlag

        # ---- Left: Config / CV Track / Stop Tracking + status label ----
        left = QWidget()
        left_hl = QHBoxLayout(left)
        left_hl.setContentsMargins(0, 0, 0, 0)
        left_hl.setSpacing(6)

        for label, handler in [
            ("⚙  Config",   self._open_config),
            ("◎  CC",       self._open_camera_control),
            ("◉  CV Track", self._open_cv),
        ]:
            btn = QPushButton(label)
            btn.setFixedHeight(60)
            btn.setStyleSheet(_action_btn_style())
            btn.clicked.connect(handler)
            left_hl.addWidget(btn)

        # Stop Tracking — hidden until CV tracking is active
        self._stop_track_btn = QPushButton("■  Stop Tracking")
        self._stop_track_btn.setFixedHeight(60)
        self._stop_track_btn.setStyleSheet("""
            QPushButton {
                background: #B71C1C; color: white; font-size: 14px;
                border: none; border-radius: 8px; padding: 0 16px;
            }
            QPushButton:pressed { background: #7F0000; }
        """)
        self._stop_track_btn.setVisible(False)
        self._stop_track_btn.clicked.connect(self._on_stop_tracking)
        left_hl.addWidget(self._stop_track_btn)

        left_hl.addStretch()

        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color:#546E7A; font-size:10px;")
        left_hl.addWidget(self._status_label)

        grid.addWidget(left, 0, 0, A.AlignVCenter | A.AlignLeft)

        # ---- Centre: Run ----
        self._run_mode_btn = QPushButton("▶  Run")
        self._run_mode_btn.setFixedHeight(60)
        self._run_mode_btn.setMinimumWidth(120)
        self._run_mode_btn.setStyleSheet(_run_btn_style(False))
        self._run_mode_btn.clicked.connect(self._toggle_run_mode)
        grid.addWidget(self._run_mode_btn, 0, 1, A.AlignVCenter | A.AlignHCenter)

        # ---- Right: E-Stop + Exit ----
        right = QWidget()
        right_hl = QHBoxLayout(right)
        right_hl.setContentsMargins(0, 0, 0, 0)
        right_hl.setSpacing(6)

        right_hl.addStretch()

        self._estop_btn = EStopButton()
        right_hl.addWidget(self._estop_btn)

        exit_btn = QPushButton("Exit")
        exit_btn.setFixedHeight(60)
        exit_btn.setStyleSheet("""
            QPushButton { background:#B71C1C; color:#FFF; font-size: 20px;
                border: 4px solid #F00; border-radius: 20px; padding:4px 16px; }
            QPushButton:pressed { background:#0D0D0D; }
        """)
        exit_btn.clicked.connect(self.close)
        right_hl.addWidget(exit_btn)

        grid.addWidget(right, 0, 2, A.AlignVCenter | A.AlignRight)

        return container

    # ------------------------------------------------------------------
    # Connect signals
    # ------------------------------------------------------------------

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_nudge_overlay") and self._nudge_overlay.isVisible():
            self._nudge_overlay._centre_on_parent()

    def _connect_signals(self) -> None:
        self._estop_btn.clicked.connect(lambda: self._mm.send_e_stop())

        self._pair_conflict_box = None   # open pairing-conflict QMessageBox, or None
        self._mm.mount_status_updated.connect(self._on_status_updated)
        self._mm.mount_connected.connect(self._on_mount_connected)
        self._mm.mount_disconnected.connect(self._on_mount_disconnected)
        self._mm.limits_found.connect(self._on_limits_found)
        self._mm.state_report_received.connect(self._on_state_report)
        self._mm.pair_conflict.connect(self._on_pair_conflict)

        self._grid.recall_requested.connect(self._on_recall)
        self._grid.store_requested.connect(self._on_store)
        self._grid.clear_requested.connect(self._on_clear)
        self._grid.label_edited.connect(self._on_label_edited)
        self._grid.pt_preset_changed.connect(self._on_pt_preset)
        self._grid.sl_preset_changed.connect(self._on_sl_preset)
        self._grid.calibrate_subject_requested.connect(self._on_calibrate_subject)
        self._grid.look_at_subject_selected.connect(self._on_look_at_subject_selected)
        self._grid.slider_jog_started.connect(self._on_slider_jog_start)
        self._grid.slider_jog_stopped.connect(self._on_slider_jog_stop)

        # v2 — subject/calib signals from mount manager
        self._mm.subject_list_received.connect(self._on_subjects_updated)
        self._mm.calib_prompt_received.connect(self._on_calib_prompt)
        self._mm.config_report_received.connect(self._on_config_report)
        self._mm.look_at_status_updated.connect(self._on_look_at_status_from_mount)

        # Track the open calibration popup per mount (mount_id → QDialog or None)
        self._calib_popups: dict[int, QDialog] = {}

        # Active look-at subject per mount (-1 = none selected)
        self._active_la_subject: dict[int, int] = {m: -1 for m in range(1, 6)}

        # Arrow-button visual state tracking for look-at mounts.
        # Mirrors PositionGrid._la_arrow_state so STATUS logic can read it without
        # accessing private grid members.
        self._la_arrow: dict[int, "str | None"] = {m: None for m in range(1, 6)}

        # Initial camera selection
        self._select_mount(1)

    # ------------------------------------------------------------------
    # Edit / SET mode logic
    # ------------------------------------------------------------------
    # Look-at arrow state helper
    # ------------------------------------------------------------------

    def _set_la_arrow(self, mount_id: int, state: "str | None") -> None:
        """Set the ◀/▶ border state in both the tracking dict and the grid."""
        self._la_arrow[mount_id] = state
        self._grid.set_la_arrow_state(mount_id, state)

    # ------------------------------------------------------------------

    def _toggle_edit(self) -> None:
        """Edit button: toggle label-edit mode (RED when active)."""
        self._edit_active = not self._edit_active
        self._mode_btn.setStyleSheet(_edit_btn_style(self._edit_active))

        if self._edit_active:
            # Edit always wins — disarm any competing mode so the grid reliably
            # lands in MODE_LABEL_EDIT.  _disarm_set / _exit_clear_mode both
            # read _edit_active (now True) and set status + grid mode correctly.
            if self._set_armed:
                self._disarm_set()
            elif self._clear_mode:
                self._exit_clear_mode()
            else:
                self._status_label.setText(
                    "EDIT — tap a position button to rename it")
                self._mode = MODE_LABEL_EDIT
                self._grid.set_mode(MODE_LABEL_EDIT)
        else:
            self._status_label.setText("")
            self._mode = MODE_MOVE
            self._grid.set_mode(MODE_MOVE)

    def _on_set_btn(self) -> None:
        """SET button: arm one-shot position-store.  Click again to disarm."""
        if self._set_armed:
            self._disarm_set()
        else:
            # Exit clear mode if active so modes don't overlap
            if self._clear_mode:
                self._exit_clear_mode()
            self._set_armed = True
            self._set_btn.setStyleSheet(_set_btn_style(True))
            self._status_label.setText("SET — tap a position button to store")
            self._mode = MODE_SET
            self._grid.set_mode(MODE_SET)

    def _disarm_set(self) -> None:
        self._set_armed = False
        self._set_btn.setStyleSheet(_set_btn_style(False))
        self._status_label.setText(
            "EDIT — tap a position button to rename it"
            if self._edit_active else "")
        self._mode = MODE_LABEL_EDIT if self._edit_active else MODE_MOVE
        self._grid.set_mode(self._mode)

    def _toggle_nudge(self) -> None:
        """Show or hide the nudge overlay for the active mount."""
        if self._nudge_overlay.isVisible():
            self._nudge_overlay.hide()
            self._move_btn.setStyleSheet(_move_btn_style(False))
        else:
            mc = self._config.mount(self._active_mount)
            self._nudge_overlay.show_for(
                mount_id=self._active_mount,
                pan_spd=NudgeOverlay.PAN_STEPS_PER_DEG,
                tilt_spd=NudgeOverlay.TILT_STEPS_PER_DEG,
                slider_spmm=NudgeOverlay.SLIDER_STEPS_PER_MM,
            )
            self._move_btn.setStyleSheet(_move_btn_style(True))

    def _on_nudge_closed(self) -> None:
        self._move_btn.setStyleSheet(_move_btn_style(False))

    # ------------------------------------------------------------------
    # Run sequence
    # ------------------------------------------------------------------

    def _toggle_run_mode(self) -> None:
        """Toggle the global run mode on/off."""
        self._run_mode = not self._run_mode
        if self._run_mode:
            if self._set_armed:         # SET and Run can't coexist
                self._disarm_set()
            self._run_mode_btn.setStyleSheet(_run_btn_style(True))
            for mid in range(1, 6):
                self._cam_btns[mid - 1].setText("▶  Run")
                self._cam_btns[mid - 1].setStyleSheet(
                    _cam_btn_style(mid, mid == self._active_mount))
        else:
            # Exit run mode — leave active runs running; restore each button
            # individually (_update_run_cam_btn shows amber "■ Stop" if still active)
            self._run_mode_btn.setStyleSheet(_run_btn_style(False))
            for mid in range(1, 6):
                self._update_run_cam_btn(mid)

    def _toggle_cam_run(self, mount_id: int) -> None:
        """Start or stop the run sequence for one camera."""
        if self._run_states[mount_id]['active']:
            self._stop_run(mount_id)
        else:
            self._start_run(mount_id)

    def _start_run(self, mount_id: int) -> None:
        if self._config.mount(mount_id).look_at_mode:
            self._start_look_at_run(mount_id)
            return
        st    = self._mm.state(mount_id)
        slots = [s for s in range(10) if st.slot_occupied_mask & (1 << s)]
        if len(slots) < 2:
            self._status_label.setText(
                f"Camera {mount_id}: need 2+ saved positions to start a run sequence.")
            return
        rs = self._run_states[mount_id]
        rs['active']      = True
        rs['slots']       = slots
        rs['target_idx']  = 0
        rs['goto_ignore'] = 3    # skip first 3 STATUS ticks to let goto arrive
        self._mm.send_goto_slot(mount_id, slots[0],
                                self._grid.get_pt_preset(mount_id),
                                self._mm.state(mount_id).active_sl_preset)
        self._update_run_cam_btn(mount_id)

    def _start_look_at_run(self, mount_id: int) -> None:
        """Run mode for look-at mounts: bounce slider end-to-end indefinitely."""
        # Use the currently selected subject; fall back to the first valid one.
        subj = self._active_la_subject.get(mount_id, -1)
        if subj < 0:
            st = self._mm.state(mount_id)
            for i, s in enumerate(st.subjects if st else []):
                if s is not None and s.valid:
                    subj = i
                    break
        if subj < 0:
            self._status_label.setText(
                f"Camera {mount_id}: select a subject before starting look-at run.")
            return
        rs = self._run_states[mount_id]
        rs['active']  = True
        rs['la_dir']  = 0        # start toward min limit
        rs['la_subj'] = subj
        rs['slots']   = []       # not used in look-at mode
        self._mm.send_start_look_at_move(
            mount_id, subj, 0, self._grid.get_sl_preset(mount_id))
        self._update_run_cam_btn(mount_id)

    def _stop_run(self, mount_id: int) -> None:
        self._run_states[mount_id]['active'] = False
        self._update_run_cam_btn(mount_id)

    def _update_run_cam_btn(self, mount_id: int) -> None:
        """Refresh the camera button text/style to reflect current run state."""
        btn = self._cam_btns[mount_id - 1]
        if self._run_states[mount_id]['active']:
            btn.setText("■  Stop")
            btn.setStyleSheet(_run_cam_active_style(mount_id))
        elif self._run_mode:
            btn.setText("▶  Run")
            btn.setStyleSheet(_cam_btn_style(mount_id, mount_id == self._active_mount))
        else:
            btn.setText(self._config.mount_label(mount_id))
            btn.setStyleSheet(_cam_btn_style(mount_id, mount_id == self._active_mount))

    # ------------------------------------------------------------------
    # Camera selection
    # ------------------------------------------------------------------

    def _make_cam_select(self, mount_id: int):
        def handler():
            if self._clear_mode:
                self._clear_entire_bank(mount_id)
            elif self._run_mode:
                self._toggle_cam_run(mount_id)
            elif self._run_states[mount_id]['active']:
                # Camera is still running after exiting run mode — stop it
                self._stop_run(mount_id)
            elif self._edit_active:
                self._rename_cam(mount_id)
            else:
                self._select_mount(mount_id)
        return handler

    def _rename_cam(self, mount_id: int) -> None:
        current = self._config.mount_label(mount_id)
        # Touchscreen-only PC: get_text() shows the on-screen keyboard while the
        # dialog is up and closes it as soon as it commits (OK / Enter / Return).
        text, ok = virtual_keyboard.get_text(
            self, "Rename Camera",
            f"Label for Camera {mount_id}:",
            current
        )
        if ok:
            self._config.mount(mount_id).label = text.strip()
            # Name changes are written to the working temp.json only — never to
            # a saved set (Default.json / user files); those change only via the
            # Config dialog's Save / Set Defaults buttons.
            from config import name_store
            name_store.save_temp(self._store, self._config)
            # Update both cam and clear buttons
            new_label = self._config.mount_label(mount_id)
            self._cam_btns[mount_id - 1].setText(new_label)

    def _on_label_edited(self, mount_id: int, slot: int, label: str) -> None:
        # A position name was edited — persist the working set to temp.json only.
        from config import name_store
        name_store.save_temp(self._store, self._config)

    def _reload_names_ui(self) -> None:
        """Refresh every camera button + position button after a name Load."""
        for mid in range(1, 6):
            self._cam_btns[mid - 1].setText(self._config.mount_label(mid))
            for slot in range(10):
                self._grid.refresh_button(mid, slot)

    def _select_mount(self, mount_id: int) -> None:
        self._active_mount         = mount_id
        self._dispatcher.active_mount_id = mount_id
        self._grid.set_active_mount(mount_id)
        self._nudge_overlay.set_mount(mount_id)
        for i, btn in enumerate(self._cam_btns):
            btn.setStyleSheet(_cam_btn_style(i + 1, i + 1 == mount_id))

    # ------------------------------------------------------------------
    # Clear mode
    # ------------------------------------------------------------------

    def _toggle_clear_mode(self) -> None:
        if self._clear_mode:
            self._exit_clear_mode()
        else:
            # Don't allow clear mode to overlap with run mode
            if self._run_mode:
                return
            if self._set_armed:
                self._disarm_set()
            self._clear_mode = True
            self._grid.set_mode(MODE_CLEAR)
            self._clear_mode_btn.setStyleSheet(_clear_mode_btn_style(True))
            self._status_label.setText(
                "CLEAR")
            # Switch cam buttons to orange "Clear" style
            for mid in range(1, 6):
                self._cam_btns[mid - 1].setText("Clear")
                self._cam_btns[mid - 1].setStyleSheet(_clear_cam_btn_style(mid))

    def _exit_clear_mode(self) -> None:
        """Exit clear mode without executing any further deletions."""
        self._clear_mode = False
        self._grid.reset_clear_selection()
        self._grid.set_mode(MODE_LABEL_EDIT if self._edit_active else MODE_MOVE)
        self._clear_mode_btn.setStyleSheet(_clear_mode_btn_style(False))
        self._status_label.setText(
            "EDIT — tap a position button to rename it"
            if self._edit_active else "")
        # Restore cam button labels and styles
        for mid in range(1, 6):
            self._update_run_cam_btn(mid)

    def _clear_entire_bank(self, mount_id: int) -> None:
        """Called when a Cam button is pressed while clear mode is active."""
        st = self._mm.state(mount_id)
        for slot in range(10):
            if st.slot_occupied_mask & (1 << slot):
                self._mm.send_clear_pos(mount_id, slot)
        self._store.clear_mount(mount_id)
        self._exit_clear_mode()

    # ------------------------------------------------------------------
    # Store (Edit mode)
    # ------------------------------------------------------------------

    @pyqtSlot(int, int)
    def _on_store(self, mount_id: int, slot: int) -> None:
        st = self._mm.state(mount_id)
        if not st.connected:
            self._status_label.setText(
                f"Cannot store — Camera {mount_id} is not connected")
            return

        # Coordinates are stored on the Teensy via CMD_STORE_POS.
        # The slot border is NOT updated optimistically — the PC app only
        # reflects what the mount reports via STATUS packets (~100 ms later).
        # This keeps the PC app view consistent with the hub display and web app.
        self._mm.send_store_pos(mount_id, slot)
        self._status_label.setText(
            f"Stored: Camera {mount_id} → Position {slot + 1}")

        # Auto-disarm SET mode after one store
        if self._set_armed:
            self._disarm_set()

    @pyqtSlot(int, int)
    def _on_clear(self, mount_id: int, slot: int) -> None:
        if self._config.mount(mount_id).look_at_mode:
            # In look-at mode 'slot' is a subject_id — delete the subject.
            self._mm.send_delete_subject(mount_id, slot)
            self._grid.mark_subject_deleted(mount_id, slot)
            # Clear main-window active-subject state so the next LOOK_AT_STATUS
            # is processed correctly and doesn't re-highlight a deleted subject.
            if self._active_la_subject.get(mount_id) == slot:
                self._active_la_subject[mount_id] = -1
        else:
            self._mm.send_clear_pos(mount_id, slot)
            self._store.clear_slots(mount_id, {slot})
            # Don't optimistically clear the border — wait for the STATUS echo
            # so the PC app stays in sync with the hub display and web app.

    # ------------------------------------------------------------------
    # Recall (Move mode)
    # ------------------------------------------------------------------

    @pyqtSlot(int, int)
    def _on_recall(self, mount_id: int, slot: int) -> None:
        st = self._mm.state(mount_id)
        if not (st.slot_occupied_mask & (1 << slot)):
            return
        pt_preset = self._grid.get_pt_preset(mount_id)
        sl_preset = self._mm.state(mount_id).active_sl_preset

        # If CV tracking is active on this mount, only move the slider —
        # pan/tilt are under CV tracking control.
        cv = getattr(self, "_cv_window", None)
        if cv and cv.tracking_mount_id == mount_id:
            axis_mask = 0x04   # slider only
        else:
            axis_mask = 0x0F   # all axes (normal)

        self._mm.send_goto_slot(mount_id, slot, pt_preset, sl_preset, axis_mask)
        # Start flashing immediately — don't wait for the Teensy STATUS round-trip
        # (~100 ms).  The STATUS-driven path in _on_status_updated keeps the flash
        # alive and clears it automatically when target_slot returns to 0xFF.
        self._grid.set_target_slot(mount_id, slot)

    # ------------------------------------------------------------------
    # Speed preset handlers
    # ------------------------------------------------------------------

    @pyqtSlot(int, int)
    def _on_pt_preset(self, mount_id: int, preset: int) -> None:
        self._dispatcher.set_pt_preset(mount_id, preset)

    @pyqtSlot(int, int)
    def _on_sl_preset(self, mount_id: int, preset: int) -> None:
        self._dispatcher.set_sz_preset(mount_id, preset)

    # ------------------------------------------------------------------
    # Mount manager signal handlers
    # ------------------------------------------------------------------

    @pyqtSlot(int)
    def _on_state_report(self, mount_id: int) -> None:
        st = self._mm.state(mount_id)
        self._grid.update_slot_masks(mount_id, st.slot_occupied_mask, st.slot_at_mask)

        # Sync preset dials and dispatcher to what the mount actually has active.
        # Do this via sync_preset_from_mount (not set_pt/sl_preset) so we don't
        # send CMD_SET_ACTIVE_PRESET back — the mount already has the right value.
        pt = st.active_pt_preset
        sl = st.active_sl_preset
        if 1 <= pt <= 4:
            self._grid.set_pt_preset(mount_id, pt)
        if 1 <= sl <= 4:
            self._grid.set_sl_preset(mount_id, sl)
        if 1 <= pt <= 4 and 1 <= sl <= 4:
            self._dispatcher.sync_preset_from_mount(mount_id, pt, sl)

    def _on_pair_conflict(self, conflict) -> None:
        """A device is claiming a camera number already bound to another mount.
        Show a Replace/Ignore prompt — like the hub display and web app — so
        pairing can be resolved from the PC with no console.  cam 0 = the hub
        cleared the conflict (device left, or it was resolved elsewhere)."""
        cam = conflict.cam
        if cam < 1 or cam > 5:
            if self._pair_conflict_box is not None:
                self._pair_conflict_box.done(0)   # dismiss the open prompt
            return
        if self._pair_conflict_box is not None:
            return                                # a prompt is already showing

        def mac(b: bytes) -> str:
            return ":".join(f"{x:02x}" for x in b)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Pairing conflict")
        box.setText(
            f"A new device {mac(conflict.new_mac)} is claiming CAM {cam}"
            + (f",\ncurrently paired to {mac(conflict.old_mac)}."
               if any(conflict.old_mac) else "."))
        box.setInformativeText("Replace binds the new device to this camera; "
                               "Ignore keeps the current one.")
        replace_btn = box.addButton("Replace", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Ignore", QMessageBox.ButtonRole.RejectRole)
        self._pair_conflict_box = box
        box.exec()
        self._pair_conflict_box = None
        if box.clickedButton() is replace_btn:
            self._mm.send_pair_decide(cam, True, conflict.new_mac)     # set
        elif box.clickedButton() is not None:
            self._mm.send_pair_decide(cam, False, conflict.new_mac)    # ignore

    @pyqtSlot(int)
    def _on_status_updated(self, mount_id: int) -> None:
        st = self._mm.state(mount_id)
        self._grid.update_slot_masks(mount_id, st.slot_occupied_mask, st.slot_at_mask)

        # target_slot comes directly from the Teensy STATUS packet (0xFF = none).
        # All connected clients receive the same value, so flashing is synchronised
        # across PC, phone, and any other WS client automatically.
        target = st.target_slot if st.target_slot != 0xFF else None
        self._grid.set_target_slot(mount_id, target)

        # Sync preset dials whenever the mount reports a different active preset.
        # STATUS packets arrive every 100ms and already carry active_pt/sl_preset,
        # so this catches the initial connect and any changes made from another
        # control surface without needing to wait for a STATE_REPORT.
        # sync_preset_from_mount is only called when something actually changes,
        # avoiding hub-announce spam on every tick.
        pt = st.active_pt_preset
        sl = st.active_sl_preset
        if (1 <= pt <= 4 and pt != self._grid.get_pt_preset(mount_id)) or \
           (1 <= sl <= 4 and sl != self._grid.get_sl_preset(mount_id)):
            if 1 <= pt <= 4:
                self._grid.set_pt_preset(mount_id, pt)
            if 1 <= sl <= 4:
                self._grid.set_sl_preset(mount_id, sl)
            self._dispatcher.sync_preset_from_mount(mount_id, pt, sl)

        # ---- Active look-at subject (byte [9] of STATUS) ----
        # Sync on every STATUS so late-connecting devices immediately show
        # which subject is selected without waiting for a move to start.
        if st.look_at_mode and st.has_slider:
            new_subj = st.active_subject_id if st.active_subject_id <= 7 else -1
            if self._active_la_subject.get(mount_id) != new_subj:
                self._active_la_subject[mount_id] = new_subj
                self._grid.set_active_la_subject(mount_id, new_subj)

        # ---- Look-at arrow button state (◀/▶ flash/green/grey) ----
        #
        # Driven by target_slot, which the MOUNT sets when a look-at move
        # actually starts and clears when the controller releases the axes
        # (arrival, E-stop or abort alike).  See TARGET_SLOT_* in protocol.py.
        #
        # This used to come from CMD_LA_MOVE_DIR, injected by the hub as it
        # relayed the command — the hub's intent, not the mount's state, so a
        # command lost on the radio still flashed an arrow for a move that was
        # never running.  Reading the mount instead also removes the heuristics
        # that propped that up: a race window waiting for the Teensy to enter
        # LOOK_AT_MOVE, and an AT_MIN/AT_MAX flag fallback to guess direction.
        # The mount now states both facts outright.
        if st.look_at_mode and st.has_slider:
            cur_arrow = self._la_arrow[mount_id]
            if st.target_slot == TARGET_SLOT_LA_MIN:
                self._set_la_arrow(mount_id, 'left_moving')
            elif st.target_slot == TARGET_SLOT_LA_MAX:
                self._set_la_arrow(mount_id, 'right_moving')
            elif cur_arrow == 'left_moving':
                self._set_la_arrow(mount_id, 'left_done')
            elif cur_arrow == 'right_moving':
                self._set_la_arrow(mount_id, 'right_done')
            # 'left_done'/'right_done'/None are left alone: green persists
            # until a jog or the next move clears it, as before.

        # ---- Run sequence advancement ----
        rs = self._run_states[mount_id]
        if not rs['active']:
            return

        # Look-at runs are advanced by _on_look_at_status_from_mount when the
        # slider reaches its limit (look_at_active goes False).  Only check for
        # joystick cancel here.
        if self._config.mount(mount_id).look_at_mode:
            if st.state == MountState.JOGGING:
                self._stop_run(mount_id)
            return

        # Rebuild the slot list if positions were added or cleared mid-run.
        current_slots = [s for s in range(10) if st.slot_occupied_mask & (1 << s)]
        if current_slots != rs['slots']:
            if len(current_slots) < 2:
                self._stop_run(mount_id)
                return
            rs['slots'] = current_slots
            if rs['target_idx'] >= len(rs['slots']):
                rs['target_idx']  = 0
                rs['goto_ignore'] = 3
                self._mm.send_goto_slot(mount_id, rs['slots'][0],
                                        self._grid.get_pt_preset(mount_id),
                                        self._mm.state(mount_id).active_sl_preset)
            return

        # Suppress checks for a few ticks after sending each goto so the
        # packet has time to reach the Teensy and the mount starts moving.
        if rs['goto_ignore'] > 0:
            rs['goto_ignore'] -= 1
            return

        # Joystick moved → cancel this camera's run.
        if st.state == MountState.JOGGING:
            self._stop_run(mount_id)
            return

        # Mount has arrived at the target slot → advance to the next one.
        target_slot = rs['slots'][rs['target_idx']]
        if st.slot_at_mask & (1 << target_slot):
            rs['target_idx']  = (rs['target_idx'] + 1) % len(rs['slots'])
            next_slot         = rs['slots'][rs['target_idx']]
            self._mm.send_goto_slot(mount_id, next_slot,
                                    self._grid.get_pt_preset(mount_id),
                                    self._mm.state(mount_id).active_sl_preset)
            rs['goto_ignore'] = 3

    @pyqtSlot(int)
    def _on_mount_connected(self, mount_id: int) -> None:
        self._conn_label.setText(f"Cam {mount_id} connected")
        self._conn_label.setStyleSheet(
            f"color:{_accent_hex(mount_id)}; font-size:10px;")
        # Apply the slot masks from the STATUS packet that triggered this signal
        # *before* removing the overlay, so correct borders are visible immediately
        # rather than waiting for the next _on_status_updated call.
        st = self._mm.state(mount_id)
        self._grid.update_slot_masks(mount_id, st.slot_occupied_mask, st.slot_at_mask)
        # Remove the inactive overlays now that the mount is online.
        self._grid.set_mount_connected(mount_id, True)
        self._cam_containers[mount_id].set_connected(True)
        # Ask the camera for its current settings — _on_config_report will update
        # the grid and local config when the CONFIG_REPORT arrives.
        # If the camera has a slider, _on_config_report also fetches the subject list.
        self._mm.send_get_config(mount_id)

        # Auto-select the lowest-numbered connected camera whenever the currently
        # active mount isn't online (covers the initial "no camera selected" state
        # at startup without overriding an explicit user selection).
        if not self._mm.state(self._active_mount).connected:
            first = next((m for m in range(1, 6) if self._mm.state(m).connected), None)
            if first is not None:
                self._select_mount(first)

        # Hub display preset will be updated once the STATE_REPORT arrives
        # (via _on_state_report → sync_preset_from_mount → send_preset_announce).

    @pyqtSlot(int)
    def _on_mount_disconnected(self, mount_id: int) -> None:
        self._conn_label.setText(f"Cam {mount_id} disconnected")
        self._conn_label.setStyleSheet("color:#EF5350; font-size:10px;")
        self._cam_containers[mount_id].set_connected(False)
        if self._run_mode and self._run_states[mount_id]['active']:
            self._stop_run(mount_id)

        # Zero slot masks immediately so all position borders go grey while the
        # mount is offline.  Labels (user-assigned names) live in PositionStore and
        # are NOT cleared — they survive any disconnect/reconnect cycle.  The real
        # masks are restored from STATUS / STATE_REPORT the moment the mount reconnects.
        self._grid.update_slot_masks(mount_id, 0, 0)

        # Clear any in-flight move flash — target_slot will be 0xFF once the
        # mount reconnects and sends STATUS again, but clear the grid immediately.
        self._grid.set_target_slot(mount_id, None)

        # Reset arrow state so stale yellow/green doesn't survive a reconnect.
        self._set_la_arrow(mount_id, None)

        # Zero speed dials so disconnected mounts are visually obvious.
        self._grid.set_mount_connected(mount_id, False)

    @pyqtSlot(int, int, int, int)
    def _on_limits_found(self, mount_id: int, axis: int,
                         min_steps: int, max_steps: int) -> None:
        mc = self._config.mount(mount_id)
        if axis == int(Axis.SLIDER):
            mc.slider_min = min_steps
            mc.slider_max = max_steps
        elif axis == int(Axis.ZOOM):
            mc.zoom_min = min_steps
            mc.zoom_max = max_steps
        save_config(self._config)
        self._status_label.setText(
            f"Limits found — Cam {mount_id} {'Slider' if axis == 2 else 'Zoom'}: "
            f"{min_steps} → {max_steps} steps")

    # ------------------------------------------------------------------
    # Action buttons
    # ------------------------------------------------------------------

    def _open_config(self) -> None:
        # Shown modeless with .show() rather than .exec(): a modal exec() loop
        # animates macOS out of the main window's fullscreen Space (the same
        # reason the CV window used .show()).  Result handled via the accepted
        # signal instead of exec()'s return value.
        if getattr(self, "_config_dlg", None):
            # raise_ only, for the same reason as the open path below: making
            # the window key would ask macOS to move the operator to it.
            self._config_dlg.raise_()
            return
        # Snapshot the connection settings so _on_config_accepted only tears
        # down + rebuilds the link if they actually changed — otherwise every
        # Config→OK blipped all mounts off/on (disconnect+reconnect), which is
        # both disruptive mid-show and the connected-only disturbance behind
        # the macOS fullscreen wobble.
        self._conn_snapshot = (self._config.bridge_mode, self._config.bridge_host,
                               self._config.bridge_tcp_port, self._config.bridge_port)
        dlg = ConfigDialog(self._config, self._mm, self._bridge, self._store, self)
        self._config_dlg = dlg
        dlg.names_changed.connect(self._reload_names_ui)
        # Shown exactly like the CV window (which floats correctly over macOS
        # fullscreen): plain modeless .show() + raise_, NO window modality.
        # WindowModal+.show() does NOT make a native sheet — it spawns a
        # separate window that lands on the desktop Space; Tool did the same.
        # The connection-dependent fullscreen wobble was the reconnect blip on
        # Config→OK, now fixed above (only reconnect if settings changed).
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dlg.accepted.connect(self._on_config_accepted)
        dlg.finished.connect(lambda _: setattr(self, "_config_dlg", None))
        dlg.show()
        # raise_() ONLY — no activateWindow().  Making a window "key" at the
        # AppKit level is what asks macOS to bring the operator TO it, and with
        # the main window in a native-fullscreen Space that means being thrown
        # to the desktop.  The Sheet flag fixed WHERE the window goes; this is
        # what was still moving the user.  The CV window has always shown with
        # show()+raise_() and no activate, and has never had the problem —
        # this now matches it exactly.
        #
        # Deferred by one event-loop turn: run synchronously after show() the
        # raise is applied before the window manager has finished placing the
        # window, which is how it ended up behind everything else.
        QTimer.singleShot(0, dlg.raise_)

    def _on_config_accepted(self) -> None:
        conn_now = (self._config.bridge_mode, self._config.bridge_host,
                    self._config.bridge_tcp_port, self._config.bridge_port)
        # Only (re)connect if the connection settings changed, or we're not
        # currently connected.  An unconditional reconnect dropped every mount
        # on each Config→OK even when nothing about the link changed.
        if conn_now != getattr(self, "_conn_snapshot", None) or not self._bridge.connected:
            self._connect_bridge()
        for mid in range(1, 6):
            self._grid.set_has_slider(mid, self._config.mount(mid).has_slider)
            self._grid.set_look_at_mode(mid, self._config.mount(mid).look_at_mode)
            # Reset cached look-at selection so stale subjects don't persist.
            self._active_la_subject[mid] = -1

    def _open_camera_control(self) -> None:
        """Blackmagic camera control over each mount's BLE link.

        Modeless and remembered, like the CV window: pressing autofocus while
        watching the shot is the whole point, and a modal dialog would sit on
        top of the thing being focused.
        """
        from .dialogs.camera_control_dialog import CameraControlDialog
        if not getattr(self, "_cc_dialog", None):
            ids    = [m.mount_id for m in self._mm.all_states()]
            labels = {i: self._config.mount_label(i) for i in ids}
            self._cc_dialog = CameraControlDialog(ids, labels, self._mm,
                                                  self._bridge, self)
            self._cc_dialog.finished.connect(
                lambda _: setattr(self, "_cc_dialog", None))
        self._cc_dialog.show()
        self._cc_dialog.raise_()

    def _open_cv(self) -> None:
        from .cv_window import CVWindow
        # Store reference — local variable would be GC'd immediately after show()
        if not hasattr(self, "_cv_window") or not self._cv_window:
            self._cv_window = CVWindow(self._config.cv_mount_id, self._mm,
                                       self._config, self)
            self._cv_window.destroyed.connect(lambda: setattr(self, "_cv_window", None))
            self._cv_window.tracking_changed.connect(self._on_cv_tracking_changed)
        self._cv_window.show()
        self._cv_window.raise_()

    def _on_cv_tracking_changed(self, mount_id: int, active: bool) -> None:
        """Show/hide the Stop Tracking button and update dispatcher when CV tracking changes."""
        self._stop_track_btn.setVisible(active)
        self._dispatcher.set_cv_tracking(mount_id, active)

    def _on_stop_tracking(self) -> None:
        """Stop Tracking button — stops CV tracking and hides itself."""
        cv = getattr(self, "_cv_window", None)
        if cv:
            cv.stop_tracking()
        self._stop_track_btn.setVisible(False)

    # ------------------------------------------------------------------
    # Slider arrow jog (PositionGrid signals)
    # ------------------------------------------------------------------

    @pyqtSlot(int, int)
    def _on_slider_jog_start(self, mount_id: int, direction: int) -> None:
        """Arrow button pressed.  If a look-at subject is active, start a look-at
        move toward that slider limit; otherwise fall back to raw slider jog."""
        # Arrow state comes from the mount's target_slot in _on_status()
        # back to all clients (including this one), so all devices stay in sync.
        # No local optimistic update needed — the round-trip is <10 ms over loopback.

        subj = self._active_la_subject.get(mount_id, -1)
        if subj >= 0:
            # direction: -1 = left = min limit (0), +1 = right = max limit (1)
            la_dir = 0 if direction < 0 else 1
            preset = self._grid.get_sl_preset(mount_id)
            self._mm.send_start_look_at_move(mount_id, subj, la_dir, preset)
        else:
            vel = 1000 * direction
            self._mm.send_jog(mount_id, 0, 0, vel, 0,
                              pt_preset=2, sz_preset=self._grid.get_sl_preset(mount_id),
                              axis_mask=0x04)

    @pyqtSlot(int)
    def _on_slider_jog_stop(self, mount_id: int) -> None:
        """Arrow button released.  Only stop if we were raw-jogging
        (look-at moves run to their natural end, not held like a jog)."""
        subj = self._active_la_subject.get(mount_id, -1)
        if subj < 0:
            self._mm.send_jog(mount_id, 0, 0, 0, 0, axis_mask=0x04)

    @pyqtSlot(int, int)
    def _on_look_at_subject_selected(self, mount_id: int, slot: int) -> None:
        """Subject button pressed in MOVE mode — toggle look-at subject selection.

        If a look-at move is already running, also sends CMD_SWITCH_SUBJECT so
        the Teensy retargets pan/tilt immediately without stopping the slider.
        """
        st = self._mm.state(mount_id)
        # Presence comes from the STATUS bitmask, which the mount re-broadcasts
        # every 100 ms, NOT from the cached SUBJECT_LIST.  That cache is a
        # one-shot reply to CMD_GET_SUBJECTS and nothing refills it once it is
        # dropped, so gating on it meant only the most recently calibrated
        # subject could be selected — every earlier one silently ignored the
        # click, with no send and no visible change.  ad80b8d moved the border
        # colour and the grid's own click test onto the bitmask; this second
        # gate was missed, so the button lit up correctly and still did nothing.
        # On a look-at mount those bits ARE the stored subjects (esp32_hub.ino:826).
        occupied = bool(st.slot_occupied_mask & (1 << slot)) if st else False
        if occupied:
            if self._active_la_subject.get(mount_id) == slot:
                # Tap again to deselect
                self._active_la_subject[mount_id] = -1
            else:
                self._active_la_subject[mount_id] = slot
                # Always send CMD_SWITCH_SUBJECT — the firmware handles both
                # states, so there is no need to gate on look_at_active (which
                # can be up to 100 ms stale).  Mid-move it retargets pan/tilt on
                # the next controller tick; when idle it calls aimAtSubject()
                # and swings pan/tilt onto the subject from where it is
                # (teensy41_mount.ino:1671-1690).  It is NOT a no-op when idle,
                # as this comment previously claimed.
                self._mm.send_switch_subject(mount_id, slot)
        self._grid.set_active_la_subject(mount_id, self._active_la_subject[mount_id])

    @pyqtSlot(int)
    def _on_look_at_status_from_mount(self, mount_id: int) -> None:
        """CMD_LOOK_AT_STATUS arrived from firmware (may originate from any device).

        Sync _active_la_subject and the PositionGrid border colours so the PC app
        stays in step with the web app and hub display when another device changes
        the selected subject.

        Also advances the look-at Run sequence: when look_at_active goes False the
        slider has reached its limit, so flip direction and fire the next move.
        """
        st = self._mm.state(mount_id)
        la = st.look_at_status if st else None
        if la is None:
            return
        new_subj = la.subject_id if la.subject_id <= 7 else -1
        if self._active_la_subject.get(mount_id) != new_subj:
            self._active_la_subject[mount_id] = new_subj
            self._grid.set_active_la_subject(mount_id, new_subj)

        # Look-at Run advancement: slider reached limit → reverse direction.
        rs = self._run_states[mount_id]
        if rs['active'] and self._config.mount(mount_id).look_at_mode:
            if not la.look_at_active:
                rs['la_dir'] = 1 - rs['la_dir']   # flip 0 ↔ 1
                # Always use the currently selected subject — never the one locked
                # in at run start — so the user can switch subjects mid-run freely.
                subj = self._active_la_subject.get(mount_id, rs['la_subj'])
                self._mm.send_start_look_at_move(
                    mount_id, subj, rs['la_dir'],
                    self._grid.get_sl_preset(mount_id))

    # ------------------------------------------------------------------
    # Subject calibration (PositionGrid signals + MountManager signals)
    # ------------------------------------------------------------------

    @pyqtSlot(int, int)
    def _on_calibrate_subject(self, mount_id: int, slot: int) -> None:
        """SET + subject button: record obs A at current position, move to far end."""
        if not self._config.mount(mount_id).has_slider:
            return
        name = f"Subj {slot + 1}"
        self._mm.send_add_subject_start(mount_id, slot, name)
        self._disarm_set()

        # Open (or reuse) the calibration popup for this mount
        popup = self._calib_popups.get(mount_id)
        if popup and popup.isVisible():
            popup.close()
        popup = _CalibPopup(mount_id, name, self._mm, self)
        popup.finished.connect(lambda _: self._calib_popups.pop(mount_id, None))
        self._calib_popups[mount_id] = popup
        popup.show()

    @pyqtSlot(int, int)
    def _on_calib_prompt(self, mount_id: int, prompt_int: int) -> None:
        popup = self._calib_popups.get(mount_id)
        if popup:
            popup.update_prompt(prompt_int)

    @pyqtSlot(int)
    def _on_subjects_updated(self, mount_id: int) -> None:
        st = self._mm.state(mount_id)
        self._grid.set_subjects(mount_id, st.subjects)

    @pyqtSlot(int, object)
    def _on_config_report(self, mount_id: int, cr) -> None:
        """Mount broadcast its saved config — sync local state and refresh the grid."""
        mc = self._config.mount(mount_id)
        mc.has_slider    = cr.has_slider
        mc.pan_invert    = cr.pan_invert
        mc.slider_invert = cr.slider_invert
        mc.zoom_invert   = cr.zoom_invert
        mc.lanc_zoom     = cr.lanc_zoom
        mc.look_at_mode  = cr.look_at_mode
        save_config(self._config)
        self._grid.set_has_slider(mount_id, cr.has_slider)
        # Invalidate our cached subject ONLY when the grid actually cleared its
        # own — set_look_at_mode() now reports that.  CONFIG_REPORT is not a
        # connect-time event: GET_CONFIG is polled every ~4 s, so resetting
        # unconditionally fought the operator, wiping the selection every few
        # seconds and letting the next STATUS re-assert whatever the MOUNT last
        # had, which is the most recently calibrated subject.  Without this
        # guard the two caches still diverge permanently on a real mode change,
        # because the STATUS sync only pushes to the grid on a CHANGE.
        if self._grid.set_look_at_mode(mount_id, cr.look_at_mode):
            self._active_la_subject[mount_id] = -1
        # Force a label refresh even if look_at_mode didn't change — covers the case
        # where refresh_button() was called during a disconnect/reconnect cycle and
        # overwrote ◄/► with "9"/"10" while _look_at_mode was already correct.
        self._grid.refresh_row_labels(mount_id)
        if cr.has_slider:
            self._mm.send_get_subjects(mount_id)

    # ------------------------------------------------------------------
    # Bridge connection
    # ------------------------------------------------------------------

    def _connect_bridge(self) -> None:
        if self._bridge.connected:
            self._bridge.disconnect()

        cfg = self._config
        if cfg.bridge_mode == "tcp":
            ok = self._bridge.connect_tcp(cfg.bridge_host, cfg.bridge_tcp_port)
            label = f"{cfg.bridge_host}:{cfg.bridge_tcp_port}"
        else:
            if not cfg.bridge_port:
                return
            ok = self._bridge.connect(cfg.bridge_port)
            label = cfg.bridge_port

        if ok:
            self._conn_label.setText(f"Hub: {label}")
            self._conn_label.setStyleSheet("color:#4CAF50; font-size:10px;")
            # Register auto-reconnect callback (safe to register multiple times —
            # _connect_bridge is called once at startup and again after config changes).
            self._bridge.on_reconnect(self._on_bridge_reconnected)
        else:
            self._conn_label.setText(f"Failed: {label}")
            self._conn_label.setStyleSheet("color:#EF5350; font-size:10px;")

    def _on_bridge_reconnected(self) -> None:
        """Called from the Bridge RX thread after a successful auto-reconnect.
        Updates the status label and asks all previously-seen mounts to resend
        their full state — the hub will forward CMD_GET_STATE to any mount that
        has recently been active, so the PC grid refreshes without a power cycle.
        """
        # UI update must happen on the Qt main thread
        QTimer.singleShot(0, self._on_bridge_reconnected_ui)

    def _on_bridge_reconnected_ui(self) -> None:
        cfg = self._config
        if cfg.bridge_mode == "tcp":
            label = f"{cfg.bridge_host}:{cfg.bridge_tcp_port}"
        else:
            label = cfg.bridge_port or "serial"
        self._conn_label.setText(f"Hub: {label} (reconnected)")
        self._conn_label.setStyleSheet("color:#FFA726; font-size:10px;")
        # Only re-query mounts that are actually present.  Querying absent mounts
        # produces GET_CONFIG commands that never ACK, which the bridge's wedge
        # detector then mistakes for a wedged link — causing an endless
        # reconnect loop when fewer than all 5 mounts are connected.
        present = [mid for mid in range(1, 6) if self._mm.state(mid).connected]
        log.info("Bridge auto-reconnected — requesting state from connected mounts: %s",
                 present or "none")
        for mid in present:
            self._mm.send_get_config(mid)


# ---------------------------------------------------------------------------
# Calibration popup — shown after SET + subject button
# ---------------------------------------------------------------------------

class _CalibPopup(QDialog):
    """
    Simple non-blocking dialog that guides the operator through point-B
    subject calibration.

    State machine (driven by CALIB_PROMPT packets):
      MOVING_TO_B  → "Slider moving to far end…"  (Set button disabled)
      WAIT_SET_B   → "Aim at subject and press Set"  (Set button enabled)
      SOLVED       → success label, auto-closes after 1.5 s
      ERROR        → error label, stays open for dismissal
    """

    def __init__(self, mount_id: int, subject_name: str,
                 mm, parent=None):
        super().__init__(parent)
        from comms.protocol import CalibPrompt
        self._mm           = mm
        self._mount_id     = mount_id
        self._subject_name = subject_name
        self._CalibPrompt  = CalibPrompt
        self._can_set      = False

        self.setWindowTitle(f"Calibrate — {subject_name}")
        self.setMinimumWidth(360)
        self.setWindowFlags(
            self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        vl = QVBoxLayout(self)
        vl.setSpacing(12)
        vl.setContentsMargins(16, 16, 16, 16)

        self._info = QLabel("Slider moving to far end…")
        self._info.setWordWrap(True)
        self._info.setStyleSheet("font-size: 14px;")
        vl.addWidget(self._info)

        self._detail = QLabel(
            f"Camera {mount_id}  ·  {subject_name}\n"
            "Observation A recorded at current position.")
        self._detail.setWordWrap(True)
        self._detail.setStyleSheet("color: #90A4AE; font-size: 11px;")
        vl.addWidget(self._detail)

        btns = QDialogButtonBox()
        self._set_btn    = btns.addButton("Set",    QDialogButtonBox.ButtonRole.AcceptRole)
        self._cancel_btn = btns.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        self._set_btn.setEnabled(False)
        self._set_btn.clicked.connect(self._on_set)
        self._cancel_btn.clicked.connect(self._on_cancel)
        vl.addWidget(btns)

    def update_prompt(self, prompt_int: int) -> None:
        CP = self._CalibPrompt
        try:
            prompt = CP(prompt_int)
        except ValueError:
            return

        if prompt == CP.MOVING_TO_B:
            self._info.setText("Slider moving to far end…")
            self._info.setStyleSheet("font-size: 14px;")
            self._set_btn.setEnabled(False)

        elif prompt == CP.WAIT_SET_B:
            self._info.setText(
                f"Slider arrived.\n\nAim at  {self._subject_name}  then press Set.")
            self._info.setStyleSheet("font-size: 14px; font-weight: bold;")
            self._set_btn.setEnabled(True)
            self._can_set = True

        elif prompt == CP.SOLVED:
            self._info.setText("✓  Subject saved.")
            self._info.setStyleSheet("font-size: 14px; color: #4CAF50; font-weight: bold;")
            self._set_btn.setEnabled(False)
            self._cancel_btn.setText("Close")
            # Refresh subject list on the manager
            self._mm.send_get_subjects(self._mount_id)
            QTimer.singleShot(1500, self.accept)

        elif prompt == CP.ERROR:
            self._info.setText(
                "✗  Could not solve 3D position.\n"
                "Try with a longer slider travel and aim more precisely.")
            self._info.setStyleSheet("font-size: 14px; color: #EF5350;")
            self._set_btn.setEnabled(False)
            self._cancel_btn.setText("Close")

    def _on_set(self) -> None:
        if self._can_set:
            self._mm.send_add_subject_set_b(self._mount_id)
            self._can_set = False
            self._set_btn.setEnabled(False)
            self._info.setText("Solving…")
            self._info.setStyleSheet("font-size: 14px;")

    def _on_cancel(self) -> None:
        self._mm.send_add_subject_abort(self._mount_id)
        self.reject()
