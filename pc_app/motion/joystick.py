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
        self._warned_no_joystick = False
        self._warned_open_failed = False

    def init(self) -> bool:
        """Initialise pygame and connect to the first available joystick.

        MUST NOT RAISE. This is the retry path while nothing is plugged in, and
        it runs every tick from CommandDispatcher._tick — a QTimer slot. PyQt6
        turns an unhandled exception in a slot into qFatal(), so anything that
        escapes here does not log a warning, it aborts the process.

        That is exactly what plugging a controller into a running app used to
        do: the app died the moment the DualSense was connected, and started
        fine once it was already there.
        """
        if not self._initialised:
            pygame.init()
            pygame.joystick.init()
            self._initialised = True

        try:
            # SDL only notices a device being plugged in while its event queue
            # is being pumped, and nothing pumped it while we were unplugged:
            # poll() returns before its own pump when there is no joystick, and
            # the dispatcher returns before poll(). So the pump belongs here,
            # in the one path that runs when nothing is connected. Without it
            # SDL's device list and get_count() can disagree about what is
            # actually openable.
            pygame.event.pump()

            if pygame.joystick.get_count() == 0:
                # Retried at the tick rate while unplugged — warn once per
                # absence, not twenty times a second.
                if not self._warned_no_joystick:
                    self._warned_no_joystick = True
                    log.warning("No joystick detected — will keep checking quietly")
                return False

            self._warned_no_joystick = False
            if self._joystick is None:
                # A device that has just appeared is not necessarily openable
                # yet. A DualSense over Bluetooth enumerates in stages, so
                # get_count() can report it a moment before SDL will hand it
                # over and Joystick(0) raises "Invalid joystick device number".
                # Failing here costs one tick; it used to cost the app.
                joy = pygame.joystick.Joystick(0)
                joy.init()
                name = joy.get_name()
        except pygame.error as e:
            # Assigned below only on a clean open, so a half-opened device never
            # becomes the live handle.
            if not self._warned_open_failed:
                self._warned_open_failed = True
                log.warning("Joystick present but not ready (%s) — retrying", e)
            return False

        if self._joystick is None:
            self._warned_open_failed = False
            self._joystick = joy
            log.info(f"Joystick: {name}")
        return True

    def poll(self) -> bool:
        """
        Pump pygame events and update self.axes.
        Returns True if axes changed since last poll.
        """
        if self._joystick is None:
            self.init()
            return False

        try:
            # Inside the guard, not before it: poll() runs from the same QTimer
            # slot as init(), so a pygame error escaping from here aborts the
            # app just as surely as one from the open. Unplugging mid-session is
            # where that would land.
            pygame.event.pump()
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
