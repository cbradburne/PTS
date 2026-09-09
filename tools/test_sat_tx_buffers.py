"""The satellite can say WHY its radio is refusing.

Foyer wedged on 2026-09-09 at 12.60 h uptime and restarted itself. All the log
could report was the refusal:

    SAT DOWNLINK Foyer  offered 58, sent 41, refused 600 (641 send calls)
                        REFUSING — 11 retries per frame offered

which is the symptom. A radio that is merely busy still returns its buffers; a
radio whose send callback has stopped never does, the pool empties, and every
send after that is refused with NO_MEM (espressif/esp-idf#18682). Opposite
diagnoses, opposite responses, and a refusal count cannot separate them — it was
the only node type with no in-flight counter at all.

    in_flight = accepted - callbacks

Two traps this pins, both learned the hard way on the mount. The floor must be
full width where it is measured, because on the mount it was a saturating
uint8_t that ALSO armed a send guard, and once in_flight passed 257 the guard
latched for the rest of the boot — cam1 answered every command at 100% for two
hours while sending no health at all. And the measurement must not sit behind
another function's early returns, which is how the mount's RF report ended up
disabled on the one mount that had the fault.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, io, logging, pathlib, re, threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

SAT  = (REPO / "firmware/esp32_satellite/esp32_satellite.ino").read_text()
HDR  = (REPO / "firmware/shared/protocol.h").read_text()
FLAT = re.sub(r"\s+", " ", SAT)


def block(start, end="\n}"):
    i = SAT.index(start + " {") if (start + " {") in SAT else SAT.index(start)
    return SAT[i:i + SAT[i:].index(end)]


# ---- 1. both halves of the subtraction -------------------------------------
print("1. what the satellite counts:")
cb = block("static void on_espnow_sent(")
assert re.search(r"_sat_cb_total\s*=\s*_sat_cb_total \+ 1;", cb), \
    "callbacks are not counted, so nothing can notice them stopping"
assert cb.index("_sat_cb_total") < cb.index("ESP_NOW_SEND_SUCCESS"), \
    "the callback is counted only on one status. BOTH outcomes return the\n" \
    "    buffer; not firing at all is the fault being measured."
assert re.search(r"_sat_cb_last_ms\s*=\s*millis\(\);", cb), \
    "nothing records WHEN the last callback arrived, so a stall has no clock"
print("   counted first, before the status, and timestamped  OK")

# The other half already existed — but only if it still counts ESP_OK alone. A
# refused send never took a buffer, so counting it would read as an outstanding
# send that can never complete.
pump = block("static void dn_pump(")
assert "_sat_dn_sent_total++;" in pump, "accepted sends are not counted"
assert pump.index("e == ESP_OK") < pump.index("_sat_dn_sent_total++;"), \
    "a REFUSED send is counted as accepted, which reads as a permanent leak"
assert SAT.count("esp_now_send(_peer") == 1, \
    "there is more than one esp_now_send() call site, so _sat_dn_sent_total no\n" \
    "    longer accounts for every buffer taken and the subtraction drifts"
print("   accepted on ESP_OK only, one send path             OK")

# ---- 2. the floor ----------------------------------------------------------
print("\n2. the floor:")
poll = block("static void sat_leak_poll(")
assert "win_min > _sat_leak_floor" in poll, \
    "the floor is assigned from the window minimum without checking it ROSE, so\n" \
    "    a quiet moment would erase the evidence"
assert re.search(r"now - win_ms < \d+UL", poll), \
    "the window is gone — a floor from a single sample is just in_flight"
assert re.search(r"static uint32_t\s+_sat_leak_floor", SAT), \
    "the floor is stored narrower than in_flight. On the mount that exact\n" \
    "    choice latched a send guard for the life of the boot."
assert not re.search(r"_sat_leak_floor\s*=[^;]*0xFFFF", poll), \
    "the poll clamps the floor; clamp it at the wire and nowhere else"
for act in ("esp_restart", "espnow_recover", "_dn_recover_run"):
    assert act not in poll, \
        f"the leak poll calls {act}. It is an instrument — acting on a number\n" \
        "    nobody has watched yet is how a healthy relay gets restarted."
print("   rises only, over a window, full width, inert        OK")

# ---- 3. it must not be behind another function's early return ---------------
print("\n3. where it is called:")
loop_tail = SAT[SAT.index("downlink_report(now);"):]
loop_tail = loop_tail[:loop_tail.index("SMARK(SSEC_REPORTS);")]
assert "sat_leak_poll(now);" in loop_tail, "the poll is never called from loop()"
assert loop_tail.index("sat_leak_poll(now);") < loop_tail.index("sat_send_health(now);"), \
    "the poll runs after the report calls. Both return early on an unconnected\n" \
    "    uplink, and a measurement behind that switches itself off exactly when\n" \
    "    the box is in trouble."
for fn in ("sat_send_health", "sat_send_downlink"):
    body = block(f"static void {fn}(")
    assert "sat_leak_poll" not in body, \
        f"the poll was moved inside {fn}(), which returns early"
print("   from loop(), above both reporting functions        OK")

# ---- 4. a stack rebuild must not read as a leak ----------------------------
# espnow_recover() deinits and reinits the driver, which hands the whole buffer
# pool back. A floor measured against the old pool describes something that no
# longer exists — and this box rebuilds on every NO_MEM burst, so carrying it
# across would not be a rare mistake.
print("\n4. across a stack rebuild:")
rec = block("static void espnow_recover(")
assert "_sat_cb_total   = _sat_dn_sent_total;" in rec, \
    "in_flight is not reconciled after the rebuild, so a stack that was just\n" \
    "    FIXED reports a permanent leak"
assert "_sat_leak_floor = 0;" in rec, "the floor survives a rebuild that invalidated it"
assert "_sat_leak_worst" in rec, \
    "reconciling erases the evidence — keep the high-water, or the rebuild\n" \
    "    destroys the reading that provoked it"
assert rec.index("_sat_leak_worst") < rec.index("_sat_leak_floor = 0;"), \
    "the high-water is taken after the floor is cleared, so it always reads 0"
assert rec.index("esp_now_init") < rec.index("_sat_cb_total   =") \
       < rec.index("esp_now_register_send_cb"), \
    "the reconcile is not between init and re-registering the callback — that\n" \
    "    is the only window where no callback can fire and change it underneath"
print("   reconciled, high-water kept, in the safe window    OK")

# ---- 5. the record grew without breaking the old one -----------------------
print("\n5. the wire:")
mn = int(re.search(r"#define SAT_DOWNLINK_MIN_LEN\s+(\d+)", HDR).group(1))
ln = int(re.search(r"#define SAT_DOWNLINK_PAYLOAD_LEN\s+(\d+)", HDR).group(1))
assert mn == 25 and ln == 33, f"lengths moved: min {mn}, long {ln}"
top = int(re.search(r"#define SAT_DOWNLINK_TOP_CMDS\s+(\d+)", HDR).group(1))
assert 16 + top * 3 <= 25, \
    f"the top-command block ({top} x 3 from byte 16) now runs past byte 25 and " \
    f"collides with the appended counters"
dn = block("static void sat_send_downlink(")
for i in (25, 28, 29, 30, 31, 32):
    assert f"p[{i}]" in dn, f"byte {i} of the appended block is never written"
assert "if (leak  > 0xFFFF) leak  = 0xFFFF;" in dn, "the floor is not saturated at the wire"
assert "_sat_cb_last_ms && inf" in dn, \
    "the callback stall is reported without checking anything is outstanding, so\n" \
    "    an idle satellite would show its stall climbing for ever"
print(f"   {mn}-byte form still valid, tail at [25..{ln-1}]         OK")

# ---- 6. the PC app reads both lengths --------------------------------------
print("\n6. what the operator sees:")
BUF = io.StringIO()
_h = logging.StreamHandler(BUF); _h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
_lg = logging.getLogger("comms.bridge")
_saved = (_lg.handlers, _lg.level, _lg.propagate)
_lg.handlers, _lg.propagate = [_h], False
_lg.setLevel(logging.INFO)

from comms.bridge import Bridge
from comms.protocol import Cmd, SAT_ADDR_BASE


class _P:
    cmd = Cmd.SAT_DOWNLINK
    mount_id = SAT_ADDR_BASE + 1
    def __init__(self, payload):
        self.payload = payload


def rec(offered, attempts, sent, refused, cb=None, leak=0, stall=0):
    p = bytearray()
    for v in (offered, attempts, sent, refused):
        p += v.to_bytes(4, "big")
    p += bytes(9)                     # three empty top-command slots
    if cb is not None:
        p += cb.to_bytes(4, "big")
        p += leak.to_bytes(2, "big")
        p += stall.to_bytes(2, "big")
    return bytes(p)


def feed(*records):
    b = Bridge.__new__(Bridge)
    b._sat_dn_prev = {}
    b._sat_names = {1: "Foyer"}
    out = []
    for r in records:
        BUF.truncate(0); BUF.seek(0)
        b._note_sat_downlink(_P(r))
        out.append(BUF.getvalue().strip())
    return out


# The old 25-byte form must still produce its line — an unflashed satellite.
old = feed(rec(100, 100, 100, 0), rec(200, 200, 200, 0))[1]
assert "SAT DOWNLINK Foyer" in old and "offered 100" in old, \
    f"a 25-byte record from an unflashed satellite no longer reads: {old!r}"
assert "NOT RETURNED" not in old and "callback" not in old, \
    f"the old form invents counters it never sent: {old}"
print("   25-byte record from an old satellite still reads   OK")

# A healthy new one says nothing extra, and stays at INFO.
ok = feed(rec(100, 100, 100, 0, cb=100), rec(200, 200, 200, 0, cb=200))[1]
assert ok.startswith("INFO"), f"a healthy relay logs as a warning: {ok}"
assert "NOT RETURNED" not in ok and "pool is emptying" not in ok, \
    f"a healthy relay reports a fault: {ok}"
print("   a healthy relay adds nothing                       OK")

# The wedge: sends accepted, no callbacks, buffers not coming back.
bad = feed(rec(100, 100, 100, 0, cb=100),
           rec(200, 300, 140, 60, cb=100, leak=7, stall=4200))[1]
assert bad.startswith("WARNING"), f"the wedge is not raised to a warning: {bad}"
assert "TX BUFFERS NOT RETURNED 7" in bad, f"the floor is not reported: {bad}"
assert "no send callback for 4.2s" in bad, f"the stall is not reported: {bad}"
assert "40 sends accepted and NOT ONE callback" in bad, \
    f"sends completing with no callbacks at all is not called out: {bad}"
print("   a wedging relay says which of the two it is        OK")

# A rebuild resets cb_total; the difference goes negative and must not be read
# as "no callbacks", which is the same shape as the fault.
reset = feed(rec(100, 100, 100, 0, cb=100),
             rec(200, 200, 200, 0, cb=50))[1]
assert "NOT ONE callback" not in reset, \
    f"a counter reset after a rebuild reads as the fault it just cleared: {reset}"
print("   a rebuilt stack is not mistaken for the fault      OK")

# Saturation has to say it saturated, or 65535 reads as an exact figure.
sat = feed(rec(1, 1, 1, 0, cb=1), rec(2, 2, 2, 0, cb=2, leak=0xFFFF))[1]
assert "NOT RETURNED 65535+" in sat, f"a saturated floor does not say so: {sat}"
print("   a saturated floor reads as 'at least'              OK")

_lg.handlers, _lg.level, _lg.propagate = _saved
print("\nALL CHECKS PASSED")
