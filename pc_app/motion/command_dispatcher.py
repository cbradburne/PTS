"""
CommandDispatcher — translates joystick input into mount commands.

- Driven by a QTimer at JOG_RATE_HZ
- Reads joystick axes, applies the active speed preset scaling
- Sends JOG packets only when axes are non-zero OR just became zero (to stop)
- Handles joystick override: if active mount is MOVING_TO_POS and joystick
  moves, the JOG command implicitly cancels the move on the Teensy side
  (the Teensy's jog() call stops the current moveTo)
"""
from __future__ import annotations

import logging
from PyQt6.QtCore import QObject, QTimer

from .joystick import JoystickHandler, circ_to_sq
from comms.mount_manager import MountManager
from comms.protocol import MountState

log = logging.getLogger(__name__)

JOG_RATE_HZ      = 20      # polls per second — balances responsiveness vs radio congestion
JOG_INTERVAL_MS  = 1000 // JOG_RATE_HZ


class CommandDispatcher(QObject):
    """
    Connects joystick → mount commands.

    active_mount_id: which mount the joystick currently drives (1-5).
    active_speed_preset: 1-4, controls the velocity scaling sent to the mount.
    """

    def __init__(self, joystick: JoystickHandler,
                 mount_manager: MountManager, parent=None):
        super().__init__(parent)
        self._joy     = joystick
        self._mm      = mount_manager
        self._active  = 1          # active mount ID
        self._last_zero = True     # True if last sent command was all-zero

        # Per-mount speed presets (1-4), initialised to 1 to match RotaryDial default
        self._pt_presets: dict[int, int] = {m: 2 for m in range(1, 6)}
        self._sz_presets: dict[int, int] = {m: 2 for m in range(1, 6)}

        # Mounts that currently have CV tracking active (pan/tilt owned by TrackingLoop)
        self._cv_tracking_mounts: set[int] = set()

        self._timer = QTimer(self)
        self._timer.setInterval(JOG_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    @property
    def active_mount_id(self) -> int:
        return self._active

    @active_mount_id.setter
    def active_mount_id(self, mount_id: int) -> None:
        if 1 <= mount_id <= 5:
            self._active = mount_id

    def set_pt_preset(self, mount_id: int, preset: int) -> None:
        self._pt_presets[mount_id] = max(1, min(4, preset))
        self.send_preset_announce(mount_id)
        from comms.protocol import AxisGroup
        self._mm.send_set_active_preset(mount_id, AxisGroup.PAN_TILT, preset)

    def set_sz_preset(self, mount_id: int, preset: int) -> None:
        self._sz_presets[mount_id] = max(1, min(4, preset))
        self.send_preset_announce(mount_id)
        from comms.protocol import AxisGroup
        self._mm.send_set_active_preset(mount_id, AxisGroup.SLIDER_ZOOM, preset)

    def send_preset_announce(self, mount_id: int) -> None:
        """Send a zero-velocity JOG so the hub display updates its speed indicators.
        Called on preset change and on mount connect."""
        pt = self._pt_presets.get(mount_id, 2)
        sz = self._sz_presets.get(mount_id, 2)
        self._mm.send_jog(mount_id, 0, 0, 0, 0, pt, sz)

    def set_cv_tracking(self, mount_id: int, active: bool) -> None:
        """Notify the dispatcher that CV tracking has started or stopped on mount_id.

        While tracking is active the dispatcher sends slider/zoom only (axis_mask=0x0C)
        so joystick slider input works without fighting the TrackingLoop's pan/tilt jog.
        """
        if active:
            self._cv_tracking_mounts.add(mount_id)
        else:
            self._cv_tracking_mounts.discard(mount_id)
            # Force a stop packet on the next tick so any residual slider/zoom jog clears.
            self._last_zero = False

    def sync_preset_from_mount(self, mount_id: int,
                               pt_preset: int, sl_preset: int) -> None:
        """Sync internal preset state from a mount STATE_REPORT without sending
        CMD_SET_ACTIVE_PRESET back to the mount (it already has the right value).
        Sends a preset-announce JOG so the hub display reflects the correct level."""
        self._pt_presets[mount_id] = max(1, min(4, pt_preset))
        self._sz_presets[mount_id] = max(1, min(4, sl_preset))
        self.send_preset_announce(mount_id)

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        if not self._joy.connected:
            self._joy.init()
            return

        changed = self._joy.poll()
        axes    = self._joy.axes

        pan, tilt, slider, zoom = axes.to_int(1000)

        # Circular-to-square: both axes of a pair reach ±1000 at 45° full deflection
        pan,    tilt = circ_to_sq(pan,    tilt)
        slider, zoom = circ_to_sq(slider, zoom)

        # Use integer-level zero check — the EMA smoother converges geometrically
        # and its float value never reaches exactly 0.0, so axes.is_zero() would
        # always return False after any movement, causing continuous zero packets
        # that fight with CMD_JOG from the hub display.

        pt = self._pt_presets.get(self._active, 2)
        sz = self._sz_presets.get(self._active, 2)

        if self._active in self._cv_tracking_mounts:
            # CV tracking owns pan/tilt — only forward slider/zoom so the joystick
            # can move the slider without fighting the TrackingLoop's pan/tilt jog.
            is_zero = (slider == 0 and zoom == 0)
            if not changed and (is_zero and self._last_zero):
                return
            self._mm.send_jog(self._active, 0, 0, slider, zoom,
                              pt_preset=pt, sz_preset=sz, axis_mask=0x0C)
        else:
            is_zero = (pan == 0 and tilt == 0 and slider == 0 and zoom == 0)
            # Only send a packet if:
            #   - axes changed (integer level), OR
            #   - axes just became zero (send one final stop packet)
            if not changed and (is_zero and self._last_zero):
                return
            self._mm.send_jog(self._active, pan, tilt, slider, zoom, pt, sz)

        self._last_zero = is_zero
