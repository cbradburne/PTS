"""Every send a mount makes waits for the one in flight, and a stall still reboots.

espressif/esp-idf#18682, open, reproduced on ESP-IDF 5.5.5 and 6.1-dev: ESP-NOW
stops finishing sends until its queue fills and every send is refused NO_MEM —
the stall the rig's mounts reboot out of about once per 80 hours on.  The one
thing reported to stop it is pacing: never send while a send is still out.

The mount paced its command replies and routine reports, but the camera's
status forward (the NimBLE task, core 0) and the Teensy's forwarded frames went
out at once, from two cores; cam1's and cam3's stalls (2026-10-08, -09) were
both first refused on a CAM_STATUS.  Now every send goes through one queue.

  1. shared/tx_pacer.h, compiled here: the queue's rules, then ten minutes of a
     simulated rig — never two in flight, order kept, a camera's burst taken,
     a bad link left alone, a stalled radio caught inside 2 s;
  2. the mount sketch: one call to the radio, the lock on both cores, the pump
     in loop(), a stall on the ladder's clock, the switch, the health tail;
  3. the PC: the health line says "paced", a stall says "held", not "refused".

Run directly, or via tools/run_tests.sh.
"""
import os, sys, pathlib, re, shutil, struct, subprocess, tempfile, types, logging
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
SHARED = REPO / "firmware" / "shared"
INO = (REPO / "firmware/esp_mount_amoled175/esp_mount_amoled175.ino").read_text()

DRIVER = r"""
#include <cstdio>
#include <cstring>
#include <cstdint>
#include <vector>
#include "tx_pacer.h"

static int ops() {                      // one rule at a time, from stdin
    TxPacer<4, 8> q;
    char op;
    while (scanf(" %c", &op) == 1) {
        if (op == 'o') {                 // offer <len> <first byte> <out>
            unsigned n, b, out;
            if (scanf("%u %u %u", &n, &b, &out) != 3) return 1;
            uint8_t d[16];
            for (unsigned i = 0; i < 16; i++) d[i] = (uint8_t)(b + i);
            printf("%d\n", (int)q.offer(d, (uint16_t)n, out != 0));
        } else if (op == 'n') {          // next <out>
            unsigned out;
            if (scanf("%u", &out) != 1) return 1;
            uint8_t d[8]; uint16_t n = 0;
            if (q.next(out != 0, d, &n)) printf("1 %u %u\n", n, d[0]); else printf("0\n");
        } else if (op == 'f') {          // front_byte <off>
            unsigned off;
            if (scanf("%u", &off) != 1) return 1;
            printf("%u\n", q.front_byte(off));
        } else if (op == 't') {          // take_stats
            uint16_t w, dp, x;
            q.take_stats(&w, &dp, &x);
            printf("%u %u %u\n", w, dp, x);
        } else if (op == 's') {          // stuck <waiting> <since cb> <since sent> <limit>
            unsigned w, cb, st, lim;
            if (scanf("%u %u %u %u", &w, &cb, &st, &lim) != 4) return 1;
            printf("%d\n", (int)tx_pacer_stuck(w != 0, cb, st, lim));
        }
    }
    return 0;
}

// Ten minutes of a mount, 1 ms a step, with the sketch's own rules: a frame is
// offered against "one is out"; loop() pumps every 10 ms; the radio finishes a
// send 3 ms after it goes, or 300 ms (a failure) while the link is bad, or never
// once it has stalled; the stall check is the pump's.
static int sim() {
    static TxPacer<16, 250> q;
    const uint32_t BAD_FROM = 300000, BAD_TO = 310000, STALL = 400000, END = 600000;
    bool out = false; uint32_t done_at = 0, cb_last = 0, ok_last = 0;
    unsigned max_out = 0, sent = 0, dropped = 0, stuck_during_bad = 0, burst_dropped = 0;
    uint32_t seq = 0, expect = 0, order_bad = 0, first_stuck = 0;
    std::vector<uint32_t> sent_seq;
    auto issue = [&](const uint8_t *d, uint32_t t) {
        uint32_t s; memcpy(&s, d + 8, 4);
        if (s != expect) order_bad++;
        expect = s + 1;
        out = true; ok_last = t; sent++;
        if (t >= STALL) done_at = 0xFFFFFFFF;                     // never finishes
        else done_at = t + ((t >= BAD_FROM && t < BAD_TO) ? 300 : 3);
        if (max_out < 1) max_out = 1;
    };
    auto offer = [&](uint32_t t) {
        uint8_t d[64] = {}; uint32_t s = seq++;
        memcpy(d + 8, &s, 4);
        switch (q.offer(d, 16, out)) {
        case TxPacer<16, 250>::SEND_NOW: issue(d, t); break;
        case TxPacer<16, 250>::DROPPED:  dropped++; expect = s + 1; break;
        default: break;
        }
        uint8_t f[250]; uint16_t n;
        while (q.next(out, f, &n)) issue(f, t);
    };
    for (uint32_t t = 1; t < END; t++) {
        if (out && t >= done_at) { out = false; cb_last = t; }     // the send callback
        if (t % 5000 == 0) offer(t);                              // STATUS heartbeat
        if (t % 10000 == 0) { offer(t); offer(t); }               // HEALTH + RF, together
        if (t % 3000 == 0) offer(t);                              // an ACK to GET_CONFIG
        if (t % 2000 == 0) offer(t);                              // a camera notification
        if (t >= 100000 && t < 100400 && t % 10 == 0) {           // the camera pairs: 40 at once
            unsigned before = dropped; offer(t); burst_dropped += dropped - before;
        }
        if (t >= 200000 && t < 205000 && t % 100 == 0) offer(t);  // a move: Teensy at 10/s
        if (t % 10 == 0) {                                        // loop(): the pump
            uint8_t f[250]; uint16_t n;
            while (q.next(out, f, &n)) issue(f, t);
            if (tx_pacer_stuck(q.count != 0, t - cb_last, t - ok_last, 1000)) {
                if (t >= BAD_FROM && t < BAD_TO + 1000) stuck_during_bad++;
                if (!first_stuck) first_stuck = t;
            }
        }
    }
    printf("max_out %u\nsent %u\ndropped %u\nburst_dropped %u\norder_bad %u\n"
           "stuck_during_bad %u\nfirst_stuck_after_stall %d\n",
           max_out, sent, dropped, burst_dropped, order_bad, stuck_during_bad,
           first_stuck ? (int)(first_stuck - STALL) : -1);
    return 0;
}

int main(int argc, char **argv) { return (argc > 1 && argv[1][0] == 's') ? sim() : ops(); }
"""

# ---- 1. the queue ----------------------------------------------------------------
print("1. shared/tx_pacer.h:")
cxx = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
if cxx:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="txpacer-"))
    (tmp / "d.cpp").write_text(DRIVER)
    r = subprocess.run([cxx, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(SHARED),
                        str(tmp / "d.cpp"), "-o", str(tmp / "d")], capture_output=True, text=True)
    assert r.returncode == 0, "tx_pacer.h does not compile cleanly:\n" + r.stderr[:2000]

    def run(script):
        return subprocess.run([str(tmp / "d")], input=script, capture_output=True,
                              text=True, check=True).stdout.split("\n")[:-1]

    SEND, WAIT, DROP = "0", "1", "2"
    got = run("o 5 10 0\n"           # nothing out, nothing waiting: straight out
              "o 5 20 1\n"           # one out: waits
              "o 5 30 0\n"           # nothing out, but one waiting: waits behind it
              "n 1\n"                # still one out: nothing
              "n 0\n" "n 0\n" "n 0\n")
    assert got == [SEND, WAIT, WAIT, "0", "1 5 20", "1 5 30", "0"], got
    print("   straight out only when nothing is out or waiting; then in order  OK")
    got = run("o 5 1 1\n" "o 5 2 1\n" "o 5 3 1\n" "o 5 4 1\n" "o 5 5 1\n"   # 4 deep
              "o 9 6 0\n"            # too long for a slot
              "f 0\n" "t\n" "t\n")
    assert got == [WAIT, WAIT, WAIT, WAIT, DROP, DROP, "1", "4 4 2", "0 0 0"], got
    print("   full, or too long, it is dropped and counted; the counts are per")
    print("   window                                                          OK")
    assert run("s 1 1001 1001 1000\n" "s 1 1000 5000 1000\n" "s 1 5000 1000 1000\n"
               "s 0 5000 5000 1000\n") == ["1", "0", "0", "0"]
    print("   stuck only with something waiting and nothing finished or sent   OK")

    out = subprocess.run([str(tmp / "d"), "sim"], capture_output=True, text=True,
                         check=True).stdout
    sim = {k: int(v) for k, v in (l.split() for l in out.splitlines())}
    assert sim["max_out"] == 1, sim
    assert sim["order_bad"] == 0, f"sends went out of order: {sim}"
    assert sim["burst_dropped"] == 0, f"a camera's burst lost notifications: {sim}"
    assert sim["stuck_during_bad"] == 0, f"a bad link was taken for a stall: {sim}"
    assert 1000 <= sim["first_stuck_after_stall"] <= 2000, \
        f"the stall was caught {sim['first_stuck_after_stall']} ms after it began: {sim}"
    print("   ten minutes of a mount: one in flight at most, %d sends in order, a"
          % sim["sent"])
    print("   camera's burst of 40 taken whole, 300 ms failures left alone, a stalled")
    print("   radio caught %.1f s in                                           OK"
          % (sim["first_stuck_after_stall"] / 1000))
    shutil.rmtree(tmp, ignore_errors=True)
else:
    print("   no C++ compiler: the queue is not run here")

# ---- 2. the mount sketch ------------------------------------------------------------
print("\n2. the mount sketch:")
code = re.sub(r"//[^\n]*", "", INO)


def body(sig):
    b = code[code.index(sig):]
    return b[:b.index("\n}\n")]


assert re.search(r"#ifndef ESPNOW_TX_PACED\s+#define ESPNOW_TX_PACED 1\s+#endif", code), \
    "pacing is not on by default, or cannot be switched off with ESPNOW_TX_PACED=0"
assert re.search(r"#define ESPNOW_TX_INFLIGHT_CAP 1\b", code), \
    "the pacing waits on ESPNOW_TX_INFLIGHT_CAP, which is no longer 1"
calls = re.findall(r"esp_now_send\(", code)
assert len(calls) == 1 and "esp_now_send(" in body("static void espnow_send_now("), \
    "esp_now_send() is called somewhere other than espnow_send_now()"
print("   on unless built ESPNOW_TX_PACED=0; one place calls the radio        OK")

tx = body("static void espnow_tx(const uint8_t *buf, uint16_t n) {")
assert "xSemaphoreTake(_tx_lock, portMAX_DELAY);" in tx and \
       "_pacer->offer(buf, n, espnow_tx_saturated())" in tx and \
       "pace_pump_locked();" in tx and "xSemaphoreGive(_tx_lock);" in tx, \
    "espnow_tx() does not offer every send to the pacer under the lock"
assert "case Pacer::DROPPED:  pace_held(cmd, true);" in tx and \
       "case Pacer::WAITING:  if (pace_stuck()) pace_held(cmd, false);" in tx, \
    "a send held by a stall, or dropped, is not counted for the ladder"
fwd = code[code.index("ble_cam_on_status([](const uint8_t *d, uint16_t n) {"):]
fwd = fwd[:fwd.index("});")]
assert "espnow_tx(buf," in fwd, "the camera's status forward does not go through the pacer"
print("   every send — the camera's, from core 0, too — offered under the lock  OK")

loop = body("void loop() {")
assert "espnow_tx_pump();" in loop, "loop() does not pump the waiting sends"
pump = body("static void espnow_tx_pump() {")
assert "if (pace_stuck()) {" in pump and "_espnow_last_tx_err = MOUNT_TX_HELD;" in pump \
       and "nomem_note(_pacer->front_byte(6));" in pump, \
    "a stall under pacing does not reach the NO_MEM ladder's clock"
held = body("static void pace_held(uint8_t cmd, bool dropped) {")
assert "_espnow_last_tx_err = MOUNT_TX_HELD;" in held and "nomem_note(cmd);" in held, \
    "a held send is not counted as the ladder's refusal"
stuck = body("static bool pace_stuck() {")
assert "clock_age_ms(now, _espnow_cb_last_ms)" in stuck and \
       "clock_age_ms(now, _espnow_ok_last_ms)" in stuck and "PACE_STUCK_MS" in stuck, \
    "the stall check does not age both stamps with clock_age_ms against PACE_STUCK_MS"
print("   the pump every pass; a stall on the ladder's clock, under MOUNT_TX_HELD  OK")

setup = body("void setup() {")
assert "heap_caps_malloc(sizeof(Pacer), MALLOC_CAP_SPIRAM)" in setup and \
       setup.index("_tx_lock = xSemaphoreCreateMutex();") < setup.index("esp_now_init()"), \
    "the pacer is not made, in PSRAM, before ESP-NOW starts"
health = body("static void send_health(bool anomaly) {")
assert "t.pace_deepest        = HEALTH_PACE_OFF;" in health and \
       "_pacer->take_stats(&t.pace_waited, &t.pace_deepest, &dropped);" in health, \
    "the health report does not say whether, and how much, the mount paced"
print("   made in PSRAM before ESP-NOW starts; the health report tells        OK")

# ---- 3. the PC ----------------------------------------------------------------------
print("\n3. what the PC says:")
try:
    from PyQt6.QtCore import QCoreApplication
except ImportError:
    print("   PyQt6 not installed: the mount manager needs it, skipping.")
    print("\nALL CHECKS PASSED")
    sys.exit(0)
sys.path.insert(0, str(REPO / "pc_app"))
import threading
from comms import bridge as B, mount_manager as MM, protocol as P
from comms.protocol import Packet, Cmd
app = QCoreApplication.instance() or QCoreApplication([])


class Lines(logging.Handler):
    def __init__(self): super().__init__(); self.got = []
    def emit(self, r): self.got.append(r.getMessage())


lines = Lines()
for name in (B.__name__, MM.__name__):
    logging.getLogger(name).addHandler(lines)
    logging.getLogger(name).setLevel(logging.INFO)

b = B.Bridge.__new__(B.Bridge)
b._diag_lock = threading.Lock()
b._node_uptime = {}; b._sat_names = {}; b._cam_ble = {}
b._node_state_cur = {}; b._node_state_prev = {}
b._node_state_written = 0.0; b._node_state_path = None
TAIL32 = struct.pack(">IIIHHHHHHHHHH", 65000, 58000, 30000, 0, 0, 12288, 0, 13, 0, 1, 0, 0, 0)


def health_line(tail):
    lines.got = []
    pl = struct.pack(">BBIIIHHbBI", 1, 1, 36000, 8400000, 8390000, 10, 1, -40, 0x06, 0) + tail
    b._note_node_health(Packet(mount_id=5, seq=0, cmd=Cmd.HEALTH, payload=pl))
    return [l for l in lines.got if l.startswith("NODE HEALTH")][-1]


line = health_line(TAIL32 + struct.pack(">HH", 12, 2))
assert "| heartbeats 1 | paced: 12 waited (queue up to 2) |" in line, line
assert "| paced, none waited" in health_line(TAIL32 + struct.pack(">HH", 0, 0))
assert "paced" not in health_line(TAIL32 + struct.pack(">HH", 0, P.HEALTH_PACE_OFF))
assert "paced" not in health_line(TAIL32)
print("   the health line: \"paced: 12 waited (queue up to 2)\", \"paced, none")
print("   waited\" — and nothing from a build that does not pace            OK")


class Link:
    connected = True
    def __init__(self): self.cb = None
    def on_packet(self, cb): self.cb = cb
    def off_packet(self, cb): pass
    def send(self, data): pass
    def seconds_since_tracked_tx(self): return 0.0


link = Link()
mm = MM.MountManager(link)


def stall(err, in_flight, held):
    lines.got = []
    pl = (bytes([P.MOUNT_EVENT_NOMEM_REBOOT, 0, 1, 0, 0, 0, 0, 0, 4, 0, held,
                 err >> 8, err & 0xFF, 0])
          + struct.pack(P._NOMEM_SNAP_FMT, 106000, 4178260, 4010, 4010, 4, in_flight, 0,
                        58000, 58000, 30000, 1, 1, 10, 1, P.MOUNT_NOMEM_BLE_LINKED,
                        int(Cmd.CAM_STATUS), 0, 5, held)
          + struct.pack(P._NOMEM_LADDER_FMT, P.MOUNT_NOMEM_STEP_RESERVE, 0, 0xFFFF, 0xFFFF,
                        1000, 3000, 12288, 0, 71000))
    link.cb(Packet(mount_id=5, seq=0, cmd=Cmd.MOUNT_EVENT, payload=pl))
    return [l for l in lines.got if l.startswith("MOUNT EVENT cam5")]


got = stall(P.MOUNT_TX_HELD, 1, 4)
assert got[0].startswith("MOUNT EVENT cam5 REBOOTED ON A STALLED RADIO — a send went out "
                         "and never finished, every send after it was held for 3 s, and "
                         "the ladder did not end it | 1 send(s) outstanding and NOT ONE "
                         "completed in the 3 s — the radio had stopped finishing sends"), got[0]
assert ("at the first held send: up" in got[-1] and "| held first: CAM_STATUS || until the "
        "reboot: 4 held, 0 completions" in got[-1] and "4 sends HELD by the mount's pacing "
        "— the one in flight never finished" in got[-1]), got[-1]
assert not any("refus" in l.lower() for l in got), f"a held stall still talks of refusals: {got}"
print("   a stall on a paced mount: a send that never finished, the rest HELD  OK")
got = stall(0x3067, 32, 31)
assert got[0].startswith("MOUNT EVENT cam5 REBOOTED ON REFUSED SENDS — every send refused "
                         "for lack of memory (NO_MEM)"), got[0]
assert "| refused first: CAM_STATUS || until the reboot: 31 refused" in got[-1] and \
       "31 sends REFUSED by the stack, last: OUT OF MEMORY" in got[-1], got[-1]
print("   one on a mount that does not pace reads as it always did          OK")

print("\nALL CHECKS PASSED")
