"""Mount isolation restart: wait only when waiting can help.

A mount that loses the hub both ways restarts itself to rebuild the ESP-NOW
stack. That wait was a flat two minutes, sized to let the cheaper remedy run
first: 4 consecutive send failures refresh the peer, 3 refreshes rebuild the
stack, ~8 attempts inside the window.

Every rung of that ladder is driven by the ESP-NOW send CALLBACK. When the
stack's TX queue is full, esp_now_send() is refused at the call and the callback
never fires — so the ladder cannot start. cam1 proved it on 2026-08-18: the
counters preserved through the restart read txfail 248 and 9 reinits, exactly
what they had been before it went silent. 137 seconds of waiting for a remedy
that could not begin.

So the window is now chosen by whether the ladder is running. This checks the
discrimination holds in both directions — the failure that matters is not
"wedge waits too long" but "hub reboot restarts too eagerly".

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
import re, shutil, subprocess, tempfile

INO = (REPO / "firmware/esp_mount_amoled175/esp_mount_amoled175.ino").read_text()

# ---- 1. the constants and the shape of the decision ------------------------
print("1. firmware:")
m = re.search(r"#define ESPNOW_RESTART_MS\s+\((\d+)UL\*(\d+)UL\*(\d+)UL\)", INO)
assert m, "ESPNOW_RESTART_MS not found"
long_ms = int(m.group(1)) * int(m.group(2)) * int(m.group(3))
m = re.search(r"#define ESPNOW_RESTART_STALLED_MS\s+\((\d+)UL\*(\d+)UL\)", INO)
assert m, "ESPNOW_RESTART_STALLED_MS not found"
short_ms = int(m.group(1)) * int(m.group(2))
assert short_ms < long_ms, (short_ms, long_ms)
print(f"   running ladder waits {long_ms/1000:.0f}s, stalled one waits {short_ms/1000:.0f}s   OK")

# The window must be selected, not fixed — and both stale checks must use it, or
# the one-way-link protection (TX fresh, RX stale => hub-side, do not restart)
# quietly changes meaning.
assert "iso_window = ladder_running ? ESPNOW_RESTART_MS" in INO, \
    "the window is not selected by whether the ladder is running"
assert "rx_stale = (rx_age > iso_window)" in INO and "tx_stale = (tx_age > iso_window)" in INO, \
    "both staleness checks must use the selected window"
print("   both RX and TX staleness use the selected window     OK")

# "Running" must mean send failures OR reinits moving. Failures alone would miss
# a stack that is rebuilding; reinits alone would miss one still counting up to
# its first rebuild.
assert "_espnow_fail_total != _iso_fail_mark" in INO, "send failures not watched"
assert "_reinit_count      != _iso_reinit_mark" in INO, "reinits not watched"
print("   ladder = send failures OR reinits still moving       OK")

# ---- 2. the decision itself, compiled ---------------------------------------
if not shutil.which("c++"):
    print("\n2. no C++ compiler — skipping the behavioural check.")
    print("\nALL CHECKS PASSED")
    raise SystemExit(0)

HARNESS = r"""
#include <cstdio>
#include <cstdint>
#define HUB_TIMEOUT_MS            5000UL
#define ESPNOW_RESTART_MS         (@LONG@UL)
#define ESPNOW_RESTART_STALLED_MS (@SHORT@UL)

// The decision, transcribed from the isolation block in the .ino.
static bool restarts_at(uint32_t rx_age, uint32_t tx_age,
                        bool fails_moving, bool reinits_moving) {
    static uint32_t _iso_since_ms = 0;
    _iso_since_ms = 0;                     // fresh state per call
    if (rx_age <= HUB_TIMEOUT_MS || tx_age <= HUB_TIMEOUT_MS) _iso_since_ms = 0;
    else _iso_since_ms = 1;
    bool ladder_running = _iso_since_ms && (fails_moving || reinits_moving);
    uint32_t w = ladder_running ? ESPNOW_RESTART_MS : ESPNOW_RESTART_STALLED_MS;
    return (rx_age > w) && (tx_age > w);
}
struct Case { const char *name; uint32_t rx, tx; bool f, r; bool want; };
int main() {
    const uint32_t S = 1000UL;
    Case cs[] = {
        // The cam1 wedge: silent, and nothing being attempted.
        { "queue wedged, 25s",        25*S, 25*S, false, false, true  },
        { "queue wedged, 15s",        15*S, 15*S, false, false, false },
        // Hub rebooted: sends fail, callbacks fire, counters move.
        { "hub off, 25s",             25*S, 25*S, true,  false, false },
        { "hub off, 90s",             90*S, 90*S, true,  false, false },
        { "hub off, 130s",           130*S,130*S, true,  false, true  },
        // Stack rebuilding counts as running even with no new failures.
        { "reinits moving, 25s",      25*S, 25*S, false, true,  false },
        // One-way link: TX still getting through. Never restart - restarting
        // this chip cannot fix a hub-side send wedge.
        { "TX fresh, RX stale 300s", 300*S,  1*S, false, false, false },
        { "RX fresh, TX stale 300s",  1*S, 300*S, false, false, false },
        // In contact both ways.
        { "healthy",                  1*S,  1*S, false, false, false },
    };
    int bad = 0;
    for (auto &c : cs) {
        bool got = restarts_at(c.rx, c.tx, c.f, c.r);
        const char *ok = (got == c.want) ? "ok" : "WRONG";
        if (got != c.want) bad++;
        printf("   %-24s restart=%-5s expected=%-5s %s\n",
               c.name, got ? "yes" : "no", c.want ? "yes" : "no", ok);
    }
    return bad;
}
""".replace("@LONG@", str(long_ms)).replace("@SHORT@", str(short_ms))

d = pathlib.Path(tempfile.mkdtemp())
(d / "iso.cpp").write_text(HARNESS)
r = subprocess.run(["c++", "-std=c++17", "-O2", "-o", str(d / "iso"), str(d / "iso.cpp")],
                   capture_output=True, text=True)
assert r.returncode == 0, f"harness did not compile:\n{r.stderr[:700]}"
r = subprocess.run([str(d / "iso")], capture_output=True, text=True)
print("\n2. behaviour:")
print(r.stdout.rstrip())
assert r.returncode == 0, "the isolation decision does not behave as intended"

print(f"\n   a wedged queue now restarts at {short_ms/1000:.0f}s instead of "
      f"{long_ms/1000:.0f}s,")
print("   and a hub reboot still gets the full window            OK")
print("\nALL CHECKS PASSED")
