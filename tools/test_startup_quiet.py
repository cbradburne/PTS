"""A mount being powered up or flashed gets one START-UP line, not a page of faults.

The operator, 2026-10-07, having flashed all five mounts: "can the pc app also
remove reports from an amoled firmware flash (POWERON → POWERON) ... I don't
need to know that there were a bunch of faults around that time.  They always
happen and I don't need them reported on each time."

cam2's flash that afternoon, as the PC logged it: switched on, greyed and back
as it booted, greyed for the 14 s upload, NODE REBOOTED (USB), greyed for the
power cycle, NODE REBOOTED for its Teensy, greyed and back as it booted again,
"unreachable" twice, an outage, lost commands and nine [ANOMALY] health
reports.  cam1's flash made the stuck-link check tear down the PC's connection
to the hub.

  1. startup_quiet on its own: when a mount is starting up, and the one line;
  2. cam2's flash through the real Bridge and MountManager: one START-UP line;
  3. what still gets through: a crash while starting up, a mount switched off
     again, a mount that was running, the app opened mid-flash, a grey-out
     after the start-up is over.

Run directly, or via tools/run_tests.sh.
"""
import os, sys, pathlib, logging, struct, tempfile, types
from datetime import datetime
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

from comms.startup_quiet import StartupQuiet, start_up_reason, START_UP_S, LEAD_S

# ---- 1. on its own -------------------------------------------------------------------
print("1. startup_quiet on its own:")
for node, reset, want in [("bridge", 1, "powered up"), ("bridge", 11, "flashed"),
                          ("bridge", 4, None), ("bridge", 3, None), ("bridge", 9, None),
                          ("teensy", 1, "powered up"), ("teensy", 11, None),
                          ("satellite", 1, None)]:
    got = start_up_reason(node, reset)
    assert got == want, f"{node} reset {reset}: got {got!r}, want {want!r}"
print("   powered up (POWERON) and flashed (USB) are start-ups; a crash,")
print("   its own restart, a brownout and a satellite are not            OK")

s = StartupQuiet()
s.boot(2, 103.0, 3, "powered up", "17:03:18")           # booted at 100
assert [s.quiet(2, t) for t in (100 - LEAD_S - 0.1, 100 - LEAD_S, 100 + START_UP_S,
                                100 + START_UP_S + 0.1)] == [False, True, True, False]
assert not s.quiet(3, 101), "another mount was taken as starting up"
print("   starting up from LEAD_S before the boot to START_UP_S after   OK")
s.boot(2, 104.5, 4, "powered up", "17:03:19")           # its Teensy: the same power-up
s.boot(2, 143.0, 3, "flashed", "17:03:58")              # booted at 140
s.boot(2, 158.0, 3, "powered up", "17:04:13")           # booted at 155
assert s.quiet(2, 155 + START_UP_S) and not s.quiet(2, 155 + START_UP_S + 0.1)
print("   each boot on purpose moves the end out                         OK")
for what, secs in [("back", 4.0), ("back", 14.0), ("back", 9.5),       # grey-outs over
                   ("lost", 0), ("lost", 0), ("lost", 0)]:
    s.fold(2, what, secs)
s.fold(3, "back", 5.0)                                  # not starting up: dropped
assert s.poll(155 + START_UP_S) == [], "summed up before it was over"
got = s.poll(155 + START_UP_S + 1)
assert got == ["START-UP cam2 — powered up 17:03:18, flashed 17:03:58, powered up "
               "17:04:13.  Not logged while it started: 3 grey-outs (28 s in all), "
               "3 lost commands"], got
assert s.poll(1000) == [] and not s.quiet(2, 200), "summed up twice"
print("   one line when it is over, the Teensy's report of the same")
print("   power-up not listed twice, said once                           OK")

s.boot(2, 503.0, 3, "powered up", "17:10:00")           # long after: a new start-up
s.fold(2, "back", 150.0); s.fold(2, "lost")
assert s.poll(503 + START_UP_S) == [
    "START-UP cam2 — powered up 17:10:00.  Not logged while it started: 1 grey-out "
    "(2.5 min in all), 1 lost command"]
s.boot(4, 1003.0, 3, "powered up", "17:20:00")
assert s.end(4) == ("START-UP cam4 — powered up 17:20:00.  Cut short by a reboot that "
                    "was not a start-up (next line)")
assert s.end(4) is None and not s.quiet(4, 1004), "still starting up after a crash"
print("   one of each, minutes, and cut short by a crash                 OK")

# ---- 2. cam2's flash, through the real bridge and mount manager --------------------------
print("\n2. cam2's flash through the real Bridge and MountManager:")
try:
    from PyQt6.QtCore import QCoreApplication
except ImportError:
    print("   PyQt6 not installed: the mount manager needs it, skipping.")
    print("\nALL CHECKS PASSED")
    sys.exit(0)
from comms import bridge as B
from comms import mount_manager as MM
from comms import protocol as P
from comms.protocol import Packet, Cmd

app = QCoreApplication.instance() or QCoreApplication([])
clock = [0.0]
WALL0 = 1.79e9
B.time = types.SimpleNamespace(monotonic=lambda: clock[0], time=lambda: WALL0 + clock[0],
                               sleep=lambda s: None)
MM.time = types.SimpleNamespace(monotonic=lambda: clock[0])
TMP = pathlib.Path(tempfile.mkdtemp())


def hhmmss(t):
    return datetime.fromtimestamp(WALL0 + t).strftime("%H:%M:%S")


class RigBridge(B.Bridge):
    connected = True                     # no transport here: the manager only asks
    _node_state_path = staticmethod(lambda: TMP / "node_state.json")
    def send(self, data): pass
    def seconds_since_tracked_tx(self): return 0.0     # no idle probes


class Log(logging.Handler):
    def __init__(self):
        super().__init__(); self.got = []
    def emit(self, r):
        self.got.append((r.levelname, r.getMessage()))
    def take(self):
        g, self.got = self.got, []
        return g


log = Log()
for name in (B.__name__, MM.__name__):
    logging.getLogger(name).addHandler(log)
    logging.getLogger(name).setLevel(logging.INFO)

POWERON, USB, PANIC = 1, 11, 4
BRIDGE, TEENSY = 1, 2


def health(uptime_s, txfail=0, reset=POWERON, node=BRIDGE, anomaly=False):
    return struct.pack(">BBIIIHHbBI", node, reset, uptime_s, 8400000, 8390000, 10,
                       txfail, -46, 0x01 if anomaly else 0, 0)


def rig():
    b = RigBridge()
    mm = MM.MountManager(b)
    log.take()
    return b, mm


def at(t):
    clock[0] = t


def rx(b, mid, cmd=Cmd.PONG, payload=b""):
    b._dispatch(Packet(mount_id=mid, seq=0, cmd=cmd, payload=payload))


def steady(b, mm, mid, t0, t1, up0):
    """Answering every 2 s and reporting every 10 s, from t0 to t1."""
    for t in range(t0, t1, 2):
        at(t); rx(b, mid); mm._heartbeat()
        if (t - t0) % 10 == 0:
            rx(b, mid, Cmd.HEALTH, health(up0 + t - t0))


def rf(worst_rssi):
    """An RF report whose worst moment was `worst_rssi` over a -97 dBm floor."""
    return struct.pack(">6bHHHH", worst_rssi, -52, -48, -97, -97, -97, 12, 0, 20, 1)


LINK_RUN = bytes([P.MOUNT_EVENT_LINK_RUN]) + bytes(P.MOUNT_EVENT_LINK_PAYLOAD_LEN - 1)


def about(mid, lines):
    """The lines that are about this mount and say something is wrong."""
    tag = "cam%d" % mid
    return [l for l in lines if tag in l[1] and (l[0] == "WARNING" or "PRESENCE" in l[1]
                                                or l[1].startswith("START-UP"))]


def begin(got, starts):
    """Each line begins as given, and there are no others."""
    return len(got) == len(starts) and all(g[1].startswith(s) for g, s in zip(got, starts))


b, mm = rig()
at(0);    rx(b, 2)                                                  # switched on
at(1);    rx(b, 2, Cmd.HEALTH, health(3, anomaly=True))             # booted at -2
at(1.1);  rx(b, 2, Cmd.HEALTH, health(3, node=TEENSY, anomaly=True))
at(1.5);  rx(b, 2, Cmd.RF_REPORT, rf(-89))                          # 8 dB at its worst
at(5);    mm._heartbeat()                                           # greyed as it boots
at(7);    rx(b, 2)                                                  # back
at(11);   rx(b, 2, Cmd.HEALTH, health(13, txfail=6, anomaly=True))
b._cmd_ledger[902] = (12.0, 2)                                      # answered, but slowly
at(13);   rx(b, 2, Cmd.ACK, bytes([902 >> 8, 902 & 0xFF]) + bytes(6))
for t in (13, 15, 17, 19, 21, 23):
    at(t); rx(b, 2); mm._heartbeat()
# The upload: silent from 23.  A GET_CONFIG sent into it goes unanswered.
b._cmd_ledger[901] = (24.0, 2)
b._pending_acks[901] = (24.0, "GET_CONFIG", 2)
at(27);   mm._heartbeat()                                           # greyed
stuck = b._wedge_check(36.0)[0]                                     # 12 s unanswered
at(36);   b._cmd_ledger_tick(36.0)                                  # the GET_CONFIG ages out
at(38);   rx(b, 2)                                                  # back from the upload
at(38.5); rx(b, 0xFE, Cmd.HUB_EVENT, bytes([14, 2, 0, 0, 15, 0, 0, 0, 0]))   # 15 s outage
at(39);   rx(b, 2, Cmd.HEALTH, health(3, txfail=1, reset=USB, anomaly=True))  # flashed: 36
at(39.5); rx(b, 2, Cmd.MOUNT_EVENT, LINK_RUN)                       # its sends lost booting
at(43);   mm._heartbeat()                                           # greyed: power cycle
at(52);   rx(b, 2)                                                  # back
at(54);   rx(b, 2, Cmd.HEALTH, health(3, reset=POWERON, anomaly=True))   # booted at 51,
at(54.1); rx(b, 2, Cmd.HEALTH, health(3, node=TEENSY, anomaly=True))     # same uptime
steady(b, mm, 2, 56, 176, 5)
b._cmd_report_t = -1000.0
b._cmd_ledger_tick(176.0)                                           # the 5-minute report
got = about(2, log.take())
assert got == [
    ("INFO", "PRESENCE cam2 ACTIVE — first heard since the app started (PONG)"),
    ("INFO", "START-UP cam2 — powered up %s, flashed %s, powered up %s.  Not logged while "
             "it started: 3 grey-outs (22 s in all), 1 lost command"
             % (hhmmss(-2), hhmmss(36), hhmmss(51)))], "\n".join(map(str, got))
print("   switched on, flashed, powered up again: its first appearance and")
print("   one START-UP line; no grey-outs, accounts, NODE REBOOTED, [ANOMALY],")
print("   RF, link-run, outage, lost or slow command warnings            OK")
assert stuck == 0.0, \
    "a mount being flashed was taken for a stuck link: the PC would reconnect to the hub"
print("   a mount being flashed is not a stuck link: no reconnect        OK")

at(200);  mm._heartbeat()                                           # over: a grey-out is news
got = about(2, log.take())
assert len(got) == 1 and got[0][0] == "WARNING" and got[0][1].startswith(
    "PRESENCE cam2 GREYED after"), got
print("   after it, a grey-out is logged as before                       OK")

# ---- 3. what still gets through -------------------------------------------------------
print("\n3. what still gets through:")
b, mm = rig()
at(0);  rx(b, 3)
at(1);  rx(b, 3, Cmd.HEALTH, health(3))                             # powered up at -2
steady(b, mm, 3, 2, 30, 4)
at(30); rx(b, 3, Cmd.HEALTH, health(2, reset=PANIC))                # crashed
at(34); mm._heartbeat()                                             # and went quiet
got = about(3, log.take())
assert begin(got, [
    "PRESENCE cam3 ACTIVE — first heard since the app started",
    "START-UP cam3 — powered up %s.  Cut short by a reboot that was not a start-up"
    % hhmmss(-2),
    "NODE REBOOTED — cam3/bridge uptime 0.01h -> 0.00h | reset reason: PANIC(crash) | "
    "its uptime went down",
    "PRESENCE cam3 GREYED after 34.0 s active — nothing heard from"]), got
print("   a crash while starting up: NODE REBOOTED, the start-up cut short,")
print("   and what follows logged                                        OK")

b, mm = rig()
at(0);  rx(b, 4)
at(1);  rx(b, 4, Cmd.HEALTH, health(3))                             # powered up at -2
at(9);  rx(b, 4)                                                    # then switched off
for t in range(12, 124, 2):
    at(t); mm._heartbeat()
got = about(4, log.take())
assert begin(got, ["PRESENCE cam4 ACTIVE — first heard since the app started",
                   "START-UP cam4 — powered up %s" % hhmmss(-2),
                   "PRESENCE cam4 GREYED at the end of its start-up — nothing heard "
                   "from it for 111.0 s"]) and got[1][1] == "START-UP cam4 — powered up %s" \
    % hhmmss(-2) and got[2][0] == "WARNING", got
print("   switched off again before it was done: said when it is over    OK")

b, mm = rig()
at(0);  rx(b, 5, Cmd.HEALTH, health(5000))                          # running for hours
at(4);  mm._heartbeat()                                             # its own fault
at(6);  rx(b, 5)
at(16); rx(b, 5, Cmd.HEALTH, health(5016, txfail=45))
got = about(5, log.take())
assert begin(got, ["PRESENCE cam5 ACTIVE — first heard since the app started",
                   "PRESENCE cam5 GREYED after 4.0 s active",
                   "PRESENCE cam5 ACTIVE after 2.0 s greyed",
                   "PRESENCE cam5 — its own account of the 2.0 s greyed: 45 sends failed"]), got
print("   a mount that was running: its grey-out logged as always        OK")

b, mm = rig()
at(0);  rx(b, 1, Cmd.HEALTH, health(40, reset=USB))                 # app opened mid-flash
at(0);  rx(b, 5, Cmd.HEALTH, health(400, reset=POWERON))            # on for 6.7 min
at(4);  mm._heartbeat()                                             # both quiet
got = [g for g in log.take() if "PRESENCE" in g[1]]
assert begin(got, ["PRESENCE cam1 ACTIVE — first heard since the app started (HEALTH)",
                   "PRESENCE cam5 ACTIVE — first heard since the app started (HEALTH)",
                   "PRESENCE cam5 GREYED after 4.0 s active"]), got
print("   the app opened mid-flash: still starting up; switched on minutes")
print("   ago: not                                                       OK")

print("\nALL CHECKS PASSED")
