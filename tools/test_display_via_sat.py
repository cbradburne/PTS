"""The hub display says which satellite a mount is reached through.

A mount attaches to whichever satellite it hears best, in its own scan, and the
hub learns the route by seeing whose uplink the traffic arrives on. Every
network client has been able to show that; the two screens on the hub's own
display could not, and read "-67 dBm" whether the mount was on the hub's radio
or three rooms away through a relay.

Two surfaces show it: the mount cell on the main page and the PAIRED MOUNTS
panel. Both need the route AND the name, which arrive by different routes — the
route rides on every status, the names come when they change.

Run directly, or via tools/run_tests.sh with the rest.
"""
import pathlib, re

REPO = pathlib.Path(__file__).resolve().parent.parent
HUB  = (REPO / "firmware/esp32_hub_eth/esp32_hub_eth.ino").read_text()
DISP = (REPO / "firmware/esp32_display/hub_display.cpp").read_text()
INO  = (REPO / "firmware/esp32_display/esp32_display.ino").read_text()
UART = (REPO / "firmware/shared/disp_uart.h").read_text()
PROT = (REPO / "firmware/shared/protocol.h").read_text()

# ---- 1. the payload fits the link ------------------------------------------
# 6 slots x 13 bytes = 78 against a 80-byte cap. Either constant growing by one
# silently truncates every name, so the two are checked against each other
# rather than trusted.
print("1. the names fit the UART:")
slots = int(re.search(r"#define SAT_SLOTS\s+(\d+)", PROT).group(1))
nlen  = int(re.search(r"#define SAT_NAME_LEN\s+(\d+)", PROT).group(1))
cap   = int(re.search(r"#define DISP_UART_MAX_PAYLOAD\s+(\d+)", UART).group(1))
need  = slots * nlen
assert need <= cap, \
    f"SAT_SLOTS({slots}) x SAT_NAME_LEN({nlen}) = {need} bytes will not fit " \
    f"DISP_UART_MAX_PAYLOAD ({cap}) — names would be silently truncated"
print(f"   {slots} x {nlen} = {need} bytes into {cap}                        OK")

# ---- 2. the hub sends both halves ------------------------------------------
print("\n2. what the hub sends:")
d = HUB[HUB.index("static void disp_update_cam("):]
d = d[:d.index("\n}")]
assert "uint8_t buf[6]" in d, \
    "UPDATE_CAM is not six bytes, so the route never leaves the hub"
assert "_mount_sat[mount_id - 1] + 1" in d, \
    "the satellite slot is not sent as slot+1, so slot 0 is indistinguishable\n" \
    "    from 'on the hub's own radio'"
print("   route on every UPDATE_CAM, as slot+1               OK")

# Inside send_sat_names(), not at its call sites: there are three of those and a
# fourth would have been added without it.
sn = HUB[HUB.index("static void send_sat_names()"):]
sn = sn[:sn.index("\n}")]
assert "disp_send_sat_names();" in sn, \
    "the display push is at the call sites rather than inside send_sat_names(),\n" \
    "    so a new caller would leave the screen showing slot numbers while every\n" \
    "    other client showed names"
assert HUB.count("send_sat_names();") >= 3, "the sat-name callers changed"
print("   names pushed from inside send_sat_names()          OK")

# The one moment the display has no names at all is its own boot.
gm = HUB[HUB.index("if (type == DISP_MSG_GET_MOUNT_TABLE) {"):]
gm = gm[:gm.index("return;")]
assert "disp_send_sat_names();" in gm, \
    "the display's boot request gets the mount table but not the names, so a\n" \
    "    screen that came up after the satellites shows SAT n until one of them\n" \
    "    happens to reconnect"
print("   and on the display's boot request                  OK")

# ---- 3. the display reads them, old hub or new -----------------------------
print("\n3. what the display does with them:")
disp = INO[INO.index("case DISP_MSG_UPDATE_CAM:"):]
disp = disp[:disp.index("case DISP_MSG_SAT_NAMES")]
assert "(len >= 6) ? d[5] : 0" in disp, \
    "the sixth byte is read without a length guard, so a hub from before this\n" \
    "    would feed the display whatever follows the payload"
assert "DISP_MSG_SAT_NAMES" in INO, "the names message is never dispatched"
print("   sixth byte optional, names dispatched              OK")

# ---- 4. both surfaces show it ----------------------------------------------
print("\n4. both screens:")
tile = DISP[DISP.index("    if (rssi_changed) {"):]
tile = tile[:tile.index("lv_label_set_text(_tile_rssi[i], rssi_buf);")]
assert "sat_suffix" in tile, "the mount cell does not show the route"
panel = DISP[DISP.index("static void mounts_panel_refresh()"):]
panel = panel[:panel.index("\n}")]
assert "sat_suffix" in panel, "the PAIRED MOUNTS panel does not show the route"
print("   mount cell and PAIRED MOUNTS panel both            OK")

# A mount roaming between satellites changes the line without changing the dBm,
# and the tile only redraws on a change — so the gate has to include the route.
gate = DISP[DISP.index("bool rssi_changed  ="):]
gate = gate[:gate.index(";")]
assert "old_sat" in gate, \
    "the RSSI line is redrawn only when the signal number moves, so a mount\n" \
    "    roaming to another satellite would keep the old name on screen until\n" \
    "    its dBm happened to change"
print("   redrawn when the route changes, not just the dBm   OK")

# ---- 5. a mount on the hub's own radio says nothing extra -------------------
print("\n5. the quiet case:")
ss = DISP[DISP.index("static void sat_suffix("):]
ss = ss[:ss.index("\n}")]
assert "if (!sat || sat > SAT_SLOTS) { out[0] = 0; return; }" in ss, \
    "a mount on the hub's own radio would get a suffix, or an out-of-range slot\n" \
    "    would index past the name table"
assert 'via SAT %u' in ss, \
    "a satellite that never introduced itself leaves the cell with no name at\n" \
    "    all rather than its slot number"
print("   sat 0 adds nothing; an unnamed slot reads SAT n    OK")

# The tile is ~128px at montserrat_10 and the panel is 620px, so they clip
# differently. If both passed the same length the tile would overflow.
tile_call = re.search(r"sat_suffix\(sat, via, sizeof\(via\), (\d+)\)", DISP)
panel_call = re.search(r"sat_suffix\(_cam\[i\]\.sat, via, sizeof\(via\), ([^)]+)\)", DISP)
assert tile_call and panel_call, "could not find both sat_suffix call sites"
assert int(tile_call.group(1)) < nlen - 1, \
    f"the tile passes the full name length ({tile_call.group(1)}); at " \
    f"montserrat_10 a 12-character name overflows a 128px cell"
print(f"   tile clips at {tile_call.group(1)}, panel at {panel_call.group(1).strip()}          OK")

print("\nALL CHECKS PASSED")
