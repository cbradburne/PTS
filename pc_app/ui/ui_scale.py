"""One scale number, shared by everything in the UI that is drawn in pixels.

The app was drawn against a 1920x1080 screen. Anything expressed as a raw
pixel count is therefore right on that display and progressively smaller on
anything with more logical pixels — HiDPI is not the variable, since Qt reports
logical pixels either way; the number of them is.

Three things now read from here:

  PositionGrid    scales from the height it is given, and needs nothing from
                  this module — it is the pattern the other two follow.
  the top and
  bottom bars     forty stylesheets, scaled through qss() at the STYLE HELPERS
                  rather than at each setStyleSheet() call, because those run
                  again on every state change and one missed would resize a
                  button the moment it was pressed.
  the Move panel  sized from px() and then scaled internally from its own side,
                  the same way the grid works.

It lives in its own module because the Move panel needs the same number the
bars use, and importing main_window from a widget it builds is a cycle. A
second copy of the arithmetic would have been the easier change and the wrong
one: on this rig every duplicated implementation has eventually diverged.

Read once at build time rather than on resize — the app runs full screen, so
the window does not change size, and re-applying forty stylesheets to handle a
case that cannot happen is cost without benefit.
"""
from __future__ import annotations

import functools
import re

from PyQt6.QtWidgets import QApplication

REF_H = 1080.0

# Clamped: a phone-sized or wall-sized display should still be usable rather
# than faithfully proportioned into uselessness.
MIN_SCALE = 0.65
MAX_SCALE = 3.0

_SCALE = 1.0


def init_ui_scale(height: int | None = None) -> float:
    """Set the scale from the screen, or from an explicit height for tests."""
    global _SCALE
    if height is None:
        scr = QApplication.primaryScreen()
        if scr is None:
            return _SCALE
        height = scr.geometry().height()
    _SCALE = max(MIN_SCALE, min(MAX_SCALE, height / REF_H))
    return _SCALE


def scale() -> float:
    """The current scale. A function, not a global anyone can import a stale
    copy of — `from ui_scale import _SCALE` would freeze at whatever it was."""
    return _SCALE


def px(n: float) -> int:
    return max(1, round(n * _SCALE))


def qss(sheet: str) -> str:
    """Scale every pixel length in a stylesheet."""
    return re.sub(r"(\d+)px", lambda m: f"{px(int(m.group(1)))}px", sheet)


def scaled_style(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return qss(fn(*args, **kwargs))
    return wrapper
