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

from PyQt6.QtCore import Qt, QPointF, QSize, pyqtSignal
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


def _hue_unit(hue: float) -> float:
    """Length of the offset triple for this hue at magnitude 1.

    Varies between 0.707 (secondaries) and 0.816 (primaries) because the hue
    ramp is a hexagon, not a circle.  Needed to turn a stored value back into a
    puck position.
    """
    r, g, b = colorsys.hsv_to_rgb(hue % 1.0, 1.0, 1.0)
    m = (r + g + b) / 3.0
    return math.sqrt((r - m) ** 2 + (g - m) ** 2 + (b - m) ** 2) or 1.0


class ColourWheel(QWidget):
    changed = pyqtSignal(float, float, float, float)   # r, g, b, y

    def __init__(self, title: str, span: float = 1.0, centre: float = 0.0,
                 lo: float = -16.0, hi: float = 16.0, parent=None):
        """`span` is the offset at the rim; `centre` is the parameter's neutral.

        Centre matters more than it looks.  Lift and gamma are trims either side
        of ZERO, but Blackmagic's gain is a MULTIPLIER whose neutral is ONE — a
        gain wheel centred on zero sends gain 0 in every direction, which is a
        black picture, and it is black sitting at rest too.  Storing the wire
        value rather than the offset is what keeps that straight: r/g/b/y here
        are always exactly what goes out, and the puck is derived from them.

        `lo`/`hi` clamp to the parameter's legal range, so dragging to the rim
        cannot ask for a negative gain.
        """
        super().__init__(parent)
        self._span = span
        self._centre = centre
        self._lo, self._hi = lo, hi
        self._r = self._g = self._b = self._y = centre
        self._drag = False
        self.setMinimumSize(190, 236)
        # Vertically Preferred, not Expanding: the ring is limited by the
        # narrower of width and height, so letting the widget swallow spare
        # height just opens a dead band between the wheels and whatever sits
        # under them.  The panel puts its stretch below the sliders instead.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._title = title

    def sizeHint(self):
        return QSize(210, 300)

    # -- value ------------------------------------------------------------
    def values(self) -> tuple[float, float, float, float]:
        return (self._r, self._g, self._b, self._y)

    def centre(self) -> float:
        return self._centre

    def set_values(self, r: float, g: float, b: float, y: float) -> None:
        """Set without emitting — for showing what the camera reported."""
        self._r, self._g, self._b, self._y = r, g, b, y
        self.update()

    def reset(self) -> None:
        c = self._centre
        self._r = self._g = self._b = self._y = c
        self.update()
        self.changed.emit(c, c, c, c)

    # -- geometry ---------------------------------------------------------
    # The master strip hangs off the RING, not off the bottom of the widget.
    # Pinned to the widget it drifted further from the wheel the taller the
    # panel got, which is the opposite of the reference layout where the master
    # sits tucked under its own wheel.
    _STRIP_H = 12
    _FOOT = 42            # strip + numbers, below the ring

    def _ring(self) -> tuple[QPointF, float]:
        w, h = self.width(), self.height() - self._FOOT
        d = max(20, min(w, h) - 8)
        return QPointF(w / 2.0, 4 + d / 2.0), d / 2.0

    def _strip_top(self) -> int:
        c, rad = self._ring()
        return int(c.y() + rad + 6)

    def _hue_mag(self) -> tuple[float, float]:
        """Recover the puck's hue and distance from the stored wire values.

        The offset triple's length is NOT constant around the wheel — a primary
        gives 0.816 and a secondary 0.707 — so the hue has to be recovered first
        and the length divided by that hue's own unit length.  Normalising by a
        constant instead puts the puck several pixels from the mouse.
        """
        o = [self._r - self._centre, self._g - self._centre, self._b - self._centre]
        length = math.sqrt(sum(v * v for v in o))
        if length < 1e-9:
            return 0.0, 0.0
        lo = min(o)
        rgb = [v - lo for v in o]
        top = max(rgb)
        if top < 1e-9:
            return 0.0, 0.0
        h, _, _ = colorsys.rgb_to_hsv(*[v / top for v in rgb])
        return h, length / _hue_unit(h)

    def _puck(self) -> QPointF:
        c, rad = self._ring()
        h, mag = self._hue_mag()
        if mag < 1e-6:
            return c
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

        # Master strip — tucked under the ring, as on the reference panel
        y0 = self._strip_top()
        p.setPen(QPen(QColor(90, 95, 102), 1))
        p.drawRect(8, y0, self.width() - 16, self._STRIP_H)
        frac = 0.5 + ((self._y - self._centre) / (2 * max(1e-6, self._span)))
        frac = max(0.0, min(1.0, frac))
        x = 8 + frac * (self.width() - 16)
        p.setBrush(QColor(210, 214, 220))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(int(x) - 2, y0 - 2, 4, self._STRIP_H + 4)

        f = p.font(); f.setPointSize(9); p.setFont(f)
        cols = [("", self._y), ("R", self._r), ("G", self._g), ("B", self._b)]
        wdt = self.width() / 4.0
        for i, (lbl, v) in enumerate(cols):
            p.setPen(QColor(230, 232, 236) if not lbl else
                     {"R": QColor(224, 108, 108), "G": QColor(120, 200, 120),
                      "B": QColor(110, 150, 235)}[lbl])
            p.drawText(int(i * wdt), y0 + self._STRIP_H + 3, int(wdt), 18,
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

    def _clamp(self, v: float) -> float:
        return max(self._lo, min(self._hi, v))

    def _apply(self, ev) -> None:
        c, rad = self._ring()
        pos = ev.position() if hasattr(ev, "position") else ev.pos()
        y0 = self._strip_top()
        if pos.y() >= y0 - 4:                       # on the master strip
            frac = max(0.0, min(1.0, (pos.x() - 8) / max(1.0, self.width() - 16)))
            self._y = self._clamp(self._centre + (frac - 0.5) * 2 * self._span)
        else:
            dx, dy = pos.x() - c.x(), pos.y() - c.y()
            dist = min(math.hypot(dx, dy), rad * 0.86)
            ang = math.atan2(dy, dx) + math.pi / 2
            hue = (ang / (2 * math.pi)) % 1.0
            mag = (dist / max(1e-6, rad * 0.86)) * self._span
            off = _hue_to_offsets(hue, mag)
            # Offsets ride on the neutral: zero for lift and gamma, ONE for gain.
            self._r, self._g, self._b = (self._clamp(self._centre + o) for o in off)
        self.update()
        self.changed.emit(self._r, self._g, self._b, self._y)


class LabelledWheel(QWidget):
    """A wheel with its title above, as the panel lays them out."""

    def __init__(self, title: str, span: float = 1.0, centre: float = 0.0,
                 lo: float = -16.0, hi: float = 16.0, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        cap = QLabel(title)
        cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cap.setStyleSheet("color:#cfd3d8; font-size:13px; font-weight:600;")
        self.wheel = ColourWheel(title, span, centre, lo, hi)
        lay.addWidget(cap)
        lay.addWidget(self.wheel, 1)
