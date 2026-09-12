"""The hub reports how many stations are on its access point.

An access point holds frames for an associated station that goes to sleep, out
of the same buffer pool the ESP-NOW sends draw on. The hub transmits from an AP
(WiFi.softAP, peers on WIFI_IF_AP) while every bench measurement to date was
taken station-to-station — which is the one structural difference between the
instrument and the thing it models, and it is under test on the bench now.

This is what lets the rig's own logs be read back against that result, either
way it lands.

WHY IT RIDES IN THE RSSI FIELD, which is not tidy:

PayloadHealth is a fixed 24 bytes and uniform across every node type, so adding
a field means reflashing the mounts and the satellites. The reason this is
wanted at all is that those cannot be reached until Tuesday, so a wire-format
change is exactly the thing it must not need. rssi is documented as "last link
RSSI where meaningful, else 0", the hub has always sent 0 into it, and the only
consumer anywhere is the app's own print. node_u32 has no room: sends held
back, ring overflows, leak floor and ghost drops take a byte each.

So the field carries a hub-only meaning, the app prints it under that meaning,
and every other node is untouched. That last part is what this test is for —
a field that means two things is one edit away from meaning the wrong one.

Run directly, or via tools/run_tests.sh with the rest.
"""
import io
import logging
import os
import pathlib
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

INO = (REPO / "firmware/esp32_hub_eth/esp32_hub_eth.ino").read_text()

# ---- 1. the hub actually reads the AP ---------------------------------------
print("1. the hub's side:")
assert re.search(r"WiFi\.softAPgetStationNum\(\)", INO), \
    "the hub never asks the AP how many stations are on it, so the field would\n" \
    "    carry a constant and read as 'never any station attached'"
flat = re.sub(r"\s+", " ", INO)
assert re.search(r"h\.rssi\s*=\s*\(int8_t\)\(\s*ap_sta\s*>\s*127", flat), \
    "the count is not clamped before the int8_t store. Above 127 it goes\n" \
    "    NEGATIVE, and a hub with a busy AP would report -128 stations"
assert "h.rssi          = 0;" not in INO, \
    "the old constant zero is still assigned somewhere — whichever runs last\n" \
    "    wins, and the field silently goes back to meaning nothing"
print("   reads softAPgetStationNum, clamped before store        OK")

# ---- 2. no other node's rssi is disturbed -----------------------------------
# The mount's RSSI is real and is read against txfail to tell a link problem
# from a radio problem. If this ever starts printing "stations" for a mount,
# that comparison silently stops being possible.
print("\n2. every other node keeps its RSSI:")
from comms.bridge import Bridge
import comms.bridge as bridgemod

BUF = io.StringIO()
_h = logging.StreamHandler(BUF)
_h.setFormatter(logging.Formatter("%(message)s"))
_lg = logging.getLogger("comms.bridge")
_saved = (_lg.handlers, _lg.level, _lg.propagate)
_lg.handlers, _lg.propagate = [_h], False
_lg.setLevel(logging.INFO)

src = (REPO / "pc_app/comms/bridge.py").read_text()
m = re.search(r'rssi_txt\s*=\s*\((.*?)\)\s*if\s+who\s*==\s*"hub"\s*else\s*\((.*?)\)', src)
assert m, \
    "the health line no longer chooses its label by node. Either every node\n" \
    "    reads 'stations' — wrong for a mount — or the hub is back to 'rssi 0'"
assert "stations" in m.group(1) and "rssi" in m.group(2), \
    f"the two branches are the wrong way round: hub={m.group(1)}, other={m.group(2)}"
print("   hub prints stations, everything else prints rssi       OK")

# And the format string must take the label, not a bare number, or the two
# branches are computed and thrown away.
assert re.search(r"txfail %d \| %s \| %s \| reset", src), \
    "the line still formats rssi with %d, so rssi_txt is dead code and the hub\n" \
    "    prints a raw number under the wrong name"
print("   the label reaches the format string                    OK")

_lg.handlers, _lg.level, _lg.propagate = _saved

# ---- 3. it degrades honestly on an older hub --------------------------------
# A hub flashed before this sends 0, which prints "stations 0" — true of it as
# well, since it had no way to have any. Nothing can distinguish "no stations"
# from "firmware cannot count them", so nothing may claim to.
print("\n3. an older hub:")
assert "predates" in src[src.index("rssi_txt") - 900:src.index("rssi_txt")], \
    "the app does not record what an older hub's 0 means, so the first person\n" \
    "    to read 'stations 0' off a stale hub will take it as a measurement"
print("   0 from a stale hub is not claimed as a measurement     OK")

print("\nALL CHECKS PASSED")
