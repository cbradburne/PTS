"""Run, owned by the mount — and the phone's way to start it, stop it and see it.

2026-09-27.  Run cycles a camera's stored positions (or, on a look-at mount,
ping-pongs its subject) until stopped.  The PC app used to drive a position run
itself: watch each STATUS, see the mount arrive, send the next goto.  Now the
mount's own bridge does it (shared/pos_run.h), the way the look-at run already
worked, so it carries on whatever happens to the device that started it — a
phone that locks, a PC app that closes — and any device can start or stop it.

WHAT THIS TEST IS PROTECTING.

  the decisions               shared/pos_run.h, compiled here: first stored
                              slot, wrap, arrival on the TARGET's at-bit, the
                              settle after each goto, a cleared target skipped,
                              stop on a jog / fewer than two / a stalled goto
  only a real takeover        a jog ends a run only if it moves an axis: the
                              PC announces every speed change with a zero jog,
                              and screens send one as a safety stop, and ending
                              runs on those stopped a run whenever a speed was
                              changed anywhere.  Stop is its own command
  where they are wired        the bridge consumes START_RUN and STOP_RUN (the
                              Teensy has no idea what a run is), every operator
                              move stops both kinds of run, the deadman, and
                              STATUS [10] on every STATUS that leaves —
                              forwarded or its own
  said without a burst        a start or stop goes out on the next loop pass
                              through the same in-flight guard as every report,
                              aged so that it cannot wrap past it
  one byte, three readers     START_RUN 0x2A, STOP_RUN 0x2B and the run byte
                              [10] agree in protocol.h, protocol.py and
                              web_app.h; a 10-byte STATUS reads as "this mount
                              cannot say", never 0
  the PC follows the mount    [10] present: start and stop are one command each
                              and the button waits for the mount to say so; a
                              disconnect does not stop a run the mount owns.
                              [10] absent: the old PC-driven run, and the zero
                              jog that older firmware takes as Stop
  the phone does the same     the REAL toggleRun, run in node: every branch —
                              offline, running, old firmware, look-at, too few
                              positions, a start — sends what the PC sends
  where the phone shows it    RUN on every screen, MOVE and RUN either side of
                              the hub status on the GC screen, the Move popup
                              letting go of everything when it closes, and its
                              focus button following the selected camera

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re, shutil, subprocess, tempfile, json
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

from comms import protocol as P

SHARED = REPO / "firmware/shared"
PROTO_H = (SHARED / "protocol.h").read_text()
MOUNT = (REPO / "firmware/esp_mount_amoled175/esp_mount_amoled175.ino").read_text()
WEB = (SHARED / "web_app.h").read_text()
TMP = pathlib.Path(tempfile.mkdtemp())


def body_of(src, signature):
    """The text of the block whose opening starts with `signature` — skipping
    any match that reaches a ';' before its '{': a forward declaration, or a
    one-line if, is not the block."""
    i = src.index(signature)
    while ";" in src[i:src.index("{", i)]:
        i = src.index(signature, i + 1)
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError(f"unbalanced braces after {signature!r}")


# ---- 1. the decisions: shared/pos_run.h, compiled here ----------------------
print("1. shared/pos_run.h, compiled on this machine:")
cxx = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
assert cxx, "no C++ compiler — the mount's run cannot be checked"
(TMP / "h.cpp").write_text(r"""
#include <cstdio>
#include <cstring>
#include "pos_run.h"
static PosRun R;
int main() {
    printf("%d %d %d %d %lu %lu\n", NUM_POSITIONS, STATE_IDLE, STATE_JOGGING,
           STATE_MOVING_TO_POS, (unsigned long)POS_RUN_SETTLE_MS, (unsigned long)POS_RUN_STALL_MS);
    char cmd[16];
    while (scanf("%15s", cmd) == 1) {
        unsigned a, b, c, d;
        if (!strcmp(cmd, "START") && scanf("%u %u", &a, &b) == 2) {
            unsigned f = pos_run_start(&R, (uint16_t)a, b);
            printf("%u %d\n", f, R.active ? 1 : 0);
        } else if (!strcmp(cmd, "ST") && scanf("%u %u %u %u", &a, &b, &c, &d) == 4) {
            uint8_t slot = 0xEE;
            int act = pos_run_on_status(&R, (uint8_t)a, (uint16_t)b, (uint16_t)c, d, &slot);
            printf("%d %u %d\n", act, slot, R.active ? 1 : 0);
        } else if (!strcmp(cmd, "STOP")) {
            pos_run_stop(&R);
            printf("%d\n", R.active ? 1 : 0);
        } else if (!strcmp(cmd, "NEXT") && scanf("%u %u", &a, &b) == 2) {
            printf("%u\n", pos_run_next((uint16_t)a, (uint8_t)b));
        } else if (!strcmp(cmd, "JOG")) {
            char hx[80] = ""; uint8_t pl[40]; unsigned n = 0;
            if (scanf("%79s", hx) == 1 && strcmp(hx, "-"))
                for (; hx[2 * n] && hx[2 * n + 1] && n < sizeof(pl); n++) {
                    unsigned v; sscanf(hx + 2 * n, "%2x", &v); pl[n] = (uint8_t)v;
                }
            printf("%d\n", jog_moves(pl, (uint16_t)n) ? 1 : 0);
        }
        fflush(stdout);
    }
}
""")
r = subprocess.run([cxx, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-Wno-unused-function",
                    "-I", str(SHARED), str(TMP / "h.cpp"), "-o", str(TMP / "h")],
                   capture_output=True, text=True)
assert r.returncode == 0, "pos_run.h does not compile cleanly on the host:\n" + r.stderr[:2000]


def mount(lines):
    r = subprocess.run([str(TMP / "h")], input="\n".join(lines) + "\n",
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = r.stdout.splitlines()
    return out[0], out[1:]


consts, _ = mount([])
NPOS, IDLE, JOG, MOVING, SETTLE, STALL = (int(x) for x in consts.split())
assert (NPOS, IDLE, JOG) == (10, P.MountState.IDLE, P.MountState.JOGGING), consts
PR_NONE, PR_GOTO, PR_STOP_JOGGED, PR_STOP_TOO_FEW, PR_STOP_STALLED = range(5)
m = lambda *slots: sum(1 << s for s in slots)

# pos_run_next: the next stored slot after `after`, wrapping; the first stored
# when `after` is not a slot; nothing when fewer than two are stored.
_, out = mount([f"NEXT {m(0, 3, 7)} 0", f"NEXT {m(0, 3, 7)} 3", f"NEXT {m(0, 3, 7)} 7",
                f"NEXT {m(0, 3, 7)} 255", f"NEXT {m(0, 3, 7)} 5", f"NEXT {m(4)} 255",
                f"NEXT 0 255", f"NEXT {m(9, 2)} 9", f"NEXT {m(0, 3) | (1 << 12)} 3"])
assert out == ["3", "7", "0", "0", "7", "255", "255", "2", "0"], \
    f"pos_run_next went {out}: expected on 3, 7, wrap to 0, first 0, 7 after an empty 5, " \
    "nothing with one or none stored, 9 wraps to 2, bits past slot 10 ignored"

# A full lap: starts at the FIRST stored slot, advances on that slot's at-bit
# once the settle has passed, wraps.
L = [f"START {m(1, 4, 6)} 1000",
     f"ST {MOVING} {m(1, 4, 6)} 0 1100",              # moving, inside the settle
     f"ST {IDLE} {m(1, 4, 6)} {m(1)} 1200",           # arrived — but inside the settle
     f"ST {IDLE} {m(1, 4, 6)} {m(1)} {1000 + SETTLE}",
     f"ST {MOVING} {m(1, 4, 6)} 0 {1000 + SETTLE + 400}",
     f"ST {IDLE} {m(1, 4, 6)} {m(1)} {1000 + SETTLE + 900}",   # someone else's at-bit
     f"ST {IDLE} {m(1, 4, 6)} {m(4)} {1000 + SETTLE + 1000}",
     f"ST {IDLE} {m(1, 4, 6)} {m(6)} {1000 + 2 * SETTLE + 2000}",
     f"STOP",
     f"ST {IDLE} {m(1, 4, 6)} {m(1)} 99999"]
_, out = mount(L)
assert out[0] == "1 1", f"a run over slots 2, 5, 7 started with {out[0]}, not slot 2"
assert out[1].split()[0] == str(PR_NONE) and out[2].split()[0] == str(PR_NONE), \
    f"the run acted inside the {SETTLE} ms settle after its goto: {out[1:3]} — the " \
    "mount had not had time to start the move, so a stale at-bit read as arrival"
assert out[3] == f"{PR_GOTO} 4 1", f"arrival at slot 2 gave {out[3]}, not a goto to slot 5"
assert out[4].split()[0] == str(PR_NONE), f"moving, the run acted: {out[4]}"
assert out[5].split()[0] == str(PR_NONE), \
    f"another slot's at-bit read as arriving at the target: {out[5]}"
assert out[6] == f"{PR_GOTO} 6 1", f"arrival at slot 5 gave {out[6]}, not a goto to slot 7"
assert out[7] == f"{PR_GOTO} 1 1", f"arrival at slot 7 gave {out[7]}, not a wrap to slot 2"
assert out[8] == "0" and out[9].split()[0] == str(PR_NONE), \
    f"a stopped run still acted: {out[8:]}"
print(f"   first stored slot; advances on the target's own at-bit after the {SETTLE} ms\n"
      "   settle; wraps; stopped means silent   OK")

# The stops, each with its own reason.
_, out = mount([f"START {m(0, 1)} 0", f"ST {JOG} {m(0, 1)} 0 {SETTLE}"])
assert out[1] == f"{PR_STOP_JOGGED} 238 0", f"a jogging mount gave {out[1]}, not a stop"
_, out = mount([f"START {m(0, 1)} 0", f"ST {JOG} {m(0, 1)} 0 {SETTLE - 1}"])
assert out[1].split()[0] == str(PR_NONE), \
    "a jog state inside the settle stopped the run — the goto's own start can look like one"
_, out = mount([f"START {m(0, 1, 2)} 0", f"ST {IDLE} {m(1, 2)} 0 {SETTLE}"])
assert out[1] == f"{PR_GOTO} 1 1", \
    f"the target was cleared mid-run and the run gave {out[1]}, not a move on to slot 2"
_, out = mount([f"START {m(0, 1)} 0", f"ST {IDLE} {m(1)} 0 {SETTLE}"])
assert out[1] == f"{PR_STOP_TOO_FEW} 238 0", \
    f"one position left and the run gave {out[1]}, not a stop"
_, out = mount([f"START {m(0, 5)} 0", f"ST {IDLE} {m(0, 5)} 0 {STALL - 1}",
                f"ST {IDLE} {m(0, 5)} 0 {STALL}"])
assert out[1].split()[0] == str(PR_NONE) and out[2] == f"{PR_STOP_STALLED} 238 0", \
    f"idle with no arrival gave {out[1:]}: stop at {STALL} ms, not before"
_, out = mount([f"START {m(0, 5)} 0", f"ST {MOVING} {m(0, 5)} 0 {STALL * 10}"])
assert out[1].split()[0] == str(PR_NONE), \
    "a long goto still MOVING was called stalled — only an idle mount can be"
_, out = mount([f"START {m(3)} 0", f"START 0 0"])
assert out == ["255 0", "255 0"], f"a run started with fewer than two positions: {out}"
_, out = mount([f"START {m(0, 1)} {2**32 - 100}",
                f"ST {IDLE} {m(0, 1)} {m(0)} {SETTLE - 101}",     # 1 ms short, across the wrap
                f"ST {IDLE} {m(0, 1)} {m(0)} {SETTLE - 100}"])    # the settle, to the ms
assert out[1].split()[0] == str(PR_NONE) and out[2] == f"{PR_GOTO} 1 1", \
    f"across millis() wrapping the run gave {out[1:]} — the elapsed-time sums are not unsigned"
print("   stops on a jog (not inside the settle), with one position left, and on an\n"
      "   idle mount that never arrived (not a long move); a cleared target is\n"
      "   skipped; fewer than two never starts; millis() wrap is harmless   OK")

# jog_moves: what counts as the operator taking over.  From real packets: the
# PC's speed-preset announcement and the web app's safety stop are zero jogs,
# and ending runs on them stopped a run whenever a speed changed anywhere.
payload_of = lambda pkt: bytes(P.parse_packet(pkt).payload)
jcases = [
    (payload_of(P.pkt_jog(3, 0, 0, 0, 0, 4, 1)), 0, "the PC's speed-preset announcement"),
    (bytes(8) + bytes([2, 2]), 0, "the web app's safety stop (mkJog, all zero)"),
    (payload_of(P.pkt_jog(3, 0, 0, 0, 0, 2, 2, axis_mask=0x0C)), 0, "CV tracking's zero"),
    (payload_of(P.pkt_jog(3, 1, 0, 0, 0)), 1, "pan at 1"),
    (payload_of(P.pkt_jog(3, 0, 0, 0, -1)), 1, "zoom at -1"),
    (payload_of(P.pkt_jog(3, 0, 0, 500, 0, axis_mask=0x03)), 0, "slider under the pan/tilt-only mask"),
    (payload_of(P.pkt_jog(3, 0, 0, 500, 0, axis_mask=0x0C)), 1, "slider under the slider/zoom mask"),
    (payload_of(P.pkt_jog(3, 300, 0, 0, 0, axis_mask=0x0C)), 0, "pan under the slider/zoom mask"),
    (payload_of(P.pkt_jog(3, 0, 0, 500, 0, axis_mask=0x01)), 1,
     "slider under mask 0x01, which the Teensy reads as all four"),
    (bytes([0, 5, 0, 0, 0, 0, 0]), 0, "7 bytes, which the Teensy ignores"),
]
_, out = mount([f"JOG {c[0].hex() or '-'}" for c in jcases])
for (pl, want, what), got in zip(jcases, out):
    assert int(got) == want, \
        f"jog_moves on {what} ({pl.hex()}) gave {got}, not {want}"
print("   a jog ends a run only if it moves an axis the Teensy will move: the PC's\n"
      "   preset announcement, the web app's safety stop and CV's zero do not   OK")

# ---- 2. where they are wired: the mount's bridge -------------------------------
print("\n2. the bridge (esp_mount_amoled175.ino):")
assert '#include "../shared/pos_run.h"' in MOUNT
hub = body_of(MOUNT, "static void handle_hub_packet(")
start = hub.index("if (pkt.cmd == CMD_START_RUN)")
fwd = hub.index("Serial1.write(fwd, build_packet(fwd, pkt.mount_id, pkt.seq,")
blk = body_of(hub[start:], "if (pkt.cmd == CMD_START_RUN)")
assert start < fwd and blk.rstrip().endswith("return;\n    }") or blk.count("return;") >= 2, blk
assert blk.strip().split("\n")[-2].strip() == "return;", \
    "START_RUN falls through to the forward — the Teensy would get a command it does not know"
assert "FLAG_LOOK_AT_MODE" in blk and "return;" in blk.split("FLAG_LOOK_AT_MODE")[1].split("}")[0], \
    "a look-at mount would cycle its SUBJECTS as positions"
assert "pos_run_start(&_prun, _ms.slot_occupied, millis())" in blk
assert blk.index("run_stop(") < blk.index("pos_run_start("), \
    "a position run started over a look-at run without ending it"
assert blk.count("runs_changed()") == 2, \
    "a start (or a refusal) is not reported — the button would wait for the next STATUS"
assert not re.search(r"if \(pkt\.cmd == CMD_JOG\)\s*\{[^}]*run_stop\(", hub), \
    "any jog ends a run again — a zero jog is the PC's speed announcement"
for cmd, sig in (("CMD_E_STOP", "if (pkt.cmd == CMD_E_STOP)"),
                 ("CMD_JOG", "if (pkt.cmd == CMD_JOG && jog_moves(pkt.payload, pkt.payload_len))")):
    assert sig in hub, f"{cmd}: no '{sig}' in the hub handler"
    b = body_of(hub, sig)
    assert "prun_stop(" in b and "run_stop(" in b, f"{cmd} does not end both kinds of run"
stop_ = body_of(hub, "if (pkt.cmd == CMD_STOP_RUN)")
assert hub.index("if (pkt.cmd == CMD_STOP_RUN)") < fwd and \
       stop_.strip().split("\n")[-2].strip() == "return;", \
    "STOP_RUN falls through to the forward — the Teensy would get a command it does not know"
assert "run_stop(" in stop_ and "prun_stop(" in stop_, "STOP_RUN does not end both kinds of run"
assert "runs_changed();" in stop_, \
    "STOP_RUN on a mount that was not running says nothing — a stale ■ Stop stays"
mv = body_of(hub, "if (pkt.cmd == CMD_GOTO || pkt.cmd == CMD_GOTO_SLOT || pkt.cmd == CMD_MOVE_REL)")
assert "prun_stop(" in mv and "run_stop(" in mv, "a commanded move does not end the runs"
la = body_of(hub, "if (pkt.cmd == CMD_START_LOOK_AT_MOVE && pkt.payload_len >= 3)")
assert "prun_stop(" in la, "a look-at move started over a position run without ending it"
tee = body_of(MOUNT, "static void handle_teensy_packet(")
st_blk = body_of(tee, "if (pkt.cmd == CMD_STATUS && pkt.payload_len >= 2)")
assert "pos_run_on_status(&_prun, _ms.state, _ms.slot_occupied" in st_blk and \
       "_ms.slot_at, millis(), &next)" in st_blk, "the run is not advanced from the Teensy's STATUS"
assert st_blk.index("upd(_ms.slot_at") < st_blk.index("pos_run_on_status("), \
    "the run decides on the PREVIOUS STATUS's masks"
for r_ in ("PR_STOP_JOGGED", "PR_STOP_TOO_FEW", "PR_STOP_STALLED"):
    assert r_ in st_blk, f"{r_} is not handled — the run would stop without saying so"
assert "runs_changed();" in st_blk.split("if (why)")[1], \
    "a run the mount ended is not reported at once"
assert "prun_goto(next);" in st_blk
app_ = body_of(tee[tee.index("if (pkt.cmd == CMD_STATUS && pkt.payload_len == STATUS_RUN_BYTE)"):],
               "if (pkt.cmd == CMD_STATUS && pkt.payload_len == STATUS_RUN_BYTE)")
assert "st[STATUS_RUN_BYTE] = run_flags();" in app_ and "sizeof(st)" in app_, app_
hb = body_of(MOUNT, "static void send_status_heartbeat()")
assert "uint8_t p[11];" in hb and "p[STATUS_RUN_BYTE] = run_flags();" in hb and \
       "send_to_hub(CMD_STATUS, p, sizeof(p));" in hb, \
    "the bridge's own STATUS does not carry the run byte — it would come and go"
# Aged with clock_silent(): a raw `millis() - _last_hub_rx_ms` wraps to 49 days
# when a frame lands between the two reads (test_clock_age.py).
assert re.search(r"if \(_prun\.active && clock_silent\(millis\(\), _last_hub_rx_ms, RUN_DEADMAN_MS\)\)\s*"
                 r"\n\s*prun_stop\(", MOUNT), \
    "no deadman: a mount that lost every base would cycle on unwatched"
for fn in ("static void prun_stop(", "static void run_stop("):
    assert "runs_changed();" in body_of(MOUNT, fn), f"{fn} ends a run without saying so"
# Said on the next pass, through the in-flight guard — never a send of its own.
# A send from runs_changed() lands beside the forwarded STATUS or the periodic
# reports in one pass, the burst test_periodic_burst_guard exists to stop.
rc = body_of(MOUNT, "static void runs_changed()")
assert "send_" not in rc and "espnow_tx" not in rc, \
    "runs_changed() sends by itself — outside periodic_held, beside whatever else goes this pass"
assert "_runs_report_due = true; _runs_due_ms = millis();" in rc, rc
assert "_runs_report_due   = false;" in hb, \
    "the heartbeat does not clear the due flag — a run change would be re-sent every pass"
loop_ = body_of(MOUNT, "void loop()")
beat = loop_[loop_.index("static bool hb_held = false;"):]
beat = beat[:beat.index("send_status_heartbeat();")]
assert "if (_runs_report_due) {" in beat and "hb_int = 0;" in beat and \
       "hb_age = millis() - _runs_due_ms;" in beat, \
    "a run change waits for the next 5 s beat, or is not aged from its own flag"
assert "now - _runs_due_ms" not in loop_, \
    "the due report is aged against `now`, read before the packets that set the flag:\n" \
    "    a stamp from after `now` wraps to days — overdue, so the guard waves it through"
assert "periodic_held(hb_age, hb_int, &hb_held)" in beat, "the due report skips the guard"
assert "CMD_GOTO_SLOT, &slot, 1)" in body_of(MOUNT, "static void prun_goto("), \
    "the goto carries presets — a speed dial turned mid-run would be ignored"
print("   START_RUN consumed here, never forwarded, refused on a look-at mount;\n"
      "   E-STOP, jogs, gotos, moves and look-at moves end both runs; advanced\n"
      "   from the Teensy's STATUS after the masks update; [10] on forwarded and\n"
      "   own STATUS alike; deadman; every start and stop said on the next pass,\n"
      "   through the in-flight guard, aged so it cannot wrap past it   OK")

# ---- 3. one byte, three readers -------------------------------------------------
print("\n3. protocol.h, protocol.py, web_app.h:")
for name, val in (("START_RUN", 0x2A), ("STOP_RUN", 0x2B)):
    assert re.search(rf"CMD_{name}\s*=\s*0x{val:02X}", PROTO_H), f"protocol.h CMD_{name}"
    assert P.Cmd[name] == val, f"protocol.py Cmd.{name}"
    assert re.search(rf"const CMD_{name}\s*=\s*0x{val:02X};", WEB), f"web_app.h CMD_{name}"
sp = P.parse_packet(P.pkt_stop_run(3))
assert (sp.mount_id, int(sp.cmd), bytes(sp.payload)) == (3, 0x2B, b""), "pkt_stop_run"
for name, val in (("STATUS_RUN_BYTE", 10), ("STATUS_RUN_POSITIONS", 1), ("STATUS_RUN_LOOK_AT", 2)):
    assert re.search(rf"#define {name}\s+(0x0?{val:X}|{val})\b", PROTO_H), f"protocol.h {name}"
    assert getattr(P, name) == val, f"protocol.py {name}"
    assert re.search(rf"const {name}\s*=\s*(0x0?{val:X}|{val});", WEB), f"web_app.h {name}"
ten = bytes([0, 0x10, 2, 3, 0, 0b101, 0, 0b1, 0xFF, 0xFF])
assert P.decode_status(ten).run_flags is None, \
    "a 10-byte STATUS (bridge firmware from before) reads as a run byte"
assert P.decode_status(ten + b"\x00").run_flags == 0
assert P.decode_status(ten + b"\x03").run_flags == 3
pkt = P.pkt_start_run(4)
assert pkt[3] == 4 and pkt[6] == 0x2A and pkt[2] == 4, f"pkt_start_run built {pkt.hex()}"
web_parse = re.search(r"cs\.runFlags\s*=\s*\(plen > STATUS_RUN_BYTE\)\s*\?\s*v\.getUint8\(STATUS_RUN_BYTE\)\s*:\s*null;", WEB)
assert web_parse, "the web app does not read [10] as the PC does — present, or null"
print("   0x2A, 0x2B and [10] agree; 10 bytes reads as 'cannot say' (None/null),\n"
      "   never 0   OK")

# ---- 4. the PC follows the mount --------------------------------------------------
print("\n4. the PC app's main window:")
from PyQt6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])
import ui.main_window as mw
from comms.mount_manager import MountState_


class Rec:
    """Any attribute a recorder: every call is logged, and returns a Rec."""
    def __init__(self, name, log): self._n, self._log = name, log
    def __getattr__(self, a): return Rec(f"{self._n}.{a}", self._log)
    def __call__(self, *a, **k):
        self._log.append((self._n.split(".")[-1], a, k))
        return Rec(self._n + "()", self._log)


class MM:
    def __init__(self, log):
        self.log, self.st = log, {i: MountState_(mount_id=i) for i in range(1, 6)}
    def state(self, i): return self.st[i]
    def send_start_run(self, i): self.log.append(("start_run", (i,), {}))
    def send_stop_run(self, i): self.log.append(("stop_run", (i,), {}))
    def send_jog(self, *a): self.log.append(("jog", a, {}))
    def send_goto_slot(self, *a): self.log.append(("goto_slot", a, {}))
    def send_start_look_at_move(self, *a, **k): self.log.append(("look_at", a, k))


class Cfg:
    def __init__(self): self.la = set()
    def mount(self, i): return type("M", (), {"look_at_mode": i in self.la})()


def window():
    log = []
    w = type("W", (), {})()
    w.log, w._mm, w._config = log, MM(log), Cfg()
    w._grid, w._dispatcher = Rec("grid", log), Rec("dispatcher", log)
    w._status_label, w._conn_label = Rec("status", log), Rec("conn", log)
    w._cam_containers = {i: Rec("cont", log) for i in range(1, 6)}
    w._run_mode = True
    w._run_states = {i: {'active': False, 'slots': [], 'target_idx': 0, 'goto_ignore': 0}
                     for i in range(1, 6)}
    w._active_la_subject = {}
    w._update_run_cam_btn = lambda i: log.append(("button", (i,), {}))
    w._set_la_arrow = lambda *a: None
    for name in ("_stop_run", "_start_run", "_start_look_at_run", "_on_status_updated",
                 "_on_mount_disconnected", "_toggle_cam_run"):
        setattr(w, name, getattr(mw.MainWindow, name).__get__(w))
    return w


def calls(w, *names):
    return [(n, a) for n, a, k in w.log if n in names]


# A mount that reports [10]: one command, and the button waits for the mount.
w = window()
st = w._mm.st[2]
st.run_flags, st.slot_occupied_mask = 0, m(0, 3, 5)
w._toggle_cam_run(2)
assert calls(w, "start_run", "goto_slot") == [("start_run", (2,))], \
    f"starting a run on a mount that owns runs sent {calls(w, 'start_run', 'goto_slot')}"
assert not w._run_states[2]['active'] and not calls(w, "button"), \
    "the button changed before the mount said the run was going"
st.run_flags = P.STATUS_RUN_POSITIONS
w._on_status_updated(2)
assert w._run_states[2]['active'] and calls(w, "button") == [("button", (2,))], \
    "the mount reported its run and the button did not follow"
w.log.clear()
w._on_status_updated(2)
assert not calls(w, "button", "goto_slot"), \
    "an unchanged run byte redrew the button or drove the run from here"
w._toggle_cam_run(2)
assert calls(w, "jog", "stop_run") == [("stop_run", (2,))] and w._run_states[2]['active'], \
    f"Stop sent {calls(w, 'jog', 'stop_run')} — it is STOP_RUN alone, not a zero jog\n" \
    "    (the mount no longer reads one as Stop), and the button waits for the mount"
st.run_flags = 0
w._on_status_updated(2)
assert not w._run_states[2]['active'], "the mount reported its run over and the button stayed"
# A run started elsewhere — a phone — shows here; one the mount ended stops showing.
w.log.clear()
w._mm.st[3].run_flags = P.STATUS_RUN_LOOK_AT
w._on_status_updated(3)
assert w._run_states[3]['active'], "a look-at run started from a phone does not show here"
w._on_mount_disconnected(3)
assert not calls(w, "jog", "stop_run"), \
    "a disconnect stopped a run the mount owns — it goes on without this app"
w._mm.st[4].run_flags, w._mm.st[4].slot_occupied_mask = 0, m(2)
w._start_run(4)
assert not calls(w, "start_run"), "a run was asked for with one position stored"
print("   [10] present: one START_RUN, the button waits for the mount; Stop is\n"
      "   STOP_RUN and waits too; runs from elsewhere show; a disconnect leaves\n"
      "   the mount's run alone   OK")

# Bridge firmware from before: the PC drives it, as it always did.
w = window()
st = w._mm.st[1]
st.slot_occupied_mask = m(1, 4)
assert st.run_flags is None
w._toggle_cam_run(1)
assert [n for n, a in calls(w, "start_run", "goto_slot")] == ["goto_slot"] and \
    calls(w, "goto_slot")[0][1][:2] == (1, 1) and w._run_states[1]['active'], \
    f"an older mount's run was not driven from here: {calls(w, 'start_run', 'goto_slot')}"
w._on_mount_disconnected(1)
assert calls(w, "jog") == [("jog", (1, 0, 0, 0, 0))] and not w._run_states[1]['active'], \
    "a run this app drives carried on 'running' after its mount disconnected"
print("   [10] absent: the old PC-driven run, stopped on a disconnect   OK")

# Look-at: repeat is the mount's either way; the button waits only when it can.
for flags, waits in ((0, True), (None, False)):
    w = window()
    w._config.la.add(5)
    w._mm.st[5].run_flags = flags
    w._active_la_subject[5] = 2
    w._toggle_cam_run(5)
    la_ = calls(w, "look_at")
    assert la_ and la_[0][1][:3] == (5, 2, 0), f"look-at run sent {la_}"
    assert [k for n, a, k in w.log if n == "look_at"][0].get("repeat") is True
    assert w._run_states[5]['active'] is (not waits), \
        f"run_flags={flags}: the button {'did not wait' if waits else 'waited'} for the mount"
print("   look-at: START_LOOK_AT_MOVE with repeat; the button waits when [10] can say   OK")

# ---- 5. the phone does the same: the REAL toggleRun, in node ---------------------
print("\n5. web_app.h's toggleRun, run in node:")
if not shutil.which("node"):
    print("   node not installed — skipped")
else:
    def js_const(name):
        mm = re.search(rf"^const {name}\s*=[^;]*;", WEB, re.M)
        assert mm, name
        return mm.group(0)
    js = "\n".join([js_const(n) for n in (
        "CMD_JOG", "CMD_START_LOOK_AT_MOVE", "CMD_START_RUN", "CMD_STOP_RUN", "STATUS_RUN_BYTE",
        "STATUS_RUN_POSITIONS", "STATUS_RUN_LOOK_AT", "NUM_SLOTS", "FLAG_HAS_SLIDER",
        "FLAG_LOOK_AT_MODE")] + [
        "let _ptPreset = 2, _szPreset = 2;", "let _seq = 0;",
        body_of(WEB, "function nextSeq("), body_of(WEB, "function crc16("),
        body_of(WEB, "function buildPkt("), body_of(WEB, "function clamp("),
        body_of(WEB, "function mkJog("), body_of(WEB, "function mkStartLookAtMove("),
        body_of(WEB, "function camIsLookAt("), body_of(WEB, "function camRunning("),
        body_of(WEB, "function toggleRun("),
        r"""
let camSt = {}; const sent = [], toasts = [];
function wsSend(b) { sent.push(Array.from(b)); }
function toast(m) { toasts.push(m); }
function camName(i) { return 'CAM ' + i; }
const out = [];
for (const c of JSON.parse(require('fs').readFileSync(process.argv[2]))) {
    sent.length = 0; toasts.length = 0;
    camSt[c.cam] = Object.assign({connected: true, runFlags: 0, flags: 0, slotOccupied: 0,
                                  activeLaSubject: -1, activeSlPreset: 2}, c.st);
    toggleRun(c.cam);
    out.push({sent: sent.slice(), toasts: toasts.slice()});
}
console.log(JSON.stringify(out));
"""])
    (TMP / "run.js").write_text(js)
    LA = 0x10 | 0x80
    cases = [
        dict(cam=1, st=dict(connected=False)),                                  # offline
        dict(cam=2, st=dict(runFlags=1, slotOccupied=m(0, 1))),                 # running -> stop
        dict(cam=3, st=dict(runFlags=None, slotOccupied=m(0, 1))),              # old firmware
        dict(cam=4, st=dict(flags=LA, activeLaSubject=3, activeSlPreset=4)),    # look-at, subject 4
        dict(cam=4, st=dict(flags=LA, slotOccupied=m(2, 5))),                   # look-at, first stored
        dict(cam=4, st=dict(flags=LA)),                                         # look-at, no subject
        dict(cam=5, st=dict(slotOccupied=m(7))),                                # one position
        dict(cam=5, st=dict(slotOccupied=m(0, 9))),                             # a start
        dict(cam=2, st=dict(runFlags=2, flags=LA, activeLaSubject=1)),          # look-at running -> stop
    ]
    (TMP / "cases.json").write_text(json.dumps(cases))
    r = subprocess.run(["node", str(TMP / "run.js"), str(TMP / "cases.json")],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"node failed:\n{r.stderr[:1500]}"
    res = json.loads(r.stdout)

    def pkt(b):
        p = P.parse_packet(bytes(b))           # the PC's parser: framing and CRC
        return p.mount_id, int(p.cmd), bytes(p.payload)

    assert res[0] == {"sent": [], "toasts": ["CAM 1 is offline"]}, res[0]
    assert [pkt(b) for b in res[1]["sent"]] == [(2, 0x2B, b"")] and not res[1]["toasts"], \
        f"Stop on a running camera sent {res[1]} — the PC's Stop is STOP_RUN, not a zero jog"
    assert res[2]["sent"] == [] and "firmware" in res[2]["toasts"][0], \
        f"a mount that cannot report runs was started blind: {res[2]}"
    assert [pkt(b) for b in res[3]["sent"]] == [(4, 0x27, bytes([3, 0, 4, 1]))], \
        f"the look-at run sent {res[3]} — the PC sends subject, direction 0, the SZ preset, repeat"
    assert [pkt(b) for b in res[4]["sent"]] == [(4, 0x27, bytes([2, 0, 2, 1]))], \
        f"with no subject selected the run sent {res[4]}, not the first stored (3)"
    assert res[5]["sent"] == [] and "subject" in res[5]["toasts"][0], res[5]
    assert res[6]["sent"] == [] and "two" in res[6]["toasts"][0], \
        f"a run with one position stored: {res[6]}"
    assert [pkt(b) for b in res[7]["sent"]] == [(5, 0x2A, b"")] and not res[7]["toasts"], \
        f"a start sent {res[7]}, not an empty START_RUN to the mount"
    assert pkt(res[7]["sent"][0])[:2] == (P.parse_packet(P.pkt_start_run(5)).mount_id,
                                          int(P.parse_packet(P.pkt_start_run(5)).cmd))
    assert [pkt(b) for b in res[8]["sent"]] == [(2, 0x2B, b"")], \
        f"Stop on a running look-at mount sent {res[8]}, not STOP_RUN"
    print("   offline, stop, old firmware, look-at (chosen and first stored subject,\n"
          "   none), one position, a start — each sends what the PC sends, framed\n"
          "   and CRC'd as the PC's own parser accepts   OK")

# ---- 6. where the phone shows it ----------------------------------------------------
print("\n6. the web app's screens:")
for bid in ("btn-run-p", "gc-run", "ext-pos-run-btn"):
    tag = re.search(rf'<button[^>]*\bid="{bid}"[^>]*>RUN</button>', WEB)
    assert tag and re.search(r'class="[^"]*\brun-btn\b', tag.group(0)), f"no RUN button {bid}"
assert "['btn-run-p', 'gc-run', 'ext-pos-run-btn'].forEach(id =>" in WEB
bar = WEB[WEB.index('<div id="gc-bottom">'):]
bar = bar[:bar.index('<button class="ctrl-btn" id="gc-estop">')]
mid_ = bar[bar.index('<div class="gc-mid">'):]
i_move, i_stat, i_run = (mid_.index(s) for s in ('id="gc-move"', 'id="gc-stat"', 'id="gc-run"'))
assert i_move < i_stat < i_run, \
    "the GC bottom bar is not MOVE, then the hub status, then RUN"
ctrl = WEB[WEB.index('<div id="gc-ctrl-row">'):WEB.index('<div id="gc-bottom">')]
assert 'gc-run' not in ctrl and 'gc-move' not in ctrl, \
    "RUN or MOVE is still in the row with CLEAR and SET"
lbl = body_of(WEB, "function camBtnLabel(")
assert "'■ STOP'" in lbl and "'▶ RUN'" in lbl
click = WEB[WEB.index("function makeCamBtns("):WEB.index("function refreshCamBtns(")]
assert "if (_runMode || camRunning(i)) { toggleRun(i); return; }" in click and \
    click.index("toggleRun(i)") < click.index("selCam = i;"), \
    "a camera tap in Run mode selects the camera — the PC toggles its run instead"
srm = body_of(WEB, "function setRunMode(")
assert "setUiMode('move')" in srm and "_extSetMode = _extClearMode = _extLaSetMode = false" in srm
sum_ = body_of(WEB, "function setUiMode(")
assert "setRunMode(false)" in sum_ or "_runMode" in sum_, "SET/CLEAR/EDIT do not disarm Run"
# The Move popup: everything held is let go on close, and closing GC closes it.
cm = body_of(WEB, "function closeMove(")
assert "[mvHslZoom, mvHslSlider, mvJoy].forEach(w => w && w.cancel && w.cancel());" in cm and \
       "_stopJogLoop();" in cm, "closing Move does not let go of the faders and the stick"
assert "closeMove();" in body_of(WEB, "function setGcView("), "leaving GC leaves Move open"
aa = body_of(WEB, "function _anyActive(")
assert all(x in aa for x in ("mvHslZoom", "mvHslSlider", "mvJoy")), \
    "the jog loop does not see the Move popup's controls"
assert re.search(r"if \(!_moveOpen\) return;", WEB), "the popup's controls act while it is shut"
# Its focus button: the selected camera's, by the same switch as the others.
assert '<button class="focus-btn" id="mv-focus" data-cam="sel"></button>' in WEB, \
    "the Move popup has no focus button for the selected camera"
assert "wireFocusBtn(document.getElementById('mv-focus'));" in WEB, \
    "the Move popup's focus button is never wired — a tap would do nothing"
smc = body_of(WEB, "function sizeMoveControls(")
assert "focusBtnsOn ? MV_FOCUS : 0" in smc, "the popup's controls ignore the focus button's room"
assert "if (_moveOpen) moveTitle();" in body_of(WEB, "function refreshCamBtnLabels("), \
    "the popup's title keeps a camera the pad has switched away from"
assert "if (_moveOpen) requestAnimationFrame(sizeMoveControls);" in body_of(WEB, "function setFocusBtns(")
print("   RUN on the portrait, GC and Extended screens; GC's bottom bar is MOVE,\n"
      "   status, RUN; a Run-mode tap toggles, never selects; Move lets go of\n"
      "   everything on close and with GC; its focus button is the selected\n"
      "   camera's and its controls make room for it   OK")

print("\nALL CHECKS PASSED")
