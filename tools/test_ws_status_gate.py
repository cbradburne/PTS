"""A mount's STATUS reaches the web app at the same rate as before, but is never lost.

SET a position on the iPad (2026-10-14) and its border stayed red, "stored,
not here", for 5 s before turning green, where the PC app went green at once.
The mount's store sends a STATUS at once with the old at-position bits, and the
corrected one follows 100 ms later.  The hub (and the satellite, copying it)
let one STATUS per mount through to web-app clients every 200 ms and DROPPED
the rest.  That was harmless while the mount streamed STATUS at 10 Hz, but the
mount's bridge now sends one only on a change, plus a 5 s refresh.  So the
correction was dropped, and the refresh was the next word.  The PC, not
throttled, had both.

  1. shared/ws_status_gate.h, compiled here under the address sanitizer: the
     store's correction arrives within the window and goes when it ends; the
     newest of several held wins; one that passes retires anything held; a
     jog's hush holds everything until it lifts; mounts are independent; a
     clock stamp a moment ahead is not a 49-day age; the rate is still at most
     one per window, and the last word always arrives;
  2. the hub: STATUS through the gate, held when it cannot go, released every
     loop, the jog hush intact;
  3. the satellite: the same gate, released every loop.

Run directly, or via tools/run_tests.sh.
"""
import os, pathlib, re, shutil, subprocess, sys, tempfile
REPO = pathlib.Path(__file__).resolve().parent.parent
SHARED = REPO / "firmware/shared"
HUB = (REPO / "firmware/esp32_hub_eth/esp32_hub_eth.ino").read_text()
SAT = (REPO / "firmware/esp32_satellite/esp32_satellite.ino").read_text()

DRIVER = r"""
#include <cstdio>
#include <cstring>
#include <cstdint>
#include "ws_status_gate.h"

int main() {
    static WsStatusGate<5, 64> g(200);
    char op[16];
    while (scanf("%15s", op) == 1) {
        unsigned m, now, hush, b, n;
        if (!strcmp(op, "admit")) {                 // admit m now hush
            if (scanf("%u %u %u", &m, &now, &hush) != 3) return 1;
            printf("%d\n", (int)g.admit(m, now, hush != 0));
        } else if (!strcmp(op, "hold")) {           // hold m byte len
            if (scanf("%u %u %u", &m, &b, &n) != 3) return 1;
            uint8_t f[300];
            for (unsigned i = 0; i < n && i < sizeof(f); i++) f[i] = (uint8_t)(b + i);
            printf("%d\n", (int)g.hold(m, f, (uint16_t)n));
        } else if (!strcmp(op, "offer")) {          // what the hub and satellite do
            if (scanf("%u %u %u %u", &m, &now, &hush, &b) != 4) return 1;
            uint8_t f[20];
            for (unsigned i = 0; i < sizeof(f); i++) f[i] = (uint8_t)(b + i);
            if (g.admit(m, now, hush != 0))   printf("go %u\n", b);
            else if (g.hold(m, f, sizeof(f))) printf("held\n");
            else                              printf("go %u\n", b);   // cannot hold: send
        } else if (!strcmp(op, "due")) {            // due m now hush -> 1 first-byte len | 0
            if (scanf("%u %u %u", &m, &now, &hush) != 3) return 1;
            uint8_t out[64]; uint16_t len = 0;
            if (g.due(m, now, hush != 0, out, &len)) printf("1 %u %u\n", out[0], len);
            else printf("0\n");
        } else return 2;
    }
    return 0;
}
"""

tmp = pathlib.Path(tempfile.mkdtemp())
(tmp / "d.cpp").write_text(DRIVER)
cxx = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
assert cxx, "no C++ compiler on this machine"
base = [cxx, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(SHARED),
        "-o", str(tmp / "d"), str(tmp / "d.cpp")]
r = subprocess.run(base[:1] + ["-fsanitize=address,undefined", "-g"] + base[1:],
                   capture_output=True, text=True)
sanitized = r.returncode == 0
if not sanitized:
    r = subprocess.run(base, capture_output=True, text=True)
assert r.returncode == 0, "shared/ws_status_gate.h does not compile:\n" + r.stderr


def run(script: str) -> list[str]:
    p = subprocess.run([str(tmp / "d")], input=script, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and "Sanitizer" not in p.stderr, f"the gate crashed:\n{p.stderr[-1500:]}"
    return p.stdout.split("\n")[:-1]


# ---- 1. the gate ----------------------------------------------------------------
print("1. shared/ws_status_gate.h, compiled here"
      + (" under the address sanitizer:" if sanitized else ":"))

# The store: its own STATUS (red) goes at once; the correction (green) 100 ms
# later is held, and goes when the window ends, 200 ms after the first.
out = run("admit 0 5000 0\n"           # red: goes
          "admit 0 5100 0\n"           # green: inside the window
          "hold 0 7 20\n"              # ...so held
          "due 0 5150 0\n"             # not yet
          "due 0 5200 0\n"             # now
          "admit 0 5250 0\n"           # the next: its window began at the release
          "due 0 5500 0\n")            # nothing left held: never twice
assert out[:3] == ["1", "0", "1"], f"the store's two STATUS went {out[:3]}, want go, hold"
assert out[3] == "0", "a held STATUS went before its window ended: the rate is not kept"
assert out[4] == "1 7 20", f"the held correction did not go when the window ended: {out[4]}"
assert out[5] == "0", \
    "a STATUS 50 ms after a release went at once: the release did not restart the window"
assert out[6] == "0", "a held STATUS went twice"
print("   the store: red at once, the green held, sent 200 ms after the red, once; "
      "the window restarts there")

# Several held: only the newest goes.
out = run("admit 1 6000 0\nadmit 1 6050 0\nhold 1 10 20\nadmit 1 6100 0\nhold 1 20 20\n"
          "due 1 6200 0\n")
assert out[-1] == "1 20 20", f"an older held STATUS went instead of the newest: {out[-1]}"
print("   of several held, the newest goes")

# One that passes on its own retires anything held: no stale re-send after it.
out = run("admit 2 7000 0\nadmit 2 7050 0\nhold 2 30 20\nadmit 2 7300 0\ndue 2 7400 0\n"
          "due 2 7600 0\n")
assert out[3] == "1", "a STATUS after the window was held instead of going"
assert out[4:] == ["0", "0"], \
    f"a STATUS held BEFORE a newer one went was sent after it: {out[4:]} (stale state)"
print("   one that goes on its own retires the older held one")

# The jog's hush holds everything, and the newest goes when it lifts.
out = run("admit 3 8000 1\nhold 3 40 20\ndue 3 8300 1\ndue 3 8300 0\n")
assert out[0] == "0", "a STATUS went during the jog hush"
assert out[2] == "0", "a held STATUS went during the jog hush"
assert out[3] == "1 40 20", f"the held STATUS did not go when the hush lifted: {out[3]}"
print("   the jog hush holds everything; it goes when the hush lifts")

# Mounts are independent.
out = run("admit 0 9000 0\nadmit 1 9000 0\nadmit 0 9050 0\nhold 0 50 20\nadmit 4 9060 0\n")
assert out == ["1", "1", "0", "1", "1"], f"one mount's window held another's: {out}"
print("   each mount has its own window")

# A stamp a moment ahead of the clock (another task's millis()) is age 0, not
# 49 days: it must not release early.
out = run("admit 0 9000 0\nadmit 0 9050 0\nhold 0 60 20\ndue 0 8999 0\n")
assert out[-1] == "0", "a stamp a moment ahead of the clock read as 49 days: released at once"
print("   a clock stamp a moment ahead is age 0, not a 49-day wrap")

# Can't hold it: the caller must be told, and send it rather than lose it.
out = run("hold 0 70 65\nhold 9 70 20\nadmit 9 1000 0\n")
assert out == ["0", "0", "1"], f"an unholdable STATUS was not reported as such: {out}"
print("   too long to hold, or no such mount: says so, so it is sent, not lost")

# The rate, and the last word: a STATUS that changes at 10 Hz for 10 s, offered
# the way the hub and the satellite offer it, with the loop releasing between.
script = []
for i in range(100):
    t = 20000 + i * 100
    script.append(f"offer 0 {t} 0 {i}")
    script.append(f"due 0 {t + 50} 0")
script.append("due 0 31000 0")
out = run("\n".join(script) + "\n")
went = []                                        # (time, which) of every STATUS sent
for i in range(100):
    t = 20000 + i * 100
    o, d = out[2 * i], out[2 * i + 1]
    if o.startswith("go"):
        went.append((t, int(o.split()[1])))
    if d.startswith("1"):
        went.append((t + 50, int(d.split()[1])))
if out[-1].startswith("1"):
    went.append((31000, int(out[-1].split()[1])))
gaps = [b[0] - a[0] for a, b in zip(went, went[1:])]
assert min(gaps) >= 200, f"two STATUS {min(gaps)} ms apart: the 200 ms window was not kept"
assert went[-1][1] == 99, \
    f"the last STATUS to go was #{went[-1][1]}, not the final #99: the end state was lost"
print(f"   a 10 Hz stream for 10 s: {len(went)} sent, never closer than 200 ms, "
      f"and the final one among them")

# ---- 2. the hub -------------------------------------------------------------------
print("\n2. the hub:")
flat = re.sub(r"\s+", " ", HUB)
assert "_ws_status_ms" not in HUB, "the old drop-only throttle is still in the hub"
assert '#include "../shared/ws_status_gate.h"' in HUB
assert "static WsStatusGate<NUM_MOUNTS, WS_STATUS_MAX> _ws_gate(WS_STATUS_INTERVAL_MS);" in HUB
assert re.search(r"if \(pkt\.cmd == CMD_STATUS\) \{ uint32_t t = millis\(\); "
                 r"bool hush = clock_age_ms\(t, _ws_last_jog_ms\) < WS_JOG_SUPPRESS_MS; "
                 r"if \(!_ws_gate\.admit\(msg\.src_idx, t, hush\)\) \{", flat), \
    "the hub's STATUS must go through the gate, with the jog hush"
assert re.search(r"if \(_ws_gate\.hold\(msg\.src_idx, sb, sn\)\) ws_send = false;", flat), \
    "a STATUS the gate cannot hold must still be sent: never lost"
rel = HUB[HUB.find("// ---- A STATUS the gate held back goes"):HUB.find("MARK(SEC_RELAY);")]
assert re.search(r"if \(_ws_gate\.due\(i, t, hush, sb, &sn\)\) _ws\.binaryAll\(sb, \(size_t\)sn\);", rel) \
    and "clock_age_ms(t, _ws_last_jog_ms) < WS_JOG_SUPPRESS_MS" in rel, \
    "the hub must release a held STATUS every loop, hush and all"
print("   STATUS through the gate, held when it cannot go, released every loop")

# ---- 3. the satellite ---------------------------------------------------------------
print("\n3. the satellite:")
assert "_ws_status_ms" not in SAT, "the old drop-only throttle is still in the satellite"
assert '#include "../shared/ws_status_gate.h"' in SAT
fn = SAT[SAT.find("static bool ws_should_send("):SAT.find("static void ws_release_held()")]
assert "if (_ws_gate.admit(mid - 1, millis(), false)) return true;" in fn and \
    "return !_ws_gate.hold(mid - 1, f, len);" in fn, \
    "the satellite's STATUS must go through the gate, and be sent if it cannot be held"
assert re.search(r"if \(_ws_gate\.due\(i, t, false, sb, &sn\) && _ws\.count\(\)\) ws_queue\(sb, sn\);", SAT), \
    "the satellite must queue a held STATUS when its window ends"
assert re.search(r"drain_hub_to_ws\(\);\s*ws_release_held\(\);", SAT), \
    "the satellite must release held STATUS every loop"
print("   the same gate, released every loop")

print("\nALL CHECKS PASSED")
