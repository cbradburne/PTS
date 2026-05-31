"""
Xbox-compatible joystick input handler.

Axis mapping:
  Left stick  X  →  Slider  (left/right)
  Left stick  Y  →  Zoom    (up=zoom in, down=zoom out)
  Right stick X  →  Pan     (left/right)
  Right stick Y  →  Tilt    (up/down)

Pygame axis indices for a standard Xbox controller:
  0 = Left stick X,  1 = Left stick Y
  2 = Right stick X, 3 = Right stick Y

Outputs normalised float values in [-1.0, 1.0] per axis.
A deadzone is applied to prevent drift.

Runs on the Qt main thread via a QTimer — pygame event pump is called
from poll() which the main window drives at ~60Hz.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

import pygame

log = logging.getLogger(__name__)

# Pygame axis indices
_AXIS_LEFT_X  = 0   # Slider
_AXIS_LEFT_Y  = 1   # Zoom
_AXIS_RIGHT_X = 2   # Pan
_AXIS_RIGHT_Y = 3   # Tilt

DEFAULT_DEADZONE  = 0.08
# EMA coefficient for axis smoothing.  Higher = more responsive, less filtering.
# 0.6 at 20 Hz → ~83 ms time constant — removes ADC noise without dulling feel.
_SMOOTH_ALPHA     = 0.6


@dataclass
class JoystickAxes:
    """Normalised axis values in [-1.0, 1.0]. Zero = centred/no input."""
    pan:    float = 0.0
    tilt:   float = 0.0
    slider: float = 0.0
    zoom:   float = 0.0

    def is_zero(self) -> bool:
        return self.pan == 0.0 and self.tilt == 0.0 \
               and self.slider == 0.0 and self.zoom == 0.0

    def to_int(self, scale: int = 1000) -> tuple[int, int, int, int]:
        """Scale to integers for protocol encoding."""
        return (
            int(self.pan    * scale),
            int(self.tilt   * scale),
            int(self.slider * scale),
            int(self.zoom   * scale),
        )


def circ_to_sq(x: int, y: int) -> tuple[int, int]:
    """
    Circular-to-square mapping for a joystick axis pair.

    A physical (or virtual) stick constrained to a circle reports
    x = y = 0.707 at 45° full deflection.  This remaps so that at full
    deflection in any direction the largest component reaches ±scale, and
    both components hit ±scale simultaneously at exactly 45°.

    Magnitude is preserved: half-deflection at any angle gives half speed.
    Zero inputs are returned unchanged.
    """
    if x == 0 and y == 0:
        return x, y
    mag   = math.hypot(x, y)
    max_c = max(abs(x), abs(y))
    return round(x / max_c * mag), round(y / max_c * mag)


class JoystickHandler:
    """
    Manages pygame joystick initialisation and axis reading.

    Call poll() regularly to pump the pygame event queue.
    Read .axes for the latest values.
    """

    def __init__(self, deadzone: float = DEFAULT_DEADZONE):
        self._deadzone    = deadzone
        self._joystick: Optional[pygame.joystick.Joystick] = None
        self.axes         = JoystickAxes()
        self._smoothed    = JoystickAxes()   # EMA state
        self._initialised = False

    def init(self) -> bool:
        """Initialise pygame and connect to the first available joystick."""
        if not self._initialised:
            pygame.init()
            pygame.joystick.init()
            self._initialised = True

        count = pygame.joystick.get_count()
        if count == 0:
            log.warning("No joystick detected")
            return False

        if self._joystick is None:
            self._joystick = pygame.joystick.Joystick(0)
            self._joystick.init()
            log.info(f"Joystick: {self._joystick.get_name()}")
        return True

    def poll(self) -> bool:
        """
        Pump pygame events and update self.axes.
        Returns True if axes changed since last poll.
        """
        if self._joystick is None:
            self.init()
            return False

        pygame.event.pump()

        try:
            raw_lx = self._joystick.get_axis(_AXIS_LEFT_X)
            raw_ly = self._joystick.get_axis(_AXIS_LEFT_Y)
            raw_rx = self._joystick.get_axis(_AXIS_RIGHT_X)
            raw_ry = self._joystick.get_axis(_AXIS_RIGHT_Y)
        except pygame.error:
            log.warning("Joystick disconnected")
            self._joystick = None
            self.axes = JoystickAxes()
            return True

        raw = JoystickAxes(
            pan    =  self._apply_deadzone(raw_rx),
            tilt   = -self._apply_deadzone(raw_ry),  # invert Y: up = positive
            slider =  self._apply_deadzone(raw_lx),
            zoom   = -self._apply_deadzone(raw_ly),  # invert Y: up = zoom in
        )

        # EMA smoothing — dampens ADC noise that would otherwise cause
        # micro speed-variations on the mount (judder).
        a = _SMOOTH_ALPHA
        b = 1.0 - a
        self._smoothed = JoystickAxes(
            pan    = a * raw.pan    + b * self._smoothed.pan,
            tilt   = a * raw.tilt   + b * self._smoothed.tilt,
            slider = a * raw.slider + b * self._smoothed.slider,
            zoom   = a * raw.zoom   + b * self._smoothed.zoom,
        )

        # Compare at integer resolution: sub-LSB float noise never triggers
        # a 'changed' event, so no redundant overrideSpeed() calls are sent.
        old_int = self.axes.to_int(1000)
        new_int = self._smoothed.to_int(1000)
        changed = old_int != new_int

        self.axes = self._smoothed
        return changed

    @property
    def connected(self) -> bool:
        return self._joystick is not None

    def _apply_deadzone(self, value: float) -> float:
        if abs(value) < self._deadzone:
            return 0.0
        # Rescale so motion starts from 0 just outside the deadzone
        sign = 1.0 if value > 0 else -1.0
        return sign * (abs(value) - self._deadzone) / (1.0 - self._deadzone)
