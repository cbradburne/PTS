"""Every greying-out and coming-back of a mount is logged, with why.

The operator, 2026-10-02: cam2 greyed out for a few seconds in the middle of a
concert, and the log said nothing about it.  It took an afternoon to find,
from which mounts the idle probe skipped.  It was a ~7 s two-way radio
blackout, the same one every mount has 2-4 times a day.  "When the PC app
changes from active to greyed or from greyed to active, the log should record
what happened and why."

So every change of what the operator sees is one PRESENCE line:

  PRESENCE cam2 GREYED after 59.8 min active — nothing heard from it for
    3.1 s (the limit is 3.0 s — the last thing it sent was HEALTH).  cam3
    still answering, so the PC and the hub are fine: this is cam2's own link.
  PRESENCE cam2 ACTIVE after 4.6 s greyed — heard from again (HEALTH)
  PRESENCE cam2 — its own account of the 4.6 s greyed: 45 sends failed,
    ESP-NOW restarted once, no reboot

The last line is the mount's side.  The PC sees only silence; the mount's next
bridge HEALTH says whether its sends were failing, whether it restarted
ESP-NOW, whether it rebooted.

This drives a real MountManager with packets, a fake hub link and a clock it
controls, and reads what it logs.  Run directly, or via tools/run_tests.sh.
"""
import os, sys, pathlib, logging, struct, types
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

try:
    from PyQt6.QtCore import QCoreApplication
except ImportError:
    print("PyQt6 not installed: the mount manager needs it, skipping.")
    print("\nALL CHECKS PASSED")
    sys.exit(0)

from comms import mount_manager as MM
from comms.protocol import Packet, Cmd

app = QCoreApplication.instance() or QCoreApplication([])

clock = [1000.0]
MM.time = types.SimpleNamespace(monotonic=lambda: clock[0])


class FakeHubLink:
    connected = True
    def __init__(self): self.cb = None
    def on_packet(self, cb): self.cb = cb
    def off_packet(self, cb): pass
    def send(self, data): pass
    def seconds_since_tracked_tx(self): return 0.0     # no idle probes


class Lines(logging.Handler):
    def __init__(self):
        super().__init__(); self.got = []
    def emit(self, r):
        msg = r.getMessage()
        if msg.startswith("PRESENCE"):
            self.got.append((r.levelname, msg))
    def take(self):
        g, self.got = self.got, []
        return g


lines = Lines()
logging.getLogger(MM.__name__).addHandler(lines)
logging.getLogger(MM.__name__).setLevel(logging.INFO)

BRIDGE, TEENSY = 1, 2


def health(node, uptime_s, txfail=0, reinits=0):
    return struct.pack(">BBIIIHHbBI", node, 1, uptime_s, 200000, 190000, 10,
                       txfail, -46, 0, reinits << 4)


def fresh():
    link = FakeHubLink()
    mm = MM.MountManager(link)
    on, off = [], []
    mm.mount_connected.connect(on.append)
    mm.mount_disconnected.connect(off.append)
    lines.take()
    return mm, link, on, off


def hear(link, mid, cmd=Cmd.PONG, payload=b""):
    link.cb(Packet(mount_id=mid, seq=0, cmd=cmd, payload=payload))


def at(t):
    clock[0] = 1000.0 + t


# ---- 1. what happened, and why --------------------------------------------------
print("1. one line per change, saying why:")
mm, link, on, off = fresh()
at(0)
hear(link, 2, Cmd.HEALTH, health(BRIDGE, 2916, txfail=2, reinits=0))   # cam2, 11:09:57
hear(link, 3)
got = lines.take()
assert got == [("INFO", "PRESENCE cam2 ACTIVE — first heard since the app started (HEALTH)"),
               ("INFO", "PRESENCE cam3 ACTIVE — first heard since the app started (PONG)")], got
print("   first heard: ACTIVE, and what it was heard by                 OK")

at(2.0); hear(link, 3)                     # cam3 keeps answering; cam2 has gone quiet
at(3.5); mm._heartbeat()
got = lines.take()
assert len(got) == 1 and got[0][0] == "WARNING", got
assert got[0][1] == (
    "PRESENCE cam2 GREYED after 3.5 s active — nothing heard from it for 3.5 s "
    "(the limit is 3.0 s — the last thing it sent was HEALTH).  cam3 still "
    "answering, so the PC and the hub are fine: this is cam2's own link."), got
assert off == [2], off
print("   greyed: WARNING, how long it was silent, after what, and that")
print("   cam3 still answering puts the fault on cam2's own link        OK")

at(8.1); hear(link, 2, Cmd.HEALTH, health(TEENSY, 2925))   # back, via its Teensy's report
got = lines.take()
assert got == [("INFO", "PRESENCE cam2 ACTIVE after 4.6 s greyed — heard from again (HEALTH)")], got
assert on[-1] == 2, on
print("   back: INFO, how long it was greyed, what brought it back      OK")

# ---- 2. the mount's own account ---------------------------------------------------
print("\n2. the mount's own account, from its next bridge report:")
at(18.0); hear(link, 2, Cmd.HEALTH, health(BRIDGE, 2937, txfail=47, reinits=1))  # 11:10:18
got = lines.take()
assert got == [("INFO", "PRESENCE cam2 — its own account of the 4.6 s greyed: 45 sends "
                        "failed, ESP-NOW restarted once, no reboot")], got
at(28.0); hear(link, 2, Cmd.HEALTH, health(BRIDGE, 2947, txfail=47, reinits=1))
assert lines.take() == [], "the account was given twice"
print("   today's 11:10 numbers: 45 sends failed, one ESP-NOW restart    OK")
print("   given once, on the first bridge report back                   OK")

# Nothing wrong on the mount's side: the silence was on the way to it.
at(31.5); mm._heartbeat()                  # cam2 and cam3 both quiet now
lines.take()
at(41.0); hear(link, 2, Cmd.HEALTH, health(BRIDGE, 2960, txfail=47, reinits=1))
got = lines.take()
assert got[-1] == ("INFO", "PRESENCE cam2 — its own account of the 9.5 s greyed: 0 sends "
                           "failed, no ESP-NOW restart, no reboot: its radio saw nothing "
                           "wrong, so the silence was on the way to it"), got
print("   nothing failed on the mount: it says the fault was elsewhere  OK")

# A reboot in the gap.
mm, link, on, off = fresh()
at(0); hear(link, 4, Cmd.HEALTH, health(BRIDGE, 5000, txfail=9, reinits=2))
at(5); mm._heartbeat()
at(20); hear(link, 4, Cmd.HEALTH, health(BRIDGE, 12, txfail=0, reinits=0))
got = lines.take()
assert got[-1] == ("INFO", "PRESENCE cam4 — its own account of the 15.0 s greyed: it "
                           "rebooted in the gap (up 12 s now)"), got
print("   a reboot in the gap is called a reboot                        OK")

# Flapping twice before it reports: one account, of both gaps, from before the first.
mm, link, on, off = fresh()
at(0);  hear(link, 5, Cmd.HEALTH, health(BRIDGE, 100, txfail=10, reinits=3))
at(4);  mm._heartbeat()                    # greyed 4 s after its last
at(6);  hear(link, 5)                      # back after 2 s
at(10); mm._heartbeat()                    # greyed again
at(13); hear(link, 5)                      # back after 3 s
at(14); hear(link, 5, Cmd.HEALTH, health(BRIDGE, 114, txfail=70, reinits=5))
got = lines.take()
assert got[-1] == ("INFO", "PRESENCE cam5 — its own account of the 5.0 s greyed: 60 sends "
                           "failed, ESP-NOW restarted 2 times, no reboot"), got
print("   two grey-outs before it reports: one account covering both    OK")

# The counters' edges: tx_fail is 16 bits, the restart count stops at 15.
mm, link, on, off = fresh()
at(0); hear(link, 1, Cmd.HEALTH, health(BRIDGE, 100, txfail=65530, reinits=2))
at(4); mm._heartbeat()
at(6); hear(link, 1, Cmd.HEALTH, health(BRIDGE, 106, txfail=5, reinits=3))
got = lines.take()
assert got[-1] == ("INFO", "PRESENCE cam1 — its own account of the 2.0 s greyed: 11 sends "
                           "failed, ESP-NOW restarted once, no reboot"), got
print("   failed sends counted across the 16-bit wrap                   OK")

mm, link, on, off = fresh()
at(0); hear(link, 1, Cmd.HEALTH, health(BRIDGE, 100, txfail=40, reinits=15))
at(4); mm._heartbeat()
at(6); hear(link, 1, Cmd.HEALTH, health(BRIDGE, 106, txfail=85, reinits=15))
got = lines.take()
assert got[-1] == ("INFO", "PRESENCE cam1 — its own account of the 2.0 s greyed: 45 sends "
                           "failed, ESP-NOW restarts unknown (its count is at the 15 cap), "
                           "no reboot"), got
print("   a restart count at its cap says so, not \"no restart\"          OK")

# ---- 3. placing the fault -----------------------------------------------------------
print("\n3. where the fault is, from who else is answering:")
mm, link, on, off = fresh()
at(0); hear(link, 2); hear(link, 3); hear(link, 4)
lines.take()
at(3.5); mm._heartbeat()                   # all three quiet at once: the hub, not a mount
got = lines.take()
assert [g[1].split(" — ")[0] for g in got] == ["PRESENCE cam2 GREYED after 3.5 s active",
                                               "PRESENCE cam3 GREYED after 3.5 s active",
                                               "PRESENCE cam4 GREYED after 3.5 s active"], got
for g in got:
    assert g[1].endswith("No other mount is answering either, so this is the hub or the "
                         "PC's link to it, not cam%s." % g[1][12]), g
print("   all quiet together: every one of them blames the hub, the last")
print("   one too                                                       OK")

mm, link, on, off = fresh()
at(0); hear(link, 4)
at(3.5); mm._heartbeat()
got = lines.take()
assert got[-1][1].endswith("No other mount is on to compare with."), got
print("   alone: says it has nothing to compare with                    OK")

# ---- 4. heard but not acting -------------------------------------------------------
print("\n4. heard, but not acting on commands:")
mm, link, on, off = fresh()
at(0); hear(link, 3, Cmd.HEALTH, health(BRIDGE, 50, txfail=0, reinits=0))
lines.take()
for _ in range(3):
    mm.send_get_config(3)
got = lines.take()
assert len(got) == 1 and got[0][0] == "WARNING" and got[0][1].startswith(
    "PRESENCE cam3 GREYED after 0.0 s active — still heard, but its last 3 commands "
    "went unacknowledged: it is not acting on what it is told."), got
at(2); hear(link, 3, Cmd.HEALTH, health(BRIDGE, 52, txfail=0, reinits=0))
assert lines.take() == [], "a report from a mount still greyed gave an account"
at(3); hear(link, 3, Cmd.ACK, b"\x00" * 8)
got = lines.take()
assert got == [("INFO", "PRESENCE cam3 ACTIVE after 3.0 s greyed — acknowledged a command again")], got
print("   greyed with the count of unacknowledged commands; back on the")
print("   first ACK; no account while it was still greyed               OK")

# ---- 5. quiet when nothing changes ---------------------------------------------------
print("\n5. silent when nothing the operator sees has changed:")
mm, link, on, off = fresh()
at(0); hear(link, 2)
lines.take()
for t in range(1, 30):
    at(t * 0.5); hear(link, 2); hear(link, 2, Cmd.HEALTH, health(TEENSY, t)); mm._heartbeat()
assert lines.take() == [], "PRESENCE lines while nothing changed"
assert on == [2] and off == [], (on, off)
print("   15 s of packets and heartbeats: no lines, one signal          OK")

print("\nALL CHECKS PASSED")
