"""ColourWheel — a Resolve/ATEM-style lift-gamma-gain control.

Drag the puck: angle picks a hue, distance from centre picks how much of it.
The master (Y) sits on its own strip below, as it does on the ATEM panel, because
it moves all three channels together and mixing it into the same gesture makes
both harder to place.

The four numbers underneath are the wire values, not a prettified version of
them: Blackmagic takes R, G, B and Y as signed 5.11 fixed point, and what is
shown here is what is sent.  A control that displays something other than what it
transmits is the thing that made the extended-page speed dials lie for a week.

Emits `changed(r, g, b, y)` continuously while dragging.  There is no
acknowledgement anywhere in the Blackmagic protocol, so the camera cannot
confirm a value and this widget never pretends it has: the puck follows the
mouse because the operator put it there, and the numbers say what was sent.
"""
from __future__ import annotations

import colorsys
import math

from PyQt6.QtCore import Qt, QPointF, pyqtSignal
from PyQt6.QtGui import QPainter, QConicalGradient, QColor, QPen, QBrush
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSizePolicy


def _hue_to_offsets(hue: float, mag: float) -> tuple[float, float, float]:
    """A hue direction as an R/G/B offset triple, centred on zero.

    Subtracting the mean is what makes it a colour BALANCE rather than a
    brightness: pushing toward red lifts red and lowers green and blue, so the
    overall level stays put and only the tint moves.  That is what the wheel is
    for; the master strip is there for level.
    """
    r, g, b = colorsys.hsv_to_rgb(hue % 1.0, 1.0, 1.0)
    m = (r + g + b) / 3.0
    return ((r - m) * mag, (g - m) * mag, (b - m) * mag)


class ColourWheel(QWidget):
    changed = pyqtSignal(float, float, float, float)   # r, g, b, y

    def __init__(self, title: str, span: float = 1.0, parent=None):
        """`span` is the value at the rim — the parameter's useful range.

        Lift and gamma are small trims either side of zero; gain runs 0..2 or
        more.  Passing it in keeps the puck's travel meaningful for each rather
        than making the operator drag a millimetre for gain and a mile for lift.
        """
        super().__init__(parent)
        self._span = span
        self._r = self._g = self._b = 0.0
        self._y = 0.0
        self._drag = False
        self.setMinimumSize(190, 250)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._title = title

    # -- value ------------------------------------------------------------
    def values(self) -> tuple[float, float, float, float]:
        return (self._r, self._g, self._b, self._y)

    def set_values(self, r: float, g: float, b: float, y: float) -> None:
        """Set without emitting — for showing what the camera reported."""
        self._r, self._g, self._b, self._y = r, g, b, y
        self.update()

    def reset(self) -> None:
        self._r = self._g = self._b = self._y = 0.0
        self.update()
        self.changed.emit(0.0, 0.0, 0.0, 0.0)

    # -- geometry ---------------------------------------------------------
    def _ring(self) -> tuple[QPointF, float]:
        w, h = self.width(), self.height() - 62      # leave room for strip + numbers
        d = max(20, min(w, h) - 8)
        return QPointF(self.width() / 2.0, 4 + d / 2.0), d / 2.0

    def _puck(self) -> QPointF:
        c, rad = self._ring()
        # Invert _hue_to_offsets: the offsets are a direction and a magnitude.
        mx = self._r - (self._r + self._g + self._b) / 3.0
        my = self._g - (self._r + self._g + self._b) / 3.0
        mag = math.hypot(self._r, self._g, self._b)
        if mag < 1e-6:
            return c
        h, _, _ = colorsys.rgb_to_hsv(*[max(0.0, v + 1.0) for v in (self._r, self._g, self._b)])
        ang = h * 2 * math.pi
        rr = min(1.0, mag / max(1e-6, self._span)) * rad * 0.86
        return QPointF(c.x() + rr * math.cos(ang - math.pi / 2),
                       c.y() + rr * math.sin(ang - math.pi / 2))

    # -- painting ---------------------------------------------------------
    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c, rad = self._ring()

        grad = QConicalGradient(c, 90.0)
        for i in range(13):
            t = i / 12.0
            rr, gg, bb = colorsys.hsv_to_rgb(t, 0.55, 0.95)
            grad.setColorAt(t, QColor(int(rr * 255), int(gg * 255), int(bb * 255)))
        p.setPen(QPen(QBrush(grad), 9))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(c, rad - 5, rad - 5)

        p.setBrush(QColor(28, 30, 33))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(c, rad - 10, rad - 10)

        p.setPen(QPen(QColor(70, 74, 80), 1))
        p.drawLine(QPointF(c.x() - rad + 12, c.y()), QPointF(c.x() + rad - 12, c.y()))
        p.drawLine(QPointF(c.x(), c.y() - rad + 12), QPointF(c.x(), c.y() + rad - 12))

        pk = self._puck()
        p.setBrush(QColor(210, 214, 220))
        p.setPen(QPen(QColor(20, 22, 25), 2))
        p.drawEllipse(pk, 7, 7)

        # Master strip — dotted, as on the reference panel
        y0 = self.height() - 46
        p.setPen(QPen(QColor(90, 95, 102), 1))
        p.drawRect(8, y0, self.width() - 16, 12)
        frac = 0.5 + (self._y / (2 * max(1e-6, self._span)))
        frac = max(0.0, min(1.0, frac))
        x = 8 + frac * (self.width() - 16)
        p.setBrush(QColor(210, 214, 220))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(int(x) - 2, y0 - 2, 4, 16)

        p.setPen(QColor(190, 194, 200))
        f = p.font(); f.setPointSize(9); p.setFont(f)
        cols = [("", self._y), ("R", self._r), ("G", self._g), ("B", self._b)]
        wdt = self.width() / 4.0
        for i, (lbl, v) in enumerate(cols):
            p.setPen(QColor(230, 232, 236) if not lbl else
                     {"R": QColor(224, 108, 108), "G": QColor(120, 200, 120),
                      "B": QColor(110, 150, 235)}[lbl])
            p.drawText(int(i * wdt), self.height() - 26, int(wdt), 20,
                       int(Qt.AlignmentFlag.AlignCenter), f"{v:+.2f}")
        p.end()

    # -- interaction ------------------------------------------------------
    def mousePressEvent(self, ev):  self._drag = True;  self._apply(ev)
    def mouseMoveEvent(self, ev):
        if self._drag: self._apply(ev)
    def mouseReleaseEvent(self, _ev): self._drag = False

    def mouseDoubleClickEvent(self, _ev):
        """Double-click to zero, matching the reset arrow on the panel."""
        self.reset()

    def _apply(self, ev) -> None:
        c, rad = self._ring()
        pos = ev.position() if hasattr(ev, "position") else ev.pos()
        y0 = self.height() - 46
        if pos.y() >= y0 - 4:                       # on the master strip
            frac = max(0.0, min(1.0, (pos.x() - 8) / max(1.0, self.width() - 16)))
            self._y = (frac - 0.5) * 2 * self._span
        else:
            dx, dy = pos.x() - c.x(), pos.y() - c.y()
            dist = min(math.hypot(dx, dy), rad * 0.86)
            ang = math.atan2(dy, dx) + math.pi / 2
            hue = (ang / (2 * math.pi)) % 1.0
            mag = (dist / max(1e-6, rad * 0.86)) * self._span
            self._r, self._g, self._b = _hue_to_offsets(hue, mag)
        self.update()
        self.changed.emit(self._r, self._g, self._b, self._y)


class LabelledWheel(QWidget):
    """A wheel with its title above, as the panel lays them out."""

    def __init__(self, title: str, span: float = 1.0, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        cap = QLabel(title)
        cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cap.setStyleSheet("color:#cfd3d8; font-size:13px; font-weight:600;")
        self.wheel = ColourWheel(title, span)
        lay.addWidget(cap)
        lay.addWidget(self.wheel, 1)
