"""The mount counts heartbeats and probes sent on a stale clock — and only counts.

SUSPECTED, NOT YET SEEN (2026-09-27). loop() reads `now` once, before
lv_timer_handler(), then drains the Teensy again after it. A STATUS handled in
that second drain stamps _last_heartbeat_ms and _last_teensy_st_ms with a fresh
millis(), later than `now`. The heartbeat and the probe age themselves against
`now`, the unsigned difference wraps to ~4.29e9, and both read as days overdue:
an extra STATUS on the air, through the in-flight guard's overdue escape, and a
GET_STATUS the Teensy answers with an ACK the bridge forwards and a STATUS that
can start it again.

The rule on this rig is to measure a mechanism before fixing it, so the firmware
now COUNTS these and changes nothing else. This test protects the count:

  stale means the signed age   a stamp later than `now` — including across the
                               49-day millis() rollover, and never a stamp that
                               is merely old.
  counted when it goes         inside the guarded branch, so a held beat is not
                               counted as sent.
  busy is the radio it found   tested BEFORE the send, or every stale beat would
                               read as busy because of itself.
  a run report is not stale    it is due on its own fresh clock, so it went for
                               its own reason.
  one window per report        zeroed after each health report is encoded.
  the app says so              present -> printed, even at zero; absent (older
                               firmware) -> silent.

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
probe     = upto(INO, "    if (now - _last_teensy_st_ms >= TEENSY_PROBE_MS) {", "    // No delay()")
assert "hb_note_sent(hb_stale);" in heartbeat, "the heartbeat block no longer counts"
assert "_probe_stale++" in probe, "the probe block no longer counts"
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

// What the blocks touch, stubbed.  millis() is the clock AFTER the render;
// `now` is passed in as the pass read it, before.
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
// One pass: `now` read, then `later` ms of render before the heartbeat runs.
static void beat(const char *name, uint32_t now, uint32_t stamp, bool busy,
                 uint32_t later = 3) {
    fresh();
    _last_heartbeat_ms = stamp;
    _saturated = busy;
    _millis = now + later;
    heartbeat_pass(now);
    report(name);
}
static void poll(const char *name, uint32_t now, uint32_t stamp) {
    fresh();
    _last_teensy_st_ms = stamp;
    _millis = now + 3;
    probe_pass(now);
    report(name);
}

int main() {
    const uint32_t T = 100000;
    beat("fresh",          T, T - 100,  false);   // the Teensy's 10 Hz STATUS
    beat("silent",         T, T - 5200, false);   // no STATUS for 5.2 s
    beat("stale",          T, T + 3,    false);   // stamped in the second drain
    beat("stale_busy",     T, T + 3,    true);
    beat("due_busy",       T, T - 5100, true);    // a real beat, radio busy
    beat("overdue_busy",   T, T - 5500, true);    // past the guard's escape
    // A run report on a stale pass: due on its own, fresh, clock.
    fresh();
    _last_heartbeat_ms = T + 3; _runs_report_due = true; _runs_due_ms = T + 2;
    _millis = T + 3;
    heartbeat_pass(T);
    report("run_report");
    // Across the 49-day rollover: a stamp from before the wrap is old, not
    // stale; one from after it, with `now` still before, is stale.
    beat("wrap_old",       5u, 0xFFFFFFF0u, false);
    beat("wrap_stale",     0xFFFFFFF0u, 5u, false);
    // The counts saturate rather than wrapping to a clean-looking zero.
    fresh();
    _hb_sent = _hb_stale = _hb_stale_busy = 0xFFFF;
    _last_heartbeat_ms = T + 3; _saturated = true; _millis = T + 3;
    heartbeat_pass(T);
    printf("saturate %d %u %u %u %d %u\n", beats_out, _hb_sent, _hb_stale,
           _hb_stale_busy, probes_out, _probe_stale);
    poll("probe_stale",    T, T + 3);
    poll("probe_silent",   T, T - 2500);
    poll("probe_fresh",    T, T - 100);
    fresh();
    _probe_stale = 0xFFFF; _last_teensy_st_ms = T + 3; _millis = T + 3;
    probe_pass(T);
    printf("probe_saturate %d %u %u %u %d %u\n", beats_out, _hb_sent, _hb_stale,
           _hb_stale_busy, probes_out, _probe_stale);
    return 0;
}
"""
src = (HARNESS
       .replace("@HB@", define("STATUS_HEARTBEAT_MS"))
       .replace("@PROBE@", define("TEENSY_PROBE_MS"))
       .replace("@DEFER@", define("ESPNOW_PERIODIC_DEFER_MS"))
       .replace("@COUNTERS@", counters)
       .replace("@DEFER_FN@", defer)
       .replace("@HELD_FN@", held)
       .replace("@NOTE_FN@", note)
       .replace("@HEARTBEAT@", heartbeat)
       .replace("@PROBE_BLOCK@", probe))
d = pathlib.Path(tempfile.mkdtemp())
(d / "hb.cpp").write_text(src)
r = subprocess.run(["c++", "-std=c++17", "-O1", "-Wall", "-o", str(d / "hb"), str(d / "hb.cpp")],
                   capture_output=True, text=True)
assert r.returncode == 0, f"the lifted blocks did not compile:\n{r.stderr[:1500]}"
r = subprocess.run([str(d / "hb")], capture_output=True, text=True, timeout=30)
assert r.returncode == 0, f"the harness failed: {r.stderr[:500]}"
got = {}
for line in r.stdout.split("\n"):
    if line.strip():
        name, *vals = line.split()
        got[name] = tuple(int(v) for v in vals)
# (beats sent, hb_sent, hb_stale, hb_stale_busy, probes sent, probe_stale)

print("\n3. the heartbeat, run on the host:")
assert got["fresh"][0] == 0, \
    f"a beat went out with the Teensy's STATUS 100 ms old: {got['fresh']}"
assert got["fresh"][1] == 0, \
    f"a beat was counted that never went out: {got['fresh']}"
assert got["silent"][:4] == (1, 1, 0, 0), \
    f"a beat due because the Teensy went quiet is counted as stale: {got['silent']}"
print("   Teensy talking -> no beat; quiet 5 s -> a beat, not stale  OK")
assert got["stale"][:3] == (1, 1, 1), \
    f"a stamp later than `now` is not counted as a stale beat: {got['stale']}"
# Tested before the send: after it, the beat is itself in flight and every
# stale beat would read as busy.
assert got["stale"][3] == 0, \
    f"a stale beat on an idle radio is counted as busy: {got['stale']}"
assert got["stale_busy"][:4] == (1, 1, 1, 1), \
    f"a stale beat through a busy radio is not counted as busy: {got['stale_busy']}"
print("   stamp after `now` -> stale; radio busy -> busy too          OK")
# The contrast that makes the busy count mean something: the same busy radio
# HOLDS a genuine beat, and only the stale one goes through.
assert got["due_busy"][0] == 0, \
    f"the guard no longer holds a genuine beat for a busy radio: {got['due_busy']}"
assert got["due_busy"][1] == 0, \
    f"a beat the guard held is counted as sent: {got['due_busy']}"
assert got["overdue_busy"][:4] == (1, 1, 0, 0), \
    f"a genuine beat past the guard's escape is counted as stale: {got['overdue_busy']}"
print("   same busy radio holds a real beat, lets the stale one by    OK")
assert got["run_report"][:4] == (1, 1, 0, 0), \
    f"a run report on a stale pass is blamed on the stale clock: {got['run_report']}"
print("   a run report is its own cause, not a stale clock            OK")
assert got["wrap_old"][:3] == (0, 0, 0), \
    f"a stamp from just before the millis() rollover reads as stale: {got['wrap_old']}"
assert got["wrap_stale"][:3] == (1, 1, 1), \
    f"a stale stamp across the rollover is missed: {got['wrap_stale']}"
print("   right on both sides of the 49-day millis() rollover         OK")
assert got["saturate"][1:4] == (0xFFFF, 0xFFFF, 0xFFFF), \
    f"a heartbeat count wraps past 65535 instead of holding: {got['saturate']}"
print("   the counts saturate at 65535                                OK")

print("\n4. the probe, run on the host:")
assert got["probe_stale"][4:] == (1, 1), \
    f"a probe sent on a stale stamp is not counted: {got['probe_stale']}"
assert got["probe_silent"][4:] == (1, 0), \
    f"a probe due because the Teensy went quiet is counted as stale: {got['probe_silent']}"
assert got["probe_fresh"][4:] == (0, 0), \
    f"a probe went out with the Teensy's STATUS 100 ms old: {got['probe_fresh']}"
assert got["probe_saturate"][5] == 0xFFFF, \
    f"the probe count wraps past 65535: {got['probe_saturate']}"
print("   stale -> counted; quiet 2.5 s -> sent, not stale; saturates OK")

# ---- 5. what the app makes of it ---------------------------------------------
print("\n5. the health line:")
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


old = line()
assert "heartbeats" not in old and "stale" not in old, \
    f"a mount on the ladder's firmware is shown with heartbeat counts: {old}"
zero = line(struct.pack(">HHHH", 0, 0, 0, 0))
assert "| heartbeats 0" in zero, f"a zero count is not printed, so it reads as absent: {zero}"
assert "stale" not in zero, f"a window with nothing stale mentions it: {zero}"
quiet = line(struct.pack(">HHHH", 2, 0, 0, 0))
assert "| heartbeats 2" in quiet and "stale" not in quiet, \
    f"genuine beats (the Teensy quiet) are shown as stale: {quiet}"
bad = line(struct.pack(">HHHH", 41, 40, 7, 39))
assert "| heartbeats 41 (40 on a stale clock, 7 with a send in flight)" in bad, bad
assert "| stale Teensy probes 39" in bad, bad
assert bad.index("last scan held") < bad.index("heartbeats"), \
    "the counts are not appended after the existing fields"
print("   " + bad[bad.index("| heartbeats"):])
print("   absent -> silent, zero -> 'heartbeats 0', stale -> spelled out OK")

_lg.handlers, _lg.level, _lg.propagate = _saved
print("\nALL CHECKS PASSED")
