"""A satellite's address must be findable without a laptop and a ladder.

A satellite takes its wired address from DHCP and prints the lease it was given
at boot — to its own USB serial. That is fine on a bench and useless once the
unit is rigged, which is the only time anyone needs it.

The hub already knew. Every satellite dials in over TCP, so the hub has the
address the connection came FROM, and was printing it to ITS serial too:

    [SAT] satellite 2 connected from 192.168.1.58

So nothing has to be asked of a satellite and none has to be reflashed to
become findable — the hub just has to say what it already knows, somewhere the
operator is already looking. It goes out with the satellite NAMES, in the same
packet, appended after them.

Appended rather than folded in, because both payload lengths have to keep
working: the hub sends the long form and a client accepts either, so a hub and
a PC app updated at different times still talk. The same trick as the 75/77
byte CONFIG_REPORT that carries the slider tilt.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

HUB = (REPO / "firmware/esp32_hub_eth/esp32_hub_eth.ino").read_text()
PROTO = (REPO / "firmware/shared/protocol.h").read_text()
MM = (REPO / "pc_app/comms/mount_manager.py").read_text()

from comms.protocol import (decode_sat_names, decode_sat_ips,
                            SAT_NAMES_PAYLOAD_LEN, SAT_TABLE_PAYLOAD_LEN,
                            SAT_SLOTS, SAT_NAME_LEN)

# ---- 1. the hub takes it from the connection, not from the satellite -------
print("1. where the address comes from:")
assert "_sat[i].ip     = (uint32_t)in.remoteIP();" in HUB, \
    "the hub no longer records the address a satellite connected from — it\n" \
    "    would have to ask the satellite for it, which means reflashing every\n" \
    "    satellite to make one findable"
assert "_sat[i].ip = 0;" in HUB, \
    "a disconnected slot keeps its old address and would be reported as live"
print("   taken from the TCP connection, cleared on disconnect   OK")

# ---- 2. both payload lengths stay meaningful -------------------------------
print("\n2. talking to a hub or app of a different vintage:")
assert SAT_TABLE_PAYLOAD_LEN == SAT_NAMES_PAYLOAD_LEN + SAT_SLOTS * 4, \
    "the addresses are not a plain 4-byte block per slot after the names"
assert "#define SAT_NAMES_PAYLOAD_LEN" in PROTO and \
       "#define SAT_TABLE_PAYLOAD_LEN" in PROTO, \
    "one of the two lengths is gone; both have to stay meaningful or a hub and\n" \
    "    an app updated at different times stop understanding each other"

short = bytearray(SAT_NAMES_PAYLOAD_LEN)          # a hub from before this
short[0:5] = b"Foyer"
assert decode_sat_names(bytes(short)) == {1: "Foyer"}, \
    "a short payload no longer yields its names — the old hub just broke"
assert decode_sat_ips(bytes(short)) == {}, \
    "a short payload yields addresses it does not contain"
print("   old hub  -> names parse, no addresses claimed          OK")

full = bytearray(SAT_TABLE_PAYLOAD_LEN)
full[0:5] = b"Foyer"
full[SAT_NAME_LEN:SAT_NAME_LEN + 5] = b"Stage"
full[SAT_NAMES_PAYLOAD_LEN:SAT_NAMES_PAYLOAD_LEN + 4] = bytes([192, 168, 1, 57])
full[SAT_NAMES_PAYLOAD_LEN + 4:SAT_NAMES_PAYLOAD_LEN + 8] = bytes([192, 168, 1, 58])
assert decode_sat_names(bytes(full)) == {1: "Foyer", 2: "Stage"}
assert decode_sat_ips(bytes(full)) == {1: "192.168.1.57", 2: "192.168.1.58"}, \
    "the addresses do not decode to dotted quads in slot order"
print("   new hub  -> both, in slot order                        OK")

# An empty slot reads 0.0.0.0 and must be left out, not reported as an address.
empty = bytearray(SAT_TABLE_PAYLOAD_LEN)
empty[0:5] = b"Foyer"
assert decode_sat_ips(bytes(empty)) == {}, \
    "an unoccupied slot is reported as being at 0.0.0.0"
print("   unoccupied slot -> not reported at all                 OK")

# ---- 3. the hub sends the long form ----------------------------------------
print("\n3. what the hub actually sends:")
send = HUB[HUB.index("static void send_sat_names()"):]
send = send[:send.index("\n}")]
assert "uint8_t buf[SAT_TABLE_PAYLOAD_LEN]" in send, \
    "the hub still sends the names-only payload; nothing would ever carry an\n" \
    "    address even though it has one"
assert send.index("SAT_NAME_LEN") < send.index("SAT_NAMES_PAYLOAD_LEN + i * SAT_IP_LEN"), \
    "the addresses are written before the names, so an older client reading the\n" \
    "    first block would read addresses as text"
print("   the long form, names first                             OK")

# ---- 4. the log line -------------------------------------------------------
print("\n4. what reaches the log:")
import logging
from comms import mount_manager as _mm

class _Grab(logging.Handler):
    def __init__(self): super().__init__(); self.lines = []
    def emit(self, r): self.lines.append(r.getMessage())

mm = _mm.MountManager.__new__(_mm.MountManager)
mm._sat_table = {}
grab = _Grab()
_mm.log.addHandler(grab)
# The line is log.INFO, and the module logger inherits WARNING from an
# unconfigured root — so without this the handler sees nothing and the test
# passes by capturing an empty list.
_prev_level = _mm.log.level
_mm.log.setLevel(logging.INFO)
try:
    mm._log_satellites({1: "Foyer", 2: "Stage"},
                       {1: "192.168.1.57", 2: "192.168.1.58"})
    mm._log_satellites({1: "Foyer", 2: "Stage"},
                       {1: "192.168.1.57", 2: "192.168.1.58"})   # unchanged
    mm._log_satellites({1: "Foyer"}, {1: "192.168.1.57"})        # one left
    mm._log_satellites({1: "Foyer"}, {})                         # older hub
    mm._log_satellites({}, {3: "192.168.1.61"})                  # unnamed
    mm._log_satellites({}, {})                                   # all gone
finally:
    _mm.log.removeHandler(grab)
    _mm.log.setLevel(_prev_level)

lines = [l for l in grab.lines if l.startswith("SATELLITES")]
for l in lines:
    print("   " + l)

assert "Foyer 192.168.1.57" in lines[0] and "Stage 192.168.1.58" in lines[0], \
    "the first line does not pair each name with its address"
assert len(lines) == 5, \
    f"expected 5 lines from 6 calls — the repeat must be silent — got {len(lines)}"
print("   an unchanged table logs nothing                        OK")

assert "address unknown" in lines[2], \
    "a satellite whose hub is too old to report an address is shown as though\n" \
    "    it had one, or silently dropped; say which part is missing"
assert "SAT 3" in lines[3], \
    "an unnamed satellite loses its slot number, so a findable address is\n" \
    "    attached to nothing the operator can identify"
assert "none connected" in lines[4], \
    "the satellites going away is indistinguishable from nothing having changed"
print("   unknown address, unnamed slot and 'none' all covered   OK")

# It must be its own line, not folded into the route: a satellite serving no
# mounts appears in no route, and that is the one someone is hunting for.
assert "_log_satellites" in MM and "def _log_route" in MM, \
    "the satellite table has been folded into the route line; a satellite with\n" \
    "    no mounts on it would then never be reported at all"
print("   separate from the route line                           OK")

print("\nALL CHECKS PASSED")
