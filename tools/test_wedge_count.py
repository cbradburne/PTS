"""A bridge remembers how many times it has wedged.

cam1's bridge took itself down for isolation twice in 44 hours — once after
31.1 h up, once after 11.4 h. Both times it recovered on its own, and both times
every counter that could have said so was reset by the very restart that ended
it. The only way to know it had happened twice, or that the gap between them had
shrunk by two thirds, was to read 44 hours of log.

So the count is kept in the bridge's NVS. Not RTC_NOINIT like the isolation
QUOTA above it — that one is deliberately cleared by a power cycle, because it
is a budget for one episode and the operator intervening should hand it back.
This is the opposite question, "is this bridge getting worse", and a rig
switched off overnight must not forget the answer.

It rides in the half of node_u32 that used to carry the refused-send count. That
field was there on the reasoning that a refusal climbing is the wedge starting,
and therefore warning. The field log disproved it: zero in every health report
across 44 hours, including cam1's own report 18 seconds before it went down. It
cannot be otherwise — a refusal means the radio will not accept a send, so the
report carrying the number is the one thing that cannot go out. The count still
reaches the operator in the post-mortem MOUNT_EVENT, which is the only place it
has ever actually been read.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, io, logging, pathlib, re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

INO = (REPO / "firmware/esp_mount_amoled175/esp_mount_amoled175.ino").read_text()
BR  = (REPO / "pc_app/comms/bridge.py").read_text()

# ---- 1. it survives what it has to survive ---------------------------------
print("1. where the count lives:")
assert "_mount_prefs.begin(\"wedge\"" in INO, \
    "the wedge count is not in NVS. RTC_NOINIT survives a restart but not a\n" \
    "    power cycle, and a rig switched off overnight must not forget how many\n" \
    "    times a bridge has taken itself down."
load = INO[INO.index("static void wedge_count_load()"):]
load = load[:load.index("\n}")]
assert 'getUInt("n", 0)' in load, "the count is not read back at boot"

bump = INO[INO.index("static void wedge_count_bump()"):]
bump = bump[:bump.index("\n}")]
assert "_wedge_count++" in bump and 'putUInt("n"' in bump, \
    "the count is incremented without being written"
assert bump.index("putUInt") > bump.index("_wedge_count++"), \
    "it writes the count before incrementing it"
assert "_mount_prefs.end();" in bump, \
    "the NVS handle is never closed, so the write may not commit before the\n" \
    "    restart that follows it"
print("   NVS, read at boot, written before the restart      OK")

# ---- 2. it is bumped where the decision is made ----------------------------
# Specifically on the ISOLATION path, not on every restart: a reflash, a
# brownout or an operator power cycle are not wedges and must not count as one.
print("\n2. when it counts:")
restarts = re.findall(r"^[^\n]*esp_restart\(\);", INO, re.M)
assert len(restarts) >= 3, f"esp_restart call sites changed: {len(restarts)}"
bumped = re.findall(r"wedge_count_bump\(\);[^\n]*\n\s*esp_restart\(\);", INO)
assert len(bumped) == 2, \
    f"{len(bumped)} of {len(restarts)} restarts bump the count. It must be the two\n" \
    "    the radio takes itself down for — MOUNT_EVENT_ISOLATED and\n" \
    "    MOUNT_EVENT_TX_WEDGE_REBOOT — and no others, or a reflash or an\n" \
    "    identity change would read as a wedge."

# Both kinds, named. From the operator's chair they are the same event: the
# radio stopped and the bridge rebooted itself to get it back.
for kind in ("MOUNT_EVENT_ISOLATED", "MOUNT_EVENT_TX_WEDGE_REBOOT"):
    blk = INO[INO.index(f"_evt_kind    = {kind};"):]
    blk = blk[:blk.index("esp_restart();")]
    assert "wedge_count_bump();" in blk, \
        f"the {kind} restart does not bump the count, so one of the two ways a\n" \
        "    bridge wedges goes unrecorded"
print("   both radio self-rescues, and no other restart      OK")

# ---- 3. it reaches the operator --------------------------------------------
print("\n3. how it is reported:")
assert "((_wedge_count & 0xFFFFUL) << 16)" in INO, \
    "the count is not put in node_u32, so nothing carries it to the app"
assert "(_espnow_tx_refused & 0xFFFFUL) << 16" not in INO, \
    "the refused count is still in health. It reads zero in every report by\n" \
    "    construction — a refusal is the radio refusing to send, so the report\n" \
    "    carrying the number cannot go out."
assert "_evt_refused = (uint16_t)_espnow_tx_refused;" in INO, \
    "the refused count was dropped from the post-mortem too — that is the one\n" \
    "    place it has ever actually been read"
print("   node_u32 high half, refusals kept in the post-mortem OK")

# The PC reads the same half the mount writes. Two ends of one field, and
# nothing else holds them together.
assert "wedges  = (n32 >> 16) & 0xFFFF" in BR, \
    "the app does not read the high half as the wedge count"
assert 'WEDGES %d" % wedges' in BR, "the count is decoded and never printed"
assert "TX REFUSED" not in BR, \
    "the app still labels that half 'TX REFUSED', so a wedge count would print\n" \
    "    under the name of the thing it replaced"
print("   the app reads the same half, and names it WEDGES   OK")

# ---- 4. and it only speaks when there is something to say ------------------
print("\n4. a bridge that has never wedged:")
import threading, time
from comms.bridge import Bridge

BUF = io.StringIO()
_h = logging.StreamHandler(BUF); _h.setFormatter(logging.Formatter("%(message)s"))
_lg = logging.getLogger("comms.bridge")
_saved = (_lg.handlers, _lg.level, _lg.propagate)
_lg.handlers, _lg.propagate = [_h], False
_lg.setLevel(logging.INFO)


# Driven through the real handler with a real packet, because the whole point
# is what the operator reads in the log.
import struct
from comms.protocol import Cmd


class _Pkt:
    cmd = Cmd.HEALTH
    mount_id = 1
    def __init__(self, payload): self.payload = payload


def health_payload(n32, node_type=1):
    # PayloadHealth: node_type, reset_reason, uptime, heap, min_heap,
    # loop_max, tx_fail, rssi, flags, node_u32
    return struct.pack(">BBIIIHHbBI", node_type, 1, 36000,
                       8400000, 8390000, 9, 6, -33, 0, n32)


def health_line(n32):
    b = Bridge.__new__(Bridge)
    b._diag_lock = threading.Lock()
    # Everything this path touches, set once rather than discovered one
    # AttributeError at a time. _node_state_path None keeps it off the disk.
    b._node_uptime = {}
    b._sat_names = {}
    b._cam_ble = {}
    b._node_state_cur = {}
    b._node_state_prev = {}
    b._node_state_written = 0.0
    b._node_state_path = None
    BUF.truncate(0); BUF.seek(0)
    b._note_node_health(_Pkt(health_payload(n32)))
    return BUF.getvalue().strip()


clean = health_line(1)                   # reinit 1, never wedged
assert clean, "the health line did not log at all"
assert "WEDGES" not in clean, \
    f"a bridge that has never wedged still says so: {clean}"
assert "n32 1" in clean, f"the reinit count moved: {clean}"

worn = health_line((3 << 16) | 1)        # three wedges, reinit 1
assert "WEDGES 3" in worn, f"three wedges do not read as three: {worn}"
assert "n32 1" in worn, \
    f"the reinit count changed when the high half did — the halves are not\n" \
    f"    independent: {worn}"
print(f"   {clean.split(chr(124))[-1].strip()}  ->  "
      f"{worn.split(chr(124))[-2].strip()}   OK")

_lg.handlers, _lg.level, _lg.propagate = _saved
print("\nALL CHECKS PASSED")
