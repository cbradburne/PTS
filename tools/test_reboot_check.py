"""A node that boots twice inside one report interval is still seen to reboot.

2026-10-07, all five mounts flashed: each flash reboots the mount (USB), and
the operator then power-cycled it a few seconds later (POWERON).  The second
boot's first report gave about the uptime the first boot's had, and "uptime
went down" was the only test, so it went unseen.  The log said:

  PRESENCE cam2 — its own account of the 9.5 s greyed: 65535 sends failed,
    no ESP-NOW restart, no reboot
  PRESENCE cam1 — its own account of the 8.0 s greyed: 0 sends failed, no
    ESP-NOW restart, no reboot: its radio saw nothing wrong, so the silence
    was on the way to it

and the same for cam3 and cam4: four wrong accounts in one afternoon, and no
NODE REBOOTED for any of the second boots.

A flash is now summed up in one START-UP line instead (startup_quiet,
tools/test_startup_quiet.py), so here it is a FAULT that boots twice inside one
report interval — a mount that crashes, comes up, and crashes again:

  1. reboot_check.rebooted(): each sign on its own, and what is not a reboot;
  2. through the real Bridge: NODE REBOOTED for both crashes, with what shows it;
  3. through the real MountManager: the accounts say "rebooted".

Run directly, or via tools/run_tests.sh.
"""
import os, sys, pathlib, logging, struct, threading, types
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

from comms.reboot_check import NodeReport as R, rebooted, REBOOT_SLACK_S

NAMES = {1: "POWERON", 11: "USB"}

# ---- 1. the rule ------------------------------------------------------------------
print("1. reboot_check.rebooted():")
cases = [  # (was, now, what it should say (None: not a reboot), why)
    # Each sign alone: the others say nothing in these.
    (R(5000, 9, 1, 0.0), R(9, 9, 1, 10.0), "its uptime went down",
     "uptime went down"),
    (R(3, 0, 1, 0.0), R(3, 0, 1, 15.0),
     "it has been up 3 s, but its last report was 15 s ago",
     "up less time than has passed (two power-ups)"),
    (R(3, 0, 11, 0.0), R(4, 0, 1, 4.5), "its reset reason changed from USB to POWERON",
     "the reset reason changed"),
    (R(3, 2, 1, 0.0), R(4, 0, 1, 4.5),
     "its failed sends went down from 2 to 0, and they count from boot",
     "the failed sends went down"),
    # The afternoon's own numbers: cam2 at 17:04:16.
    (R(3, 1, 11, 0.0), R(3, 0, 1, 14.6),
     "it has been up 3 s, but its last report was 14 s ago",
     "cam2's flash then power cycle"),
    (R(5, 0, 1, 0.0), R(40, 0, 1, 150.0),
     "it has been up 40 s, but its last report was 2.5 min ago",
     "a reboot while nothing was arriving"),
    # Not reboots.
    (R(5000, 9, 1, 0.0), R(5010, 9, 1, 10.0), None, "a node that kept running"),
    (R(5000, 9, 1, 0.0), R(5010, 9, 1, 28.0), None,
     "its report 18 s late (the satellite's link stalled)"),
    (R(5000, 9, 1, 0.0), R(5010, 9, 1, 1.0), None, "its report early (a backlog)"),
    (R(100, 65530, 3, 0.0), R(110, 4, 3, 10.0), None, "failed sends wrapping past 65535"),
    (R(800000, 65535, 1, 0.0), R(800010, 65535, 1, 10.0), None,
     "a satellite's failed sends stopped at 65535"),
    (R(3, None, None, 0.0), R(4, 0, 1, 4.5), None, "nothing known to compare"),
    (R(3, 0, 1, 0.0), R(3, 0, 1, 3 + REBOOT_SLACK_S), None, "exactly the slack: not yet"),
    (R(3, 0, 1, 0.0), R(3, 0, 1, 3 + REBOOT_SLACK_S + 0.1),
     "it has been up 3 s, but its last report was 5 s ago", "just past the slack"),
]
bad = []
for was, now, want, why in cases:
    got = rebooted(was, now, NAMES)
    if got != want:
        bad.append(f"{why}: got {got!r}, want {want!r}")
    else:
        print(f"   {why:52} {'reboot' if want else 'not a reboot':12} OK")
assert not bad, "\n".join(bad)          # every case that failed, not just the first

# held_up(): a report built before the node was heard again, delivered late.
from comms.reboot_check import held_up
for was, now, back, want, why in [
    (R(56628, 9, 1, 0.0), R(56638, 9, 1, 390.7), 390.25, True,
     "cam5's 08:49 report, delivered at 08:56"),
    (R(56628, 9, 1, 0.0), R(57021, 1820, 1, 392.8), 390.25, False,
     "its next, built after it came back"),
    (R(5000, 9, 1, 0.0), R(5016, 9, 1, 16.0), 13.5, False,
     "an ordinary report after a grey-out"),
    (R(1000, 0, 1, 0.0), R(1008, 0, 1, 12.0), 10.0, False,
     "8 s on, back at 10 s: within the slack"),
    (R(1000, 0, 1, 0.0), R(1007, 0, 1, 12.0), 10.0, True,
     "7 s on, back at 10 s: built before"),
    (R(3, 0, 3, 0.0), R(3, 0, 3, 15.0), 13.0, False,
     "younger than the silence: could have rebooted"),
]:
    got = held_up(was, now, back)
    assert got == want, f"{why}: got {got}, want {want}"
    print(f"   {why:52} {'held up' if want else 'not held up':12} OK")

# ---- 2. the real bridge -------------------------------------------------------------
print("\n2. a mount crashing twice, through the real Bridge:")
from comms import bridge as B
from comms.protocol import Packet, Cmd

clock = [0.0]
B.time = types.SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1.79e9 + clock[0])


class Lines(logging.Handler):
    def __init__(self, prefix):
        super().__init__(); self.prefix, self.got = prefix, []
    def emit(self, r):
        if r.getMessage().startswith(self.prefix):
            self.got.append(r.getMessage())
    def take(self):
        g, self.got = self.got, []
        return g


rb = Lines("NODE REBOOTED")
logging.getLogger(B.__name__).addHandler(rb)
logging.getLogger(B.__name__).setLevel(logging.INFO)


def health(uptime_s, txfail, reset, node=1):
    return struct.pack(">BBIIIHHbBI", node, reset, uptime_s, 8400000, 8390000, 10,
                       txfail, -46, 0, 0)


def bare_bridge():
    b = B.Bridge.__new__(B.Bridge)            # as the other suites build it
    b._diag_lock = threading.Lock()
    b._node_uptime = {}; b._sat_names = {}; b._cam_ble = {}
    b._node_state_cur = {}; b._node_state_prev = {}
    b._node_state_written = 0.0; b._node_state_path = None
    return b


def report(b, t, mid, uptime_s, txfail, reset):
    clock[0] = t
    b._note_node_health(Packet(mount_id=mid, seq=0, cmd=Cmd.HEALTH,
                               payload=health(uptime_s, txfail, reset)))


PANIC = 4
b = bare_bridge()
report(b, 0.0,  2, 5000, 6, 1)     # running
report(b, 10.0, 2, 3, 0, PANIC)    # crashed, and came up
report(b, 24.0, 2, 3, 0, PANIC)    # crashed again inside one report: the same
report(b, 34.0, 2, 13, 0, PANIC)   # uptime, reason and failed sends as before
report(b, 44.0, 2, 23, 0, PANIC)
got = rb.take()
assert got == [
    "NODE REBOOTED — cam2/bridge uptime 1.39h -> 0.00h | reset reason: PANIC(crash) | "
    "its uptime went down",
    "NODE REBOOTED — cam2/bridge uptime 0.00h -> 0.00h | reset reason: PANIC(crash) | "
    "it has been up 3 s, but its last report was 14 s ago"], got
print("   the first crash: NODE REBOOTED, as before")
print("   the second, 14 s later with the same uptime: NODE REBOOTED too,")
print("   saying how we know; then quiet while it runs                    OK")

b = bare_bridge()
for t, up, tf in [(0, 5000, 65500), (10, 5010, 65500), (38, 5020, 65510),   # 18 s late
                  (39, 5030, 65520), (49, 5040, 65530), (59, 5050, 4)]:      # wraps
    report(b, t, 4, up, tf, 1)
got = rb.take()
assert got == [], f"a running mount with late, early and wrapping reports was called rebooted: {got}"
print("   a running mount: late and early reports, failed sends wrapping:")
print("   no NODE REBOOTED                                                OK")

# ---- 3. the real mount manager ---------------------------------------------------------
print("\n3. crashes in a grey-out, through the real MountManager's PRESENCE account:")
try:
    from PyQt6.QtCore import QCoreApplication
except ImportError:
    print("   PyQt6 not installed: the mount manager needs it, skipping.")
    print("\nALL CHECKS PASSED")
    sys.exit(0)
from comms import mount_manager as MM

app = QCoreApplication.instance() or QCoreApplication([])
MM.time = types.SimpleNamespace(monotonic=lambda: clock[0])
pres = Lines("PRESENCE")
logging.getLogger(MM.__name__).addHandler(pres)
logging.getLogger(MM.__name__).setLevel(logging.INFO)


class FakeHubLink:
    connected = True
    def __init__(self): self.cb = None
    def on_packet(self, cb): self.cb = cb
    def off_packet(self, cb): pass
    def send(self, data): pass
    def seconds_since_tracked_tx(self): return 0.0


def account(mid, steps):
    """Play (t, what, args) through a fresh MountManager; return the account."""
    link = FakeHubLink()
    mm = MM.MountManager(link)
    pres.take()
    for t, what, args in steps:
        clock[0] = 1000.0 + t
        if what == "health":
            link.cb(Packet(mount_id=mid, seq=0, cmd=Cmd.HEALTH, payload=health(*args)))
        elif what == "pong":
            link.cb(Packet(mount_id=mid, seq=0, cmd=Cmd.PONG, payload=b""))
        else:
            mm._heartbeat()
    acc = [l for l in pres.take() if "its own account" in l]
    assert len(acc) == 1, acc
    return acc[0]


# Each crashes again soon after coming up; each time one sign alone shows it.
WDT, SW = 6, 3
got = account(1, [(0.0, "health", (3, 2, PANIC)),
                  (3.5, "beat", ()),                 # greyed
                  (4.0, "pong", ()),                 # back
                  (4.5, "health", (4, 0, PANIC))])   # too soon to time; same reason
assert got == ("PRESENCE cam1 — its own account of the 0.5 s greyed: it rebooted in "
               "the gap (up 4 s now)"), got
print("   failed sends gone down: \"rebooted\", not \"65534 sends failed\"     OK")

got = account(2, [(0.0, "health", (3, 0, PANIC)),
                  (3.5, "beat", ()),
                  (4.0, "pong", ()),
                  (4.5, "health", (4, 0, WDT))])     # too soon to time; no sends
assert got == ("PRESENCE cam2 — its own account of the 0.5 s greyed: it rebooted in "
               "the gap (up 4 s now)"), got
print("   reset reason changed: \"rebooted\"                                OK")

got = account(4, [(0.0, "health", (3, 0, SW)),
                  (3.5, "beat", ()),
                  (13.0, "pong", ()),
                  (15.0, "health", (3, 0, SW))])     # same reason, no failed sends
assert got == ("PRESENCE cam4 — its own account of the 9.5 s greyed: it rebooted in "
               "the gap (up 3 s now)"), got
print("   only the time since its last report: \"rebooted\", not \"the silence")
print("   was on the way to it\"                                          OK")

got = account(3, [(0.0, "health", (5000, 9, 1)),
                  (4.0, "beat", ()),
                  (13.5, "pong", ()),
                  (13.0 + 3.0, "health", (5016, 9, 1))])
assert got == ("PRESENCE cam3 — its own account of the 9.5 s greyed: 0 sends failed, no "
               "ESP-NOW restart, no reboot: its radio saw nothing wrong, so the silence "
               "was on the way to it"), got
print("   a running mount's silence is still put on the way to it          OK")

print("\nALL CHECKS PASSED")
