"""FindLimitsDialog — drive one axis to its end stops to measure its travel.

One dialog per axis: the caller says which, so there is no axis picker and the
heading names the axis outright ("Find Slider Limits").  The choice now lives
on the camera's Config tab as separate Slider and Zoom buttons, matching the
web app's extended config.

Button behaviour is deliberately symmetrical, so whichever button is nearer
the pointer does the sane thing:

    idle      ->  [ Start ]   [ Close ]
    running   ->  [ Cancel ]  [ Cancel ]     both stop the axis; the right one
                                             also closes the dialog
    finished  ->  [ Start ]   [ Close ]      Start re-runs, so a bad result can
                                             be retried without reopening

The measured travel stays on screen after a run — it is the useful output, and
re-running replaces it rather than blanking it first.
"""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
)
from PyQt6.QtCore import QTimer, pyqtSlot
from PyQt6.QtGui import QFont

from comms.mount_manager import MountManager
from comms.protocol import Axis, LIMIT_LEG_NOT_RUN, LIMIT_LEG_STALL, LIMIT_LEG_CAP
from ui.widgets.slider_travel_anim import SliderTravelAnim

# The firmware has its own timeout; this only stops the UI sitting on
# "searching" forever if nothing comes back.  10.5 min covers both ends of a
# 3-metre slider at the slowest find speed.
_TIMEOUT_MS = 630_000

_AXIS_NAME = {Axis.SLIDER: "Slider", Axis.ZOOM: "Zoom"}


def _h_rule() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setStyleSheet("color: #37474F;")
    return f


class FindLimitsDialog(QDialog):

    def __init__(self, mount_id: int, mount_manager: MountManager,
                 axis: Axis, stall_threshold: int = 80, parent=None):
        super().__init__(parent)
        self._mount_id  = mount_id
        self._mm        = mount_manager
        self._axis      = axis
        self._threshold = max(1, stall_threshold)
        self._running   = False

        name = _AXIS_NAME.get(axis, "Axis")
        self.setWindowTitle(f"Find {name} Limits — Camera {mount_id}")
        self.setMinimumWidth(420)
        self._build(name)

        self._mm.limits_found.connect(self._on_limits_found)
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.setInterval(_TIMEOUT_MS)
        self._timeout.timeout.connect(self._on_timeout)

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _build(self, name: str) -> None:
        vl = QVBoxLayout(self)
        vl.setSpacing(12)
        vl.setContentsMargins(16, 16, 16, 16)

        self._heading = QLabel(f"Finding {name} Limits")
        self._heading.setFont(QFont("Arial", 14, QFont.Weight.Bold))
        vl.addWidget(self._heading)
        vl.addWidget(_h_rule())

        warn = QLabel(
            f"The {name.lower()} will drive to each end stop until it stalls.  "
            "Make sure it is clear to move along its whole travel before starting."
        )
        warn.setWordWrap(True)
        warn.setStyleSheet("color:#FFB74D; font-size:11px;")
        vl.addWidget(warn)

        self._anim = SliderTravelAnim()
        vl.addWidget(self._anim)

        self._status = QLabel("Ready.")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("font-size:12px; color:#B0BEC5;")
        vl.addWidget(self._status)

        # Measured travel — kept on screen after a run; this is the output.
        self._result = QLabel("")
        self._result.setWordWrap(True)
        self._result.setFont(QFont("Arial", 11, QFont.Weight.Bold))
        self._result.setStyleSheet("color:#4CAF50;")
        vl.addWidget(self._result)

        # What the mount measured on each leg, so a StallGuard threshold can be
        # tuned from here: it used to need a USB cable to the Teensy inside the
        # mount.  Empty for firmware that does not send the readings.
        self._readings = QLabel("")
        self._readings.setWordWrap(True)
        self._readings.setStyleSheet("font-size:11px; color:#B0BEC5;")
        self._readings.setToolTip(
            "SG is the driver's load reading. It falls as the load rises, and\n"
            "the driver calls a stall when it drops to the trip point.\n\n"
            "Lowest: the lowest reading while moving, leaving out the last\n"
            "0.15 s (the approach to the end).\n"
            "Last: the final reading. On a run that pushed into its end\n"
            "without stopping, that is the reading at the end.")
        vl.addWidget(self._readings)

        vl.addWidget(_h_rule())

        row = QHBoxLayout()
        self._action_btn = QPushButton("Start")
        self._action_btn.setFixedHeight(44)
        self._action_btn.setStyleSheet(
            "background:#1565C0; color:white; border:none; border-radius:6px;"
            "font-size:13px; font-weight:bold;")
        self._action_btn.clicked.connect(self._on_action)

        self._close_btn = QPushButton("Close")
        self._close_btn.setFixedHeight(44)
        # Wide enough for "Stop & Close" from the outset, so the row doesn't
        # jump when the caption changes mid-run.
        self._close_btn.setMinimumWidth(130)
        self._close_btn.clicked.connect(self._on_close)

        row.addWidget(self._action_btn)
        row.addWidget(self._close_btn)
        vl.addLayout(row)

        self._apply_state()

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def _apply_state(self) -> None:
        """The single place that sets both captions, so they cannot disagree
        about whether a run is in progress.

        Both said "Cancel" while running, which put two identical buttons side
        by side with no way to tell which was which.  They do differ — one
        stops and stays so the run can be retried, the other stops and leaves
        — so the captions say so.
        """
        if self._running:
            self._action_btn.setText("Stop")
            self._close_btn.setText("Stop && Close")   # && escapes the mnemonic
        else:
            self._action_btn.setText("Start")
            self._close_btn.setText("Close")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_action(self) -> None:
        if self._running:
            self._stop("Cancelled.")
        else:
            self._start()

    def _on_close(self) -> None:
        # While running this reads "Cancel": stop the axis first, then close.
        # Leaving a mount driving into a stop with nothing watching is the one
        # outcome worth going out of the way to prevent.
        if self._running:
            self._stop("Cancelled.")
        self.accept()

    def _start(self) -> None:
        self._running = True
        self._status.setText(
            f"Searching for {_AXIS_NAME.get(self._axis, 'axis').lower()} limits…")
        self._status.setStyleSheet("font-size:12px; color:#B0BEC5;")
        self._result.setText("")
        self._readings.setText("")
        self._anim.start(forward=True)
        self._apply_state()
        self._mm.send_find_limits(self._mount_id, self._axis, self._threshold)
        self._timeout.start()

    def _stop(self, message: str) -> None:
        self._timeout.stop()
        if self._running:
            self._mm.send_e_stop(self._mount_id)
        self._running = False
        self._anim.park(at_far_end=False)
        self._status.setText(message)
        self._status.setStyleSheet("font-size:12px; color:#EF5350;")
        self._apply_state()

    # ------------------------------------------------------------------
    # Mount responses
    # ------------------------------------------------------------------

    @pyqtSlot(int, int, int, int)
    def _on_limits_found(self, mount_id: int, axis: int,
                         min_steps: int, max_steps: int) -> None:
        if mount_id != self._mount_id or not self._running:
            return
        if Axis(axis) != self._axis:
            return

        self._timeout.stop()
        self._running = False
        if min_steps == 0 and max_steps == 0:
            # No end found.  A leg ran out of travel (the zoom gives up after
            # a fixed number of motor turns) or time without a stall, so the
            # mount stopped and cleared this axis's limits.  0/0 is what every
            # client already reads as "no limits"; a found range never has its
            # home end at 0.  It used to read "Done. Travel: 0 steps".
            self._anim.park(at_far_end=False)
            self._status.setText("Stopped — no end stop found.")
            self._status.setStyleSheet("font-size:12px; color:#EF5350;")
            self._result.setStyleSheet("color:#FFB74D;")
            self._result.setText(
                f"The threshold ({self._threshold}) never tripped, so the mount "
                f"stopped and cleared the {_AXIS_NAME.get(self._axis, 'axis').lower()} "
                "limits. If it ran into its end and kept going, raise the "
                "threshold and try again.")
            self._readings.setText(self._describe_readings())
            self._apply_state()
            return
        self._anim.park(at_far_end=True)
        self._status.setText("Done.")
        self._status.setStyleSheet("font-size:12px; color:#B0BEC5;")
        self._result.setStyleSheet("color:#4CAF50;")
        self._result.setText(self._describe(min_steps, max_steps))
        self._readings.setText(self._describe_readings())
        self._apply_state()

    def _describe_readings(self) -> str:
        """The StallGuard readings the mount sent with this result, one line a
        leg and then what to do with them.

        Read from the mount's state, where MountManager puts them before it
        signals — the same route the end margin takes below.  Empty when the
        firmware sends none.
        """
        try:
            rep = getattr(self._mm.state(self._mount_id), "last_limit_find", None)
        except Exception:
            rep = None
        if rep is None:
            return ""
        lines, hint = [], ""
        for name, leg in zip(("Home end", "Far end"), rep.legs):
            if leg.outcome == LIMIT_LEG_NOT_RUN:
                continue
            dist = self._distance(leg.travelled, rep.usteps_per_turn)
            if leg.outcome == LIMIT_LEG_STALL:
                low = ("too soon for a reading" if leg.sg_lowest is None
                       else f"lowest SG while moving {leg.sg_lowest}")
                lines.append(f"{name}: stalled after {dist} — {low}, "
                             f"trips at {leg.trip}.")
                continue
            why = "its travel limit" if leg.outcome == LIMIT_LEG_CAP else "timed out"
            low  = "-" if leg.sg_lowest is None else leg.sg_lowest
            last = "-" if leg.sg_last is None else leg.sg_last
            lines.append(f"{name}: no stall in {dist} ({why}) — lowest SG {low}, "
                         f"last {last}, trips at {leg.trip}.")
            if leg.sg_last is not None and not hint:
                hint = (f"If it was pushing against its end, SG read about "
                        f"{leg.sg_last} there: raise the threshold until the trip "
                        f"point is above that.")
        if not hint:
            hint = ("If an end stopped short, lower it: the trip point has to "
                    "stay below the lowest reading while moving.")
        lines.append(f"Threshold {rep.threshold}. {hint}")
        return "\n".join(lines)

    def _distance(self, steps: int, per_turn: int) -> str:
        """How far a leg went: millimetres on the rail, motor turns on the zoom
        — the unit someone watching the axis can check."""
        if self._axis == Axis.SLIDER:
            from ui.widgets.nudge_overlay import NudgeOverlay
            return f"{steps / NudgeOverlay.SLIDER_STEPS_PER_MM:.0f} mm"
        return f"{steps / (per_turn or 1):.2f} turns"

    def _describe(self, min_steps: int, max_steps: int) -> str:
        """The result line, in the units the axis is actually measured in.

        Millimetres for the rail: a step count is a number nobody can check
        against the rail in front of them, and this line is the one an operator
        reads after a limit find. Zoom stays in steps — it is a lens ring, not a
        distance.

        SLIDER_STEPS_PER_MM comes from the Move panel rather than a conversion
        of its own, so what this reports and what the nudge buttons send cannot
        drift apart.
        """
        travel = max_steps - min_steps
        if self._axis != Axis.SLIDER:
            return f"Travel: {travel} steps    (min {min_steps}, max {max_steps})"
        from ui.widgets.nudge_overlay import NudgeOverlay
        spmm = NudgeOverlay.SLIDER_STEPS_PER_MM
        line = (f"Travel: {travel / spmm:.0f} mm    "
                f"(min {min_steps / spmm:.0f} mm, max {max_steps / spmm:.0f} mm)")
        # And say which end margin produced them.
        #
        # The margin is a setting that only takes effect when a limit find runs,
        # so changing it and not re-running one leaves the old limits in place —
        # which looks exactly like the setting being ignored. The number was
        # already implicit in "min", but only to someone who knew to read it
        # that way. Reported from the MOUNT's own config report, so it is what
        # the find actually used rather than what the dialog last sent.
        margin = self._reported_margin_mm()
        if margin:
            line += f"    [end margin {margin} mm]"
        return line

    def _reported_margin_mm(self) -> int:
        try:
            cr = self._mm.state(self._mount_id).last_config_report
            return int(getattr(cr, "slider_end_margin_mm", 0) or 0)
        except Exception:
            # A mount on older firmware reports no margin, and a stub in a test
            # has no state at all. Neither is worth a traceback in a slot.
            return 0

    def _on_timeout(self) -> None:
        self._running = False
        self._anim.park(at_far_end=False)
        self._status.setText("Timed out — no response from the mount.")
        self._status.setStyleSheet("font-size:12px; color:#EF5350;")
        self._apply_state()

    # ------------------------------------------------------------------

    def closeEvent(self, event):
        # Covers the window's own close button, which bypasses _on_close().
        if self._running:
            self._stop("Cancelled.")
        self._anim.park()
        super().closeEvent(event)
