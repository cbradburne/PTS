"""A mount starting up is not a fault: one line for it, not dozens.

The operator, 2026-10-07, after flashing all five mounts: "can the pc app also
remove reports from an amoled firmware flash (POWERON → POWERON) ... I don't
need to know that there were a bunch of faults around that time.  They always
happen."

A flash is three boots: the mount is powered up (POWERON), flashed (USB), and
powered up again (POWERON).  Each one greys it out, loses the commands sent
while it was down and sets off the PC's own alarms.  That afternoon five flashes
logged some fifty PRESENCE lines, a dozen NODE REBOOTED, ten "unreachable"
warnings, 45 [ANOMALY] health reports and five lost-command warnings, and the
stuck-link check tore down the PC's connection to the hub because cam1 was not
answering while it was being flashed.

So a mount whose radio board boots because it was powered up or flashed, or
whose Teensy boots because it was powered up, is STARTING UP until START_UP_S
after the last such boot.  Meanwhile what would have been logged about it is
counted instead, the stuck-link check leaves it out, and when it is over one
line says what happened:

  START-UP cam2 — powered up 17:03:18, flashed 17:03:58, powered up 17:04:13.
    Not logged while it started: 4 grey-outs (25 s in all), 3 lost commands

A boot for any other reason (a crash, a watchdog, the mount's own restart) is a
fault: it ends the start-up and is reported as it always was.  What the PC
logged before it heard the first boot's report stays logged: usually the mount
going quiet as it was switched off, and coming back.

Pure Python, no Qt: tools/test_startup_quiet.py drives it.
"""
from __future__ import annotations

import threading
from typing import Optional

START_UP_S = 120.0    # starting up until this long after its last power-up or flash
LEAD_S     = 15.0     # a command sent this long before a power-up went to a mount
                      # already going down for it
_SAME_BOOT_S = 10.0   # the radio board and the Teensy report one power-up twice

# Boots someone caused on purpose.  esp_reset_reason() on the radio board;
# the low byte of SRC_SRSR on the Teensy, whose bit 0 is its power-on reset.
_RADIO = {1: "powered up", 11: "flashed"}            # POWERON, USB
_TEENSY = {1: "powered up"}


def start_up_reason(node_name: str, reset: int) -> Optional[str]:
    """What a mount node's boot was if someone did it ("powered up",
    "flashed"), or None if it was a fault."""
    return {"bridge": _RADIO, "teensy": _TEENSY}.get(node_name, {}).get(reset)


def _secs(s: float) -> str:
    return "%d s" % round(s) if s < 120 else "%.1f min" % (s / 60.0)


class StartupQuiet:
    """Which mounts are starting up, and what was not logged about them."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._win: dict[int, dict] = {}

    def boot(self, mid: int, at: float, uptime_s: float, what: str, clock: str) -> None:
        """Mount `mid` booted on purpose: `what` it was, reported at `at`
        (monotonic) by a node up `uptime_s`; `clock` is the time of day it
        booted, for the summary."""
        t = at - uptime_s
        with self._lock:
            w = self._win.get(mid)
            if w is None or t > w["last"] + START_UP_S:
                w = self._win[mid] = {"first": t, "last": t, "boots": [],
                                      "grey": 0, "grey_s": 0.0, "lost": 0}
            w["first"], w["last"] = min(w["first"], t), max(w["last"], t)
            if not any(b[1] == what and abs(b[0] - t) < _SAME_BOOT_S for b in w["boots"]):
                w["boots"].append((t, what, clock))

    def quiet(self, mid: int, at: float) -> bool:
        """Was mount `mid` starting up at `at`?"""
        with self._lock:
            w = self._win.get(mid)
            return w is not None and w["first"] - LEAD_S <= at <= w["last"] + START_UP_S

    def fold(self, mid: int, what: str, secs: float = 0.0) -> None:
        """Count, for the summary, something not logged: "back" (a grey-out
        that lasted `secs` is over) or "lost" (a command).  A grey-out still
        going when the start-up ends is not counted: the mount manager logs
        it then, as it stands."""
        with self._lock:
            w = self._win.get(mid)
            if w is None:
                return
            if what == "back":
                w["grey"] += 1
                w["grey_s"] += secs
            elif what == "lost":
                w["lost"] += 1

    def end(self, mid: int) -> Optional[str]:
        """A boot that was not on purpose: the start-up is over, cut short.
        Its summary, or None if it was not starting up."""
        with self._lock:
            w = self._win.pop(mid, None)
        return self._summary(mid, w, cut_short=True) if w else None

    def poll(self, at: float) -> list[str]:
        """The summaries of the start-ups over by `at`."""
        with self._lock:
            done = [m for m, w in self._win.items() if at > w["last"] + START_UP_S]
            wins = [(m, self._win.pop(m)) for m in sorted(done)]
        return [self._summary(m, w) for m, w in wins]

    @staticmethod
    def _summary(mid: int, w: dict, cut_short: bool = False) -> str:
        line = "START-UP cam%d — %s" % (
            mid, ", ".join("%s %s" % (what, clock) for _t, what, clock in sorted(w["boots"])))
        folded = []
        if w["grey"]:
            folded.append("%d grey-out%s (%s in all)" % (
                w["grey"], "" if w["grey"] == 1 else "s", _secs(w["grey_s"])))
        if w["lost"]:
            folded.append("%d lost command%s" % (w["lost"], "" if w["lost"] == 1 else "s"))
        if folded:
            line += ".  Not logged while it started: " + ", ".join(folded)
        if cut_short:
            line += ".  Cut short by a reboot that was not a start-up (next line)"
        return line
