"""The rail's end margin is a setting, not a rebuild.

Both ends of the rail are held back from the stall that found them, because a
stall is noticed late and the recorded position is already inside the stop.
30 mm is a workaround for that, and the right value is whatever the rig turns
out to need — which is found by trying one and listening.

As a compile-time constant, every attempt cost a Teensy flash: the board out,
re-homed, ref 0/0, recalibrated. So it is per-mount, stored in EEPROM, and
carried on the wire in both directions — settable from the config dialog, and
REPORTED back so the dialog shows what the mount holds rather than what was
last typed at it.

The wire is checked by round trip rather than by reading the packer's source.
Both new fields are OPTIONAL: an older payload has to keep working, because the
hub display sends CMD_SET_ORIENTATION with the flags byte alone and an Apply
from the display must not reset the rail's end margin any more than it may
level the rail.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re, struct

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

from comms.protocol import (encode_set_orientation, decode_config_report,
                            CONFIG_REPORT_PAYLOAD_LEN,
                            SLIDER_END_MARGIN_MM_DEFAULT,
                            SLIDER_END_MARGIN_MM_MIN,
                            SLIDER_END_MARGIN_MM_MAX)

INO   = (REPO / "firmware/teensy41_mount/teensy41_mount.ino").read_text()
CPP   = (REPO / "firmware/teensy41_mount/MountMotion.cpp").read_text()
HDR   = (REPO / "firmware/teensy41_mount/MountMotion.h").read_text()
DLG   = (REPO / "pc_app/ui/dialogs/config_dialog.py").read_text()

# ---- 1. setting it -----------------------------------------------------------
print("1. what goes out on the wire:")
bare = encode_set_orientation(False, False)
assert len(bare) == 3, \
    f"the flags+tilt payload is {len(bare)} bytes. Omitting the margin has to " \
    "leave\n    the mount's stored value alone — the hub display sends the flags " \
    "byte alone,\n    and its Apply must not reset the rail."
with_margin = encode_set_orientation(False, False, slider_end_margin_mm=15)
assert len(with_margin) == 5, f"the margin payload is {len(with_margin)} bytes, expected 5"
assert struct.unpack(">H", with_margin[3:5])[0] == 15, \
    f"15 mm went out as {struct.unpack('>H', with_margin[3:5])[0]}"
assert with_margin[:3] == bare, "adding the margin changed the flags or the tilt"
print(f"   {len(bare)} bytes without, {len(with_margin)} bytes with, "
      f"flags and tilt unmoved  OK")

# Clamped on the way out AND on the way in — a margin inside the jog back-off
# puts the soft limit a jog respects further out than the limit a goto drives to.
for asked, want in ((0, SLIDER_END_MARGIN_MM_MIN), (1, SLIDER_END_MARGIN_MM_MIN),
                    (9999, SLIDER_END_MARGIN_MM_MAX), (15, 15)):
    got = struct.unpack(">H", encode_set_orientation(
        False, False, slider_end_margin_mm=asked)[3:5])[0]
    assert got == want, f"asking for {asked} mm sent {got}, expected {want}"
print(f"   clamped to {SLIDER_END_MARGIN_MM_MIN}..{SLIDER_END_MARGIN_MM_MAX} mm"
      f" before it is sent          OK")

handler = INO[INO.index("case CMD_SET_ORIENTATION:"):]
handler = handler[:handler.index("\n        }")]
assert "if (len >= 5)" in handler, \
    "the mount does not read the margin, so the setting never arrives"
assert "SLIDER_END_MARGIN_MM_MIN" in handler and "SLIDER_END_MARGIN_MM_MAX" in handler, \
    "the mount trusts the sender's number. The app clamps, but the web app and\n" \
    "    the display are senders too, and only the mount can be the last word."
print("   and clamped again by the mount                    OK")

# ---- 2. reading it back ------------------------------------------------------
print("\n2. what the mount reports:")
p = bytearray(CONFIG_REPORT_PAYLOAD_LEN)
p[77:79] = struct.pack(">H", 15)
assert decode_config_report(bytes(p)).slider_end_margin_mm == 15, \
    "a 79-byte config report does not carry the margin"
print(f"   {CONFIG_REPORT_PAYLOAD_LEN}-byte report -> 15 mm                       OK")

# Older firmware is a working mount, not a parse error — and reports 0, which
# the dialog reads as "unknown" rather than "no margin at all".
for older in (77, 75, 73):
    cr = decode_config_report(bytes(p[:older]))
    assert cr.slider_end_margin_mm == 0, \
        f"a {older}-byte report invents a margin of {cr.slider_end_margin_mm}"
print("   73, 75 and 77 still decode, and claim none        OK")

assert re.search(r"if margin_spin is not None and getattr\(cr, "
                 r"\"slider_end_margin_mm\", 0\)", DLG), \
    "the dialog takes a reported 0 as a real margin, so a mount on older\n" \
    "    firmware would show a rail with no end margin at all"
print("   0 from an older mount leaves the field alone      OK")

# ---- 3. the mount uses it ----------------------------------------------------
print("\n3. what the limit find does with it:")
assert "void     setSliderEndMarginMm(uint16_t mm)" in HDR, \
    "MountMotion cannot be told the margin"
lf = CPP[CPP.index("case LimitFindState::MOVING_TO_MAX:"):]
lf = lf[:lf.index("break;")]
assert "_slider_end_margin_mm" in lf and "AXIS_SLIDER" in lf, \
    "the limit find still uses the compiled constant, so the setting does nothing"
assert "LIMIT_SAFETY_MARGIN[idx]" in lf, \
    "the other axes lost their margin — this was a slider-only request"
print("   slider from the setting, other axes unchanged     OK")

# It has to survive a power cycle, or it is not a setting.
assert "uint16_t         slider_end_margin_mm;" in INO, \
    "the margin is not in EepromConfig, so it is forgotten at every power-off"
m = re.search(r"#define EEPROM_MAGIC\s+(0x[0-9A-Fa-f]+)", INO)
assert m and int(m.group(1), 16) >= 0xCB0C, \
    "EEPROM_MAGIC did not move with the layout. Reading the old bytes into the " \
    "new\n    shape hands every field after the change a value from the wrong " \
    "offset."
print(f"   in EEPROM, magic moved to {m.group(1)}                OK")

# A zero from EEPROM written before the field existed is not a valid margin.
load = INO[INO.index("mount.setSliderTiltDeg(cfg.slider_tilt_deg);"):]
load = load[:load.index("_cfg = cfg;")]
assert "SLIDER_END_MARGIN_MM_DEFAULT" in load and "SLIDER_END_MARGIN_MM_MIN" in load, \
    "a margin loaded from EEPROM is used unchecked — zero from an older layout\n" \
    "    is exactly the grinding this setting exists to prevent"
print("   and a stale zero from EEPROM falls back           OK")

# ---- 4. the operator can reach it --------------------------------------------
print("\n4. the field in Settings:")
assert "end_margin_spin = QSpinBox()" in DLG, "there is no control for it"
assert 'end_margin_spin.setRange(3, 200)' in DLG, \
    "the dialog's range does not match the firmware's clamp, so it can offer a\n" \
    "    value the mount will silently refuse"
assert 'end_margin_spin.setSuffix(" mm")' in DLG, "the units are not shown"
assert "mc.slider_end_margin_mm  = end_margin" in DLG, "Apply does not store it"
assert re.search(r"send_set_orientation\([^)]*end_margin\)", DLG, re.S), \
    "Apply does not send it to the mount"
print("   spin box, mm, stored and sent on Apply            OK")

CFG = (REPO / "pc_app/config/mount_config.py").read_text()
assert "slider_end_margin_mm: int = 30" in CFG and \
       'd.get("slider_end_margin_mm"' in CFG, \
    "the margin is not saved in the app's own config, so it is lost on restart"
print("   saved in the app config too                       OK")

print("\nALL CHECKS PASSED")
