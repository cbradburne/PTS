"""A mount's account of a run of unanswered sends, from its radio to the log.

2026-10-02: every mount loses its link both ways for about 7 s, 2-4 times a
day, and comes back after one ESP-NOW restart.  The PC sees only the silence.
The mount now measures each run (shared/link_run.h) and reports it the moment a
send gets through again (MOUNT_EVENT_LINK_RUN).  The operator flashes this on
one or two mounts.  This checks the chain end to end:

  1. the tracker, compiled HERE from the header the mount builds, plays runs
     through it — today's blackout first — and its report is decoded by the
     PC's decoder, field by field;
  2. what does not count: a routine run, a run before loop() could read the
     radio, a second run while one is waiting, a run longer than a u16 holds;
  3. the PC turns the report into the log lines the operator reads.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, logging, shutil, subprocess, tempfile, types
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
SHARED = REPO / "firmware" / "shared"
sys.path.insert(0, str(REPO / "pc_app"))

from comms import protocol as P

# ---- the tracker, compiled here ----------------------------------------------
cxx = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
if not cxx:
    print("no C++ compiler: skipping the mount half.")
    print("\nALL CHECKS PASSED")
    sys.exit(0)
TMP = pathlib.Path(tempfile.mkdtemp(prefix="linkrun-"))
(TMP / "h.cpp").write_text(r"""
#include <cstdio>
#include <cstring>
#include "link_run.h"
static LinkRunTracker T;
int main() {
    char cmd[16];
    while (scanf("%15s", cmd) == 1) {
        unsigned t, a, b, c, d, e, f, g; int ri, ni;
        if (!strcmp(cmd, "ok"))   { scanf("%u", &t); T.on_result(t, true); }
        if (!strcmp(cmd, "fail")) { scanf("%u", &t); T.on_result(t, false); }
        if (!strcmp(cmd, "rx"))   { scanf("%u %d %d", &t, &ri, &ni); T.on_rx(t, (int8_t)ri, (int8_t)ni); }
        if (!strcmp(cmd, "ref"))  { scanf("%u", &t); T.on_refused(t); }
        if (!strcmp(cmd, "act"))  { scanf("%u %u", &t, &a); T.on_action(t, (LinkAction)a); }
        if (!strcmp(cmd, "start")) {         // chan ble notifies drops iram inflight hubchan
            scanf("%u %u %u %u %u %u %u", &a, &b, &c, &d, &e, &f, &g);
            LinkSample s = {(uint8_t)a, (uint8_t)b, c, d, e, f};
            T.start_sample(s, (uint8_t)g);
        }
        if (!strcmp(cmd, "take")) {          // chan ble notifies drops hubchan
            scanf("%u %u %u %u %u", &a, &b, &c, &d, &g);
            LinkSample s = {(uint8_t)a, (uint8_t)b, c, d, 0, 0};
            MountLinkRun r;
            if (T.take(s, (uint8_t)g, &r)) {
                uint8_t p[MOUNT_EVENT_LINK_RUN_LEN];
                encode_mount_link_run(p, &r);
                printf("RUN ");
                for (int i = 0; i < MOUNT_EVENT_LINK_RUN_LEN; i++) printf("%02x", p[i]);
                printf("\n");
            } else printf("NONE\n");
        }
        if (!strcmp(cmd, "state")) printf("STATE %d %d %u\n", T.need_start, T.due, T.lost);
    }
}
""")
r = subprocess.run([cxx, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-Wno-unused-function",
                    "-Wno-unused-result", "-I", str(SHARED), str(TMP / "h.cpp"),
                    "-o", str(TMP / "h")], capture_output=True, text=True)
assert r.returncode == 0, "link_run.h does not compile cleanly on the host:\n" + r.stderr[:2000]

LINKED_BONDED = P.MOUNT_NOMEM_BLE_LINKED | P.MOUNT_NOMEM_BLE_BONDED


def mount(lines):
    out = subprocess.run([str(TMP / "h")], input="\n".join(lines) + "\n",
                         capture_output=True, text=True, check=True).stdout.split("\n")
    runs = [P.decode_mount_link_run(bytes.fromhex(l[4:])) for l in out if l.startswith("RUN ")]
    state = [tuple(int(x) for x in l.split()[1:]) for l in out if l.startswith("STATE")]
    return runs, state


def healthy(t0, t1):
    """Normal traffic: a send through every 100 ms, a frame heard every 250."""
    ev = []
    for t in range(t0, t1, 50):
        if t % 100 == 0:
            ev.append((t, f"ok {t}"))
        if t % 250 == 0:
            ev.append((t, f"rx {t} -46 -92"))
    return ev


def in_order(*parts):
    """Events in time order.  `start` and `take` carry no time of their own:
    each goes right after the event it is given with, as loop() would."""
    ev = [e for p in parts for e in p]
    return [line for _, _, line in sorted((t, i, line) for i, (t, line) in enumerate(ev))]


# ---- 1. today's blackout, as the mount would measure it ------------------------
print("1. cam2 at 11:10, played through the mount's tracker:")
fails = [10050 + 150 * k for k in range(45)]
blackout = in_order(
    healthy(0, 10000), [(9950, "ok 9950"), (9980, "rx 9980 -46 -92")],
    [(t, f"fail {t}") for t in fails],
    [(10050, f"start 6 {LINKED_BONDED} 1000 0 61000 1 6")],
    [(10450, "act 10450 0"), (11050, "act 11050 0"), (11650, "act 11650 1"),
     (16750, "rx 16750 -45 -93"), (16950, "ok 16950"),
     (16950, f"take 6 {LINKED_BONDED} 1002 0 6")])
runs, _ = mount(blackout)
assert len(runs) == 1, runs
got = runs[0]
want = P.MountLinkRun(
    uptime_s=10, dur_ms=6900, fails=45, refused=0, since_ok_ms=100, since_rx_ms=70,
    rx_during=1, t_first_rx=6700, max_cb_gap_ms=300, in_flight=1, t_refresh=400,
    t_reinit=1600, t_wifi=P.MOUNT_LINK_T_NEVER, t_scan=P.MOUNT_LINK_T_NEVER,
    n_refresh=2, n_reinit=1, chan_start=6, chan_end=6, chan_hub=6,
    ble_start=LINKED_BONDED, ble_end=LINKED_BONDED, ble_drops=0, cam_notifies=2,
    rssi_before=-46, noise_before=-92, rssi_first=-45, noise_first=-93, iram_free=61000)
for f in want.__dataclass_fields__:
    assert getattr(got, f) == getattr(want, f), (f, getattr(got, f), getattr(want, f))
print("   45 failures over 6.9 s, the last good send 100 ms and the last frame")
print("   70 ms before; two peer refreshes from 0.4 s, the restart at 1.6 s;")
print("   the first frame back at 6.7 s; every field decoded by the PC   OK")

# ---- 2. what counts, and what does not ------------------------------------------
print("\n2. the edges:")
short = ["ok 0"] + [f"fail {100 * k}" for k in range(1, P.MOUNT_LINK_RUN_MIN_FAILS)] \
    + ["ok 900", "take 6 12 0 0 6"]
assert mount(short)[0] == [], "a run below the minimum was reported"
just = ["ok 0"] + [f"fail {100 * k}" for k in range(1, P.MOUNT_LINK_RUN_MIN_FAILS + 1)] \
    + ["ok 1000", "take 6 12 0 0 6"]
runs, _ = mount(just)
assert len(runs) == 1 and runs[0].fails == P.MOUNT_LINK_RUN_MIN_FAILS, runs
print(f"   {P.MOUNT_LINK_RUN_MIN_FAILS - 1} failures: routine, no report; "
      f"{P.MOUNT_LINK_RUN_MIN_FAILS}: reported                 OK")

# Ended before loop() read the radio: the start sample is too late, and says so.
early = ["ok 0", "rx 10 -50 -90"] + [f"fail {20 + k}" for k in range(10)] \
    + ["ok 40", "start 6 12 500 0 61000 1 6", "take 11 4 509 3 6", "state"]
runs, state = mount(early)
r0 = runs[0]
assert (r0.chan_start, r0.chan_end, r0.chan_hub, r0.ble_start, r0.cam_notifies,
        r0.ble_drops, r0.iram_free) == (0, 11, 6, 0, 0, 0, 0), r0
assert state == [(0, 0, 0)], state
print("   a run over before loop() read the radio: its start reads 0, not a")
print("   stale reading, and camera counts stay 0 rather than since boot  OK")

# Two runs before loop() reports the first: the first is kept, the second counted.
two = ["ok 0"] + [f"fail {10 + k}" for k in range(9)] + ["ok 30"] \
    + [f"fail {40 + k}" for k in range(12)] + ["ok 60", "state", "take 6 12 0 0 6",
                                               "take 6 12 0 0 6", "state"]
runs, state = mount(two)
assert [x.fails for x in runs] == [9], runs
assert state == [(0, 1, 1), (0, 0, 1)], state
print("   a second run while one waits: the first kept, the second counted  OK")

# A long one: the hub gone for 80 s.  Times cap at 0xFFFE, the length is a u32,
# and a refusal before the run is not the run's.
long_run = in_order(
    [(900, "ref 900"), (1000, "ok 1000"), (1000, "rx 1000 -60 -95")],
    [(1100 + 100 * k, f"fail {1100 + 100 * k}") for k in range(800)],
    [(7200, "act 7200 3"), (5000, "ref 5000"), (5100, "ref 5100"),
     (81200, "ok 81200"), (81200, "take 1 4 0 0 6")])
runs, _ = mount(long_run)
r1 = runs[0]
assert (r1.dur_ms, r1.fails, r1.refused, r1.t_scan, r1.since_rx_ms, r1.rx_during,
        r1.t_first_rx, r1.max_cb_gap_ms) == (80100, 800, 2, 6100, 100, 0,
                                             P.MOUNT_LINK_T_NEVER, 200), r1
print("   80 s without the hub: the length past 65 s kept, the scan's time,")
print("   refusals counted apart from failures and only in the run         OK")

stall = ["ok 0"] + [f"fail {100 + 10 * k}" for k in range(8)] + ["fail 3200", "ok 3300",
                                                                  "take 6 12 0 0 6"]
runs, _ = mount(stall)
assert runs[0].max_cb_gap_ms == 3030, runs[0]
quiet_first = ["ok 0"] + [f"fail {5000 + 10 * k}" for k in range(8)] + ["ok 5090",
                                                                        "take 6 12 0 0 6"]
runs, _ = mount(quiet_first)
assert runs[0].max_cb_gap_ms == 20 and runs[0].since_ok_ms == 5000, runs[0]
print("   send results stopping for 3 s is measured as the longest wait;")
print("   5 s with nothing to send before the run is not counted in it     OK")

two_frames = ["ok 0", "rx 0 -40 -90"] + [f"fail {100 + 100 * k}" for k in range(8)] \
    + ["rx 350 -70 -85", "rx 600 -50 -96", "ok 900", "take 6 12 0 0 6"]
two_frames = in_order([(int(l.split()[1]), l) for l in two_frames[:-1]], [(900, two_frames[-1])])
runs, _ = mount(two_frames)
assert (runs[0].rx_during, runs[0].t_first_rx, runs[0].rssi_first,
        runs[0].noise_first) == (2, 250, -70, -85), runs[0]
print("   frames heard in the run: counted, and the FIRST one's time kept   OK")

# ---- 3. what the operator reads ---------------------------------------------------
print("\n3. the log lines:")
try:
    from PyQt6.QtCore import QCoreApplication
except ImportError:
    print("   PyQt6 not installed: skipping the log half.")
    print("\nALL CHECKS PASSED")
    sys.exit(0)
from comms import mount_manager as MM
from comms.protocol import Packet, Cmd
app = QCoreApplication.instance() or QCoreApplication([])
MM.time = types.SimpleNamespace(monotonic=lambda: 1000.0)


class FakeHubLink:
    connected = True
    def __init__(self): self.cb = None
    def on_packet(self, cb): self.cb = cb
    def off_packet(self, cb): pass
    def send(self, data): pass
    def seconds_since_tracked_tx(self): return 0.0


class Lines(logging.Handler):
    def __init__(self): super().__init__(); self.got = []
    def emit(self, rec):
        if rec.getMessage().startswith("MOUNT EVENT"):
            self.got.append((rec.levelname, rec.getMessage()))


lines = Lines()
logging.getLogger(MM.__name__).addHandler(lines)
link = FakeHubLink()
mm = MM.MountManager(link)


def told(run_bytes):
    lines.got = []
    payload = bytes([P.MOUNT_EVENT_LINK_RUN] + [0] * 13) + run_bytes
    link.cb(Packet(mount_id=2, seq=0, cmd=Cmd.MOUNT_EVENT, payload=payload))
    assert all(lv == "WARNING" for lv, _ in lines.got), lines.got
    return [m for _, m in lines.got]


def encode(run: "P.MountLinkRun") -> bytes:
    import struct
    return struct.pack(P._LINK_RUN_FMT, *(getattr(run, f) for f in run.__dataclass_fields__))


got = told(encode(want))
expect = [
    "MOUNT EVENT cam2 LINK RUN: 45 sends went out unanswered over 6.9 s, then one "
    "got through — no reboot",
    "MOUNT EVENT cam2   before (up 10.0 s): last send through 100 ms before the first failure, "
    "last frame heard 70 ms before (-46 dBm, noise -92); 1 in flight, 59 KB internal "
    "RAM free",
    "MOUNT EVENT cam2   during: heard 1 frame, the first at 6.7 s (-45 dBm, noise -93); "
    "longest wait for a send result 300 ms",
    "MOUNT EVENT cam2   radio: channel 6 -> 6 (hub on 6); BLE camera linked -> camera "
    "linked; the camera link dropped 0 times, 2 camera notifications",
    "MOUNT EVENT cam2   it tried: hub-peer refresh at 0.4 s (2 times); ESP-NOW restart "
    "at 1.6 s",
    "MOUNT EVENT cam2   reading: deaf and mute together for 6.7 s: its receiver came "
    "back 0.2 s before its sends did; send results kept coming, each a failure, so the "
    "frames went out and nothing answered; back 5.3 s after its ESP-NOW restart",
]
assert got == expect, "\n".join(got)
print("   today's blackout, as the operator will read it:")
for line in got:
    print("     " + line.replace("MOUNT EVENT cam2 ", ""))

# The other readings, one clause each.
hearing = P.MountLinkRun(**{**want.__dict__, "rx_during": 12, "t_first_rx": 180})
assert "it kept hearing (12 frames, the first 0.2 s in) while its sends went " \
       "unanswered, so its receiver worked and the failure was on the way to the hub" \
       in told(encode(hearing))[-1]
deaf = P.MountLinkRun(**{**want.__dict__, "rx_during": 0, "t_first_rx": P.MOUNT_LINK_T_NEVER})
assert "reading: deaf and mute together: it heard nothing in the run" in told(encode(deaf))[-1]
stalled = P.MountLinkRun(**{**want.__dict__, "max_cb_gap_ms": 3030})
assert "send results stopped for 3.0 s, so its transmit path stalled" in told(encode(stalled))[-1]
moved = P.MountLinkRun(**{**want.__dict__, "chan_end": 11})
assert "its channel was not the hub's" in told(encode(moved))[-1]
dropped = P.MountLinkRun(**{**want.__dict__, "ble_drops": 1})
assert "its camera link dropped too" in told(encode(dropped))[-1]
unsampled = P.MountLinkRun(**{**want.__dict__, "chan_start": 0, "ble_start": 0})
assert told(encode(unsampled))[3].endswith("(the run ended before the mount read its radio)")
print("   kept hearing / heard nothing / results stopped / channel moved /")
print("   camera link dropped / radio not read: each says so               OK")

short_pl = told(b"")
assert short_pl == ["MOUNT EVENT cam2 link-run report too short (14 bytes)"], short_pl
print("   a short report is called short, not \"RESTARTED ITSELF\"         OK")

shutil.rmtree(TMP, ignore_errors=True)
print("\nALL CHECKS PASSED")
