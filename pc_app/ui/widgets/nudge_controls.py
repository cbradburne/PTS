"""
Custom controls for the Move panel: a split-arc dial for pan/tilt, a stadium
track for the slider, and a matching column for zoom.

The panel used to be a cross of square buttons — tilt in a column, pan in a
row, slider along the bottom. Everything worked, but the shape carried no
meaning: nothing about a grid of squares says which of them turns the head
which way.

  RadialNudge   pan and tilt as four separated arc groups around a hub.
                Up and down are tilt, left and right are pan; the step grows
                with the radius — fine nearest the hub, then small, then large
                on the outside. The gaps at the diagonals are the point: a slip
                near one lands on nothing rather than on the wrong axis, and
                nudging pan when you meant tilt is a shot on air going the
                wrong way.

                The hub carries a legend naming which way each axis lies —
                the one question a round control has to answer that a cross of
                labelled squares answered by being a cross.

  SliderTrack   the slider as a rail rather than a row of buttons. Four tap
                zones for the discrete steps, and a run zone at each end that
                sends the carriage all the way to that limit. No carriage
                drawn: the mount can report where it is, but drawing it would
                need the rail's length as well, and the operator asked for the
                panel without a readout.

                The run zones are a different colour and a different shape from
                the nudges, because they are a different kind of action — one
                is a step, the other is the whole rail — and they go grey
                whenever the mount has no calibrated limits to run to.

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
RUN_LINE  = "#F48FB1"; RUN_FILL  = "#6A1040"; RUN_LIT  = "#A01860"
DEAD_LINE = "#5A5560"; DEAD_FILL = "#241F28"
WELL      = "#0E1518"; WELL_EDGE = "#2A363C"

# Where the four arc groups sit, clockwise from twelve.
_UP, _RIGHT, _DOWN, _LEFT = 0.0, 90.0, 180.0, 270.0

# Half-width of each group, and the gap that separates them. 45 - 32 = 13
# degrees of dead space either side of every diagonal.
_HALF_SPAN = 32.0

# Radii as a fraction of the dial's half-size, so the whole control scales.
# Three rings now, innermost first: the step grows with the radius, which is
# the only ordering that needs no explaining. Equal widths and equal gaps, and
# the outer edge stays where it was — the slider track is matched to it.
_R_HUB = 0.170
_RINGS = ((0.215, 0.413),      # fine
          (0.468, 0.667),      # small
          (0.722, 0.920))      # large
_R_OUT_1 = _RINGS[-1][1]       # the dial's drawn diameter, in half-sizes


def _qt_angle(clock_deg: float) -> float:
    """Degrees clockwise from twelve -> Qt's counter-clockwise from three."""
    return 90.0 - clock_deg


def _deg_label(v: float) -> str:
    """+10°, +1°, +0.2° — no trailing zeros, and a real minus sign."""
    return f"{'+' if v > 0 else '−'}{abs(v):g}°"


class RadialNudge(QWidget):
    """Pan and tilt as four separated arc groups. Emits (axis, degrees)."""

    nudged = pyqtSignal(str, float)      # "pan" | "tilt", signed degrees

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self.setMinimumSize(240, 240)
        self._steps = (0.2, 1.0, 10.0)               # one per ring, inner first
        self._lit: tuple[float, int] | None = None   # (group angle, ring)

    # -- state ---------------------------------------------------------
    def set_steps(self, fine: float, small: float, large: float) -> None:
        self._steps = (fine, small, large)
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
        """(centre angle, ring, axis, signed degrees) for all twelve arcs."""
        for centre, axis, sign in ((_UP, "tilt", +1), (_DOWN, "tilt", -1),
                                   (_RIGHT, "pan", +1), (_LEFT, "pan", -1)):
            for ring, step in enumerate(self._steps):
                yield centre, ring, axis, sign * step

    def _label_font(self, p, half) -> QFont:
        """One size for all twelve labels: the largest that fits the worst arc.

        Labels are horizontal, so which way an arc constrains them depends on
        where it sits. Across the top and bottom the text runs along the arc
        and the CHORD is the limit; out to the left and right the same
        horizontal text runs straight through the ring, so the ring's WIDTH is.
        The narrow one is a third of the wide one, and sizing every arc by its
        chord is what put "−0.2°" through the side of its ring and into its
        neighbour.

        Twelve labels at twelve sizes would fit better still and look like an
        accident, so the worst case sets the size for all of them. Measured
        rather than derived, so it holds for whatever steps a mount is
        configured with — 0.2 is only the default.
        """
        base = max(8.0, half * 0.085)
        f = QFont(); f.setPointSizeF(base); f.setBold(True)
        p.setFont(f)
        fm = p.fontMetrics()
        best = base
        for _centre, ring, axis, deg in self._groups():
            r0, r1 = _RINGS[ring]
            if axis == "tilt":
                room = 2.0 * half * ((r0 + r1) / 2.0) \
                       * math.sin(math.radians(_HALF_SPAN)) * 0.72
            else:
                room = half * (r1 - r0) * 0.82
            w = fm.horizontalAdvance(_deg_label(deg))
            if w > room:
                # Every ratio is against the SAME measured width, so the
                # smallest wins outright — shrinking as we go would compound
                # each ratio into the next and end up far too small.
                best = min(best, base * room / w)
        f.setPointSizeF(max(6.0, best))
        return f

    # -- painting ------------------------------------------------------
    def paintEvent(self, _event):
        cx, cy, half = self._metrics()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        label_font = self._label_font(p, half)

        for centre, ring, axis, deg in self._groups():
            r0, r1 = _RINGS[ring]
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
            txt = _deg_label(deg)
            p.setFont(label_font)
            p.setPen(QColor(line))
            fm = p.fontMetrics()
            p.drawText(int(lx - fm.horizontalAdvance(txt) / 2),
                       int(ly + fm.capHeight() / 2), txt)

        # Hub — a legend, not a readout. It names which way each axis lies,
        # which is the thing a round control has to answer and a cross of
        # labelled squares answered by itself.
        hr = half * _R_HUB
        p.setBrush(QColor(WELL))
        p.setPen(QPen(QColor(WELL_EDGE), max(1.5, half * 0.012)))
        p.drawEllipse(QRectF(cx - hr, cy - hr, hr * 2, hr * 2))

        f = QFont(); f.setPointSizeF(max(7.0, half * 0.052)); f.setBold(True)
        p.setFont(f)
        fm = p.fontMetrics()
        for i, (name, col) in enumerate((("TILT", TILT_LINE), ("PAN", PAN_LINE))):
            p.setPen(QColor(col))
            y = cy + (i * 2 - 1) * fm.height() * 0.58 + fm.capHeight() / 2
            p.drawText(int(cx - fm.horizontalAdvance(name) / 2), int(y), name)
        p.end()

    # -- input ---------------------------------------------------------
    def _hit(self, x, y):
        cx, cy, half = self._metrics()
        dx, dy = x - cx, y - cy
        r = math.hypot(dx, dy) / max(1.0, half)
        ring = next((i for i, (r0, r1) in enumerate(_RINGS) if r0 <= r <= r1),
                    None)
        if ring is None:
            return None                      # hub, a gap between rings, or outside
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


# The end-run zones, as a share of one nudge zone. Narrower because the label
# is two characters against four, and because a smaller target is the right
# shape for the one control here that moves the whole rail.
_RUN_W = 0.75


class SliderTrack(QWidget):
    """The rail, drawn as a rail. Four nudge zones between two run-to-end zones."""

    nudged = pyqtSignal(float)           # signed millimetres
    run    = pyqtSignal(int)             # -1 = run to the left end, +1 = right

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(76)
        self._small, self._large = 10.0, 100.0
        self._runs_enabled = True
        self._lit: int | None = None

    def set_steps(self, small: float, large: float) -> None:
        self._small, self._large = small, large
        self.update()

    def set_runs_enabled(self, enabled: bool) -> None:
        """Grey the run zones out when the mount has no limits to run to.

        A run is a relative move long enough to overshoot the rail, stopped by
        the Teensy's own clamp. An uncalibrated slider has no clamp, so the
        control has to say so rather than send a move that would not stop.
        """
        if enabled != self._runs_enabled:
            self._runs_enabled = enabled
            self.update()

    def _zones(self):
        """(x0, x1, kind, value, label) across the track, left to right.

        kind is "run" (value -1 / +1) or "nudge" (value in signed mm).
        """
        w, h = self.width(), self.height()
        pad = h * 0.10
        x0, x1 = pad, w - pad
        unit = (x1 - x0) / (4.0 + 2.0 * _RUN_W)
        spec = [("run", -1, "<<", _RUN_W)]
        spec += [("nudge", mm, f"{'+' if mm > 0 else '−'}{abs(mm):g}", 1.0)
                 for mm in (-self._large, -self._small,
                            +self._small, +self._large)]
        spec += [("run", +1, ">>", _RUN_W)]
        x = x0
        for kind, value, label, width in spec:
            yield x, x + unit * width, kind, value, label
            x += unit * width

    def _colours(self, i, kind):
        """(fill, text) for one zone."""
        if kind == "run":
            if not self._runs_enabled:
                return DEAD_FILL, DEAD_LINE
            return (RUN_LIT if self._lit == i else RUN_FILL), RUN_LINE
        if self._lit == i:
            return SL_LIT, SL_LINE
        return (SL_FILL if i in (1, 4) else "#2C0A40"), SL_LINE

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
        zones = list(self._zones())
        p.save()
        p.setClipPath(outer)
        p.setPen(Qt.PenStyle.NoPen)
        for i, (zx0, zx1, kind, _v, _lbl) in enumerate(zones):
            p.setBrush(QColor(self._colours(i, kind)[0]))
            p.drawRect(QRectF(zx0, body.top(), zx1 - zx0, body.height()))
        p.setPen(QPen(QColor("#160820"), 2.0))
        for zx0, _zx1, _k, _v, _lbl in zones[1:]:
            p.drawLine(int(zx0), int(body.top()), int(zx0), int(body.bottom()))
        p.restore()

        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(SL_LINE), 2.0))
        p.drawPath(outer)

        f = QFont(); f.setPointSizeF(max(8.0, h * 0.20)); f.setBold(True)
        p.setFont(f); fm = p.fontMetrics()
        for i, (zx0, zx1, kind, _v, lbl) in enumerate(zones):
            p.setPen(QColor(self._colours(i, kind)[1]))
            p.drawText(int((zx0 + zx1) / 2 - fm.horizontalAdvance(lbl) / 2),
                       int(body.center().y() + fm.capHeight() / 2), lbl)

        f2 = QFont(); f2.setPointSizeF(max(6.5, h * 0.145))
        p.setFont(f2); fm2 = p.fontMetrics()
        p.setPen(QColor("#8A6E93"))
        p.drawText(int(w / 2 - fm2.horizontalAdvance("SLIDER") / 2), int(h - 2), "SLIDER")
        p.end()

    def mousePressEvent(self, event):
        x = event.position().x()
        for i, (zx0, zx1, kind, value, _lbl) in enumerate(self._zones()):
            if zx0 <= x <= zx1:
                if kind == "run" and not self._runs_enabled:
                    return                   # drawn dead, so behave dead
                self._lit = i
                self.update()
                if kind == "run":
                    self.run.emit(int(value))
                else:
                    self.nudged.emit(value)
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
        # stretching into a strip. Both are set again by the panel's
        # _apply_scale() — see set_metrics().
        self._w, self._h = 96, 300
        self.setFixedWidth(self._w)
        self.setMaximumHeight(self._h)
        self._vel = (+fast, +slow, -slow, -fast)
        self._lbl = ("▲▲", "▲", "▼", "▼▼")
        self._lit: int | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._repeat)
        self._held: int | None = None

    def set_metrics(self, width: int, height: int) -> None:
        """Resize the column with the panel it sits in.

        The maximum has to move with the hint, not stay where it was: a hint
        that grows past a stale maximum is silently ignored, and the column
        would stop growing while everything around it kept going.
        """
        self._w, self._h = int(width), int(height)
        self.setFixedWidth(self._w)
        self.setMaximumHeight(self._h)
        self.updateGeometry()
        self.update()

    def sizeHint(self):
        # A real hint, because a layout that is given an ALIGNMENT for this
        # widget sizes it from the hint rather than expanding it — and a bare
        # QWidget has no hint, so it lands at zero height and vanishes without
        # any error at all.
        from PyQt6.QtCore import QSize
        return QSize(self._w, self._h)

    def _caps(self):
        """Height reserved for the IN / OUT captions at each end."""
        return max(15.0, self.height() * 0.055)

    def _body(self):
        w, h = self.width(), self.height()
        pad, cap = w * 0.06, self._caps()
        return QRectF(pad, cap, w - pad * 2, h - cap * 2)

    def _zones(self):
        b = self._body()
        span = b.height() / 4.0
        for i in range(4):
            yield b.top() + i * span, b.top() + (i + 1) * span, i

    def paintEvent(self, _event):
        w, h = self.width(), self.height()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = self._body()
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

        # Which way is which. The arrows say the direction but not the axis,
        # and up-is-in is a convention rather than something the control tells
        # you — worth a word at each end rather than a moment's doubt on air.
        cf = QFont(); cf.setPointSizeF(max(6.5, w * 0.105)); cf.setBold(True)
        p.setFont(cf); cfm = p.fontMetrics()
        p.setPen(QColor("#5E7FA6"))
        for txt, y in (("ZOOM IN", body.top() - self._caps() * 0.30),
                       ("ZOOM OUT", body.bottom() + self._caps() * 0.80)):
            p.drawText(int(w / 2 - cfm.horizontalAdvance(txt) / 2), int(y), txt)
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
