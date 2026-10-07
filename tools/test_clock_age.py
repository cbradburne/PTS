"""An age taken against a stamp another task writes must never wrap to 49 days.

cam1's own report, 2026-10-07: its base-silence check read the clock, the WiFi
task stamped _last_hub_rx_ms with a fresh millis() a moment later, and the
silence came out at -1 ms.  Unsigned, that is 4294967295 ms: the mount took its
base for 49 days silent and scanned every channel for another, deaf and mute
for seven seconds.  That was every blackout, two or three a day on every mount.
The same unsigned age guarded the run dead-man and the watchdog that E-STOPs
the Teensy.

  1. clock_age.h, compiled here: a stamp a little ahead of the clock reads as
     just now; real ages, the 49.7-day clock wrap and the edges are kept;
  2. the mount sketch: no age against _last_hub_rx_ms, _last_espnow_tx_ok_ms,
     _espnow_cb_last_ms or _espnow_ok_last_ms is taken unguarded, and each site
     that acts on one goes through clock_age_ms / clock_silent;
  3. the stall report carries the radio's history across the reboot.

Run directly, or via tools/run_tests.sh with the rest.
"""
import pathlib, re, shutil, subprocess, sys, tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
SHARED = REPO / "firmware" / "shared"
INO = (REPO / "firmware/esp_mount_amoled175/esp_mount_amoled175.ino").read_text()

# ---- 1. the helper, compiled here ------------------------------------------------
print("1. clock_age.h:")
cxx = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
if cxx:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="clockage-"))
    (tmp / "h.cpp").write_text(r"""
#include <cstdio>
#include "clock_age.h"
int main() {
    unsigned now, stamp, limit;
    char op;
    while (scanf(" %c %u %u %u", &op, &now, &stamp, &limit) == 4)
        printf("%u\n", op == 'a' ? clock_age_ms(now, stamp)
                                 : (unsigned)clock_silent(now, stamp, limit));
}
""")
    r = subprocess.run([cxx, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(SHARED),
                        str(tmp / "h.cpp"), "-o", str(tmp / "h")], capture_output=True, text=True)
    assert r.returncode == 0, "clock_age.h does not compile cleanly:\n" + r.stderr[:1500]
    W = 0xFFFFFFFF
    cases = [  # (op, now, stamp, limit, expected, what)
        ("a", 1000, 1000, 0, 0, "a stamp taken now is 0 old"),
        ("a", 1000, 0, 0, 1000, "an old stamp keeps its age"),
        ("a", 1000, 1001, 0, 0, "a stamp 1 ms AHEAD (the race) is just now, not 49 days"),
        ("a", 1000, 1000 + 59999, 0, 0, "up to a minute ahead is still a race"),
        ("a", 1000, 1000 + 60001, 0, W - 60000, "more than a minute ahead is an age"),
        ("a", 5, W - 15, 0, 21, "the 49.7-day clock wrap keeps the age"),
        ("s", 1000, 1001, 6000, 0, "the race does not read as silent (it did: every blackout)"),
        ("s", 7001, 1000, 6000, 1, "6.001 s of silence is silent"),
        ("s", 7000, 1000, 6000, 0, "exactly the limit is not yet silent"),
    ]
    out = subprocess.run([str(tmp / "h")], input="\n".join(f"{c[0]} {c[1]} {c[2]} {c[3]}"
                                                         for c in cases) + "\n",
                         capture_output=True, text=True, check=True).stdout.split()
    for c, got in zip(cases, out):
        assert int(got) == c[4], f"{c[5]}: got {got}, want {c[4]}"
        print(f"   {c[5]:60} OK")
    shutil.rmtree(tmp, ignore_errors=True)
else:
    print("   no C++ compiler: the helper is not run here")

# ---- 2. the sketch uses it everywhere ------------------------------------------
print("\n2. the mount sketch:")
code = re.sub(r"//[^\n]*", "", INO)                       # comments say "now - x" too
STAMPS = ("_last_hub_rx_ms", "_last_espnow_tx_ok_ms", "_espnow_cb_last_ms", "_espnow_ok_last_ms")
for st in STAMPS:
    raw = re.findall(r"[^\n]*-\s*%s\b[^\n]*" % st, code)
    assert not raw, f"an unguarded age against {st}:\n    " + "\n    ".join(l.strip() for l in raw)
print(f"   no unguarded age against {', '.join(STAMPS)}  OK")
heard = re.findall(r"[^\n]*-\s*heard\b[^\n]*", code)
assert [l.strip() for l in heard] == ["(int32_t)(nowm - heard));"], heard
print("   the base-silence check's own copy is aged only in the signed record  OK")
SITES = {
    "the base-silence check (the scans)":     "bool     silent = clock_silent(nowm, heard, REACQ_SILENT_MS);",
    "the run dead-man":                       "if (_run_active && clock_silent(millis(), _last_hub_rx_ms, RUN_DEADMAN_MS))",
    "the position-run dead-man":              "if (_prun.active && clock_silent(millis(), _last_hub_rx_ms, RUN_DEADMAN_MS))",
    "the watchdog (E-STOP to the Teensy)":    "if (!_watchdog_fired && clock_silent(millis(), _last_hub_rx_ms, WATCHDOG_MS))",
    "adopting a base after a scan":           "clock_silent(nowm, _last_hub_rx_ms, REACQ_ADOPT_MS)",
    "the TX-wedge's 'RX is alive'":           "bool rx_alive  = !clock_silent(nw, _last_hub_rx_ms, HUB_TIMEOUT_MS - 1);",
    "hub_ok":                                 "bool hub_ok = !clock_silent(millis(), _last_hub_rx_ms, HUB_TIMEOUT_MS - 1);",
    "the isolation restart's RX age":         "uint32_t rx_age = clock_age_ms(millis(), _last_hub_rx_ms);",
    "the isolation restart's TX age":         "uint32_t tx_age = clock_age_ms(millis(), _last_espnow_tx_ok_ms);",
}
for what, line in SITES.items():
    assert line in code, f"{what} does not take its age with the helper: expected\n    {line}"
    print(f"   {what:42} uses the helper  OK")

# ---- 3. the stall carries the radio's history across the reboot --------------
print("\n3. the stall report's radio history:")
stash = code[code.index("static void nomem_stash_event"):]
stash = stash[:stash.index("\n}\n")]
assert "_radio.fill(&t, first);" in stash and "first = _nomem_since_ms;" in stash, \
    "the stall stash does not take the radio history at the first refusal"
assert "encode_mount_link_radio(_evt_radio, &t);" in stash and \
       "_evt_radio_magic = MOUNT_EVT_MAGIC;" in stash, "the history is not kept for the reboot"
assert re.search(r"RTC_NOINIT_ATTR static uint8_t\s+_evt_radio\[MOUNT_EVENT_LINK_TAIL_LEN\]", code), \
    "the history is not in RTC memory, so the restart would wipe it"
send = code[code.index("if (_evt_pending && hub_ok) {"):]
send = send[:send.index("send_to_hub(CMD_MOUNT_EVENT, p, n);")]
assert "if (_evt_radio_magic == MOUNT_EVT_MAGIC) {" in send and \
       "n = MOUNT_EVENT_NOMEM_PAYLOAD_LEN_V2;" in send and "_evt_radio_magic = 0;" in send, \
    "the report after the reboot does not attach the history, or reuses a stale one"
cured = code[code.index("static void nomem_send_cured"):]
cured = cured[:cured.index("\n}\n")]
assert "_radio.fill(&t, first);" in cured and \
       "encode_mount_link_radio(p + MOUNT_EVENT_NOMEM_PAYLOAD_LEN, &t);" in cured, \
    "a stall the ladder cured does not carry the history"
print("   taken at the first refusal, kept in RTC memory across the reboot,")
print("   attached once (never stale), and on a cured stall too           OK")

print("\nALL CHECKS PASSED")
