"""The mount's heartbeat and Teensy probe age against a fresh clock — and say so.

SEEN ON THE BENCH, 2026-09-27. loop() reads `now` once, before
lv_timer_handler(), then drains the Teensy again after it. A STATUS handled in
that second drain stamps _last_heartbeat_ms and _last_teensy_st_ms with a fresh
millis(), later than `now`. The heartbeat and the probe aged themselves against
`now`, the unsigned difference wrapped to ~4.29e9, and both read as days
overdue: a STATUS on the air through the in-flight guard's overdue escape, and
a GET_STATUS the Teensy ACKs (the ACK is forwarded) for a STATUS it had just
sent. The counters added in d22cc92 showed it was every heartbeat the bench
mount sent: 985 of 985 in 14 minutes, 983 stale probes with them.

Both now age against a fresh millis(). The counters stay, as the check that it
stays fixed. This test protects:

  a stamp after `now` sends     nothing — neither a beat nor a probe — whether
  nothing                       the radio is idle or busy, and across the
                                49-day millis() rollover.
  real beats still go           the Teensy quiet for 5 s is a beat, held by a
                                busy radio until the guard's escape, and never
                                counted as stale; a run report goes on its own
                                fresh clock; a quiet Teensy is still probed.
  the counters would catch a    the same blocks, compiled with the old `now`
  revert                        ages, send and COUNT the stale beat and probe —
                                so a zero on the rig means fixed, not blind.
  counting                      tested before the send (busy is the radio it
                                found), saturating at 65535, one window per
                                health report, and printed by the app.

The heartbeat and probe blocks are lifted out of the .ino and compiled here, so
the scenarios run the text that ships, not a transcription of it.

Run directly, or via tools/run_tests.sh with the rest.
"""
import io
import logging
import os
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

INO = (REPO / "firmware/esp_mount_amoled175/esp_mount_amoled175.ino").read_text()


def span(src: str, start: str, end: str) -> str:
    """From `start` up to and including the first `end` after it."""
    i = src.index(start)
    return src[i:src.index(end, i) + len(end)]


def upto(src: str, start: str, stop: str) -> str:
    """From `start` up to, not including, the first `stop` after it.  Anchored
    on what FOLLOWS a block, so reordering inside it cannot break the lift."""
    i = src.index(start)
    return src[i:src.index(stop, i)]


def define(name: str) -> str:
    m = re.search(rf"^#define\s+{name}\s+(\d+)", INO, re.MULTILINE)
    assert m, f"{name} is not defined in the mount firmware"
    return m.group(1)


# ---- 1. what is lifted out of the firmware ----------------------------------
print("1. the firmware's own blocks:")
counters  = span(INO, "static uint16_t _hb_sent", "static uint16_t _probe_stale   = 0;")
note      = span(INO, "static inline void hb_note_sent(bool stale) {", "\n}\n")
defer     = span(INO, "static inline bool espnow_defer_periodic(", "\n}\n")
held      = span(INO, "static inline bool periodic_held(", "\n}\n")
heartbeat = upto(INO, "    static bool hb_held = false;", "    // ── Health telemetry")
probe     = upto(INO, "    // ── Teensy probe", "    // No delay()")
assert "hb_note_sent(hb_stale);" in heartbeat, "the heartbeat block no longer counts"
assert "_probe_stale++" in probe, "the probe block no longer counts"
# The ages the fix is about, and the checks beside them.  The behaviour below
# is what matters; these say which line to look at when it fails.
HB_FRESH, HB_OLD = ("uint32_t hb_age = millis() - _last_heartbeat_ms;",
                    "uint32_t hb_age = now - _last_heartbeat_ms;")
PR_FRESH, PR_OLD = ("if (millis() - _last_teensy_st_ms >= TEENSY_PROBE_MS) {",
                    "if (now - _last_teensy_st_ms >= TEENSY_PROBE_MS) {")
assert HB_FRESH in heartbeat, "the heartbeat is no longer aged against a fresh millis()"
assert PR_FRESH in probe, "the Teensy probe is no longer aged against a fresh millis()"
assert "bool hb_stale = (int32_t)(now - _last_heartbeat_ms) < 0;" in heartbeat, \
    "hb_stale is not worked out against `now`, so it cannot catch a revert"
assert "(int32_t)(now - _last_teensy_st_ms) < 0 && _probe_stale < 0xFFFF" in probe, \
    "probe_stale is not worked out against `now`, or no longer saturates"
print("   counters, hb_note_sent, the guard, heartbeat, probe    OK")

# ---- 2. the per-window reset, which the host run cannot see ------------------
print("\n2. one window per health report:")
health = span(INO, "static void send_health(bool anomaly) {", "\n}\n")
copies = []
for f in ("hb_sent", "hb_stale", "hb_stale_busy", "probe_stale"):
    copy = f"t.{f:<19} = _{f};"
    assert copy in health, \
        f"the health tail does not carry _{f}, so the count never leaves the mount"
    copies.append(health.index(copy))
RESET = "_hb_sent = _hb_stale = _hb_stale_busy = _probe_stale = 0;"
assert RESET in health, \
    "the counts are never zeroed, so each report is since boot, not one window"
assert max(copies) < health.index(RESET), \
    "the counts are zeroed before they are copied into the tail, so every report reads 0"
print("   carried in the tail, zeroed once it has them           OK")

# ---- 3. the blocks, compiled and driven -------------------------------------
if not shutil.which("c++"):
    print("\n3. no C++ compiler — skipping the behavioural check.")
    print("\nALL CHECKS PASSED")
    raise SystemExit(0)

HARNESS = r"""
#include <cstdint>
#include <cstdio>
#define STATUS_HEARTBEAT_MS      @HB@
#define TEENSY_PROBE_MS          @PROBE@
#define ESPNOW_PERIODIC_DEFER_MS @DEFER@
#define PKT_BUF_SIZE             64
#define CMD_GET_STATUS           0x10

// What the blocks touch, stubbed.  millis() is the clock AFTER the render and
// the second drain, so it is never earlier than a stamp; `now` is passed in as
// the pass read it, before them.
static uint32_t _millis;
static uint32_t millis() { return _millis; }
static bool     _saturated;
static bool     espnow_tx_saturated() { return _saturated; }
static uint32_t _espnow_drain_deferred;
static uint32_t _last_heartbeat_ms, _last_teensy_st_ms, _runs_due_ms;
static bool     _runs_report_due;
static int      beats_out, probes_out;
static void send_status_heartbeat() {
    beats_out++;
    _last_heartbeat_ms = millis();
    _runs_report_due   = false;
    _saturated         = true;     // the beat itself is in flight now, cap 1
}
static uint8_t  _mount_id = 1;
static uint16_t _tx_seq;
static uint16_t build_packet(uint8_t *, uint8_t, uint16_t, int, const uint8_t *, int) {
    return 7;
}
static struct { void write(const uint8_t *, uint16_t) { probes_out++; } } Serial1;

@COUNTERS@
@DEFER_FN@
@HELD_FN@
@NOTE_FN@

static void heartbeat_pass(uint32_t now) {
@HEARTBEAT@
}
static void probe_pass(uint32_t now) {
@PROBE_BLOCK@
}

static void fresh() {
    _hb_sent = _hb_stale = _hb_stale_busy = _probe_stale = 0;
    beats_out = probes_out = 0;
    _saturated = false;
    _runs_report_due = false;
}
static void report(const char *name) {
    printf("%s %d %u %u %u %d %u\n", name, beats_out, _hb_sent, _hb_stale,
           _hb_stale_busy, probes_out, _probe_stale);
}
// One pass: `now` read, then `later` ms of render and drain before the
// heartbeat runs.  `later` must reach the stamp — the chip's clock does.
static void beat(const char *name, uint32_t now, uint32_t stamp, bool busy,
                 uint32_t later = 5) {
    fresh();
    _last_heartbeat_ms = stamp;
    _saturated = busy;
    _millis = now + later;
    heartbeat_pass(now);
    report(name);
}
static void poll(const char *name, uint32_t now, uint32_t stamp, uint32_t later = 5) {
    fresh();
    _last_teensy_st_ms = stamp;
    _millis = now + later;
    probe_pass(now);
    report(name);
}

int main() {
    const uint32_t T = 100000;
    beat("fresh",          T, T - 100,  false);   // the Teensy's 10 Hz STATUS
    beat("silent",         T, T - 5200, false);   // no STATUS for 5.2 s
    beat("second_drain",   T, T + 3,    false);   // stamped after `now` was read
    beat("second_drain_busy", T, T + 3, true);
    beat("due_busy",       T, T - 5100, true);    // a real beat, radio busy
    beat("overdue_busy",   T, T - 5500, true);    // past the guard's escape
    // A run report on a second-drain pass: due on its own, fresh, clock.
    fresh();
    _last_heartbeat_ms = T + 3; _runs_report_due = true; _runs_due_ms = T + 2;
    _millis = T + 5;
    heartbeat_pass(T);
    report("run_report");
    // Across the 49-day rollover: a stamp from just before the wrap is recent;
    // one from after it, with `now` still before, is a second-drain stamp; one
    // 5.2 s before a `now` that has wrapped is a real beat.
    beat("wrap_recent",    5u, 0xFFFFFFF0u, false, 3);
    beat("wrap_drain",     0xFFFFFFF0u, 5u, false, 21);
    beat("wrap_due",       5u, 0xFFFFFFF0u - 5200u, false, 3);
    // The counting itself, driven directly: it is what would register a revert.
    fresh();                  hb_note_sent(true); report("note_stale");
    fresh(); _saturated = true; hb_note_sent(true); report("note_stale_busy");
    fresh(); _hb_sent = _hb_stale = _hb_stale_busy = 0xFFFF; _saturated = true;
    hb_note_sent(true); report("note_saturate");
    poll("probe_second_drain", T, T + 3);
    poll("probe_silent",   T, T - 2500);
    poll("probe_fresh",    T, T - 100);
    poll("probe_wrap_drain", 0xFFFFFFF0u, 5u, 21);
    return 0;
}
"""


def run(hb_block: str, probe_block: str, tag: str) -> dict:
    src = (HARNESS
           .replace("@HB@", define("STATUS_HEARTBEAT_MS"))
           .replace("@PROBE@", define("TEENSY_PROBE_MS"))
           .replace("@DEFER@", define("ESPNOW_PERIODIC_DEFER_MS"))
           .replace("@COUNTERS@", counters)
           .replace("@DEFER_FN@", defer)
           .replace("@HELD_FN@", held)
           .replace("@NOTE_FN@", note)
           .replace("@HEARTBEAT@", hb_block)
           .replace("@PROBE_BLOCK@", probe_block))
    d = pathlib.Path(tempfile.mkdtemp())
    (d / f"{tag}.cpp").write_text(src)
    r = subprocess.run(["c++", "-std=c++17", "-O1", "-Wall", "-o", str(d / tag),
                        str(d / f"{tag}.cpp")], capture_output=True, text=True)
    assert r.returncode == 0, f"the lifted blocks ({tag}) did not compile:\n{r.stderr[:1500]}"
    r = subprocess.run([str(d / tag)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"the harness ({tag}) failed: {r.stderr[:500]}"
    out = {}
    for line in r.stdout.split("\n"):
        if line.strip():
            name, *vals = line.split()
            out[name] = tuple(int(v) for v in vals)
    return out


# (beats sent, hb_sent, hb_stale, hb_stale_busy, probes sent, probe_stale)
got = run(heartbeat, probe, "fixed")

print("\n3. the heartbeat, run on the host:")
for case in ("second_drain", "second_drain_busy", "wrap_drain"):
    assert got[case][:4] == (0, 0, 0, 0), \
        f"{case}: a stamp later than `now` sent a heartbeat — the age is taken " \
        f"against `now` again, and it wraps to days overdue: {got[case]}"
print("   stamp after `now` -> no beat, idle or busy, and across the wrap  OK")
assert got["fresh"][:2] == (0, 0), \
    f"a beat went out with the Teensy's STATUS 100 ms old: {got['fresh']}"
assert got["silent"][:4] == (1, 1, 0, 0), \
    f"the Teensy quiet 5.2 s did not give one beat, not stale: {got['silent']}"
assert got["due_busy"][:2] == (0, 0), \
    f"the guard no longer holds a genuine beat for a busy radio: {got['due_busy']}"
assert got["overdue_busy"][:4] == (1, 1, 0, 0), \
    f"a genuine beat past the guard's escape did not go, or is stale: {got['overdue_busy']}"
assert got["run_report"][:4] == (1, 1, 0, 0), \
    f"a run report on a second-drain pass did not go, or is blamed on the clock: " \
    f"{got['run_report']}"
assert got["wrap_recent"][:3] == (0, 0, 0), \
    f"a stamp from just before the millis() rollover reads as due: {got['wrap_recent']}"
assert got["wrap_due"][:4] == (1, 1, 0, 0), \
    f"a real beat across the millis() rollover did not go: {got['wrap_due']}"
print("   real beats still go: Teensy quiet, busy radio held then let go,\n"
      "   a run report, both sides of the rollover                        OK")

print("\n4. the counting, driven directly:")
assert got["note_stale"][1:4] == (1, 1, 0), \
    f"a stale send on an idle radio is not counted, or counted busy: {got['note_stale']}"
assert got["note_stale_busy"][1:4] == (1, 1, 1), \
    f"a stale send through a busy radio is not counted busy: {got['note_stale_busy']}"
assert got["note_saturate"][1:4] == (0xFFFF, 0xFFFF, 0xFFFF), \
    f"a heartbeat count wraps past 65535 instead of holding: {got['note_saturate']}"
print("   stale, busy, and saturating at 65535                             OK")

print("\n5. the probe, run on the host:")
for case in ("probe_second_drain", "probe_wrap_drain"):
    assert got[case][4:] == (0, 0), \
        f"{case}: a STATUS stamped after `now` still sent a probe — the age is " \
        f"taken against `now` again: {got[case]}"
assert got["probe_silent"][4:] == (1, 0), \
    f"a Teensy quiet 2.5 s is not probed, or the probe is stale: {got['probe_silent']}"
assert got["probe_fresh"][4:] == (0, 0), \
    f"a probe went out with the Teensy's STATUS 100 ms old: {got['probe_fresh']}"
print("   stamp after `now` -> no probe; quiet 2.5 s -> probed, not stale  OK")

# ---- 6. the counters catch the old code --------------------------------------
# Zero on the rig has to mean fixed, not blind.  The same blocks with the ages
# put back against `now` must send the stale beat and probe AND count them.
print("\n6. the counters, against the code before the fix:")
assert heartbeat.count(HB_FRESH) == 1 and probe.count(PR_FRESH) == 1
old = run(heartbeat.replace(HB_FRESH, HB_OLD), probe.replace(PR_FRESH, PR_OLD), "old")
assert old["second_drain"][:4] == (1, 1, 1, 0), \
    f"with the old age the stale beat is not sent and counted: {old['second_drain']}"
assert old["second_drain_busy"][:4] == (1, 1, 1, 1), \
    f"with the old age a stale beat through a busy radio is not counted busy: " \
    f"{old['second_drain_busy']}"
assert old["probe_second_drain"][4:] == (1, 1), \
    f"with the old age the stale probe is not sent and counted: {old['probe_second_drain']}"
print("   old ages -> the stale beat and probe go, and are counted          OK")

# ---- 7. what the app makes of it ---------------------------------------------
print("\n7. the health line:")
BUF = io.StringIO()
_h = logging.StreamHandler(BUF); _h.setFormatter(logging.Formatter("%(message)s"))
_lg = logging.getLogger("comms.bridge")
_saved = (_lg.handlers, _lg.level, _lg.propagate)
_lg.handlers, _lg.propagate = [_h], False
_lg.setLevel(logging.INFO)
from comms.bridge import Bridge
from comms.protocol import Cmd


class _P:
    cmd = Cmd.HEALTH
    mount_id = 1
    def __init__(self, payload): self.payload = payload


LADDER = struct.pack(">IIIHHHHHH", 142000, 118000, 60000, 0, 0, 12288, 0, 87, 58)


def line(beats: bytes = b"") -> str:
    b = Bridge.__new__(Bridge)
    b._diag_lock = threading.Lock(); b._node_uptime = {}; b._sat_names = {}
    b._cam_ble = {}; b._node_state_cur = {}; b._node_state_prev = {}
    b._node_state_written = 0.0; b._node_state_path = None
    BUF.truncate(0); BUF.seek(0)
    b._note_node_health(_P(struct.pack(">BBIIIHHbBI", 1, 1, 36000, 8400000, 8390000,
                                       9, 6, -33, 0, 1 << 4) + LADDER + beats))
    return BUF.getvalue().strip()


old_fw = line()
assert "heartbeats" not in old_fw and "stale" not in old_fw, \
    f"a mount on the ladder's firmware is shown with heartbeat counts: {old_fw}"
zero = line(struct.pack(">HHHH", 0, 0, 0, 0))
assert "| heartbeats 0" in zero, f"a zero count is not printed, so it reads as absent: {zero}"
assert "stale" not in zero, f"a window with nothing stale mentions it: {zero}"
quiet = line(struct.pack(">HHHH", 2, 0, 0, 0))
assert "| heartbeats 2" in quiet and "stale" not in quiet, \
    f"genuine beats (the Teensy quiet) are shown as stale: {quiet}"
bad = line(struct.pack(">HHHH", 41, 40, 7, 39))
assert "| heartbeats 41 (40 on a stale clock, 7 with a send in flight)" in bad, bad
assert "| stale Teensy probes 39" in bad, bad
assert bad.index("last scan heard") < bad.index("heartbeats"), \
    "the counts are not appended after the existing fields"
print("   " + bad[bad.index("| heartbeats"):])
print("   absent -> silent, zero -> 'heartbeats 0', stale -> spelled out OK")

_lg.handlers, _lg.level, _lg.propagate = _saved
print("\nALL CHECKS PASSED")
