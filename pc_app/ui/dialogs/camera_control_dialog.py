"""CameraControlDialog — Blackmagic camera control over each mount's BLE link.

One row per camera, because on this rig the camera is part of the mount: there
is no sensible "current camera" to act on, and a dialog that made you pick one
first would add a step to every use.

What a row can tell you, and why all three states matter:

    "no camera support"   that mount's firmware has no BLE camera build, so
                          nothing here will ever work until it is reflashed
    "camera off"          BLE build, but no link — the camera is off, asleep,
                          or out of range
    "camera ready"        paired; the button will do something

Those first two look identical from a greyed-out button, and they need
completely different actions, so they are spelled out rather than implied.

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

_POLL_MS = 2000          # health arrives every 10 s; this is just the redraw
_FLASH_MS = 400          # how long a button shows it fired

_STATE_STYLE = {
    "ready":   ("camera ready",       "#2E7D32"),
    "off":     ("camera off",         "#B71C1C"),
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
        self._af.setMinimumWidth(130)
        self._af.clicked.connect(self._on_autofocus)
        hl.addWidget(self._af)

        self.refresh()

    # ------------------------------------------------------------------

    def _link_state(self) -> str:
        if not self._mm.state(self._mount_id).connected:
            return "nomount"
        link = self._bridge.cam_ble_link(self._mount_id)
        if link is None:
            return "nobuild"
        return "ready" if link else "off"

    def refresh(self) -> None:
        key = self._link_state()
        text, colour = _STATE_STYLE[key]
        self._state_lbl.setText(f"<span style='color:{colour}'>{text}</span>")
        self._af.setEnabled(key == "ready")
        self._set_btn_style()

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
            "Auto Focus is instantaneous — the camera has no way to report back, "
            "so the button confirms the command was sent, not that the lens moved."
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
        btns.addWidget(close)
        vl.addLayout(btns)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_all)
        self._timer.start(_POLL_MS)

    def _refresh_all(self) -> None:
        for r in self._rows:
            r.refresh()
