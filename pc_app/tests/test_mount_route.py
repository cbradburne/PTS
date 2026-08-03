"""CMD_MOUNT_ROUTE: which path the hub reaches each mount by.

Guards the half of the picture that RSSI alone cannot give.  Signal strength is
measured wherever the frame actually arrived, so once satellites are relaying,
a mount reads -40 because a satellite is beside it and -85 the moment that
satellite drops and it falls back to the hub — the same mount, unmoved, with
nothing else on screen to explain the change.

Covers the wire decode, the manager's state and signal, and the label in the
Config dialog, because a badge that silently stops updating looks exactly like
a mount that never roams.
"""
import sys, os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, ".")
from PyQt6.QtWidgets import QApplication
from config.mount_config import AppConfig
from comms.bridge import Bridge
from comms.mount_manager import MountManager
from comms.protocol import (Cmd, build_packet, decode_mount_route, parse_packet,
                            MOUNT_ROUTE_PAYLOAD_LEN, NUM_MOUNTS, ParseError)
from ui.dialogs.config_dialog import ConfigDialog

app = QApplication(sys.argv)
mm  = MountManager(Bridge())
cfg = AppConfig()
dlg = ConfigDialog(cfg, mm, Bridge()); dlg.show(); app.processEvents()

fails = []
def chk(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label:46s} got={got!r} want={want!r}")

def feed(routes):
    """Hand the manager a CMD_MOUNT_ROUTE exactly as the hub would send it."""
    pkt = build_packet(0xFE, Cmd.MOUNT_ROUTE, bytes(routes))
    mm._on_packet(parse_packet(pkt))
    app.processEvents()

print("1. Decoder")
chk("all direct",      decode_mount_route(bytes([0, 0, 0, 0, 0])), [0, 0, 0, 0, 0])
chk("mixed routes",    decode_mount_route(bytes([0, 2, 0, 1, 0])), [0, 2, 0, 1, 0])
try:
    decode_mount_route(bytes([0, 0]))
    chk("short payload rejected", False, True)
except ParseError:
    chk("short payload rejected", True, True)

print("\n2. Manager state and signal")
seen = []
mm.mount_route_updated.connect(lambda r: seen.append(list(r)))
chk("defaults to all-direct", mm.mount_route, [0] * NUM_MOUNTS)
feed([0, 0, 0, 2, 0])
chk("cam 4 via satellite 2", mm.mount_route, [0, 0, 0, 2, 0])
chk("signal emitted",        seen[-1] if seen else None, [0, 0, 0, 2, 0])

n_before = len(seen)
feed([0, 0, 0, 2, 0])
chk("unchanged route is not re-emitted", len(seen), n_before)

print("\n3. Roaming back to the hub clears the badge")
# The bug this guards against lived in the hub: the route was only ever ADDED,
# never removed, so a mount that returned to the hub's own radio kept a stale
# satellite attribution — and _mount_sat[] also decides where commands are SENT.
feed([0, 0, 0, 0, 0])
chk("cam 4 back to direct", mm.mount_route, [0, 0, 0, 0, 0])

print("\n4. Config dialog labels")
feed([0, 3, 0, 2, 0])
chk("cam 1 direct -> blank",   dlg._pair_via_lbls[1].text(), "")
chk("cam 2 -> via SAT 3",      dlg._pair_via_lbls[2].text(), "via SAT 3")
chk("cam 4 -> via SAT 2",      dlg._pair_via_lbls[4].text(), "via SAT 2")
feed([0, 0, 0, 0, 0])
chk("cam 2 cleared on return", dlg._pair_via_lbls[2].text(), "")
chk("cam 4 cleared on return", dlg._pair_via_lbls[4].text(), "")

print("\n5. Payload length agrees with the firmware constant")
chk("MOUNT_ROUTE_PAYLOAD_LEN", MOUNT_ROUTE_PAYLOAD_LEN, NUM_MOUNTS)

print("\nRESULT: " + ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
