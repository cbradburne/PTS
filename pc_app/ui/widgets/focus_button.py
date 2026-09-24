"""
FocusButton — the crosshair between a camera row's tenth position and its dials.

One tap fires an instantaneous autofocus on that mount's camera: the same
command as Auto Focus in Camera Control, without opening it.

Drawn, not loaded: a red ring with four ticks pointing in from 12, 3, 6 and 9
o'clock.  Grey and untappable while the camera cannot take the command, which
is how the slider dial beside it shows a mount with no slider.

After a tap it flashes green.  That confirms the command was SENT and nothing
more — the Blackmagic protocol has no acknowledgement, so the only real
confirmation is the lens visibly hunting.
"""
from __future__ import annotations

from PyQt6.QtWidgets import QAbstractButton
from PyQt6.QtCore import Qt, QTimer, QEvent, QPointF, QSize
from PyQt6.QtGui import QPainter, QPen, QColor

READY_COLOUR = QColor("#F44336")   # the grid's red
SENT_COLOUR  = QColor("#4CAF50")   # the grid's green
OFF_COLOUR   = QColor("#555555")   # the dials' disabled grey

# As long as Camera Control's Auto Focus shows it fired.
_FLASH_MS = 400

# Proportions of the reference icon: stroke 8% of the button, ticks ending 40%
# of the way from the centre to the ring.
_STROKE_FRAC = 0.08
_TICK_INNER  = 0.40


class FocusButton(QAbstractButton):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sent = False
        self._flash = QTimer(self)
        self._flash.setSingleShot(True)
        self._flash.setInterval(_FLASH_MS)
        self._flash.timeout.connect(self._end_flash)
        self.clicked.connect(self._start_flash)
        # A floor, not a size: the grid sets the maximum from the row height,
        # as it does for the dials, so the button scales with the screen.
        self.setMinimumSize(28, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Auto focus")

    def sizeHint(self) -> QSize:
        return QSize(48, 48)

    def _start_flash(self) -> None:
        self._sent = True
        self._flash.start()
        self.update()

    def _end_flash(self) -> None:
        self._sent = False
        self.update()

    def changeEvent(self, event) -> None:
        # Repaint on enable/disable so the grey look applies at once.
        if event.type() == QEvent.Type.EnabledChange:
            self.update()
        super().changeEvent(event)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        size   = min(self.width(), self.height())
        stroke = max(2.0, size * _STROKE_FRAC)
        r      = size / 2 - stroke / 2 - 1
        c      = QPointF(self.width() / 2, self.height() / 2)

        if not self.isEnabled():
            colour = OFF_COLOUR
        elif self._sent:
            colour = SENT_COLOUR
        else:
            colour = READY_COLOUR

        # Filled while a finger is on it and while it shows the command went,
        # so a touch screen gets feedback before the release.
        if self.isEnabled() and (self.isDown() or self._sent):
            fill = QColor(colour)
            fill.setAlpha(70)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fill)
            p.drawEllipse(c, r, r)

        p.setPen(QPen(colour, stroke, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(c, r, r)
        inner = r * _TICK_INNER
        for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
            p.drawLine(QPointF(c.x() + dx * r,     c.y() + dy * r),
                       QPointF(c.x() + dx * inner, c.y() + dy * inner))
