"""A mount must not scan its way out of earshot of a base that is answering.

On the bench, 2026-10-14, cam1 could not get back onto a freshly flashed hub
for minutes at a time, and the web app showed no cameras until the PC app was
started.  The mount counted its own scans as base silence.  Each scan ran
8.8 s, one every 10 s left 1.2 s of listening, and 10 s is exactly five of the
hub's 2 s heartbeats, so every PING fell while it was away.
Any frame that landed in those 1.2 s broke the cycle, and the PC app's traffic
did.  Fixed by counting silence only while the radio is on the base's channel
(shared/base_silence.h).

  1. base_silent(), compiled here: both stamps must be old, a stamp a moment
     ahead of the clock (the race clock_age.h exists for) is just now;
  2. a mount and a hub, simulated a millisecond at a time with that helper
     making the mount's decision:
       - after a hub restart the mount stops scanning within one listening
         window of the hub being back, for every heartbeat phase, scan length
         and restart moment, with every other PING lost as well;
       - a mount whose base answers never scans;
       - a base that has really gone is still hunted, at a bounded interval;
  3. the mount sketch uses it, and stamps the radio's return on completion.

Run directly, or via tools/run_tests.sh with the rest.
"""
import pathlib, re, shutil, subprocess, sys, tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
SHARED = REPO / "firmware" / "shared"
INO = (REPO / "firmware/esp_mount_amoled175/esp_mount_amoled175.ino").read_text()
PROTO = (SHARED / "protocol.h").read_text()


def define(name, text):
    m = re.search(r"#define\s+%s\s+\(?([0-9]+)UL\s*\*?\s*([A-Z_]*)\)?" % name, text)
    assert m, f"{name} not found"
    return m.group(1), m.group(2)


# The constants the simulation runs on are the firmware's own.
HB = int(define("BASE_HEARTBEAT_MS", PROTO)[0])
k, unit = define("BASE_SILENT_MS", PROTO)
assert unit == "BASE_HEARTBEAT_MS", "BASE_SILENT_MS is no longer counted in heartbeats"
SILENT = int(k) * HB
PERIOD = int(define("REACQ_PERIOD_MS", INO)[0])
assert re.search(r"#define\s+REACQ_SILENT_MS\s+BASE_SILENT_MS\b", INO), \
    "the mount's scan silence is no longer BASE_SILENT_MS"
hub = (REPO / "firmware/esp32_hub_eth/esp32_hub_eth.ino").read_text()
PRESENT = int(define("MOUNT_PRESENT_MS", hub)[0])

cxx = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
if not cxx:
    print("no C++ compiler: sections 1 and 2 are not run here")
else:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="basesilence-"))
    (tmp / "sim.cpp").write_text(r"""
#include <cstdio>
#include <cstring>
#include "base_silence.h"

static const uint32_t HB = %(HB)d, SILENT = %(SILENT)d, PERIOD = %(PERIOD)d,
                      PRESENT = %(PRESENT)d;
static const uint32_t NEVER = 0xFFFFFFFFu;
static const uint32_t T0 = 100000;          // the hub goes quiet here
static const uint32_t DOWNS[] = { 9000, 13700, 16000, 21300, 27900 };

struct Out { int scans, after; long last_after; uint32_t max_gap; };

// One mount and one hub, a millisecond at a time.  The mount last heard the hub
// at T0 and has not scanned for a long while.  The hub is back at `up` with its
// PINGs on a 2 s grid from up+phase, and like the real one it sends only to a
// mount it has heard within PRESENT ms — after a restart, nothing until one of
// the mount's frames gets through: one every `every` ms while the radio is on
// the channel, and the first send when a scan gives the radio back.  A scan
// keeps the radio on the base's channel for `dwell` ms, then away until D.
static Out run(uint32_t D, uint32_t dwell, uint32_t up, uint32_t phase,
               uint32_t every, uint32_t lose, uint32_t end, uint32_t hub_heard) {
    uint32_t heard = T0, back = 0, last_scan = 0, start = 0, prev = 0, pings = 0;
    bool scanning = false;
    Out o = { 0, 0, -1, 0 };
    for (uint32_t t = T0; t < end; t++) {
        bool on_ch  = !scanning || t - start < dwell;
        bool hub_up = up != NEVER && t >= up;
        if (hub_up && on_ch && t %% every == 0) hub_heard = t;
        if (hub_up && t >= up + phase && (t - up - phase) %% HB == 0 &&
            hub_heard && t - hub_heard <= PRESENT) {
            pings++;
            if (on_ch && !(lose && pings %% lose == 0)) heard = t;
        }
        // hub_reacquire_poll(), run every millisecond
        if (!scanning) {
            if (base_silent(t, heard, back, SILENT) && t - last_scan > PERIOD) {
                scanning = true;
                start = last_scan = t;
                o.scans++;
                if (prev && t - prev > o.max_gap) o.max_gap = t - prev;
                prev = t;
                if (hub_up) { o.after++; o.last_after = (long)(t - up); }
            }
        } else if (t - start >= D) {
            scanning = false;
            back = t;
            if (hub_up) hub_heard = t;
        }
    }
    return o;
}

int main() {
    char cmd[16];
    while (scanf("%%15s", cmd) == 1) {
        unsigned a, b, c, d;
        if (scanf("%%u %%u %%u %%u", &a, &b, &c, &d) != 4) return 2;
        if (!strcmp(cmd, "unit")) {
            printf("%%d\n", (int)base_silent(a, b, c, d));
        } else if (!strcmp(cmd, "settle")) {          // D dwell every lose
            long worst = -2; unsigned wph = 0, wdown = 0; int runs = 0;
            for (unsigned down : DOWNS)
                for (unsigned ph = 0; ph < HB; ph += 50) {
                    Out o = run(a, b, T0 + down, ph, c, d, T0 + down + 180000, 0);
                    runs++;
                    if (o.last_after > worst) { worst = o.last_after; wph = ph; wdown = down; }
                }
            printf("%%ld %%u %%u %%d\n", worst, wph, wdown, runs);
        } else if (!strcmp(cmd, "working")) {         // D dwell every lose
            int scans = 0;
            for (unsigned ph = 0; ph < HB; ph += 50)
                scans += run(a, b, T0 - 60000, ph, c, d, T0 + 600000, T0).scans;
            printf("%%d\n", scans);
        } else if (!strcmp(cmd, "gone")) {            // D dwell every lose
            Out o = run(a, b, NEVER, 0, c, d, T0 + 600000, 0);
            printf("%%d %%u\n", o.scans, o.max_gap);
        }
    }
}
""" % dict(HB=HB, SILENT=SILENT, PERIOD=PERIOD, PRESENT=PRESENT))
    r = subprocess.run([cxx, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", "-I", str(SHARED),
                        str(tmp / "sim.cpp"), "-o", str(tmp / "sim")], capture_output=True, text=True)
    assert r.returncode == 0, "base_silence.h does not compile cleanly:\n" + r.stderr[:1500]

    def sim(lines):
        out = subprocess.run([str(tmp / "sim")], input="\n".join(lines) + "\n",
                             capture_output=True, text=True, check=True, timeout=600).stdout
        return out.strip().splitlines()

    # Sections 1 and 2 report every failure rather than stopping at the first,
    # so the simulation's verdict on a broken helper is seen, not just the
    # unit check that happens to run before it.
    failed = []

    def check(ok, msg):
        if not ok:
            failed.append(msg)
            print(f"   FAIL: {msg}")
        return ok

    # ---- 1. the helper ----------------------------------------------------------
    print("1. base_silent():")
    units = [  # (now, heard, back, expected, what)
        (20000, 10000, 0, 1, "heard 10 s ago, never scanned: silent"),
        (20000, 10000, 15000, 0, "radio back 5 s ago: not yet, whatever was heard"),
        (20000, 10000, 13999, 1, "radio back 6.001 s ago and nothing heard: silent"),
        (20000, 10000, 14000, 0, "exactly three heartbeats of listening: not yet"),
        (20000, 19000, 0, 0, "heard a second ago: not silent"),
        (20000, 20001, 0, 0, "a frame stamped 1 ms AHEAD of the clock (the race) is now"),
        (20000, 0, 20001, 0, "so is a return stamped 1 ms ahead"),
        (3000, 0, 0, 0, "just booted, nothing heard yet: not silent (the boot pick runs)"),
    ]
    got = sim([f"unit {n} {h} {b} {SILENT}" for n, h, b, _, _ in units])
    for (n, h, b, want, what), g in zip(units, got):
        if check(int(g) == want, f"{what}: got {g}, want {want}"):
            print(f"   {what:66} OK")

    # ---- 2. the simulation ------------------------------------------------------
    print(f"\n2. a mount and a hub (heartbeat {HB} ms, silent after {SILENT} ms, "
          f"scans at least {PERIOD} ms apart):")
    # Scan lengths: short, middling, and cam1's with its camera linked: 8.8 s
    # from start to finish, the first 640 ms of it still on the hub's channel
    # (dwell; 0 is a hub on a channel scanned last).  The mount sending every
    # 200 ms is the bench log; every 5 s is a mount with nothing to say but its
    # STATUS heartbeat, which is the slow way back for a hub that must hear it
    # before it will send.
    cases = [(D, dw, ev, lo) for D in (2000, 5000, 8800) for dw in (0, 640)
             for ev in (200, 5000) for lo in (0, 2)]
    got = sim([f"settle {D} {dw} {ev} {lo}" for D, dw, ev, lo in cases])
    # Back for good no later than one listening window after the hub is: if the
    # hub returns just as the mount starts listening, before it has heard the
    # mount it says nothing, so one more scan can start BASE_SILENT_MS in.
    bound = SILENT + HB
    worst_all, settled = -1, True
    for (D, dw, ev, lo), line in zip(cases, got):
        worst, ph, down, runs = (int(x) for x in line.split())
        what = (f"scan {D / 1000:.1f} s, {dw} ms on the channel first, mount sends every "
                f"{ev} ms" + (", every other PING lost" if lo else ""))
        settled &= check(worst <= bound, (
            f"{what}: the mount was still scanning {worst / 1000:.1f} s after its hub came back "
            f"(heartbeat phase {ph} ms, hub away {down / 1000:.1f} s) — it is counting its own "
            f"scans as the base's silence again"))
        worst_all = max(worst_all, worst)
    if settled:
        print(f"   after a hub restart: {len(cases)} kinds of scan x {runs} restarts each "
              f"(every 50 ms of heartbeat phase,")
        print(f"   five restart moments): the last scan starts at most {worst_all / 1000:.1f} s "
              f"after the hub is back (limit {bound / 1000:.0f} s)  OK")
    check(worst_all > 0, "no scan ever followed the hub's return: the simulation proves nothing")

    got = sim([f"working {D} {dw} {ev} 0" for D in (2000, 8800) for dw in (0, 640) for ev in (200, 5000)])
    if check(all(int(g) == 0 for g in got), f"a mount scanned while its base was answering: {got}"):
        print("   a base that answers: no scan, at any heartbeat phase                     OK")

    # The hunt for a base that has gone: one scan per scan length plus three
    # heartbeats of listening, or the period, whichever is longer.  Never zero.
    for D in (2000, 8800):
        scans, gap = (int(x) for x in sim([f"gone {D} 640 5000 0"])[0].split())
        expect = max(PERIOD, D + SILENT) + 1
        ok = check(scans >= 600000 // expect - 1,
                   f"scan {D} ms, base gone: only {scans} scans in 10 min — the hunt stopped")
        ok &= check(gap <= expect + 1, f"scan {D} ms, base gone: scans {gap} ms apart, want {expect}")
        if ok:
            print(f"   a base that has gone, {D / 1000:.1f} s scans: hunted every {gap / 1000:.1f} s "
                  f"({scans} scans in 10 min)  OK")
    shutil.rmtree(tmp, ignore_errors=True)
    if failed:
        print(f"\n{len(failed)} CHECK(S) FAILED")
        sys.exit(1)

# ---- 3. the sketch --------------------------------------------------------------
print("\n3. the mount sketch:")
code = re.sub(r"//[^\n]*", "", INO)
assert '#include "../shared/base_silence.h"' in INO, "the sketch does not include base_silence.h"
assert "bool     silent = base_silent(nowm, heard, _reacq_back_ms, REACQ_SILENT_MS);" in code, \
    "the base-silence check does not count only time spent listening"
print("   the scan decision uses base_silent() with the radio's return        OK")
poll = code[code.index("static void hub_reacquire_poll()"):]
poll = poll[:poll.index("\n}\n")]
done = poll.index("if (n == WIFI_SCAN_RUNNING) return;")
assert re.search(r"static uint32_t _reacq_back_ms = 0;", code), "_reacq_back_ms is not declared 0"
stamps = re.findall(r"(?<!uint32_t )_reacq_back_ms\s*=(?!=)", code)
assert len(stamps) == 1, f"_reacq_back_ms is written {len(stamps)} times; want once, on completion"
stamp = poll.find("_reacq_back_ms  = nowm;")
assert stamp > done, "the radio's return is not stamped when the scan completes"
assert "esp_wifi_set_channel(_hub_channel" in poll[stamp:], \
    "the channel is not set back after the stamp"
print("   stamped once, when the scan completes (not when it starts)        OK")

print("\nALL CHECKS PASSED")
