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
                            decode_sat_names, MOUNT_ROUTE_PAYLOAD_LEN,
                            SAT_NAMES_PAYLOAD_LEN, SAT_NAME_LEN, SAT_SLOTS,
                            NUM_MOUNTS, ParseError)
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

print("\n5. Satellite names replace the slot number")
# The slot is TCP accept order and means nothing to anyone in the building.
def feed_names(names: dict):
    """names: {slot (1-based): text} -> push a CMD_SAT_NAMES frame."""
    buf = bytearray(SAT_NAMES_PAYLOAD_LEN)
    for slot, txt in names.items():
        raw = txt.encode()[:SAT_NAME_LEN - 1]
        buf[(slot - 1) * SAT_NAME_LEN:(slot - 1) * SAT_NAME_LEN + len(raw)] = raw
    mm._on_packet(parse_packet(build_packet(0xFE, Cmd.SAT_NAMES, bytes(buf))))
    app.processEvents()

chk("decode skips unnamed slots", decode_sat_names(bytes(SAT_NAMES_PAYLOAD_LEN)), {})
feed_names({2: "Foyer", 3: "Balcony"})
chk("slot 2 named",   mm.sat_label(2), "Foyer")
chk("slot 3 named",   mm.sat_label(3), "Balcony")
# The fallback is not cosmetic: a satellite on firmware from before names
# existed never introduces itself, and a blank label would read as a broken
# route rather than an out-of-date box.
chk("unnamed slot falls back", mm.sat_label(5), "SAT 5")

feed([0, 3, 0, 2, 0])
chk("cam 2 -> via Balcony", dlg._pair_via_lbls[2].text(), "via Balcony")
chk("cam 4 -> via Foyer",   dlg._pair_via_lbls[4].text(), "via Foyer")

# Names can arrive AFTER the route, so a late CMD_SAT_NAMES must relabel rows
# already on screen rather than wait for the next roam.
feed_names({2: "Foyer", 3: "Circle"})
chk("late rename redraws", dlg._pair_via_lbls[2].text(), "via Circle")
# ...and a satellite that drops clears its name, so the row must not keep
# showing a room that is no longer relaying anything.
feed_names({})
chk("name cleared -> number", dlg._pair_via_lbls[2].text(), "via SAT 3")

print("\n6. Payload lengths agree with the firmware constants")
chk("MOUNT_ROUTE_PAYLOAD_LEN", MOUNT_ROUTE_PAYLOAD_LEN, NUM_MOUNTS)
chk("SAT_NAMES_PAYLOAD_LEN",   SAT_NAMES_PAYLOAD_LEN, SAT_SLOTS * SAT_NAME_LEN)

print("\nRESULT: " + ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
