"""The rail's inclination reaches every consumer, and actually steers the maths.

Mount 5's slider is rigged at 21 degrees. The triangulation assumed a level
rail: the two viewpoints it solved from were placed at (xa,0,0) and (xb,0,0),
so the camera it computed from was never where the camera actually was. Over a
1200 mm rail at 21 degrees the far end is 430 mm higher than the model believed
— which is why a subject could not be locked.

Two separate things have to hold, and the second is the one worth testing:

  The number has to survive the trip. It is written by the mount into
  CONFIG_REPORT and read by four consumers — PC app, web app, hub display, and
  the mount's own EEPROM. A byte offset agreed in five places drifts silently;
  nothing here would throw, the tilt would just read as some other number.

  The number has to be USED. A tilt that is stored, reported, and displayed
  everywhere but never reaches the triangulation looks completely healthy from
  the outside and fixes nothing.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re, math
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

PROTO_H  = (REPO / "firmware/shared/protocol.h").read_text()
INO      = (REPO / "firmware/teensy41_mount/teensy41_mount.ino").read_text()
WEB      = (REPO / "firmware/esp32_hub/web_app.h").read_text()
DISPLAY  = (REPO / "firmware/esp32_display/hub_display.cpp").read_text()

# ---- 1. one agreed length, and the tilt is inside it -------------------------
print("1. the wire format:")
m = re.search(r"#define\s+CONFIG_REPORT_PAYLOAD_LEN\s+(\d+)", PROTO_H)
assert m, "CONFIG_REPORT_PAYLOAD_LEN is gone"
PLEN = int(m.group(1))
assert PLEN == 77, f"payload is {PLEN}B; the tilt needs 77 (75 of config + int16)"
print(f"   CONFIG_REPORT_PAYLOAD_LEN = {PLEN}                    OK")

assert "payload[75] = (uint8_t)((uint16_t)t10 >> 8);" in INO, \
    "the mount no longer writes the tilt high byte at [75]"
assert "payload[76] = (uint8_t)((uint16_t)t10 & 0xFF);" in INO, \
    "the mount no longer writes the tilt low byte at [76]"
assert "lroundf(_cfg.slider_tilt_deg * 10.0f)" in INO, \
    "the mount no longer sends tenths of a degree"
print("   mount writes int16 tenths, big-endian, at [75..76]  OK")

# The hub relays this buffer. It used to size it with a literal 75, which would
# have chopped the tilt off in transit while everything still looked fine.
HUB = (REPO / "firmware/esp32_hub_eth/esp32_hub_eth.ino").read_text()
i = HUB.index("disp_config_report")
seg = HUB[i:i + 1200]
assert re.search(r"uint8_t\s+buf\[1 \+ CONFIG_REPORT_PAYLOAD_LEN\]", seg), \
    "the hub relay buffer is hardcoded again — it will truncate the tilt"
assert "sizeof(buf)" in seg, "the hub relay sends a hardcoded length again"
print("   hub relays the whole payload, no literals           OK")

# ---- 2. every consumer reads the same two bytes ------------------------------
print("\n2. the four consumers:")

from comms.protocol import decode_config_report, encode_set_orientation
import struct


def report(tilt_deg=None):
    """A CONFIG_REPORT body; tilt_deg=None makes an old 75-byte one."""
    body = bytearray(75)
    if tilt_deg is None:
        return bytes(body)
    body += struct.pack(">h", round(tilt_deg * 10))
    return bytes(body)


for want in (21.0, -21.0, 0.0, 9.9, -0.1):
    got = decode_config_report(report(want)).slider_tilt_deg
    assert abs(got - want) < 0.05, f"PC app decoded {want} as {got}"
print("   PC app  decodes positive and negative               OK")

# An un-updated mount must not read as a level rail by accident... but the PC
# app has to put SOMETHING in a float. It uses 0.0 and that is defensible only
# because the field is also what a level rail reports. Assert the old-length
# path at least does not throw or misread the last config byte as tilt.
old = decode_config_report(report(None))
assert old.slider_tilt_deg == 0.0, "a 75-byte report no longer decodes to 0.0"
print("   PC app  tolerates an old 75-byte report             OK")

# Encoding back the other way: flags byte then int16 tenths.
enc = encode_set_orientation(False, False, has_slider=True, slider_tilt_deg=-21.0)
assert len(enc) == 3, f"SET_ORIENTATION is {len(enc)}B, expected 3"
assert struct.unpack(">h", enc[1:3])[0] == -210, "the tilt is not int16 tenths"
print("   PC app  encodes it back to the mount                OK")

# Web app — same offsets, and it must sign-extend. A missing sign-extend is the
# classic JS bug here: 21 degrees would read fine and -21 would read as 6528.
w = WEB[WEB.index("if (plen >= 77)"):]
w = w[:w.index("}")]
assert "buf[base + 75]" in w and "buf[base + 76]" in w, "web app reads other bytes"
assert "0x8000" in w and "0x10000" in w, "web app does not sign-extend the tilt"
assert "/ 10" in w, "web app does not scale tenths to degrees"
print("   web app reads [75..76], sign-extended               OK")

# and it must be guarded, or an old mount's 75-byte report reads two stray bytes
assert "plen >= 77" in WEB, "the web app tilt read is not length-guarded"
print("   web app is guarded against a 75-byte report         OK")

# Hub display — same again.
d = DISPLAY[DISPLAY.index("payload_len >= 77"):]
d = d[:d.index("} else")]
assert "payload[75]" in d and "payload[76]" in d, "display reads other bytes"
assert "(int16_t)" in d, "display does not treat the tilt as signed"
print("   display reads [75..76], signed                      OK")
assert 'lv_label_set_text(_cfg_lbl_tilt, "--")' in DISPLAY, \
    "display invents a value when the mount does not report one"
print("   display shows -- rather than a made-up 0.0          OK")

# ---- 3. an Apply from the display must not wipe the tilt --------------------
# The display's config screen sends CMD_SET_ORIENTATION with the flags byte
# ALONE. If the mount treated a short packet as "tilt = 0", pressing Apply on
# the display would silently level the rail and break tracking again.
print("\n3. a short SET_ORIENTATION preserves the stored tilt:")
seg = INO[INO.index("case CMD_SET_ORIENTATION"):]
# to the NEXT case label — the handler's own early `break` on a zero-length
# packet is only a few lines in, and slicing at it would hide the whole body
seg = seg[:re.search(r"\n\s*case CMD_", seg[10:]).end() + 10]
assert re.search(r"if \(len >= 3\) \{", seg), \
    "the mount no longer guards the optional tilt — a 1-byte Apply may wipe it"
assert not re.search(r"else\s*\{?\s*_cfg\.slider_tilt_deg\s*=\s*0", seg), \
    "the mount zeroes the tilt when the sender omits it"
assert "_send_cb(mount_id, CMD_SET_ORIENTATION, &ori, 1);" in DISPLAY, \
    "the display's Apply changed shape — re-check it cannot clobber the tilt"
print("   mount keeps its tilt when the packet is 1 byte      OK")

# ---- 4. the tilt actually steers the geometry -------------------------------
print("\n4. the maths uses it:")
assert "slider_world_pos" in INO, "the tilt-aware position helper is gone"
body = INO[INO.index("static void slider_world_pos"):]
body = body[:body.index("\n}")]
assert "cosf(t)" in body and "sinf(t)" in body, "slider_world_pos is not resolving the tilt"
print("   slider_world_pos resolves along/up components       OK")

for fn in ("solve_subject_3d", "look_at_pan_deg", "look_at_tilt_deg"):
    seg = INO[INO.index(f"{fn}("):]
    seg = seg[:seg.index("\n}")]
    assert "slider_world_pos" in seg, f"{fn} still assumes a level rail"
print("   triangulation, pan and tilt all go through it       OK")

# ---- 5. what it is worth in millimetres -------------------------------------
# The assertions above prove the plumbing. This says why it mattered.
print("\n5. the error the level-rail model was making:")
RAIL_MM = 1200.0
for deg in (5.0, 21.0, 45.0):
    t = math.radians(deg)
    along, rise = RAIL_MM * math.cos(t), RAIL_MM * math.sin(t)
    # the level model put the far end at (1200, 0); it is really at (along, rise)
    err = math.hypot(RAIL_MM - along, rise)
    print(f"   {deg:>4.0f} deg over {RAIL_MM:.0f} mm rail:"
          f" camera off by {err:6.1f} mm  (rise {rise:5.1f})")
t = math.radians(21.0)
assert math.hypot(RAIL_MM - RAIL_MM*math.cos(t), RAIL_MM*math.sin(t)) > 400, \
    "the 21-degree case should be a large error; check the arithmetic"
print("   at 21 deg the two rays never met near the subject")

print("\nALL CHECKS PASSED")
