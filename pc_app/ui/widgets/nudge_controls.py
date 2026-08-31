"""
Custom controls for the Move panel: a split-arc dial for pan/tilt, a stadium
track for the slider, and a matching column for zoom.

The panel used to be a cross of square buttons — tilt in a column, pan in a
row, slider along the bottom. Everything worked, but the shape carried no
meaning: nothing about a grid of squares says which of them turns the head
which way.

  RadialNudge   pan and tilt as four separated arc groups around a hub.
                Up and down are tilt, left and right are pan; the inner arc is
                the small step and the outer the large one. The gaps at the
                diagonals are the point: a slip near one lands on nothing
                rather than on the wrong axis, and nudging pan when you meant
                tilt is a shot on air going the wrong way.

                The hub is left free for the live angles, which the old cross
                had nowhere to put.

  SliderTrack   the slider as the thing it actually is — a carriage on a rail
                of known length. Four tap zones for the same discrete steps as
                before, and the carriage drawn where the mount says it is.

  ZoomColumn    zoom stays OUT of the dial. It is press-and-hold: it runs while
                held rather than stepping a fixed amount, and putting it among
                discrete nudges would make it look like the one thing it is
                not. It gets the same rounded treatment so it belongs, without
                pretending to be a nudge.

Angles here are degrees CLOCKWISE FROM TWELVE, because that is how the control
reads on screen. Qt measures counter-clockwise from three, so every arc call
converts — see _qt_angle().
"""
from __future__ import annotations

import math
from PyQt6.QtWidgets import QWidget, QSizePolicy
from PyQt6.QtCore import Qt, QRectF, QTimer, pyqtSignal
from PyQt6.QtGui import QPainter, QPainterPath, QColor, QPen, QFont

# Axis colours, matching the rest of the app.
TILT_LINE = "#7FBF72"; TILT_FILL = "#1B5E20"; TILT_LIT = "#2E7D32"
PAN_LINE  = "#D4B800"; PAN_FILL  = "#3E3000"; PAN_LIT  = "#5D4700"
ZOOM_LINE = "#90CAF9"; ZOOM_FILL = "#12305E"; ZOOM_LIT = "#1A3F7A"
SL_LINE   = "#CE93D8"; SL_FILL   = "#3B0A57"; SL_LIT   = "#5A1080"
WELL      = "#0E1518"; WELL_EDGE = "#2A363C"

# Where the four arc groups sit, clockwise from twelve.
_UP, _RIGHT, _DOWN, _LEFT = 0.0, 90.0, 180.0, 270.0

# Half-width of each group, and the gap that separates them. 45 - 32 = 13
# degrees of dead space either side of every diagonal.
_HALF_SPAN = 32.0

# Radii as a fraction of the dial's half-size, so the whole control scales.
_R_HUB   = 0.30
_R_IN_0  = 0.33
_R_IN_1  = 0.575
_R_OUT_0 = 0.655
_R_OUT_1 = 0.92


def _qt_angle(clock_deg: float) -> float:
    """Degrees clockwise from twelve -> Qt's counter-clockwise from three."""
    return 90.0 - clock_deg


class RadialNudge(QWidget):
    """Pan and tilt as four separated arc groups. Emits (axis, degrees)."""

    nudged = pyqtSignal(str, float)      # "pan" | "tilt", signed degrees

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self.setMinimumSize(240, 240)
        self._small = 1.0
        self._large = 10.0
        self._pan_deg: float | None = None
        self._tilt_deg: float | None = None
        self._lit: tuple[float, int] | None = None   # (group angle, ring)

    # -- state ---------------------------------------------------------
    def set_steps(self, small: float, large: float) -> None:
        self._small, self._large = small, large
        self.update()

    def set_angles(self, pan: float | None, tilt: float | None) -> None:
        """Live head angles for the hub, or None when they are not known."""
        self._pan_deg, self._tilt_deg = pan, tilt
        self.update()

    # -- geometry ------------------------------------------------------
    def _metrics(self):
        s = min(self.width(), self.height())
        return self.width() / 2.0, self.height() / 2.0, s / 2.0

    def _arc(self, cx, cy, half, r0, r1, centre_deg) -> QPainterPath:
        a0, a1 = centre_deg - _HALF_SPAN, centre_deg + _HALF_SPAN
        ro, ri = QRectF(cx - half * r1, cy - half * r1, half * r1 * 2, half * r1 * 2), \
                 QRectF(cx - half * r0, cy - half * r0, half * r0 * 2, half * r0 * 2)
        start, span = _qt_angle(a1), (a1 - a0)
        p = QPainterPath()
        p.arcMoveTo(ro, start)
        p.arcTo(ro, start, span)
        p.arcTo(ri, start + span, -span)
        p.closeSubpath()
        return p

    def _groups(self):
        """(centre angle, ring, axis, signed degrees) for all eight arcs."""
        for centre, axis, sign in ((_UP, "tilt", +1), (_DOWN, "tilt", -1),
                                   (_RIGHT, "pan", +1), (_LEFT, "pan", -1)):
            yield centre, 0, axis, sign * self._small
            yield centre, 1, axis, sign * self._large

    # -- painting ------------------------------------------------------
    def paintEvent(self, _event):
        cx, cy, half = self._metrics()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        for centre, ring, axis, deg in self._groups():
            r0, r1 = (_R_IN_0, _R_IN_1) if ring == 0 else (_R_OUT_0, _R_OUT_1)
            line = TILT_LINE if axis == "tilt" else PAN_LINE
            fill = TILT_FILL if axis == "tilt" else PAN_FILL
            if self._lit == (centre, ring):
                fill = TILT_LIT if axis == "tilt" else PAN_LIT
            path = self._arc(cx, cy, half, r0, r1, centre)
            p.setBrush(QColor(fill))
            p.setPen(QPen(QColor(line), max(2.0, half * 0.018)))
            p.drawPath(path)

            # Label on the arc's own centre line.
            rl = (r0 + r1) / 2.0
            t = math.radians(centre)
            lx, ly = cx + half * rl * math.sin(t), cy - half * rl * math.cos(t)
            f = QFont(); f.setPointSizeF(max(8.0, half * 0.085)); f.setBold(True)
            p.setFont(f)
            p.setPen(QColor(line))
            txt = f"{'+' if deg > 0 else '−'}{abs(deg):.0f}°"
            fm = p.fontMetrics()
            p.drawText(int(lx - fm.horizontalAdvance(txt) / 2),
                       int(ly + fm.capHeight() / 2), txt)

        # Hub — the live angles, or dashes until the mount has told us.
        hr = half * _R_HUB
        p.setBrush(QColor(WELL))
        p.setPen(QPen(QColor(WELL_EDGE), max(1.5, half * 0.012)))
        p.drawEllipse(QRectF(cx - hr, cy - hr, hr * 2, hr * 2))

        f = QFont(); f.setPointSizeF(max(7.0, half * 0.072))
        p.setFont(f)
        fm = p.fontMetrics()
        rows = (("TILT", self._tilt_deg, TILT_LINE), ("PAN", self._pan_deg, PAN_LINE))
        for i, (name, val, col) in enumerate(rows):
            txt = f"{name} {val:+.1f}°" if val is not None else f"{name}   —"
            p.setPen(QColor(col))
            y = cy + (i * 2 - 1) * fm.height() * 0.62 + fm.capHeight() / 2
            p.drawText(int(cx - fm.horizontalAdvance(txt) / 2), int(y), txt)
        p.end()

    # -- input ---------------------------------------------------------
    def _hit(self, x, y):
        cx, cy, half = self._metrics()
        dx, dy = x - cx, y - cy
        r = math.hypot(dx, dy) / max(1.0, half)
        if r < _R_IN_0 or r > _R_OUT_1:
            return None
        ring = 0 if r <= _R_IN_1 else (1 if r >= _R_OUT_0 else None)
        if ring is None:
            return None                      # the gap between the two rings
        ang = (math.degrees(math.atan2(dx, -dy))) % 360.0
        for centre, rng, axis, deg in self._groups():
            if rng != ring:
                continue
            d = abs((ang - centre + 180.0) % 360.0 - 180.0)
            if d <= _HALF_SPAN:
                return centre, ring, axis, deg
        return None                          # a diagonal gap: deliberately dead

    def mousePressEvent(self, event):
        hit = self._hit(event.position().x(), event.position().y())
        if hit is None:
            return
        centre, ring, axis, deg = hit
        self._lit = (centre, ring)
        self.update()
        self.nudged.emit(axis, deg)
        QTimer.singleShot(120, self._unlight)

    def _unlight(self):
        self._lit = None
        self.update()


class SliderTrack(QWidget):
    """The rail, drawn as a rail. Four tap zones, plus the live carriage."""

    nudged = pyqtSignal(float)           # signed millimetres

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(76)
        self._small, self._large = 10.0, 100.0
        self._mm: float | None = None
        self._min_mm, self._max_mm = 0.0, 0.0
        self._lit: int | None = None

    def set_steps(self, small: float, large: float) -> None:
        self._small, self._large = small, large
        self.update()

    def set_position(self, mm: float | None, min_mm: float, max_mm: float) -> None:
        self._mm, self._min_mm, self._max_mm = mm, min_mm, max_mm
        self.update()

    def _zones(self):
        """(x0, x1, signed mm, label) across the track, left to right."""
        w, h = self.width(), self.height()
        pad = h * 0.10
        x0, x1 = pad, w - pad
        span = (x1 - x0) / 4.0
        for i, mm in enumerate((-self._large, -self._small,
                                +self._small, +self._large)):
            yield x0 + i * span, x0 + (i + 1) * span, mm, \
                  f"{'+' if mm > 0 else '−'}{abs(mm):.0f}"

    def paintEvent(self, _event):
        w, h = self.width(), self.height()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pad = h * 0.10
        body = QRectF(pad, pad, w - pad * 2, h - pad * 2 - h * 0.20)
        rad = body.height() / 2.0

        # The well, then each zone clipped to the stadium so the end zones
        # take the rounded corners and read as the ends of a rail.
        outer = QPainterPath()
        outer.addRoundedRect(body, rad, rad)
        p.save()
        p.setClipPath(outer)
        p.setPen(Qt.PenStyle.NoPen)
        for i, (zx0, zx1, mm, _lbl) in enumerate(self._zones()):
            p.setBrush(QColor(SL_LIT if self._lit == i else
                              (SL_FILL if i in (0, 3) else "#2C0A40")))
            p.drawRect(QRectF(zx0, body.top(), zx1 - zx0, body.height()))
        p.setPen(QPen(QColor("#160820"), 2.0))
        for zx0, _zx1, _mm, _lbl in list(self._zones())[1:]:
            p.drawLine(int(zx0), int(body.top()), int(zx0), int(body.bottom()))
        p.restore()

        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(SL_LINE), 2.0))
        p.drawPath(outer)

        f = QFont(); f.setPointSizeF(max(8.0, h * 0.20)); f.setBold(True)
        p.setFont(f); fm = p.fontMetrics()
        p.setPen(QColor(SL_LINE))
        for zx0, zx1, _mm, lbl in self._zones():
            p.drawText(int((zx0 + zx1) / 2 - fm.horizontalAdvance(lbl) / 2),
                       int(body.center().y() + fm.capHeight() / 2), lbl)

        # The carriage, only once the mount has said where it is. A guessed
        # position on a rail is worse than no position at all.
        if self._mm is not None and self._max_mm > self._min_mm:
            frac = (self._mm - self._min_mm) / (self._max_mm - self._min_mm)
            frac = min(max(frac, 0.0), 1.0)
            cxp = body.left() + frac * body.width()
            r = body.height() * 0.17
            p.setBrush(QColor("#F3D9F7"))
            p.setPen(QPen(QColor(SL_LINE), 2.0))
            p.drawEllipse(QRectF(cxp - r, body.center().y() - r, r * 2, r * 2))

        f2 = QFont(); f2.setPointSizeF(max(6.5, h * 0.145))
        p.setFont(f2); fm2 = p.fontMetrics()
        p.setPen(QColor("#8A6E93"))
        cap = (f"SLIDER   {self._mm:.0f} mm  /  {self._min_mm:.0f}–{self._max_mm:.0f}"
               if self._mm is not None and self._max_mm > self._min_mm
               else "SLIDER   position not reported yet")
        p.drawText(int(w / 2 - fm2.horizontalAdvance(cap) / 2), int(h - 2), cap)
        p.end()

    def mousePressEvent(self, event):
        x, y = event.position().x(), event.position().y()
        for i, (zx0, zx1, mm, _lbl) in enumerate(self._zones()):
            if zx0 <= x <= zx1:
                self._lit = i
                self.update()
                self.nudged.emit(mm)
                QTimer.singleShot(120, self._unlight)
                return

    def _unlight(self):
        self._lit = None
        self.update()


class ZoomColumn(QWidget):
    """Press-and-hold zoom, in the same rounded idiom as the slider track."""

    started = pyqtSignal(int)            # signed velocity
    stopped = pyqtSignal()

    def __init__(self, fast: int, slow: int, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        # Wide enough that the arrows are a target rather than a hint, and
        # capped in height so it stays a column beside the dial instead of
        # stretching into a strip.
        self.setFixedWidth(96)
        self.setMaximumHeight(360)
        self._vel = (+fast, +slow, -slow, -fast)
        self._lbl = ("▲▲", "▲", "▼", "▼▼")
        self._lit: int | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._repeat)
        self._held: int | None = None

    def sizeHint(self):
        # A real hint, because a layout that is given an ALIGNMENT for this
        # widget sizes it from the hint rather than expanding it — and a bare
        # QWidget has no hint, so it lands at zero height and vanishes without
        # any error at all.
        from PyQt6.QtCore import QSize
        return QSize(96, 300)

    def _zones(self):
        w, h = self.width(), self.height()
        pad = w * 0.06
        y0, y1 = pad, h - pad
        span = (y1 - y0) / 4.0
        for i in range(4):
            yield y0 + i * span, y0 + (i + 1) * span, i

    def paintEvent(self, _event):
        w, h = self.width(), self.height()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pad = w * 0.06
        body = QRectF(pad, pad, w - pad * 2, h - pad * 2)
        rad = body.width() / 2.0
        outer = QPainterPath()
        outer.addRoundedRect(body, rad, rad)
        p.save()
        p.setClipPath(outer)
        p.setPen(Qt.PenStyle.NoPen)
        for zy0, zy1, i in self._zones():
            p.setBrush(QColor(ZOOM_LIT if self._lit == i else
                              (ZOOM_FILL if i in (0, 3) else "#0F2647")))
            p.drawRect(QRectF(body.left(), zy0, body.width(), zy1 - zy0))
        p.restore()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(ZOOM_LINE), 2.0))
        p.drawPath(outer)

        f = QFont(); f.setPointSizeF(max(9.0, w * 0.26)); f.setBold(True)
        p.setFont(f); fm = p.fontMetrics()
        p.setPen(QColor(ZOOM_LINE))
        for zy0, zy1, i in self._zones():
            p.drawText(int(w / 2 - fm.horizontalAdvance(self._lbl[i]) / 2),
                       int((zy0 + zy1) / 2 + fm.capHeight() / 2), self._lbl[i])
        p.end()

    def _repeat(self):
        if self._held is not None:
            self.started.emit(self._vel[self._held])

    def mousePressEvent(self, event):
        y = event.position().y()
        for zy0, zy1, i in self._zones():
            if zy0 <= y <= zy1:
                self._held = self._lit = i
                self.update()
                self.started.emit(self._vel[i])
                self._timer.start()
                return

    def mouseReleaseEvent(self, _event):
        self._timer.stop()
        self._held = self._lit = None
        self.update()
        self.stopped.emit()
