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

import time

from PyQt6.QtCore import Qt, QTimer, QObject, QEvent
from PyQt6.QtGui import QValidator
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QAbstractSpinBox,
    QSlider, QWidget, QFrame, QDoubleSpinBox, QSpinBox, QComboBox,
)

from comms.mount_manager import MountManager
from comms.protocol import NUM_MOUNTS, ISO_STEPS
from ui.widgets.colour_wheel import LabelledWheel
# The one palette both this dialog and the main window read, so the picker
# cannot drift out of step with the buttons behind it.
from ui.widgets.position_grid import CAM_COLORS

_SHUTTERS = [24, 25, 30, 48, 50, 60, 100, 120, 125, 200, 250, 500, 1000, 2000]

# How long a control stays the operator's after they last moved it.  The camera
# reports its state continuously and those reports lag what has just been sent,
# so applying them while someone is clicking an arrow drags the value backwards
# under the cursor.  Long enough to cover repeated clicks and a slider drag,
# short enough that the panel is following the camera again before anyone reads
# it as stuck.
_HOLD_S = 0.8

# This panel is driven by fingertips on a touch screen, not a mouse.  Every
# number that can be stepped gets the same 46x44 pair the everyday camera dialog
# uses, sliders get a handle big enough to catch, and rows are spaced far enough
# apart that reaching for one does not land on its neighbour.
_TOUCH_H = 44           # control height
_TOUCH_W = 46           # +/- button width
_ROW_GAP = 26           # between rows in Lens / Camera
_COL_GAP = 30           # between a wheel and its sliders, and between them

_STEP_CSS = """
QPushButton { background:#2A3038; color:#e6e8ec; font-size:22px; font-weight:600;
              border:none; border-radius:6px; }
QPushButton:pressed  { background:#1565C0; }
QPushButton:disabled { background:#23272c; color:#5A6472; }
"""

# A slider you can actually hit: 28px handle on a 10px groove.
_SLIDER_CSS = """
QSlider::groove:horizontal { height:10px; background:#3A3F45; border-radius:5px; }
QSlider::sub-page:horizontal { background:#1565C0; border-radius:5px; }
QSlider::add-page:horizontal { background:#3A3F45; border-radius:5px; }
QSlider::handle:horizontal {
    width:28px; height:28px; margin:-10px 0; border-radius:14px;
    background:#E6E8EC; border:1px solid #10131a;
}
QSlider::handle:horizontal:pressed { background:#BBDEFB; }
"""


class _NoStrayWheel(QObject):
    """Swallow scroll events on every camera control in this panel.

    Qt changes a combo box or spin box on a scroll even when it does not have
    focus, and on a touch screen a press with the smallest drag in it arrives
    as a scroll.  A press aimed at one row was rolling the box below it to the
    next stop — and TRANSMITTING it, because the change is indistinguishable
    from the operator making it.

    Nothing on this panel is worth scrolling, so the whole class of accident
    goes away by refusing the event rather than by moving controls apart.
    """

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Wheel:
            ev.ignore()
            return True
        return False


class _ListSpin(QSpinBox):
    """A spin box that steps through an arbitrary list rather than by a fixed
    increment.

    Blackmagic's ISO stops are not evenly spaced — 800, 1250, 3200 — so a plain
    spin box cannot walk them.  Qt's own value here is the INDEX into the list;
    value_of() and set_value_of() convert at the edges, so callers deal in ISO
    numbers and never in indices.
    """

    def __init__(self, values, parent=None):
        super().__init__(parent)
        self._values = list(values)
        self.setRange(0, len(self._values) - 1)

    def textFromValue(self, i: int) -> str:
        return f"{self._values[i]}"

    def valueFromText(self, text: str) -> int:
        t = text.replace(self.suffix(), "").strip()
        try:
            n = int(t)
        except ValueError:
            return self.value()
        # A typed value between stops goes to the nearest one the camera has,
        # rather than being rejected.
        return min(range(len(self._values)), key=lambda i: abs(self._values[i] - n))

    def validate(self, text: str, pos: int):
        t = text.replace(self.suffix(), "").strip()
        if t == "":
            return (QValidator.State.Intermediate, text, pos)
        return ((QValidator.State.Acceptable if t.isdigit()
                 else QValidator.State.Invalid), text, pos)

    def value_of(self):
        return self._values[self.value()]

    def set_value_of(self, v) -> None:
        """Show exactly what was reported, learning stops we did not know about.

        The camera's ISO series is finer than the stops offered for stepping —
        it has reported both 1250 and 1600 — and this used to ignore anything
        not already in the list, so the panel silently kept showing the old
        value and looked like it had missed the change.  A reported stop is by
        definition one the camera has, so it is inserted in order: the display
        stays honest and stepping finds it next time.
        """
        if v not in self._values:
            cur = self._values[self.value()] if self._values else None
            self._values.append(v)
            self._values.sort()
            was = self.blockSignals(True)
            self.setRange(0, len(self._values) - 1)
            if cur is not None:
                self.setValue(self._values.index(cur))
            self.blockSignals(was)
        self.setValue(self._values.index(v))



def _row(parent_lay, label: str, widget) -> QLabel:
    h = QHBoxLayout()
    h.setSpacing(10)
    lab = QLabel(label)
    lab.setStyleSheet("color:#9aa0a8; font-size:13px;")
    lab.setMinimumWidth(96)
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
        self._tint = None       # built with the correction panel, not the camera one
        self._touched: dict = {}    # widget -> monotonic time the operator last moved it
        self._nowheel = _NoStrayWheel(self)
        self.setWindowTitle("Camera Control — Advanced")
        self.resize(1500, 1000)
        self.setMinimumSize(1240, 860)
        self.setStyleSheet(
            "QDialog{background:#1b1d20;} QLabel{color:#cfd3d8;}"
            # Style the entry widgets explicitly — left to the platform they
            # come out light-on-light against this panel.
            "QComboBox,QAbstractSpinBox{font-size:16px; padding:4px 10px;"
            " background:#2A3038; color:#e6e8ec; border:1px solid #3A3F45;"
            " border-radius:6px;}"
            "QComboBox QAbstractItemView{background:#2A3038; color:#e6e8ec;"
            " selection-background-color:#1565C0;}"
            + _SLIDER_CSS)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 14)
        root.setSpacing(10)

        # Camera picker, centred, in the same colours as the Cam 1-5 buttons on
        # the main window directly behind this dialog.  A QTabBar cannot carry
        # a per-tab background — stylesheets have no way to address one tab —
        # so these are buttons wearing the main window's own palette.
        self._cam_btns: dict[int, QPushButton] = {}
        picker = QHBoxLayout()
        picker.setSpacing(12)
        picker.addStretch(1)
        for i in range(1, NUM_MOUNTS + 1):
            b = QPushButton(f"Cam {i}")
            b.setFixedHeight(58)
            b.setMinimumWidth(150)
            b.clicked.connect(lambda _c, m=i: self._select_cam(m))
            self._cam_btns[i] = b
            picker.addWidget(b)
        picker.addStretch(1)
        root.addLayout(picker)

        self._link = QLabel("")
        self._link.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._link.setStyleSheet("color:#e0a030; font-size:13px;")
        root.addWidget(self._link)

        body = QHBoxLayout()
        body.setSpacing(14)
        body.addWidget(self._build_camera_panel(), 0)
        body.addWidget(self._build_correction_panel(), 1)
        root.addLayout(body, 1)

        close = QPushButton("Close")
        close.setFixedHeight(_TOUCH_H)
        close.setMinimumWidth(140)
        close.clicked.connect(self.accept)
        foot = QHBoxLayout()
        foot.addStretch(1)
        foot.addWidget(close)
        root.addLayout(foot)

        self._mm.cam_status_received.connect(self._on_cam_status)
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._refresh_link)
        self._poll.start(1000)
        self._paint_cam_btns()
        self._refresh_link()
        self._show_known()

    # ── left: camera settings ────────────────────────────────────────────
    def _build_camera_panel(self) -> QWidget:
        box = QFrame()
        box.setStyleSheet("QFrame{background:#232629; border-radius:6px;}")
        box.setFixedWidth(400)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(18, 16, 18, 18)
        lay.setSpacing(_ROW_GAP)

        head = QLabel("Camera")
        head.setStyleSheet("color:#e6e8ec; font-size:14px; font-weight:600;")
        lay.addWidget(head)

        self._nd = QDoubleSpinBox(); self._nd.setRange(0, 12); self._nd.setSingleStep(0.5)
        self._nd.setSuffix(" stop")
        self._spin(self._nd)
        self._nd.valueChanged.connect(
            lambda v: self._send(self._mm.send_cam_nd, v))
        _row(lay, "Filter (ND)", self._stepper(self._nd))

        # ISO, and no separate Gain control.  Category 1 parameter 13 (gain in
        # dB) and parameter 14 (ISO) are two scales for one sensor
        # amplification: setting one moves the other, which the camera
        # demonstrated by stepping ISO down a stop, about 0.8s after every press
        # of a gain button, and reporting it.  Only ISO is ever reported back,
        # so gain was the half that could never be confirmed — two controls for
        # one setting, one of them unverifiable.  Nothing is lost by dropping it
        # beyond a second way of writing the same number.
        #
        # Stepped through the camera's own ISO stops, which are not evenly
        # spaced, so the box counts list positions and shows the value.
        self._iso = _ListSpin(ISO_STEPS)
        self._iso.set_value_of(400)
        self._spin(self._iso)
        self._iso.valueChanged.connect(
            lambda _i: self._send(self._mm.send_cam_iso, self._iso.value_of()))
        _row(lay, "ISO", self._stepper(self._iso))

        self._shut = QComboBox()
        for s in _SHUTTERS:
            self._shut.addItem(f"1/{s}", s)
        self._shut.setFixedHeight(_TOUCH_H)
        self._shut.installEventFilter(self._nowheel)
        self._shut.setCurrentIndex(_SHUTTERS.index(50))
        self._shut.currentIndexChanged.connect(lambda _i: self._touch(self._shut))
        self._shut.currentIndexChanged.connect(
            lambda _i: self._send(self._mm.send_cam_shutter_speed, self._shut.currentData()))
        _row(lay, "Shutter", self._shut)

        # Type a temperature straight in, or step it with the arrows.  Step 50
        # so the arrows move a useful amount; typed values are taken as typed.
        self._wb = QSpinBox(); self._wb.setRange(2500, 10000); self._wb.setSingleStep(50)
        self._wb.setSuffix(" K"); self._wb.setValue(5600)
        self._spin(self._wb)
        self._wb.setToolTip("Type a value and press Enter, or use − / +")
        self._wb.valueChanged.connect(self._send_wb)
        _row(lay, "Balance", self._stepper(self._wb))
        # Tint lives in the Gain column of the correction panel, as on the
        # reference.  _send_wb sends both, so it has to be built by then.

        wbrow = QHBoxLayout()
        for text, fn in (("Auto WB", "send_cam_auto_wb"),
                         ("Restore", "send_cam_restore_auto_wb")):
            b = QPushButton(text)
            b.setFixedHeight(_TOUCH_H)
            b.setStyleSheet(_STEP_CSS.replace('font-size:22px', 'font-size:14px'))
            b.clicked.connect(lambda _c, f=fn: self._send(getattr(self._mm, f)))
            wbrow.addWidget(b)
        lay.addLayout(wbrow)

        lay.addStretch(1)
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
            b.setFixedHeight(_TOUCH_H)
            b.setStyleSheet(_STEP_CSS.replace('font-size:22px', 'font-size:14px'))
            b.clicked.connect(lambda _c, f=fn: self._send(getattr(self._mm, f)))
            arow.addWidget(b)
        lay.addLayout(arow)

        lay.addStretch(1)
        return box

    def _slider(self, lo: int, hi: int, val: int) -> QSlider:
        s = QSlider(Qt.Orientation.Horizontal)
        s.setMinimumHeight(_TOUCH_H)
        s.installEventFilter(self._nowheel)
        s.setRange(lo, hi); s.setValue(val)
        s.valueChanged.connect(lambda _v, w=s: self._touch(w))
        return s

    def _stepper(self, box) -> QWidget:
        """A spin box between two finger-sized buttons.

        The spin box's own arrows are a few pixels tall and hopeless with a
        fingertip, so they are switched off and replaced with the same pair the
        everyday camera dialog uses.  The field stays typeable — the buttons are
        an addition, not a replacement — and stepping through stepBy() means a
        press goes down exactly the same path as a typed value or an arrow key.
        """
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        box.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        box.setFixedHeight(_TOUCH_H)
        box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        def button(text: str, delta: int) -> QPushButton:
            b = QPushButton(text)
            b.setFixedSize(_TOUCH_W, _TOUCH_H)
            b.setStyleSheet(_STEP_CSS)
            b.setAutoRepeat(True)          # hold it down and it keeps stepping
            b.setAutoRepeatDelay(400)
            b.setAutoRepeatInterval(120)
            b.clicked.connect(lambda _c, d=delta, s=box: s.stepBy(d))
            return b

        h.addWidget(button("−", -1))
        h.addWidget(box, 1)
        h.addWidget(button("+", 1))
        return w

    def _spin(self, box):
        """Every spin box in the panel: typeable, and it holds off the camera.

        keyboardTracking off matters more than it looks — with it on, a QSpinBox
        emits on every keystroke, so typing 6500 sends 6, 65, 650 and 6500 as
        four separate commands, the first three of them clamped to the minimum.
        Off, it commits once on Enter or when focus leaves.
        """
        box.setKeyboardTracking(False)
        box.setAccelerated(True)        # hold the arrow down and it ramps
        box.installEventFilter(self._nowheel)
        box.valueChanged.connect(lambda _v, w=box: self._touch(w))
        return box

    # ── right: colour correction ─────────────────────────────────────────
    def _build_correction_panel(self) -> QWidget:
        box = QFrame()
        box.setStyleSheet("QFrame{background:#232629; border-radius:6px;}")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(18, 16, 18, 18)
        lay.setSpacing(12)

        head = QHBoxLayout()
        cap = QLabel("Color Correction")
        cap.setStyleSheet("color:#e6e8ec; font-size:14px; font-weight:600;")
        head.addWidget(cap)
        head.addStretch(1)
        rst = QPushButton("Reset All")
        rst.setFixedHeight(_TOUCH_H)
        rst.setMinimumWidth(130)
        rst.clicked.connect(self._reset_all)
        head.addWidget(rst)
        lay.addLayout(head)

        # span = offset at the rim, centre = the parameter's neutral, lo/hi = its
        # legal range, all from the Blackmagic category-8 table.  Gain's neutral
        # is ONE, not zero: it is a multiplier, and a zero-centred gain wheel
        # sends a black picture in every direction including at rest.
        self._w_lift  = LabelledWheel("Lift",  span=0.5, centre=0.0, lo=-2.0, hi=2.0)
        self._w_gamma = LabelledWheel("Gamma", span=1.0, centre=0.0, lo=-4.0, hi=4.0)
        self._w_gain  = LabelledWheel("Gain",  span=1.0, centre=1.0, lo=0.0,  hi=16.0)

        # Three columns, exactly as the reference panel groups them: each wheel
        # keeps its own two sliders directly underneath it.  The pairing is the
        # reference's, not a functional one — pivot belongs to contrast, but
        # saturation and lum mix have nothing to do with gamma.  It is a layout
        # the operator already reads fluently, which is the whole point.
        #
        # Tint sits here under Gain rather than in the Camera panel because that
        # is where the reference puts it, even though it is a white-balance
        # parameter and travels in the same command as the temperature.
        cols = QHBoxLayout()
        cols.setSpacing(22)
        self._sliders = {}
        # Readouts follow the reference, which shows the slider's POSITION across
        # its range rather than the raw wire value: contrast reads 49% at
        # mid-track and hue reads 180° at centre, though on the wire those are
        # 0.98 and 0.00.  `f` is that fraction, `v` the value actually sent.
        pct   = lambda v, f: f"{f * 100:.0f}%"
        two   = lambda v, f: f"{v:.2f}"
        deg   = lambda v, f: f"{f * 360:.0f}°"
        whole = lambda v, f: f"{v:.0f}"
        column_specs = [
            (self._w_lift, "send_cam_lift", [
                ("Contrast",   0.0, 2.0, 1.0, 100, pct,   self._send_contrast),
                ("Pivot",      0.0, 1.0, 0.5, 100, two,   self._send_contrast),
            ]),
            (self._w_gamma, "send_cam_gamma", [
                ("Saturation", 0.0, 2.0, 1.0, 100, pct,   self._send_hue_sat),
                ("Lum Mix",    0.0, 1.0, 1.0, 100, pct,
                 lambda: self._send(self._mm.send_cam_luma_mix,
                                    self._sliders["Lum Mix"].value() / 100.0)),
            ]),
            (self._w_gain, "send_cam_gain_cc", [
                ("Hue",       -1.0, 1.0, 0.0, 100, deg,   self._send_hue_sat),
                ("Tint",     -50.0, 50.0, 0.0,  1, whole, self._send_wb),
            ]),
        ]
        for wheel, send, sliders in column_specs:
            wheel.wheel.changed.connect(
                lambda *_a, w=wheel.wheel: self._touch(w))
            wheel.wheel.changed.connect(
                lambda r, g, b, y, f=send: self._send(getattr(self._mm, f), r, g, b, y))
            col = QVBoxLayout()
            col.setSpacing(_COL_GAP)
            col.addWidget(wheel)
            for spec in sliders:
                col.addWidget(self._slider_cell(*spec))
            col.addStretch(1)
            cols.addLayout(col, 1)
        lay.addLayout(cols, 1)

        # Tint is a slider now, but the rest of the dialog only ever calls
        # .value()/.setValue() on it, which a QSlider answers the same way the
        # spin box did.
        self._tint = self._sliders["Tint"]
        return box

    def _slider_cell(self, name: str, lo: float, hi: float, init: float,
                     scale: int, fmt, cb) -> QWidget:
        """Name top-left, value top-right, slider full width beneath.

        `scale` is slider-steps per unit — 100 for the fractional parameters,
        1 for tint, which is a whole number on the wire.
        """
        cell = QWidget()
        v = QVBoxLayout(cell)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        frac = lambda value: (value - lo) / (hi - lo) if hi > lo else 0.0

        lab = QLabel(name)
        lab.setStyleSheet("color:#9aa0a8; font-size:11px;")
        val = QLabel(fmt(init, frac(init)))
        val.setStyleSheet("color:#e6e8ec; font-size:12px; font-weight:600;")
        val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(lab)
        head.addStretch(1)
        head.addWidget(val)
        v.addLayout(head)

        sl = self._slider(int(lo * scale), int(hi * scale), int(init * scale))
        sl.valueChanged.connect(
            lambda x, l=val, s=scale, f=fmt, q=frac: l.setText(f(x / s, q(x / s))))
        sl.valueChanged.connect(lambda _x, f=cb: f())
        self._sliders[name] = sl
        v.addWidget(sl)
        return cell

    # ── sending ──────────────────────────────────────────────────────────
    def _send(self, fn, *args) -> None:
        if self._loading:
            return          # this value came FROM the camera; do not echo it back
        fn(self._mount, *args)

    def _send_wb(self) -> None:
        """Temperature and tint travel in one command, so both go every time."""
        tint = self._tint.value() if getattr(self, "_tint", None) is not None else 0
        self._send(self._mm.send_cam_white_balance, self._wb.value(), tint)

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
            c = w.wheel.centre()          # gain's neutral is 1.0, not 0.0
            w.wheel.set_values(c, c, c, c)
        self._mm.send_cam_cc_reset(self._mount)

    # ── receiving ────────────────────────────────────────────────────────
    def _select_cam(self, mount_id: int) -> None:
        self._mount = mount_id
        self._paint_cam_btns()
        self._refresh_link()
        self._touched.clear()   # a hold belongs to the camera it was made on
        self._show_known()

    def _paint_cam_btns(self) -> None:
        """Selected camera wears its accent; the rest wear the same grey the
        main window uses, so the two rows read as one control."""
        for mid, b in self._cam_btns.items():
            col = CAM_COLORS[mid]
            border = col["accent"].name() if mid == self._mount else "#333333"
            b.setStyleSheet(f"""
                QPushButton {{
                    background: {col['btn_bg']}; color: {col['btn_text']};
                    border: 5px solid {border};
                    border-radius: 20px; font-size: 24px; font-weight: bold;
                    padding: 4px 12px;
                }}
            """)

    # ── hands off while the operator is working ──────────────────────────
    def _touch(self, w) -> None:
        """Note that the OPERATOR just moved this control.

        Only user-driven changes count: _loading marks the ones this dialog
        made itself while showing a camera report, and those must not extend
        the hold or the panel would lock itself out of its own updates.
        """
        if not self._loading:
            self._touched[w] = time.monotonic()

    def _held(self, w) -> bool:
        """True while a camera report must keep its hands off this control."""
        if isinstance(w, QAbstractSpinBox) and w.hasFocus():
            return True         # they are typing in it; do not rewrite the text
        t = self._touched.get(w)
        return t is not None and (time.monotonic() - t) < _HOLD_S

    def _set_if_free(self, w, value) -> None:
        if not self._held(w):
            w.setValue(value)

    def _on_cam_status(self, mount_id: int) -> None:
        """A fresh report arrived for this camera."""
        if mount_id == self._mount:
            self._show_known()

    def _show_known(self) -> None:
        """Put everything known about this camera onto the controls.

        Called when a report arrives, when the dialog opens, and when the
        camera is switched.  The last two matter: without them the panel was
        built neutral and stayed neutral until the camera next volunteered
        something — which for gain, shutter, ND and the whole of colour
        correction is never, because the camera does not report those at all.

        The source is mount_manager.cam_known(), which merges what the camera
        has said over what this app has sent, so a reported value always wins
        and an unreported one still shows what it was set to.

        A control the operator has a hand on is left alone — see _held().  The
        camera reports continuously and its reports lag what has just been
        sent, so applying them unconditionally means each click of an arrow is
        answered by the previous value a moment later.
        """
        adv = (self._mm.cam_known(self._mount)
               if hasattr(self._mm, "cam_known") else {})
        self._loading = True
        try:
            if "white_balance" in adv:
                self._set_if_free(self._wb, int(adv["white_balance"]))
            if "tint" in adv:
                self._set_if_free(self._tint, int(adv["tint"]))
            # No "is it one of our stops?" test — the box learns any stop the
            # camera reports, and screening them out here was half of why a
            # reported ISO could vanish without trace.
            if adv.get("iso") is not None and not self._held(self._iso):
                self._iso.set_value_of(adv["iso"])
            for key, wheel in (("lift", self._w_lift), ("gamma", self._w_gamma),
                               ("gain_cc", self._w_gain)):
                v = adv.get(key)
                if v and len(v) == 4 and not self._held(wheel.wheel):
                    wheel.wheel.set_values(*v)
            if "contrast" in adv:
                pv, adj = adv["contrast"]
                self._set_if_free(self._sliders["Pivot"], int(pv * 100))
                self._set_if_free(self._sliders["Contrast"], int(adj * 100))
            if "hue_sat" in adv:
                hu, sa = adv["hue_sat"]
                self._set_if_free(self._sliders["Hue"], int(hu * 100))
                self._set_if_free(self._sliders["Saturation"], int(sa * 100))
            if "luma_mix" in adv:
                self._set_if_free(self._sliders["Lum Mix"],
                                  int(adv["luma_mix"] * 100))
            for key, w, scale in (("iris", self._iris, 100), ("zoom", self._zoom, 100),
                                  ("focus", self._focus, 100)):
                if key in adv:
                    self._set_if_free(w, int(adv[key] * scale))
            if ("shutter_speed" in adv and adv["shutter_speed"] in _SHUTTERS
                    and not self._held(self._shut)):
                self._shut.setCurrentIndex(_SHUTTERS.index(adv["shutter_speed"]))
            if "nd" in adv:
                self._set_if_free(self._nd, float(adv["nd"]))
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
        self._link.setText(f"Cam {self._mount}: {txt}")
        self._link.setStyleSheet("color:%s; font-size:12px;" %
                                 ("#7dc47d" if ble is True else "#e0a030"))

    def closeEvent(self, ev):
        self._poll.stop()
        super().closeEvent(ev)
