"""
SpeedDial — 4-level speed selector widget.

Displays 4 segments like a signal-strength bar.
Tap/click any segment to select that preset level.
Emits preset_changed(int) when level changes.
"""
from __future__ import annotations

from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import pyqtSignal, Qt, QRect
from PyQt6.QtGui import QPainter, QColor, QPen


ACTIVE_COLOUR   = QColor("#4FC3F7")   # light blue — active segment
INACTIVE_COLOUR = QColor("#2A2A2A")   # dark — inactive segment
BORDER_COLOUR   = QColor("#555555")
LABEL_COLOUR    = QColor("#FFFFFF")


class SpeedDial(QWidget):
    """
    4-bar speed level selector.

    preset_changed emits the new preset (1-4).
    """

    preset_changed = pyqtSignal(int)

    def __init__(self, label: str = "", parent=None):
        super().__init__(parent)
        self._preset = 2        # 1-4
        self._label  = label
        self.setMinimumSize(100, 48)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    @property
    def preset(self) -> int:
        return self._preset

    @preset.setter
    def preset(self, value: int) -> None:
        value = max(1, min(4, value))
        if value != self._preset:
            self._preset = value
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        bar_w   = (w - 5 * 4) // 4   # 4 bars with 4px gaps, 4px margin each side
        gap     = 4
        margin  = (w - 4 * bar_w - 3 * gap) // 2
        max_h   = h - 24             # leave room for label below

        for i in range(4):
            level    = i + 1
            bar_h    = int(max_h * (0.4 + 0.2 * i))   # growing heights
            x        = margin + i * (bar_w + gap)
            y        = max_h - bar_h + 4

            colour = ACTIVE_COLOUR if level <= self._preset else INACTIVE_COLOUR
            painter.setBrush(colour)
            painter.setPen(QPen(BORDER_COLOUR, 1))
            painter.drawRoundedRect(QRect(x, y, bar_w, bar_h), 2, 2)

        # Label
        painter.setPen(LABEL_COLOUR)
        font = painter.font()
        font.setPointSize(9)
        painter.setFont(font)
        painter.drawText(QRect(0, h - 18, w, 18),
                         Qt.AlignmentFlag.AlignCenter, self._label)

    def mousePressEvent(self, event):
        w       = self.width()
        margin  = 4
        bar_w   = (w - 5 * margin) // 4
        gap     = margin
        usable  = margin

        x = event.position().x()
        for i in range(4):
            left  = margin + i * (bar_w + gap)
            right = left + bar_w
            if left <= x <= right:
                new_preset = i + 1
                if new_preset != self._preset:
                    self._preset = new_preset
                    self.update()
                    self.preset_changed.emit(self._preset)
                return
