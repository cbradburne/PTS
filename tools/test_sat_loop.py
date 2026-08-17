"""Satellite loop reporting: windowed loopmax, worst section, stall -> WARNING.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))
import re, struct, logging, io

from comms.bridge import Bridge
from comms.protocol import Cmd, HEALTH_NODE_SATELLITE, HEALTH_FLAG_ANOMALY

INO = pathlib.Path(REPO / "firmware/esp32_satellite/"
                   "esp32_satellite.ino").read_text()

# ---- 1. the PC app's section table IS the firmware enum order --------------
# That order is the wire encoding: reordering the enum silently renames every
# section in the log.  Read it from the .ino so the two cannot drift.
m = re.search(r"static const char \*const SSEC_NAME\[SSEC_N\] = \{(.*?)\};", INO, re.S)
assert m, "SSEC_NAME not found in esp32_satellite.ino"
fw = [t.strip().strip('"') for t in m.group(1).replace("\n", " ").split(",") if t.strip()]
app = Bridge._SAT_SECTION_NAMES
print("1. section table vs firmware enum:")
assert len(app) == len(fw), (len(app), len(fw))
for i, name in enumerate(fw):
    assert app.get(i) == name, (i, name, app.get(i))
print(f"   {len(fw)} sections, order and names identical      OK")

# The loop must actually be marked, or every section reads zero and the worst
# is always section 0.  One mark per section, plus the macro definition.
assert INO.count("SMARK(") >= len(fw) + 1, INO.count("SMARK(")
print(f"   loop carries {INO.count('SMARK(') - 1} marks              OK")

# ---- 2. the window must be CLEARED, or the number is unreadable ------------
# This is the bug the whole change exists to fix: loop_max_ms is documented as
# "since the last report" and the satellite kept it since boot, so Foyer read
# 16238ms at 5 minutes and 16570ms four hours later and no one could tell a
# fresh stall from a stuck mark.
assert "_sat_loopmax_ms = 0;" in INO, "satellite never clears its loop max"
assert re.search(r"for \(int i = 0; i < SSEC_N; i\+\+\) _ssec_max\[i\] = 0;", INO), \
    "satellite never clears its section maxima"
print("2. loopmax and sections are cleared each report   OK")

# ---- 3. encode/decode round-trip ------------------------------------------
def sat_health(uptime_s, loopmax_ms, nomem, sect, streak, first=False):
    """Exactly what the satellite's sat_send_health() puts on the wire."""
    flags = HEALTH_FLAG_ANOMALY if (first or streak or loopmax_ms >= 500) else 0
    n32 = ((nomem & 0xFFFF) << 16) | ((sect & 0xFF) << 8) | (streak & 0xFF)
    return (bytes([HEALTH_NODE_SATELLITE, 1])
            + struct.pack(">III", uptime_s, 193 * 1024, 166 * 1024)
            + struct.pack(">HH", loopmax_ms, 396)
            + bytes([0, flags])
            + struct.pack(">I", n32))

class _P:
    def __init__(self, payload):
        self.cmd, self.payload, self.mount_id = Cmd.HEALTH, payload, 0

_buf = io.StringIO()
_h = logging.StreamHandler(_buf); _h.setFormatter(logging.Formatter("%(levelname)s|%(message)s"))
_lg = logging.getLogger("comms.bridge"); _lg.handlers = [_h]
_lg.setLevel(logging.INFO); _lg.propagate = False

_b = Bridge.__new__(Bridge)
_b._sat_names, _b._node_uptime, _b._node_state_prev, _b._cam_ble = {0: "Foyer"}, {}, {}, {}
_b._node_state_save = lambda *a, **k: None

def render(rec):
    _buf.truncate(0); _buf.seek(0)
    _b._note_node_health(_P(rec))
    lvl, _, msg = _buf.getvalue().strip().partition("|")
    return lvl, msg

print("3. rendering:")
# A quiet window: the winning section is whichever took 3ms instead of 2 —
# noise dressed as a finding, so it must NOT be printed.
lvl, msg = render(sat_health(14976, 3, 396, 5, 0))
assert lvl == "INFO" and "worst section" not in msg, msg
print("   3ms     -> INFO, no section (noise suppressed)  OK")

lvl, msg = render(sat_health(14976, 120, 396, 2, 0))
assert lvl == "INFO" and "worst section 'espnow_up'" in msg, msg
print("   120ms   -> INFO, section named                  OK")

# The real one.  16.5 seconds must not be an INFO line among four hundred.
lvl, msg = render(sat_health(14976, 16570, 396, 2, 0))
assert lvl == "WARNING", (lvl, msg)
assert "worst section 'espnow_up'" in msg and "[ANOMALY]" in msg, msg
assert "loopmax 16570ms" in msg, msg
print("   16570ms -> WARNING [ANOMALY] + section          OK")

# Every section must survive the round trip, including the last.
for i, name in enumerate(fw):
    lvl, msg = render(sat_health(14976, 16570, 0, i, 0))
    assert f"worst section '{name}'" in msg, (i, name, msg)
print(f"   all {len(fw)} sections decode from the wire         OK")

# ---- 4. the repack must not have cost the other two fields -----------------
lvl, msg = render(sat_health(14976, 600, 65535, 10, 3))
assert "nomem 65535" in msg, msg
assert "SELF-RESTARTS 3" in msg, msg
assert "worst section 'reports'" in msg, msg
print("4. nomem + streak + section coexist in node_u32   OK")

cap = int(re.search(r"#define SAT_RESTART_MAX_STREAK (\d+)", INO).group(1))
assert cap <= 0xFF, cap
print(f"   streak caps at {cap}, fits the 8 bits it now has   OK")

thresh = int(re.search(r"#define SAT_LOOP_STALL_MS\s+(\d+)", INO).group(1))
assert thresh >= 100, thresh
lvl, _ = render(sat_health(14976, thresh - 1, 0, 0, 0))
assert lvl == "INFO", "just under the threshold should stay INFO"
lvl, _ = render(sat_health(14976, thresh, 0, 0, 0))
assert lvl == "WARNING", "at the threshold should warn"
print(f"   threshold {thresh}ms: below INFO, at/above WARNING  OK")

print("\nALL CHECKS PASSED")
