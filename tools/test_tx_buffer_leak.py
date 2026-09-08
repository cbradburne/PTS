"""The bridge counts the buffers the radio never gives back.

esp_now_send() takes a buffer from a small pool; the send-complete callback
returns it. espressif/esp-idf#18682 is that pool being exhausted by callbacks
that never fire, and it is what "the stack's TX queue was full" means when a
mount goes silent.

Nothing counted the callbacks, so the difference — taken and not returned — has
never been visible anywhere. Every counter the bridge had moves only once the
pool is ALREADY empty, and by then the radio cannot report it: cam1's own health
report read a clean zero eighteen seconds before it wedged, twice in 44 hours.

    in_flight = issued - callbacks

moves at the FIRST lost buffer. The floor of it over a window only rises, so a
rising floor is a leak and a flat one is sends in progress.

This is an instrument, not a remedy. Acting on it — a clean teardown while the
radio still works, rather than twenty seconds isolated and a reboot — waits
until the rig has said what the number actually does.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, io, logging, pathlib, re, struct, threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

INO = (REPO / "firmware/esp_mount_amoled175/esp_mount_amoled175.ino").read_text()

# ---- 1. both halves of the subtraction are counted -------------------------
print("1. what the bridge counts:")
send = INO[INO.index("static void espnow_tx("):]
send = send[:send.index("\n}")]
assert "_espnow_issued = _espnow_issued + 1;" in send, \
    "sends handed to the stack are not counted, so there is nothing to subtract\n" \
    "    the callbacks from"
assert send.index("== ESP_OK") < send.index("_espnow_issued"), \
    "a REFUSED send is being counted as issued — it never took a buffer, so it\n" \
    "    would read as a leak that is not there"

cb = INO[INO.index("static void on_espnow_sent("):]
cb = cb[:cb.index("\n}")]
assert "_espnow_cb_total = _espnow_cb_total + 1;" in cb, \
    "callbacks are not counted"
assert cb.index("_espnow_cb_total") < cb.index("ESP_NOW_SEND_SUCCESS"), \
    "the callback is only counted on one status. Success and failure BOTH return\n" \
    "    the buffer; not firing at all is the fault being measured."
print("   issued on ESP_OK, callbacks on either status      OK")

# ---- 2. the floor only rises ------------------------------------------------
# in_flight bounces with every send. A leak is it never coming back down, so
# what is reported is the floor, and a floor that can fall is just in_flight
# with extra steps.
print("\n2. the floor:")
poll = INO[INO.index("static void espnow_leak_poll()"):]
poll = poll[:poll.index("\n}")]
assert "win_min > _espnow_leak_floor" in poll, \
    "the floor is assigned from the window minimum without checking it ROSE, so\n" \
    "    a quiet moment would erase the evidence of a leak"
assert "255" in poll, "the floor is not saturated, and it ships in one byte"
assert re.search(r"now - win_ms < \d+UL", poll), \
    "the window is gone — a floor taken from a single sample is just in_flight"
print("   rises only, saturates at 255, measured over a window  OK")

# It measures and does nothing else, on purpose.
for act in ("esp_restart", "espnow_wifi_restart", "_espnow_need_reinit"):
    assert act not in poll, \
        f"the leak poll calls {act}. It is an instrument: acting on a number\n" \
        "    nobody has watched yet is how a healthy mount gets restarted for a\n" \
        "    threshold that turns out to be wrong."
print("   and does not act on what it finds                 OK")

# ---- 3. it reaches the operator ---------------------------------------------
print("\n3. what the health line carries:")
assert "((uint32_t)_espnow_leak_floor << 16)" in INO, \
    "the floor is not in node_u32, so nothing carries it off the mount"
assert "(_reinit_count > 255 ? 255UL : (_reinit_count & 0xFFUL))" in INO, \
    "the reinit count is not in the low byte"
assert "_espnow_drain_deferred > 255 ? 255" in INO, \
    "the drain-deferral count is not carried, so a mount that stops wedging\n" \
    "    cannot say whether the bound had anything to do with it"
print("   wedges(8) | leak(8) | deferrals(8) | reinit(8)     OK")

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
    def __init__(self, n32):
        self.payload = struct.pack(">BBIIIHHbBI", 1, 1, 36000, 8400000, 8390000,
                                   9, 6, -33, 0, n32)


def health(n32):
    b = Bridge.__new__(Bridge)
    b._diag_lock = threading.Lock(); b._node_uptime = {}; b._sat_names = {}
    b._cam_ble = {}; b._node_state_cur = {}; b._node_state_prev = {}
    b._node_state_written = 0.0; b._node_state_path = None
    BUF.truncate(0); BUF.seek(0)
    b._note_node_health(_P(n32))
    return BUF.getvalue().strip()


clean = health(1)
assert "LEAKED" not in clean and "WEDGES" not in clean, \
    f"a healthy bridge is reporting a fault: {clean}"
assert "n32 1" in clean, f"the reinit count is not where it was: {clean}"
print("   healthy bridge says neither                       OK")

leaking = health((7 << 16) | 1)
assert "TX BUFFERS LEAKED 7" in leaking, f"a leak of 7 does not read as one: {leaking}"
assert "n32 1" in leaking, f"the reinit count moved: {leaking}"

both = health((2 << 24) | (7 << 16) | 1)
for want in ("WEDGES 2", "TX BUFFERS LEAKED 7", "n32 1"):
    assert want in both, f"missing {want!r}: {both}"
print("   7 leaked reads as 7, beside 2 wedges and reinit 1  OK")

# All FOUR fields are independent — a packing mistake shows as one moving the
# others, and this is the only thing holding the two ends of that field together.
for w, lk, df, ri in ((0, 0, 0, 3), (1, 0, 0, 1), (0, 64, 0, 2),
                      (0, 0, 9, 1), (2, 7, 40, 5), (255, 255, 255, 255)):
    line = health((w << 24) | (lk << 16) | (df << 8) | ri)
    assert f"n32 {ri}" in line, \
        f"reinit {ri} lost when wedges={w} leak={lk} defer={df}: {line}"
    if lk:
        assert f"LEAKED {lk}" in line, f"leak {lk} lost: {line}"
    if w:
        assert f"WEDGES {w}" in line, f"wedges {w} lost: {line}"
    if df:
        assert f"drain deferred {df}" in line, f"deferrals {df} lost: {line}"
    else:
        assert "drain deferred" not in line, \
            f"a bound that never engaged is reported as if it had: {line}"
print("   all four independent across the range             OK")

# The distinction the whole counter exists for: a mount that has stopped
# wedging while this reads 0 did not stop because of the bound.
quiet = health(1)
assert "drain deferred" not in quiet, "a quiet mount claims the bound engaged"
engaged = health((40 << 8) | 1)
assert "drain deferred 40" in engaged, f"the bound engaging is not reported: {engaged}"
assert health((255 << 8) | 1).count("255+") == 1, \
    "a saturated deferral count does not say it saturated, so 255 reads as an\n" \
    "    exact figure when it means 'at least'"
print("   engaged vs never-engaged are distinguishable      OK")

_lg.handlers, _lg.level, _lg.propagate = _saved
print("\nALL CHECKS PASSED")
