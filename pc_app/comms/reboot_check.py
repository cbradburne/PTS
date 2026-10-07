"""Did a node reboot between two of its HEALTH reports?

Built from a miss on 2026-10-07.  Flashing a mount reboots it (reset reason
USB), and the operator then power-cycled it a few seconds later (POWERON).
cam2's report after the flash said "up 3 s"; its next, fifteen seconds later
and after the power cycle, said "up 3 s" again.  Uptime had not gone DOWN,
which was the only test, so the second boot went unseen: its failed-send count
went from 1 back to 0 and was logged as "65535 sends failed ... no reboot",
and three other mounts flashed that afternoon were said to have had "no
reboot ... so the silence was on the way to it".  The logs from 26 Sep to
7 Oct hold nine reboots missed this way, every one a USB flash followed by a
power cycle.

Any one of these says a node booted since its last report:
  * its uptime went down;
  * it has been up for less time than has passed since that report: a node
    that kept running would be up at least that much longer;
  * its reset reason changed: the reason for the last boot can only change
    at a boot;
  * its failed-send count went down: it counts from boot, and only up.
Across the 985,372 health reports in those logs, the last two changed only
across a reboot.

Pure Python, no Qt: tools/test_reboot_check.py runs it directly.
"""
from __future__ import annotations

from typing import NamedTuple, Optional

# A report is timed when it arrives here, not when the node built it, and its
# uptime is in whole seconds, so the time since the last report can look a
# little longer than the node was really up.  A reboot closer than this to the
# last report is still caught if its reset reason or failed sends moved.
REBOOT_SLACK_S = 2.0


class NodeReport(NamedTuple):
    uptime_s: int               # the node's own, from its HEALTH
    tx_fail:  Optional[int]     # its failed sends since boot (16 bits); None: not known
    reset:    Optional[int]     # the reason for its last boot; None: not known
    at:       float             # when it arrived here, on one clock for both reports


def _secs(s: float) -> str:
    return "%d s" % s if s < 120 else "%.1f min" % (s / 60.0)


def rebooted(was: NodeReport, now: NodeReport, reset_names: Optional[dict] = None,
             slack_s: float = REBOOT_SLACK_S) -> Optional[str]:
    """What says the node booted between `was` and `now`, or None if nothing does."""
    if now.uptime_s < was.uptime_s:
        return "its uptime went down"
    since = now.at - was.at
    if now.uptime_s + slack_s < since:
        return ("it has been up %s, but its last report was %s ago"
                % (_secs(now.uptime_s), _secs(since)))
    if was.reset is not None and now.reset is not None and now.reset != was.reset:
        names = reset_names or {}
        return ("its reset reason changed from %s to %s"
                % (names.get(was.reset, "code %d" % was.reset),
                   names.get(now.reset, "code %d" % now.reset)))
    if was.tx_fail is not None and now.tx_fail is not None:
        # 16 bits that wrap (a satellite's stops at 65535): a step down of
        # under half the range is a reset to zero, not a wrap forward.
        if 0 < (was.tx_fail - now.tx_fail) & 0xFFFF < 0x8000:
            return ("its failed sends went down from %d to %d, and they count from boot"
                    % (was.tx_fail, now.tx_fail))
    return None
