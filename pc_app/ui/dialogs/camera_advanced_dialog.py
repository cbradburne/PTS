"""CameraAdvancedDialog — the full Blackmagic control surface, per camera.

Reached from the Advanced button on the ordinary camera-control dialog, which
keeps its four everyday controls. This is the panel for setting a camera up, not
for operating it during a show.

Laid out after ATEM Software Control because that is the reference the operator
already knows: camera settings down the left, the three correction wheels across
the top right, and the paired sliders beneath them.

TWO THINGS THIS PANEL CANNOT DO, both because of the protocol rather than the
implementation:

  It cannot confirm anything. The Blackmagic control protocol has no
  acknowledgement — the mount ACKs relaying the packet, the camera never says
  whether it acted. So every control here shows what was SENT. Where the camera
  volunteers a value (it reports most of them unprompted) the display follows
  it, and that is the only real confirmation available.

  It cannot read the camera's current state on open. There is no "get" in the
  protocol, only "set" and the camera's own unsolicited reports. So the panel
  opens showing neutral values and fills in as the camera talks. Values shown
  before the camera has reported are what this app last sent, not what the
  camera holds — which matters if the camera was changed at the body.

Commands are sent live as a control moves. That is deliberate: with no
acknowledgement and no read-back, an Apply button would give the operator a
confidence the system cannot support.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QSlider, QWidget, QTabBar, QFrame, QDoubleSpinBox, QSpinBox, QComboBox,
)

from comms.mount_manager import MountManager
from comms.protocol import NUM_MOUNTS
from ui.widgets.colour_wheel import LabelledWheel

_SHUTTERS = [24, 25, 30, 48, 50, 60, 100, 120, 125, 200, 250, 500, 1000, 2000]
_GAINS_DB = [-12, -6, 0, 6, 12, 18, 24, 30, 36]


def _row(parent_lay, label: str, widget) -> QLabel:
    h = QHBoxLayout()
    lab = QLabel(label)
    lab.setStyleSheet("color:#9aa0a8; font-size:11px;")
    lab.setMinimumWidth(74)
    h.addWidget(lab)
    h.addWidget(widget, 1)
    parent_lay.addLayout(h)
    return lab


class CameraAdvancedDialog(QDialog):

    def __init__(self, mm: MountManager, parent=None, mount_id: int = 1):
        super().__init__(parent)
        self._mm = mm
        self._mount = mount_id
        # Guard against the feedback loop: showing a camera-reported value moves
        # the control, which would otherwise send that value straight back.
        self._loading = False
        self.setWindowTitle("Camera Control — Advanced")
        self.resize(1180, 720)
        self.setStyleSheet("QDialog{background:#1b1d20;} QLabel{color:#cfd3d8;}")

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 10)

        self._tabs = QTabBar()
        self._tabs.setExpanding(False)
        for i in range(1, NUM_MOUNTS + 1):
            self._tabs.addTab(f"CAM{i}")
        self._tabs.setCurrentIndex(mount_id - 1)
        self._tabs.currentChanged.connect(self._on_tab)
        root.addWidget(self._tabs)

        self._link = QLabel("")
        self._link.setStyleSheet("color:#e0a030; font-size:12px;")
        root.addWidget(self._link)

        body = QHBoxLayout()
        body.setSpacing(14)
        body.addWidget(self._build_camera_panel(), 0)
        body.addWidget(self._build_correction_panel(), 1)
        root.addLayout(body, 1)

        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        foot = QHBoxLayout()
        foot.addStretch(1)
        foot.addWidget(close)
        root.addLayout(foot)

        self._mm.cam_status_received.connect(self._on_cam_status)
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._refresh_link)
        self._poll.start(1000)
        self._refresh_link()

    # ── left: camera settings ────────────────────────────────────────────
    def _build_camera_panel(self) -> QWidget:
        box = QFrame()
        box.setStyleSheet("QFrame{background:#232629; border-radius:6px;}")
        box.setFixedWidth(300)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(8)

        head = QLabel("Camera")
        head.setStyleSheet("color:#e6e8ec; font-size:14px; font-weight:600;")
        lay.addWidget(head)

        self._nd = QDoubleSpinBox(); self._nd.setRange(0, 12); self._nd.setSingleStep(0.5)
        self._nd.setSuffix(" stop")
        self._nd.valueChanged.connect(
            lambda v: self._send(self._mm.send_cam_nd, v))
        _row(lay, "Filter (ND)", self._nd)

        self._gain = QComboBox()
        for d in _GAINS_DB:
            self._gain.addItem(f"{d:+d} dB", d)
        self._gain.setCurrentIndex(_GAINS_DB.index(0))
        self._gain.currentIndexChanged.connect(
            lambda _i: self._send(self._mm.send_cam_gain_db, self._gain.currentData()))
        _row(lay, "Gain", self._gain)

        self._shut = QComboBox()
        for s in _SHUTTERS:
            self._shut.addItem(f"1/{s}", s)
        self._shut.setCurrentIndex(_SHUTTERS.index(50))
        self._shut.currentIndexChanged.connect(
            lambda _i: self._send(self._mm.send_cam_shutter_speed, self._shut.currentData()))
        _row(lay, "Shutter", self._shut)

        self._wb = QSpinBox(); self._wb.setRange(2500, 10000); self._wb.setSingleStep(50)
        self._wb.setSuffix(" K"); self._wb.setValue(5600)
        self._wb.valueChanged.connect(self._send_wb)
        _row(lay, "Balance", self._wb)

        self._tint = QSpinBox(); self._tint.setRange(-50, 50)
        self._tint.valueChanged.connect(self._send_wb)
        _row(lay, "Tint", self._tint)

        wbrow = QHBoxLayout()
        for text, fn in (("Auto WB", "send_cam_auto_wb"),
                         ("Restore", "send_cam_restore_auto_wb")):
            b = QPushButton(text)
            b.clicked.connect(lambda _c, f=fn: self._send(getattr(self._mm, f)))
            wbrow.addWidget(b)
        lay.addLayout(wbrow)

        lay.addSpacing(6)
        lens = QLabel("Lens")
        lens.setStyleSheet("color:#e6e8ec; font-size:14px; font-weight:600;")
        lay.addWidget(lens)

        self._iris = self._slider(0, 100, 50)
        self._iris.valueChanged.connect(
            lambda v: self._send(self._mm.send_cam_iris, v / 100.0))
        _row(lay, "Iris", self._iris)

        self._zoom = self._slider(0, 100, 0)
        self._zoom.valueChanged.connect(
            lambda v: self._send(self._mm.send_cam_zoom_norm, v / 100.0))
        _row(lay, "Zoom", self._zoom)

        self._focus = self._slider(0, 100, 50)
        self._focus.valueChanged.connect(
            lambda v: self._send(self._mm.send_cam_focus, v / 100.0))
        _row(lay, "Focus", self._focus)

        arow = QHBoxLayout()
        for text, fn in (("Auto Focus", "send_cam_autofocus"),
                         ("Auto Iris", "send_cam_auto_iris")):
            b = QPushButton(text)
            b.clicked.connect(lambda _c, f=fn: self._send(getattr(self._mm, f)))
            arow.addWidget(b)
        lay.addLayout(arow)

        lay.addStretch(1)
        return box

    def _slider(self, lo: int, hi: int, val: int) -> QSlider:
        s = QSlider(Qt.Orientation.Horizontal)
        s.setRange(lo, hi); s.setValue(val)
        return s

    # ── right: colour correction ─────────────────────────────────────────
    def _build_correction_panel(self) -> QWidget:
        box = QFrame()
        box.setStyleSheet("QFrame{background:#232629; border-radius:6px;}")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(12, 10, 12, 12)

        head = QHBoxLayout()
        cap = QLabel("Color Correction")
        cap.setStyleSheet("color:#e6e8ec; font-size:14px; font-weight:600;")
        head.addWidget(cap)
        head.addStretch(1)
        rst = QPushButton("Reset All")
        rst.clicked.connect(self._reset_all)
        head.addWidget(rst)
        lay.addLayout(head)

        wheels = QHBoxLayout()
        wheels.setSpacing(12)
        # Spans are each parameter's useful range, so the puck's travel means
        # something comparable on all three.
        self._w_lift  = LabelledWheel("Lift",  span=0.5)
        self._w_gamma = LabelledWheel("Gamma", span=1.0)
        self._w_gain  = LabelledWheel("Gain",  span=2.0)
        for w, send in ((self._w_lift,  "send_cam_lift"),
                        (self._w_gamma, "send_cam_gamma"),
                        (self._w_gain,  "send_cam_gain_cc")):
            w.wheel.changed.connect(
                lambda r, g, b, y, f=send: self._send(getattr(self._mm, f), r, g, b, y))
            wheels.addWidget(w, 1)
        lay.addLayout(wheels, 1)

        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        self._sliders = {}
        specs = [
            ("Contrast",   0, 0, 0.0, 2.0, 1.0, self._send_contrast),
            ("Pivot",      1, 0, 0.0, 1.0, 0.5, self._send_contrast),
            ("Saturation", 0, 1, 0.0, 2.0, 1.0, self._send_hue_sat),
            ("Lum Mix",    1, 1, 0.0, 1.0, 1.0,
             lambda: self._send(self._mm.send_cam_luma_mix,
                                self._sliders["Lum Mix"].value() / 100.0)),
            ("Hue",        0, 2, -1.0, 1.0, 0.0, self._send_hue_sat),
        ]
        for name, r, c, lo, hi, init, cb in specs:
            sl = self._slider(int(lo * 100), int(hi * 100), int(init * 100))
            val = QLabel(f"{init:.2f}")
            val.setStyleSheet("color:#e6e8ec; font-size:12px;")
            val.setMinimumWidth(44)
            val.setAlignment(Qt.AlignmentFlag.AlignRight)
            sl.valueChanged.connect(
                lambda v, l=val: l.setText(f"{v / 100.0:.2f}"))
            sl.valueChanged.connect(lambda _v, f=cb: f())
            self._sliders[name] = sl
            lab = QLabel(name)
            lab.setStyleSheet("color:#9aa0a8; font-size:11px;")
            grid.addWidget(lab, r * 2,     c)
            grid.addWidget(sl,  r * 2 + 1, c)
            grid.addWidget(val, r * 2 + 1, c + 3)
        lay.addLayout(grid)
        return box

    # ── sending ──────────────────────────────────────────────────────────
    def _send(self, fn, *args) -> None:
        if self._loading:
            return          # this value came FROM the camera; do not echo it back
        fn(self._mount, *args)

    def _send_wb(self) -> None:
        self._send(self._mm.send_cam_white_balance, self._wb.value(), self._tint.value())

    def _send_contrast(self) -> None:
        self._send(self._mm.send_cam_contrast,
                   self._sliders["Pivot"].value() / 100.0,
                   self._sliders["Contrast"].value() / 100.0)

    def _send_hue_sat(self) -> None:
        self._send(self._mm.send_cam_hue_sat,
                   self._sliders["Hue"].value() / 100.0,
                   self._sliders["Saturation"].value() / 100.0)

    def _reset_all(self) -> None:
        for w in (self._w_lift, self._w_gamma, self._w_gain):
            w.wheel.set_values(0.0, 0.0, 0.0, 0.0)
        self._mm.send_cam_cc_reset(self._mount)

    # ── receiving ────────────────────────────────────────────────────────
    def _on_tab(self, idx: int) -> None:
        self._mount = idx + 1
        self._refresh_link()

    def _on_cam_status(self, mount_id: int) -> None:
        """Follow what the camera reports — the only confirmation there is."""
        if mount_id != self._mount:
            return
        st = self._mm.state(mount_id)
        self._loading = True
        try:
            if st.cam_wb is not None:
                self._wb.setValue(int(st.cam_wb))
            if st.cam_tint is not None:
                self._tint.setValue(int(st.cam_tint))
            adv = getattr(st, "cam_adv", None) or {}
            for key, wheel in (("lift", self._w_lift), ("gamma", self._w_gamma),
                               ("gain_cc", self._w_gain)):
                v = adv.get(key)
                if v and len(v) == 4:
                    wheel.wheel.set_values(*v)
            if "contrast" in adv:
                pv, adj = adv["contrast"]
                self._sliders["Pivot"].setValue(int(pv * 100))
                self._sliders["Contrast"].setValue(int(adj * 100))
            if "hue_sat" in adv:
                hu, sa = adv["hue_sat"]
                self._sliders["Hue"].setValue(int(hu * 100))
                self._sliders["Saturation"].setValue(int(sa * 100))
            if "luma_mix" in adv:
                self._sliders["Lum Mix"].setValue(int(adv["luma_mix"] * 100))
            for key, w, scale in (("iris", self._iris, 100), ("zoom", self._zoom, 100),
                                  ("focus", self._focus, 100)):
                if key in adv:
                    w.setValue(int(adv[key] * scale))
            if "gain_db" in adv and adv["gain_db"] in _GAINS_DB:
                self._gain.setCurrentIndex(_GAINS_DB.index(adv["gain_db"]))
            if "shutter_speed" in adv and adv["shutter_speed"] in _SHUTTERS:
                self._shut.setCurrentIndex(_SHUTTERS.index(adv["shutter_speed"]))
            if "nd" in adv:
                self._nd.setValue(float(adv["nd"]))
        finally:
            self._loading = False

    def _refresh_link(self) -> None:
        """Say plainly which of the four states this camera is in.

        Same wording as the ordinary dialog, because they need the same four
        different actions and a greyed control cannot tell them apart.
        """
        ble = self._mm.cam_ble_state(self._mount) if hasattr(self._mm, "cam_ble_state") else None
        txt = {None:  "no camera support on this mount — reflash it",
               "unpaired": "no camera paired to this mount",
               False: "camera off, asleep, or out of range",
               True:  "camera ready"}.get(ble, "camera state unknown")
        self._link.setText(f"CAM{self._mount}: {txt}")
        self._link.setStyleSheet("color:%s; font-size:12px;" %
                                 ("#7dc47d" if ble is True else "#e0a030"))

    def closeEvent(self, ev):
        self._poll.stop()
        super().closeEvent(ev)
