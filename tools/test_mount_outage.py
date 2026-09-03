"""Mount outage accounting: how long each mount was UNCONTROLLABLE.

The one figure an operator asks for, and the one nothing else on this rig could
answer. Uptimes, reset reasons and ESP-NOW reinit counts all say that an
interruption happened; none of them says for how long, and multiplying a count
by an assumed duration produces a confident number that is invented.

Checks the hub's encoding against the PC app's decoder, and reads the .ino for
the two things that are easy to get subtly wrong: measuring the gap from where
it STARTED rather than where it was noticed, and including an outage that is
still running.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))
import re, logging, io

from comms.bridge import Bridge
from comms.protocol import (Cmd, decode_mount_outage, NUM_MOUNTS,
                            MOUNT_OUTAGE_PER_MOUNT, MOUNT_OUTAGE_PAYLOAD_LEN,
                            MOUNT_OUTAGE_FLAG_NOW, MOUNT_OUTAGE_MIN_MS,
                            MOUNT_STATUS_REFRESH_MS)

INO = (REPO / "firmware/esp32_hub_eth/esp32_hub_eth.ino").read_text()


def hub_encode(stats):
    """Byte-for-byte what the hub's send_mount_outage() builds."""
    buf = bytearray(NUM_MOUNTS * MOUNT_OUTAGE_PER_MOUNT)
    for mid, (count, total_s, min_s, max_s, now) in stats.items():
        e = (mid - 1) * MOUNT_OUTAGE_PER_MOUNT
        buf[e + 0:e + 2] = count.to_bytes(2, "big")
        buf[e + 2:e + 6] = total_s.to_bytes(4, "big")
        buf[e + 6:e + 8] = min(min_s, 0xFFFF).to_bytes(2, "big")
        buf[e + 8:e + 10] = min(max_s, 0xFFFF).to_bytes(2, "big")
        if now:
            buf[e + 10] |= MOUNT_OUTAGE_FLAG_NOW
    return bytes(buf)


# ---- 1. the measurement must start where the outage did --------------------
# The gap begins at the last packet heard, NOT at the moment the hub noticed the
# silence. Timing from detection would understate every outage by the whole
# presence timeout — 16 s on this rig — which is longer than most of the events
# being measured.
print("1. hub firmware:")
m = re.search(r"if \(!_out_gap_start\[i\]\) _out_gap_start\[i\] = (\w+);", INO)
assert m, "outage start is not recorded in mount_outage_poll()"
assert m.group(1) == "seen", (
    f"outage timed from '{m.group(1)}' — must be 'seen', the last packet before "
    f"the gap, or every outage is understated by the presence timeout")
print("   gap timed from the last packet, not from detection  OK")

m = re.search(r"uint32_t dur = (\w+) - _out_gap_start\[i\];", INO)
assert m and m.group(1) == "seen", "recovery is not timed to the first packet back"
print("   gap closed at the first packet back                 OK")

# Below the floor is not an outage, it is a late packet.
assert "if (dur >= MOUNT_OUTAGE_MIN_MS)" in INO, "no minimum-gap gate"
print(f"   gaps under MOUNT_OUTAGE_MIN_MS ignored              OK")

# A mount that is still down must not have a total frozen at its last recovery.
assert "if (_out_gap_start[i]) {" in INO and "live" in INO, \
    "a running outage is not included in the total"
assert "MOUNT_OUTAGE_FLAG_NOW" in INO, "the currently-down flag is never set"
print("   an outage still running is counted live             OK")

# ---- 2. the floor, stated honestly -----------------------------------------
print("\n2. resolution:")
assert MOUNT_OUTAGE_MIN_MS > MOUNT_STATUS_REFRESH_MS, \
    "the floor must exceed one status interval or jitter reads as an outage"
print(f"   status cadence {MOUNT_STATUS_REFRESH_MS/1000:.0f}s -> floor "
      f"{MOUNT_OUTAGE_MIN_MS/1000:.1f}s                     OK")
print("   (a dropout shorter than that is real and invisible)")

# ---- 3. hub encoding -> app decoding ---------------------------------------
print("\n3. round-trip:")
CASES = {
    1: (9, 312, 8, 71, False),      # several events over a day
    4: (2, 46, 14, 32, False),
    5: (11, 300, 8, 34, True),      # and one still down
}
got = decode_mount_outage(hub_encode(CASES))
for mid, (c, t, mn, mx, now) in CASES.items():
    g = got[mid]
    assert (g["count"], g["total_s"], g["min_s"], g["max_s"], g["now"]) == (c, t, mn, mx, now), (mid, g)
print(f"   {len(CASES)} mounts encode and decode exactly                OK")

# A mount that has never been out says nothing — five "0 outages" lines a minute
# would bury the one line that has a number in it.
assert 2 not in got and 3 not in got, "mounts with no outages should not appear"
print("   mounts with no outages are omitted                  OK")

# A mount down RIGHT NOW appears even with no completed events.
live = decode_mount_outage(hub_encode({3: (0, 0, 0, 0, True)}))
assert 3 in live and live[3]["now"], "a mount down now must appear before it recovers"
print("   a mount down now appears before it recovers         OK")

# ---- 4. the log line -------------------------------------------------------
print("\n4. log line:")
_buf = io.StringIO()
_h = logging.StreamHandler(_buf); _h.setFormatter(logging.Formatter("%(levelname)s|%(message)s"))
_lg = logging.getLogger("comms.bridge")
_saved = _lg.handlers, _lg.level, _lg.propagate
_lg.handlers, _lg.propagate = [_h], False
_lg.setLevel(logging.INFO)


class _P:
    def __init__(self, payload):
        self.cmd, self.payload, self.mount_id = Cmd.MOUNT_OUTAGE, payload, 0


_b = Bridge.__new__(Bridge)
_b._outage_last = {}
_b._outage_down = set()


def render(stats):
    _buf.truncate(0); _buf.seek(0)
    _b._note_mount_outage(_P(hub_encode(stats)))
    return _buf.getvalue().strip()


out = render({5: (11, 300, 8, 34, False)})
assert "cam5" in out and "5m 0s" in out and "11 event(s)" in out, out
assert "min 8s" in out and "max 34s" in out, out
assert out.startswith("INFO"), out
print(f"   {out.split('|', 1)[1]}")

_b._outage_last = {}
out = render({5: (3, 128, 11, 64, True)})
assert out.startswith("WARNING") and "DOWN" in out, out
print(f"   {out.split('|', 1)[1]}")

# The 60 s resend must not repeat an unchanged line forever.
_b._outage_last = {}
same = {1: (9, 312, 8, 71, False)}
assert render(same), "first summary should log"
assert not render(same), "an unchanged summary was logged twice"
print("   unchanged summary not repeated on the 60s resend    OK")

# ...and a mount still down does NOT keep reporting, which is the reverse of
# what this asserted until 2026-09-03.  The reasoning was that its total is
# climbing so each packet is new information — true of the number, false of the
# situation.  A mount switched off deliberately produced 461 of the 462 warnings
# in a 7.7 hour log.  Once on the way down, once on the way back; section 5
# drives the whole cycle.
_b._outage_last, _b._outage_down = {}, set()
down = {1: (1, 30, 30, 30, True)}
assert render(down), "the first packet of an outage must report it"
assert not render({1: (1, 90, 30, 90, True)}), \
    "still down a minute later and it logged again — the total climbing is not " \
    "news"
print("   a mount still down reports once, then stops         OK")

# ---- 5. the instrument must prove it is alive ------------------------------
# Everything above is silent while every mount behaves, which is right — but a
# silent instrument and a dead one read identically in a log, and this one's
# silence is supposed to carry meaning.  One line on first receipt is what makes
# the difference legible.
print("\n5. liveness:")
_b2 = Bridge.__new__(Bridge)
_b2._outage_last = {}
_b2._outage_down = set()
_b2._outage_seen = False
clean = bytes(NUM_MOUNTS * MOUNT_OUTAGE_PER_MOUNT)      # nobody has ever been out


def render2(payload):
    _buf.truncate(0); _buf.seek(0)
    _b2._note_mount_outage(_P(payload))
    return _buf.getvalue().strip()


first = render2(clean)
assert "accounting live" in first, f"a healthy rig says nothing at all: {first!r}"
assert not render2(clean), "the liveness line repeats on every packet"
print("   healthy rig confirms once, then stays quiet         OK")

# And it must not swallow a real outage arriving in that same first packet.
_b3 = Bridge.__new__(Bridge)
_b3._outage_last = {}
_b3._outage_down = set()
_b3._outage_seen = False
_buf.truncate(0); _buf.seek(0)
_b3._note_mount_outage(_P(hub_encode({4: (2, 46, 14, 32, False)})))
both = _buf.getvalue()
assert "accounting live" in both and "cam4" in both, both
print("   an outage in the first packet is still reported     OK")

# ---- 6. once per outage, not once a minute -----------------------------------
# The hub re-sends these totals every minute and a running gap grows, so every
# packet carried a different total and every one was logged. A mount switched
# off deliberately produced 461 of the 462 warnings in a 7.7 hour log and buried
# the one that meant something.
print("\n6. how often a continuing outage is reported:")
_b4 = Bridge.__new__(Bridge)
_b4._outage_last, _b4._outage_down, _b4._outage_seen = {}, set(), True


def render4(stats):
    _buf.truncate(0); _buf.seek(0)
    _b4._note_mount_outage(_P(hub_encode(stats)))
    return _buf.getvalue().strip()


# Goes down, and keeps being down with the total climbing exactly as the hub
# reports it.
first = render4({5: (0, 60, 0, 60, True)})
assert first.startswith("WARNING") and "cam5" in first, first
assert "DOWN" in first, first
print("   the moment it goes down: one WARNING               OK")

for minutes in range(2, 12):
    quiet = render4({5: (0, 60 * minutes, 0, 60 * minutes, True)})
    assert quiet == "", \
        f"still down after {minutes} minutes and it logged again: {quiet!r}\n" \
        "    A warning that repeats for an unchanged condition is weather, not " \
        "a warning."
print("   ten more minutes down: silence                     OK")

# Back. One line, and it says so.
back = render4({5: (1, 720, 720, 720, False)})
assert back.startswith("INFO") and "BACK" in back, back
assert "12m 0s" in back and "1 event(s)" in back, back
print("   when it returns: one INFO, with the duration       OK")

assert not render4({5: (1, 720, 720, 720, False)}), \
    "the recovery line repeats while the mount stays up"

# And the warning is re-armed for the next one — a mount that drops twice is
# two events, and the second must be as loud as the first.
again = render4({5: (1, 780, 60, 720, True)})
assert again.startswith("WARNING") and "DOWN" in again, \
    f"the second outage did not warn: {again!r}"
print("   next time it drops: WARNING again                  OK")

# A mount that is ALREADY down when the app starts still gets its one warning:
# the state is news to this log even though it is not news to the hub.
_b5 = Bridge.__new__(Bridge)
_b5._outage_last, _b5._outage_down, _b5._outage_seen = {}, set(), True
_buf.truncate(0); _buf.seek(0)
_b5._note_mount_outage(_P(hub_encode({5: (0, 2577, 0, 2577, True)})))
start = _buf.getvalue().strip()
assert start.startswith("WARNING") and "42m 57s" in start, start
print("   already down at startup: still reported once       OK")

_lg.handlers, _lg.level, _lg.propagate = _saved
print("\nALL CHECKS PASSED")
