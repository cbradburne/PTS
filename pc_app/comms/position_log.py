"""Log live axis positions to comms.log, so travel can be measured.

The mounts have always broadcast CMD_POSITION — pan/tilt in degrees, slider in
millimetres, zoom in steps, plus a per-axis moving mask — at 5 Hz while moving
and 1 Hz at rest.  Nothing consumed it: `position_updated` had no subscribers
and no surface displayed a position, so there was no way to answer "how far did
the slider actually travel?" from anywhere in the system.

That is exactly the question behind the slider overshoot: the head runs past
its recorded end and we have only an eyeball estimate of 3–5 cm.  Comparing the
maximum reached during Find Limits with the maximum reached during a normal
move gives the overshoot directly, in millimetres, with no unit conversion
(limits are reported in steps, positions in mm — mm against mm avoids needing
the steps-per-mm factor at all).

**Volume**: silent while nothing moves.  A mount at rest emits 1 Hz and this
logs none of it.  Once an axis moves it samples at ~1 Hz rather than the full
5 Hz, and prints one summary line per move.  So the log stays readable and only
speaks when something is actually happening.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger("comms.position")

# Axis bit positions in PositionPayload.moving_mask.
_AXES = (("pan", "deg"), ("tilt", "deg"), ("slider", "mm"), ("zoom", "steps"))

SAMPLE_INTERVAL_S = 1.0     # while moving; the wire rate is 5 Hz


@dataclass
class _Move:
    """One period of continuous motion on one mount."""
    started:  float
    start:    tuple[float, float, float, int]
    lo:       list[float]
    hi:       list[float]
    axes:     set[int] = field(default_factory=set)
    last_log: float = 0.0


class PositionLogger:
    """Subscribes to MountManager.position_updated and logs motion.

    Attach with PositionLogger(mount_manager).  Holds no UI and no state beyond
    the in-flight move per mount, so it is safe to leave running.
    """

    def __init__(self, mount_manager) -> None:
        self._mm = mount_manager
        self._moves: dict[int, _Move] = {}
        mount_manager.position_updated.connect(self._on_position)
        # Anchor to the manager.  PyQt holds bound-method slots WEAKLY, so a
        # logger the caller doesn't keep a reference to is garbage-collected and
        # the connection dies silently — no error, just no output, which is the
        # worst way for a diagnostic to fail.  Tying lifetime to the manager
        # means it cannot happen however this is constructed.
        mount_manager._position_logger = self

    @staticmethod
    def _vals(pos) -> tuple[float, float, float, int]:
        return (pos.pan_deg, pos.tilt_deg, pos.slider_mm, pos.zoom_steps)

    def _on_position(self, mount_id: int, pos) -> None:
        now = time.monotonic()
        vals = self._vals(pos)
        mv = self._moves.get(mount_id)

        if pos.moving_mask:
            if mv is None:
                mv = _Move(started=now, start=vals,
                           lo=list(vals), hi=list(vals))
                self._moves[mount_id] = mv
                log.info("MOVE START mount=%d  %s", mount_id, self._fmt(vals))
            for i, v in enumerate(vals):
                mv.lo[i] = min(mv.lo[i], v)
                mv.hi[i] = max(mv.hi[i], v)
                if pos.moving_mask & (1 << i):
                    mv.axes.add(i)
            # Downsample: the wire rate is 5 Hz, which would swamp the log.
            if now - mv.last_log >= SAMPLE_INTERVAL_S:
                mv.last_log = now
                log.info("MOVING    mount=%d  %s", mount_id, self._fmt(vals))
            return

        if mv is None:
            return          # at rest and was at rest — nothing to say

        # Motion just ended: one summary line per axis that actually moved.
        # This is the line the overshoot measurement is read from.
        #
        # Fold this final sample into the extremes first.  It arrives with the
        # moving mask already clear, so the branch above skips it — and it is
        # the single most important sample of the move: the position the axis
        # came to rest at, which for a slider that has run into its end stop IS
        # the overshoot.  Leaving it out understated max by the whole
        # deceleration overrun.
        for i, v in enumerate(vals):
            mv.lo[i] = min(mv.lo[i], v)
            mv.hi[i] = max(mv.hi[i], v)
        del self._moves[mount_id]
        dt = now - mv.started
        for i in sorted(mv.axes):
            name, unit = _AXES[i]
            log.info("MOVE END   mount=%d %-6s start=%.1f%s end=%.1f%s "
                     "min=%.1f max=%.1f travel=%.1f%s in %.1fs",
                     mount_id, name,
                     mv.start[i], unit, vals[i], unit,
                     mv.lo[i], mv.hi[i],
                     abs(vals[i] - mv.start[i]), unit, dt)

    @staticmethod
    def _fmt(vals) -> str:
        return ("pan=%.1fdeg tilt=%.1fdeg slider=%.1fmm zoom=%d"
                % (vals[0], vals[1], vals[2], vals[3]))
