"""CameraControlDialog — Blackmagic camera control over each mount's BLE link.

One row per camera, because on this rig the camera is part of the mount: there
is no sensible "current camera" to act on, and a dialog that made you pick one
first would add a step to every use.

What a row can tell you, and why all three states matter:

    "no camera support"   that mount is running firmware from before camera
                          support — nothing here works until it is reflashed
    "camera off"          firmware is current, but no link — the camera is
                          off, asleep, or out of range
    "no camera paired"    no bond on this mount, so it never reaches for a
                          camera at all — the normal state for a mount without
                          one, not a fault.  Pair it on the mount (hold the
                          screen twice) if it should have one.
    "camera ready"        paired; the button will do something

All of these look identical from a greyed-out button and need completely
different actions — reflash the mount, go and check the camera, pair one, or
nothing at all — so they are spelled out rather than implied.

Commands are fire-and-forget.  The Blackmagic control protocol has no
acknowledgement of any kind — the mount ACKs receiving the relay packet, but
the camera never says whether it acted.  So the button flashes to confirm the
command was SENT and claims nothing more; the honest feedback is the lens
visibly hunting.

Link state comes from CMD_HEALTH, which every mount emits every 10 s, polled
here rather than pushed.  A signal would be plumbing for an update rate nobody
can perceive.
"""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QWidget,
)
from PyQt6.QtCore import QTimer, Qt

from comms.mount_manager import MountManager
from comms.protocol import ISO_STEPS

_POLL_MS = 2000          # health arrives every 10 s; this is just the redraw
_FLASH_MS = 400          # how long a button shows it fired

# Shared with the advanced panel — see protocol.ISO_STEPS.
_ISO_STEPS = ISO_STEPS

# Kelvin.  Blackmagic's own presets, for the same reason.
_WB_STEPS = [2500, 2800, 3000, 3200, 3400, 3600, 4000, 4500, 4800, 5000,
             5200, 5400, 5600, 6000, 6500, 7000, 7500, 8000, 9000, 10000]


def _step(table, current, direction):
    """Next value along, clamped at both ends.

    A reported value that is not in the table — the camera was set by hand, or
    to something between presets — steps to the nearest one in that direction
    rather than jumping to the start of the list.
    """
    if current is None:
        return None
    if direction > 0:
        nxt = [v for v in table if v > current]
        return nxt[0] if nxt else table[-1]
    prv = [v for v in table if v < current]
    return prv[-1] if prv else table[0]

_STATE_STYLE = {
    "ready":   ("camera ready",       "#2E7D32"),
    "off":     ("camera off",         "#B71C1C"),
    # Grey, not orange: a mount with no camera is not a fault to be chased, it
    # is most of the rig.  Still worth naming, because "no camera paired" and
    # "camera off" want completely different actions — pair one, or go and
    # switch one on.
    "unpaired":("no camera paired",   "#5A6472"),
    "nobuild": ("no camera support",  "#5A6472"),
    "nomount": ("mount offline",      "#5A6472"),
}


def _h_rule() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setStyleSheet("color: #37474F;")
    return f


class _CamRow(QWidget):
    """One camera: label, link state, and its controls."""

    def __init__(self, mount_id: int, label: str, mm: MountManager, bridge, parent=None):
        super().__init__(parent)
        self._mount_id = mount_id
        self._mm       = mm
        self._bridge   = bridge

        hl = QHBoxLayout(self)
        hl.setContentsMargins(0, 6, 0, 6)
        hl.setSpacing(10)

        name = QLabel(f"<b>{label}</b>")
        name.setMinimumWidth(150)
        hl.addWidget(name)

        self._state_lbl = QLabel("—")
        self._state_lbl.setMinimumWidth(150)
        hl.addWidget(self._state_lbl)

        hl.addStretch(1)

        self._af = QPushButton("Auto Focus")
        self._af.setFixedHeight(44)
        self._af.setMinimumWidth(120)
        self._af.clicked.connect(self._on_autofocus)
        hl.addWidget(self._af)

        # Gain and white balance: the number between the buttons is what the
        # CAMERA last reported, never what we last sent.  It stays "—" until
        # the camera says something, and the buttons stay disabled until then,
        # because stepping from a value we invented would fight the camera.
        self._iso_lbl = self._add_stepper(hl, "ISO", self._iso_down, self._iso_up)
        self._wb_lbl  = self._add_stepper(hl, "WB",   self._wb_down,  self._wb_up)

        self.refresh()

    def _add_stepper(self, hl, caption, on_down, on_up) -> QLabel:
        cap = QLabel(caption)
        cap.setStyleSheet("color:#8A97A8;")
        hl.addWidget(cap)
        minus = QPushButton("−")
        val   = QLabel("—")
        plus  = QPushButton("+")
        val.setAlignment(Qt.AlignmentFlag.AlignCenter)
        val.setMinimumWidth(64)
        val.setStyleSheet("color:#CFD8DC; font-weight:bold;")
        for b, cb in ((minus, on_down), (plus, on_up)):
            b.setFixedSize(38, 44)
            b.clicked.connect(cb)
        hl.addWidget(minus); hl.addWidget(val); hl.addWidget(plus)
        val._minus, val._plus = minus, plus          # refresh() enables these
        return val

    # ------------------------------------------------------------------

    def _link_state(self) -> str:
        if not self._mm.state(self._mount_id).connected:
            return "nomount"
        link = self._bridge.cam_ble_link(self._mount_id)
        if link is None:
            return "nobuild"
        if link == "unpaired":
            return "unpaired"
        return "ready" if link else "off"

    def refresh(self) -> None:
        key = self._link_state()
        text, colour = _STATE_STYLE[key]
        self._state_lbl.setText(f"<span style='color:{colour}'>{text}</span>")
        ready = (key == "ready")
        self._af.setEnabled(ready)
        self._set_btn_style()

        st  = self._mm.state(self._mount_id)
        for lbl, value, suffix in ((self._iso_lbl, st.cam_iso, ""),
                                   (self._wb_lbl,  st.cam_wb,  "K")):
            known = value is not None
            lbl.setText(f"{value}{suffix}" if known else "—")
            # Disabled until the camera has told us where it is: a stepper with
            # no starting point would have to guess one.
            lbl._minus.setEnabled(ready and known)
            lbl._plus.setEnabled(ready and known)

    def _set_btn_style(self, fired: bool = False) -> None:
        bg = "#1565C0" if not fired else "#43A047"
        self._af.setStyleSheet(f"""
            QPushButton {{
                background: {bg}; color: white; font-size: 13px;
                border: none; border-radius: 6px; padding: 0 14px;
            }}
            QPushButton:disabled {{ background: #2A3038; color: #6B7683; }}
            QPushButton:pressed  {{ background: #0D47A1; }}
        """)

    # Steps send a command and stop.  The number on screen does NOT move until
    # the camera reports the change — if it never does, the display is right and
    # the command was not applied, which is the useful thing to see.
    def _iso_up(self):   self._step_iso(+1)
    def _iso_down(self): self._step_iso(-1)
    def _wb_up(self):    self._step_wb(+1)
    def _wb_down(self):  self._step_wb(-1)

    def _step_iso(self, d: int) -> None:
        nxt = _step(_ISO_STEPS, self._mm.state(self._mount_id).cam_iso, d)
        if nxt is not None:
            self._mm.send_cam_iso(self._mount_id, nxt)

    def _step_wb(self, d: int) -> None:
        nxt = _step(_WB_STEPS, self._mm.state(self._mount_id).cam_wb, d)
        if nxt is not None:
            self._mm.send_cam_white_balance(self._mount_id, nxt)

    def _on_autofocus(self) -> None:
        self._mm.send_cam_autofocus(self._mount_id)
        # Confirms the command LEFT, nothing more — see the module docstring.
        self._set_btn_style(fired=True)
        QTimer.singleShot(_FLASH_MS, lambda: self._set_btn_style(False))


class CameraControlDialog(QDialog):

    def __init__(self, mount_ids, labels: dict, mount_manager: MountManager,
                 bridge, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Camera Control")
        self.setMinimumWidth(560)

        vl = QVBoxLayout(self)
        vl.setSpacing(4)

        head = QLabel("Blackmagic camera control over each mount's Bluetooth link.")
        head.setStyleSheet("color: #8A97A8;")
        vl.addWidget(head)
        vl.addWidget(_h_rule())

        self._rows = []
        for mid in mount_ids:
            row = _CamRow(mid, labels.get(mid, f"Camera {mid}"),
                          mount_manager, bridge, self)
            self._rows.append(row)
            vl.addWidget(row)
            vl.addWidget(_h_rule())

        note = QLabel(
            "Gain and WB show what the CAMERA reports, not what was last sent — a "
            "value only changes when the camera confirms it, including changes made "
            "on the camera itself.  Auto Focus is instantaneous and the camera has "
            "no way to report back, so that button confirms the command was sent, "
            "not that the lens moved."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #5A6472; font-size: 11px;")
        vl.addWidget(note)

        btns = QHBoxLayout()
        btns.addStretch(1)
        close = QPushButton("Close")
        close.setFixedHeight(40)
        close.setMinimumWidth(110)
        close.clicked.connect(self.accept)

        # The everyday controls stay exactly as they are.  Advanced opens the
        # full Blackmagic surface — the setup panel, not the operating one.
        adv = QPushButton("Advanced...")
        adv.setFixedHeight(40)
        adv.setMinimumWidth(130)
        adv.clicked.connect(self._open_advanced)
        btns.addWidget(adv)
        btns.addWidget(close)
        vl.addLayout(btns)

        # Camera reports redraw immediately; the timer only covers link state,
        # which comes from 10-second health.  Without this a value would take up
        # to two seconds to appear after a change made on the camera body, which
        # looks like the button not working.
        self._mm = mount_manager     # the Advanced panel needs it too
        mount_manager.cam_status_received.connect(self._on_cam_status)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_all)
        self._timer.start(_POLL_MS)

    def _open_advanced(self) -> None:
        # Imported here rather than at module scope: the advanced panel pulls in
        # the colour-wheel widget and a good deal else, and this dialog opens on
        # a rig where nobody may ever press the button.
        from ui.dialogs.camera_advanced_dialog import CameraAdvancedDialog
        start = self._rows[0]._mount_id if self._rows else 1
        dlg = CameraAdvancedDialog(self._mm, self, mount_id=start)
        dlg.exec()

    def _on_cam_status(self, mount_id: int) -> None:
        for r in self._rows:
            if r._mount_id == mount_id:
                r.refresh()

    def _refresh_all(self) -> None:
        for r in self._rows:
            r.refresh()
