"""
SubjectCalibrationDialog — step-by-step 2-point subject calibration.

The operator:
  1. Types a name for the new subject.
  2. Clicks "Start" — the slider moves to its home position.
  3. Aims the camera at the subject, clicks "Set A" — records observation A;
     the slider then moves to its far end.
  4. Aims at the subject again, clicks "Set B" — records observation B and
     solves the 3D position.  The ref is auto-set; the subject is stored.
  5. Success or error feedback, then the dialog can be closed.

The dialog is driven by CALIB_PROMPT packets from the mount (via MountManager
signals) so the UI stays in sync even if the slider is still moving.
"""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QProgressBar, QFrame, QWidget
)
from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtGui import QFont

from comms.mount_manager import MountManager
from comms.protocol import CalibPrompt


# ---------------------------------------------------------------------------
# State labels matched to CalibPrompt values
# ---------------------------------------------------------------------------
_PROMPT_TEXT = {
    CalibPrompt.MOVING_TO_A: (
        "⟳  Moving slider to home position…",
        "Please wait while the slider travels to its home end."
    ),
    CalibPrompt.WAIT_SET_A: (
        "📍  Aim at subject — then click Set A",
        "Slider is at home.  Pan and tilt the camera so it points directly at the\n"
        "subject, then click Set A."
    ),
    CalibPrompt.MOVING_TO_B: (
        "⟳  Moving slider to far end…",
        "Please wait while the slider travels to its far end."
    ),
    CalibPrompt.WAIT_SET_B: (
        "📍  Aim at subject again — then click Set B",
        "Slider is at far end.  Aim at the same subject and click Set B."
    ),
    CalibPrompt.SOLVED: (
        "✓  Subject calibrated successfully!",
        "The 3D subject position has been solved and stored to EEPROM.\n"
        "The angular reference (Set Ref) has been applied automatically."
    ),
    CalibPrompt.ERROR: (
        "✗  Calibration failed",
        "Could not solve the 3D position — the two observations may be too\n"
        "similar (very short slider travel or camera aimed the same way).\n"
        "Please try again with a longer slider range."
    ),
}

_STEP_MAP = {
    CalibPrompt.MOVING_TO_A: 1,
    CalibPrompt.WAIT_SET_A:  2,
    CalibPrompt.MOVING_TO_B: 3,
    CalibPrompt.WAIT_SET_B:  4,
    CalibPrompt.SOLVED:      5,
    CalibPrompt.ERROR:       5,
}

_STEPS_TOTAL = 5


def _h_rule() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setStyleSheet("color: #37474F;")
    return f


class SubjectCalibrationDialog(QDialog):

    def __init__(self, mount_id: int, mm: MountManager, parent=None):
        super().__init__(parent)
        self._mount_id = mount_id
        self._mm       = mm
        self._phase    = None   # CalibPrompt or None

        self.setWindowTitle(f"Calibrate Subject — Camera {mount_id}")
        self.setMinimumWidth(480)
        self.setModal(True)
        self._build()
        mm.calib_prompt_received.connect(self._on_calib_prompt)

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _build(self) -> None:
        vl = QVBoxLayout(self)
        vl.setSpacing(12)
        vl.setContentsMargins(16, 16, 16, 16)

        # Title
        title = QLabel(f"New Subject — Camera {self._mount_id}")
        title.setFont(QFont("Arial", 14, QFont.Weight.Bold))
        vl.addWidget(title)
        vl.addWidget(_h_rule())

        # Name entry
        name_lbl  = QLabel("Subject name:")
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("e.g. Presenter A")
        self._name_edit.setMaxLength(15)
        name_row = QHBoxLayout()
        name_row.addWidget(name_lbl)
        name_row.addWidget(self._name_edit, 1)
        vl.addLayout(name_row)

        # Progress bar (steps 0-5)
        self._progress = QProgressBar()
        self._progress.setRange(0, _STEPS_TOTAL)
        self._progress.setValue(0)
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(8)
        vl.addWidget(self._progress)

        # State heading
        self._heading = QLabel("Enter a name and click Start.")
        self._heading.setFont(QFont("Arial", 11, QFont.Weight.Bold))
        self._heading.setWordWrap(True)
        vl.addWidget(self._heading)

        # State detail
        self._detail = QLabel("")
        self._detail.setWordWrap(True)
        self._detail.setStyleSheet("color: #B0BEC5;")
        vl.addWidget(self._detail)

        vl.addWidget(_h_rule())

        # Button row
        btn_row = QHBoxLayout()

        self._start_btn  = QPushButton("Start")
        self._set_a_btn  = QPushButton("Set A")
        self._set_b_btn  = QPushButton("Set B")
        self._abort_btn  = QPushButton("Abort")
        self._close_btn  = QPushButton("Close")

        for btn in (self._start_btn, self._set_a_btn, self._set_b_btn,
                    self._abort_btn, self._close_btn):
            btn.setFixedHeight(38)
            btn_row.addWidget(btn)

        self._set_a_btn.setEnabled(False)
        self._set_b_btn.setEnabled(False)
        self._abort_btn.setEnabled(False)
        self._close_btn.setEnabled(True)

        self._start_btn.clicked.connect(self._on_start)
        self._set_a_btn.clicked.connect(self._on_set_a)
        self._set_b_btn.clicked.connect(self._on_set_b)
        self._abort_btn.clicked.connect(self._on_abort)
        self._close_btn.clicked.connect(self.accept)

        vl.addLayout(btn_row)

        self._apply_button_states()

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _on_start(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            self._heading.setText("Please enter a subject name first.")
            return
        self._heading.setText("Sending calibration start…")
        self._detail.setText("")
        self._mm.send_add_subject_start(self._mount_id, name)
        # Disable name edit and start, enable abort
        self._name_edit.setEnabled(False)
        self._start_btn.setEnabled(False)
        self._abort_btn.setEnabled(True)

    def _on_set_a(self) -> None:
        self._set_a_btn.setEnabled(False)
        self._mm.send_add_subject_set_a(self._mount_id)

    def _on_set_b(self) -> None:
        self._set_b_btn.setEnabled(False)
        self._mm.send_add_subject_set_b(self._mount_id)

    def _on_abort(self) -> None:
        self._mm.send_add_subject_abort(self._mount_id)
        self._reset_to_idle()

    def _reset_to_idle(self) -> None:
        self._phase = None
        self._name_edit.setEnabled(True)
        self._name_edit.clear()
        self._heading.setText("Calibration aborted.  Enter a name and click Start.")
        self._detail.setText("")
        self._progress.setValue(0)
        self._apply_button_states()

    # ------------------------------------------------------------------
    # Signal handler — CALIB_PROMPT from mount
    # ------------------------------------------------------------------

    @pyqtSlot(int, int)
    def _on_calib_prompt(self, mount_id: int, prompt_int: int) -> None:
        if mount_id != self._mount_id:
            return
        try:
            prompt = CalibPrompt(prompt_int)
        except ValueError:
            return

        self._phase = prompt
        heading, detail = _PROMPT_TEXT.get(prompt, ("…", ""))
        self._heading.setText(heading)
        self._detail.setText(detail)
        self._progress.setValue(_STEP_MAP.get(prompt, 0))

        if prompt == CalibPrompt.SOLVED:
            # Calibration done — refresh subject list on the manager
            self._mm.send_get_subjects(self._mount_id)

        self._apply_button_states()

    # ------------------------------------------------------------------
    # Button visibility / enable logic
    # ------------------------------------------------------------------

    def _apply_button_states(self) -> None:
        p = self._phase
        waiting_a = (p == CalibPrompt.WAIT_SET_A)
        waiting_b = (p == CalibPrompt.WAIT_SET_B)
        done      = p in (CalibPrompt.SOLVED, CalibPrompt.ERROR)
        running   = p is not None and not done

        self._start_btn.setEnabled(not running)
        self._set_a_btn.setEnabled(waiting_a)
        self._set_b_btn.setEnabled(waiting_b)
        self._abort_btn.setEnabled(running)
        self._close_btn.setEnabled(not running or done)
        self._name_edit.setEnabled(not running)

        # Colour feedback
        if p == CalibPrompt.SOLVED:
            self._heading.setStyleSheet("color: #4CAF50; font-weight: bold;")
        elif p == CalibPrompt.ERROR:
            self._heading.setStyleSheet("color: #EF5350; font-weight: bold;")
        else:
            self._heading.setStyleSheet("")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        # If calibration is in progress, abort it
        p = self._phase
        if p is not None and p not in (CalibPrompt.SOLVED, CalibPrompt.ERROR):
            self._mm.send_add_subject_abort(self._mount_id)
        super().closeEvent(event)
