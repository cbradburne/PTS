"""
RotaryDial — circular knob widget with 4 detent positions.

Visual: dark circular knob, coloured arc track, indicator line.
Interaction: tap anywhere to advance preset 1→2→3→4→1.
             right-click / long-press to go backwards.

The arc sweeps from ~7 o'clock to ~5 o'clock (300° range).
The 4 presets are at equal intervals along that arc.
"""
from __future__ import annotations

import math
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import pyqtSignal, Qt, QPoint, QTimer, QEvent
from PyQt6.QtGui import QPainter, QColor, QPen, QBrush, QFont


# Arc extents in degrees, measured clockwise from 12 o'clock
ARC_START  = 210   # 7 o'clock
ARC_END    = 150   # 5 o'clock  (going clockwise 300° from start)
ARC_SPAN   = 300   # total sweep

KNOB_COLOUR    = QColor("#2A2A2A")
TRACK_COLOUR   = QColor("#444444")
ACTIVE_COLOUR  = QColor("#4FC3F7")
INDICATOR_COL  = QColor("#FFFFFF")


def _angle_to_xy(cx: float, cy: float, r: float, deg_cw_from_12: float):
    """Convert clockwise-from-12 angle to (x, y) on a circle."""
    rad = math.radians(deg_cw_from_12)
    return cx + r * math.sin(rad), cy - r * math.cos(rad)


class RotaryDial(QWidget):
    """
    4-preset rotary knob. Emits preset_changed(int) when value changes.
    preset is 1–4 (1 = slowest, 4 = fastest).
    """

    preset_changed = pyqtSignal(int)

    def __init__(self, accent_colour: QColor = ACTIVE_COLOUR, parent=None):
        super().__init__(parent)
        self._preset  = 0          # 0 = disconnected/unknown, 1-4 = active
        self._accent  = accent_colour
        self._pressed = False
        # A floor, not a size.  72 was large enough to set the whole grid's
        # minimum height once the dials started scaling with the row, which
        # stopped a window shrinking after it had been shown on a big screen.
        self.setMinimumSize(48, 48)
        self.setMaximumSize(90, 90)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    @property
    def preset(self) -> int:
        return self._preset

    @preset.setter
    def preset(self, value: int) -> None:
        value = max(0, min(4, value))
        if value != self._preset:
            self._preset = value
            self.update()

    # ------------------------------------------------------------------
    # Paint
    # ------------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Disabled (e.g. the slider dial on a mount with no slider) renders
        # exactly like a disconnected dial: preset 0 — grey track, no fill, no
        # indicator line, "–" in the centre.  Qt also blocks mouse events on a
        # disabled widget, so it can't be tapped.  _preset is left untouched so
        # the real value returns if the dial is ever re-enabled.
        preset = 0 if not self.isEnabled() else self._preset

        w, h   = self.width(), self.height()
        size   = min(w, h)
        cx, cy = w / 2, h / 2
        outer_r = size / 2 - 4
        inner_r = outer_r * 0.62
        track_w = outer_r - inner_r

        # --- Background track arc (full span) ---
        painter.setPen(QPen(TRACK_COLOUR, track_w, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.FlatCap))
        self._draw_arc(painter, cx, cy, (inner_r + outer_r) / 2,
                       ARC_START, ARC_SPAN)

        # --- Active track arc (up to current preset) ---
        # 4 equal segments; preset=1 fills the first segment so it never looks zero.
        # preset=0 (disconnected / no slider) → no fill drawn.
        active_span = preset * ARC_SPAN / 4
        if active_span > 0:
            painter.setPen(QPen(self._accent, track_w, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.FlatCap))
            self._draw_arc(painter, cx, cy, (inner_r + outer_r) / 2,
                           ARC_START, active_span)

        # --- Detent dots (5 dots = 4 equal segments) ---
        # No dots lit when preset=0 (disconnected / no slider).
        for i in range(5):
            dot_angle = ARC_START + i * ARC_SPAN / 4
            dx, dy    = _angle_to_xy(cx, cy, outer_r + 4, dot_angle)
            col = self._accent if (preset > 0 and i <= preset) else QColor("#555")
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(col)
            painter.drawEllipse(int(dx - 3), int(dy - 3), 6, 6)

        # --- Knob circle ---
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(KNOB_COLOUR)
        painter.drawEllipse(int(cx - inner_r), int(cy - inner_r),
                            int(inner_r * 2), int(inner_r * 2))

        # --- Indicator line (hidden when disconnected / no slider) ---
        if preset > 0:
            ind_angle = ARC_START + preset * ARC_SPAN / 4
            ix, iy    = _angle_to_xy(cx, cy, inner_r * 0.8, ind_angle)
            painter.setPen(QPen(INDICATOR_COL, 2.5, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap))
            painter.drawLine(int(cx), int(cy), int(ix), int(iy))

        # --- Preset number in centre ("–" when disconnected / no slider) ---
        painter.setPen(self._accent if preset > 0 else QColor("#555"))
        font = QFont()
        font.setPointSize(10)
        font.setBold(True)
        painter.setFont(font)
        label = str(preset) if preset > 0 else "–"
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, label)

    def _draw_arc(self, painter: QPainter, cx: float, cy: float,
                  r: float, start_cw12: float, span_cw: float) -> None:
        """Draw an arc. Angles in degrees clockwise from 12 o'clock."""
        # QPainter.drawArc uses 16ths of a degree, 0=3 o'clock, CCW positive
        qt_start = int((90 - start_cw12) * 16)
        qt_span  = int(-span_cw * 16)    # negative = clockwise in Qt
        margin = int(cx - r)
        size   = int(r * 2)
        painter.drawArc(margin, int(cy - r), size, size, qt_start, qt_span)

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------

    def changeEvent(self, event):
        # Repaint when enabled/disabled toggles so the greyed (preset-0) look
        # applies immediately.
        if event.type() == QEvent.Type.EnabledChange:
            self.update()
        super().changeEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._advance(+1)
        elif event.button() == Qt.MouseButton.RightButton:
            self._advance(-1)

    def _advance(self, direction: int) -> None:
        new_preset = (self._preset - 1 + direction) % 4 + 1
        # Don't update self._preset here — the dial only moves when the mount
        # confirms the change via a STATUS packet (active_pt/sl_preset field).
        # That drives set_pt_preset / set_sl_preset → dial.preset setter → repaint.
        self.preset_changed.emit(new_preset)
