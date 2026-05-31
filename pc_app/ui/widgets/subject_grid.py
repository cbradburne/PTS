"""
SubjectGrid — look-at tracking control panel.

Layout:
  ┌─────────────────────────────────────────────────────────────────────────┐
  │  [Cam1][Cam2][Cam3][Cam4][Cam5]                      [⚙ Add] [Delete]  │
  ├─────────────────────────────────────────────────────────────────────────┤
  │  [Subj 0][Subj 1][Subj 2][Subj 3][Subj 4][Subj 5][Subj 6][Subj 7]     │
  │           (8 subject buttons, coloured when occupied)                   │
  ├─────────────────────────────────────────────────────────────────────────┤
  │  Slider Move:  start [___] mm   end [___] mm   preset [1|2|3|4]         │
  │  Max PT speed: [___] deg/s                                              │
  ├─────────────────────────────────────────────────────────────────────────┤
  │  [Run Look-At]  [Stop]    ● Pan: 12.4°  Tilt: -3.1°  Slider: 450 mm    │
  └─────────────────────────────────────────────────────────────────────────┘

Subject buttons: grey = empty, accent colour = occupied, bright border = active.
"""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QDoubleSpinBox, QSpinBox, QButtonGroup,
    QFrame, QSizePolicy
)
from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot, QTimer
from PyQt6.QtGui import QFont, QColor

from comms.mount_manager import MountManager
from comms.protocol import MAX_SUBJECTS

# ---------------------------------------------------------------------------
# Colour palette — matches main_window cam colours
# ---------------------------------------------------------------------------
from .position_grid import CAM_COLORS

# Subject button styles — background and font never change, only border colour.
# Grey = empty, green = stored + camera currently looking at, red = stored + not looking at.
_SUBJECT_BTN_BASE = """
    QPushButton {{
        background: #1C2B33; color: #CFD8DC;
        border: 3px solid {border}; border-radius: 8px;
        font-size: 13px; font-weight: normal; padding: 4px 6px;
    }}
    QPushButton:pressed {{ background: #0D1A22; }}
"""

_SUBJECT_BTN_EMPTY  = _SUBJECT_BTN_BASE.format(border="#263238")  # grey — no subject stored
_SUBJECT_BTN_AT     = _SUBJECT_BTN_BASE.format(border="#4CAF50")  # green — stored + looking at
_SUBJECT_BTN_STORED = _SUBJECT_BTN_BASE.format(border="#F44336")  # red  — stored + not looking at

def _run_btn_style(active: bool) -> str:
    if active:
        return """QPushButton {
            background: #1A3A1A; color: #A5D6A7;
            border: 4px solid #4CAF50; border-radius: 10px;
            font-size: 15px; font-weight: bold; padding: 6px 16px;
        }"""
    return """QPushButton {
        background: #1E2A35; color: #90A4AE;
        border: 3px solid #37474F; border-radius: 10px;
        font-size: 15px; padding: 6px 16px;
    }"""

def _stop_btn_style() -> str:
    return """QPushButton {
        background: #3E0000; color: #EF9A9A;
        border: 3px solid #B71C1C; border-radius: 10px;
        font-size: 15px; padding: 6px 16px;
    }
    QPushButton:pressed { background: #1A0000; }"""

def _h_rule() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setStyleSheet("color: #263238;")
    return f


class SubjectGrid(QWidget):
    """
    Look-at tracking panel.  Embedded in MainWindow below PositionGrid
    (on a Subjects tab, or toggled by a panel-switcher button).

    Signals:
      look_at_started(mount_id)
      look_at_stopped(mount_id)
    """

    look_at_started = pyqtSignal(int)
    look_at_stopped = pyqtSignal(int)

    def __init__(self, mm: MountManager, parent=None):
        super().__init__(parent)
        self._mm              = mm
        self._active_mount    = 1
        self._active_subject  = None   # int 0-7 or None
        self._look_at_running = False

        self._build()

        mm.subject_list_received.connect(self._on_subjects_updated)
        mm.look_at_status_updated.connect(self._on_look_at_status)
        mm.mount_status_updated.connect(self._on_status_updated)
        mm.mount_connected.connect(self._on_mount_connected)
        mm.mount_disconnected.connect(self._on_mount_disconnected)

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _build(self) -> None:
        vl = QVBoxLayout(self)
        vl.setSpacing(6)
        vl.setContentsMargins(8, 6, 8, 6)

        # ── Row 1: camera selector + management buttons ─────────────────
        row1 = QHBoxLayout()
        self._cam_btns: list[QPushButton] = []
        self._cam_grp = QButtonGroup(self)
        for i in range(1, 6):
            btn = QPushButton(f"Cam {i}")
            btn.setCheckable(True)
            btn.setFixedHeight(34)
            btn.setMinimumWidth(60)
            btn.setFont(QFont("Arial", 11, QFont.Weight.Bold))
            self._cam_grp.addButton(btn, i)
            self._cam_btns.append(btn)
            row1.addWidget(btn)
        self._cam_grp.idClicked.connect(self._select_mount)
        self._cam_btns[0].setChecked(True)
        self._refresh_cam_btn_styles()

        row1.addStretch()

        self._add_subj_btn = QPushButton("+ Add Subject")
        self._del_subj_btn = QPushButton("Delete")
        self._add_subj_btn.setFixedHeight(32)
        self._del_subj_btn.setFixedHeight(32)
        self._add_subj_btn.clicked.connect(self._on_add_subject)
        self._del_subj_btn.clicked.connect(self._on_delete_subject)
        row1.addWidget(self._add_subj_btn)
        row1.addWidget(self._del_subj_btn)
        vl.addLayout(row1)

        # ── Row 2: 8 subject buttons ─────────────────────────────────────
        self._subj_btns: list[QPushButton] = []
        subj_row = QHBoxLayout()
        subj_row.setSpacing(4)
        for i in range(MAX_SUBJECTS):
            btn = QPushButton(f"{i+1}")
            btn.setFixedHeight(44)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.setStyleSheet(_SUBJECT_BTN_EMPTY)
            btn.clicked.connect(self._make_subject_selector(i))
            self._subj_btns.append(btn)
            subj_row.addWidget(btn)
        vl.addLayout(subj_row)
        vl.addWidget(_h_rule())

        # ── Row 3: slider move definition ────────────────────────────────
        slider_move_row = QHBoxLayout()
        slider_move_row.addWidget(QLabel("Slider move:"))
        slider_move_row.addWidget(QLabel("from"))
        self._start_mm_spin = QDoubleSpinBox()
        self._start_mm_spin.setRange(0, 5000)
        self._start_mm_spin.setSuffix(" mm")
        self._start_mm_spin.setDecimals(1)
        self._start_mm_spin.setValue(0.0)
        self._start_mm_spin.setFixedWidth(90)
        slider_move_row.addWidget(self._start_mm_spin)
        slider_move_row.addWidget(QLabel("to"))
        self._end_mm_spin = QDoubleSpinBox()
        self._end_mm_spin.setRange(0, 5000)
        self._end_mm_spin.setSuffix(" mm")
        self._end_mm_spin.setDecimals(1)
        self._end_mm_spin.setValue(500.0)
        self._end_mm_spin.setFixedWidth(90)
        slider_move_row.addWidget(self._end_mm_spin)
        slider_move_row.addWidget(QLabel("  preset:"))
        self._sl_preset_spin = QSpinBox()
        self._sl_preset_spin.setRange(1, 4)
        self._sl_preset_spin.setValue(2)
        self._sl_preset_spin.setFixedWidth(50)
        slider_move_row.addWidget(self._sl_preset_spin)
        slider_move_row.addStretch()

        slider_move_row.addWidget(QLabel("  Max PT:"))
        self._max_pt_spin = QDoubleSpinBox()
        self._max_pt_spin.setRange(1, 360)
        self._max_pt_spin.setSuffix(" °/s")
        self._max_pt_spin.setDecimals(1)
        self._max_pt_spin.setValue(30.0)
        self._max_pt_spin.setFixedWidth(90)
        slider_move_row.addWidget(self._max_pt_spin)
        vl.addLayout(slider_move_row)
        vl.addWidget(_h_rule())

        # ── Row 4: run / stop + live telemetry ───────────────────────────
        ctrl_row = QHBoxLayout()
        self._run_btn  = QPushButton("▶  Run Look-At")
        self._stop_btn = QPushButton("■  Stop")
        self._run_btn.setFixedHeight(40)
        self._stop_btn.setFixedHeight(40)
        self._run_btn.setStyleSheet(_run_btn_style(False))
        self._stop_btn.setStyleSheet(_stop_btn_style())
        self._stop_btn.setEnabled(False)
        self._run_btn.clicked.connect(self._on_run)
        self._stop_btn.clicked.connect(self._on_stop)
        ctrl_row.addWidget(self._run_btn)
        ctrl_row.addWidget(self._stop_btn)
        ctrl_row.addSpacing(16)

        self._telemetry_lbl = QLabel("●  Pan: —    Tilt: —    Slider: —")
        self._telemetry_lbl.setStyleSheet("color: #80CBC4; font-size: 12px;")
        ctrl_row.addWidget(self._telemetry_lbl, 1)
        vl.addLayout(ctrl_row)

    # ------------------------------------------------------------------
    # Camera selector
    # ------------------------------------------------------------------

    def _select_mount(self, mount_id: int) -> None:
        self._active_mount = mount_id
        self._active_subject = None
        self._look_at_running = False
        self._refresh_cam_btn_styles()
        self._refresh_subject_buttons()
        self._refresh_run_stop()

    def _refresh_cam_btn_styles(self) -> None:
        for i, btn in enumerate(self._cam_btns):
            mid   = i + 1
            color = CAM_COLORS[mid]["accent"].name()
            if mid == self._active_mount:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: #1A2B3A; color: {color};
                        border: 3px solid {color}; border-radius: 8px;
                        font-size: 11px; font-weight: bold; padding: 4px 8px;
                    }}""")
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: #1A2020; color: #607D8B;
                        border: 2px solid #37474F; border-radius: 8px;
                        font-size: 11px; padding: 4px 8px;
                    }}""")

    def set_active_mount(self, mount_id: int) -> None:
        """Called from MainWindow when the operator switches cameras."""
        if 1 <= mount_id <= 5:
            self._cam_grp.button(mount_id).setChecked(True)
            self._select_mount(mount_id)

    # ------------------------------------------------------------------
    # Subject management
    # ------------------------------------------------------------------

    def _make_subject_selector(self, idx: int):
        def handler():
            st = self._mm.state(self._active_mount)
            subj = st.subjects[idx] if idx < len(st.subjects) else None
            if subj is None or not subj.valid:
                return  # empty slot — ignore
            # Always send CMD_SWITCH_SUBJECT regardless of look_at_running state.
            # This makes the Teensy broadcast CMD_LOOK_AT_STATUS to all clients
            # (hub display, web app), so the hub display stays in sync even when
            # look-at isn't actively running.
            self._mm.send_switch_subject(self._active_mount, idx)
            self._active_subject = idx
            self._refresh_subject_buttons()
        return handler

    def _on_add_subject(self) -> None:
        from ui.dialogs.subject_calibration_dialog import SubjectCalibrationDialog
        dlg = SubjectCalibrationDialog(self._active_mount, self._mm, self)
        dlg.finished.connect(lambda _: self._mm.send_get_subjects(self._active_mount))
        dlg.exec()

    def _on_delete_subject(self) -> None:
        if self._active_subject is None:
            return
        self._mm.send_delete_subject(self._active_mount, self._active_subject)
        self._active_subject = None
        self._refresh_subject_buttons()
        # Refresh list after a short delay (EEPROM write + reply)
        QTimer.singleShot(300, lambda: self._mm.send_get_subjects(self._active_mount))

    def _refresh_subject_buttons(self) -> None:
        st = self._mm.state(self._active_mount)
        for i, btn in enumerate(self._subj_btns):
            occupied  = bool(st.slot_occupied_mask & (1 << i))
            is_active = (i == self._active_subject)
            subj = st.subjects[i] if i < len(st.subjects) else None
            name = subj.name if (subj is not None and subj.valid and subj.name) else None

            if is_active:
                btn.setText(name if name else str(i + 1))
                btn.setStyleSheet(_SUBJECT_BTN_AT)
            elif occupied:
                btn.setText(name if name else str(i + 1))
                btn.setStyleSheet(_SUBJECT_BTN_STORED)
            else:
                btn.setText(str(i + 1))
                btn.setStyleSheet(_SUBJECT_BTN_EMPTY)
            btn.setEnabled(True)   # always enabled; empty-slot guard is inside the click handler

    # ------------------------------------------------------------------
    # Run / Stop
    # ------------------------------------------------------------------

    def _on_run(self) -> None:
        if self._active_subject is None:
            return  # nothing selected
        start  = self._start_mm_spin.value()
        end    = self._end_mm_spin.value()
        preset = self._sl_preset_spin.value()
        max_pt = self._max_pt_spin.value()
        # Store slider move on mount
        self._mm.send_set_slider_move(self._active_mount, start, end, preset)
        # Start look-at move
        self._mm.send_start_look_at_move(self._active_mount, self._active_subject,
                                          preset, max_pt)
        self._look_at_running = True
        self._refresh_run_stop()
        self.look_at_started.emit(self._active_mount)

    def _on_stop(self) -> None:
        # Send zero jog — firmware stopAll handler decelerates all axes cleanly.
        self._mm.send_jog(self._active_mount, 0, 0, 0, 0)
        self._look_at_running = False
        self._refresh_run_stop()
        self.look_at_stopped.emit(self._active_mount)

    def _refresh_run_stop(self) -> None:
        self._run_btn.setStyleSheet(_run_btn_style(self._look_at_running))
        self._run_btn.setEnabled(
            not self._look_at_running and self._active_subject is not None)
        self._stop_btn.setEnabled(self._look_at_running)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    @pyqtSlot(int)
    def _on_subjects_updated(self, mount_id: int) -> None:
        if mount_id == self._active_mount:
            self._refresh_subject_buttons()

    @pyqtSlot(int)
    def _on_status_updated(self, mount_id: int) -> None:
        """Refresh subject button colours when slot_at_mask changes (camera moved)."""
        if mount_id == self._active_mount:
            self._refresh_subject_buttons()

    @pyqtSlot(int)
    def _on_look_at_status(self, mount_id: int) -> None:
        if mount_id != self._active_mount:
            return
        st = self._mm.state(mount_id)
        la = st.look_at_status
        if la is None:
            return

        # Update live telemetry
        self._telemetry_lbl.setText(
            f"●  Pan: {la.pan_deg:+.2f}°    "
            f"Tilt: {la.tilt_deg:+.2f}°    "
            f"Slider: {la.slider_pos_mm:.1f} mm"
        )

        # Detect look-at end (firmware stops and drops back to IDLE)
        if self._look_at_running and not la.look_at_active:
            self._look_at_running = False
            self._refresh_run_stop()
            self.look_at_stopped.emit(mount_id)

        # Sync active subject from firmware (0xFF = none / deselected)
        if la.subject_id == 0xFF:
            if self._active_subject is not None:
                self._active_subject = None
                self._refresh_subject_buttons()
        elif la.subject_id != self._active_subject:
            self._active_subject = la.subject_id
            self._refresh_subject_buttons()

    @pyqtSlot(int)
    def _on_mount_connected(self, mount_id: int) -> None:
        if mount_id == self._active_mount:
            self._mm.send_get_subjects(mount_id)

    @pyqtSlot(int)
    def _on_mount_disconnected(self, mount_id: int) -> None:
        if mount_id == self._active_mount and self._look_at_running:
            self._look_at_running = False
            self._refresh_run_stop()
