"""SliderTravelAnim — an eight-frame sketch of a carriage running its rail.

Shared by the dialogs that drive a slider and then wait: subject calibration
(travel to each end to take the two observations) and Find Limits (drive to
the stops).  Both are "the mount is moving, please wait" situations where the
firmware reports no position, so the picture deliberately shows motion without
implying progress.
"""
from __future__ import annotations

from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import Qt, QTimer, QRectF, QPointF
from PyQt6.QtGui import QPainter, QPen, QBrush, QColor


class SliderTravelAnim(QWidget):
    """Eight-frame sketch of the carriage running its rail, at 2 fps.

    Deliberately steppy rather than smooth.  The mount reports no position
    during calibration, so this is a "something is happening, please wait"
    indicator and nothing more — a smooth glide would imply a precision we
    don't have, and would look like a real position readout when it isn't.

    Between moves it parks the carriage at whichever end the slider is
    actually sitting at, so the picture still tells the truth while idle.
    """

    FRAMES   = 8
    FRAME_MS = 500          # 2 fps
    CARRIAGE_W = 18.0       # widest part; the travel inset is half of this

    _RAIL   = QColor("#37474F")
    _CARR   = QColor("#4FC3F7")   # same accent as the moving marker in Config
    _IDLE   = QColor("#546E7A")
    _BG     = QColor("#121212")   # matches the app's Window colour

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(48)
        # _frame is an ABSOLUTE position: 0 = home (left) end,
        # FRAMES-1 = far (right) end.  _step is the direction of travel.
        self._frame  = 0
        self._step   = 1
        self._moving = False
        self._timer  = QTimer(self)
        self._timer.timeout.connect(self._tick)

    # ── control ─────────────────────────────────────────────────────
    def start(self, forward: bool) -> None:
        """Begin travelling; `forward` sets the initial direction.

        Starts from the end the slider is leaving, so the first frames head
        the same way the real carriage does.
        """
        self._frame  = 0 if forward else self.FRAMES - 1
        self._step   = 1 if forward else -1
        self._moving = True
        self._timer.start(self.FRAME_MS)
        self.update()

    def park(self, at_far_end: bool | None = None) -> None:
        """Stop the animation.  Passing an end parks the carriage there;
        omitting it leaves the carriage wherever the last frame put it."""
        self._timer.stop()
        self._moving = False
        if at_far_end is not None:
            self._frame = self.FRAMES - 1 if at_far_end else 0
        self.update()

    def _tick(self) -> None:
        """Bounce between the ends rather than wrapping.

        Wrapping made the carriage reach the far stop and teleport back to the
        start, which reads as a glitch.  Reversing at each end keeps it
        travelling continuously — and since we don't know how long the real
        move takes, it simply keeps shuttling until the mount reports arrival
        and the prompt changes.
        """
        self._frame += self._step
        if self._frame >= self.FRAMES - 1:
            self._frame, self._step = self.FRAMES - 1, -1
        elif self._frame <= 0:
            self._frame, self._step = 0, 1
        self.update()

    # ── painting ────────────────────────────────────────────────────
    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        # Paint our own background rather than inheriting one.  The dialog's
        # Window colour is already #121212, so this changes nothing in the app
        # — but it makes the panel independent of the palette, of whatever it
        # is parented to, and of a restyle later on.  Without it the widget
        # draws onto whatever is behind, which is how a preview of this ended
        # up on Qt's default light grey.
        p.fillRect(0, 0, w, h, self._BG)
        # Rail spans the middle half of the widget, centred.  A full-width rail
        # made each of the eight steps ~60 px, which read as jerky; over half
        # the distance the same eight frames step ~29 px and settle down.
        rail_w = w * 0.5
        x0     = (w - rail_w) / 2.0
        x1     = x0 + rail_w
        rail_y = h - 12

        # Rail: dotted line with an end stop at each end.
        pen = QPen(self._RAIL, 2, Qt.PenStyle.DotLine)
        p.setPen(pen)
        p.drawLine(QPointF(x0, rail_y), QPointF(x1, rail_y))
        p.setPen(QPen(self._RAIL, 3))
        p.drawLine(QPointF(x0, rail_y - 7), QPointF(x0, rail_y + 5))
        p.drawLine(QPointF(x1, rail_y - 7), QPointF(x1, rail_y + 5))

        # Carriage position.  _frame is absolute — 0 is the home end,
        # FRAMES-1 the far end — so direction lives entirely in _step and this
        # needs no mirroring.
        #
        # Travel is inset by half the carriage so its SIDES come to rest
        # against the end stops, never across or past them — the centre
        # travelling the full rail let an 18 px block overhang each stop by 9,
        # which is exactly what a real carriage cannot do.
        half   = self.CARRIAGE_W / 2.0
        t0, t1 = x0 + half, x1 - half
        t  = self._frame / (self.FRAMES - 1)
        cx = t0 + t * (t1 - t0)

        col = self._CARR if self._moving else self._IDLE
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(col))
        # carriage block on the rail
        p.drawRoundedRect(QRectF(cx - self.CARRIAGE_W / 2, rail_y - 9,
                                 self.CARRIAGE_W, 8), 2, 2)
        # camera body + lens, so it reads as a mount rather than a dot
        p.drawRoundedRect(QRectF(cx - 7, rail_y - 20, 14, 10), 2, 2)
        p.setBrush(QBrush(self._BG))
        p.drawEllipse(QRectF(cx - 3, rail_y - 18, 6, 6))
        p.end()
