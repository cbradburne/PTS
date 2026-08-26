"""The slider angle is settable from every client, and unset by none of them.

It was settable in the PC app and read-only on the web app and hub display. The
awkward part is not adding the controls, it is that CMD_SET_ORIENTATION carries
the flags and the tilt in one packet, so anything that writes a flag also writes
a tilt — or must deliberately not.

The mount reads a 1-byte packet as "flags only, tilt unchanged" and a 3-byte one
as "flags and tilt". That distinction is what keeps a client from levelling a
rail it has not yet been told about:

  a web-app flag toggle before the first CONFIG_REPORT sends 1 byte
  the display's Apply before the first CONFIG_REPORT sends 1 byte
  switching camera on the display forgets the angle until the new report

Each client also commits at a different moment, and each new control matches
what is already there rather than inventing a third rule:

  web app  — on 'change', which is blur or Enter, per box
  display  — batched into Apply / camera switch / leaving the screen

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent

WEB  = (REPO / "firmware/esp32_hub/web_app.h").read_text()
DISP = (REPO / "firmware/esp32_display/hub_display.cpp").read_text()
INO  = (REPO / "firmware/teensy41_mount/teensy41_mount.ino").read_text()

# ---- 1. the mount's rule, which everything else depends on -----------------
print("1. the mount:")
h = INO[INO.index("case CMD_SET_ORIENTATION"):]
h = h[:h.index("\n        case ")]
assert "if (len >= 3) {" in h, \
    "the mount no longer treats a short packet as 'tilt unchanged' — every\n" \
    "    client that sends flags alone would now level the rail"
print("   1 byte = flags only, 3 bytes = flags + tilt          OK")

# ---- 2. web app ------------------------------------------------------------
print("\n2. web app:")
mk = WEB[WEB.index("function mkSetOrientation("):]
mk = mk[:mk.index("\n}")]
assert "tiltDeg === undefined || tiltDeg === null" in mk, \
    "mkSetOrientation always sends a tilt; a flag toggle would carry one"
assert "new Uint8Array(1)" in mk and "new Uint8Array(3)" in mk, \
    "the builder no longer has both packet lengths"
assert "Math.round(tiltDeg * 10)" in mk, "the tilt is not sent as tenths"
print("   omitting the tilt sends flags only                   OK")

# The flag toggles must NOT pass one — they know the flags, not the angle.
tog = WEB[WEB.index("function _extToggleOri("):]
tog = tog[:tog.index("\n}")]
assert "mkSetOrientation(cam, cs.oriByte)" in tog, \
    "a flag toggle now sends a tilt as well; before the first CONFIG_REPORT\n" \
    "    that would be a fabricated 0"
print("   flag toggles pass no tilt                            OK")

# The box itself sends on change — blur or Enter — like every other box here.
box = WEB[WEB.index("inp.id = 'ext-tilt-' + i;"):]
box = box[:box.index("row.appendChild(inp);")]
assert "addEventListener('change'" in box, \
    "the tilt box does not commit on 'change'; per-keystroke sends would\n" \
    "    transmit '-' and '-2' on the way to '-21'"
assert "mkSetOrientation(_i2, cs.oriByte, v)" in box, "the box does not send the tilt"
assert "Math.max(-90, Math.min(90," in box, "the box is unclamped"
assert "if (!cs.connected" in box, "the box sends to a mount that is not there"
print("   the box commits on blur/Enter, clamped, guarded      OK")

# ---- 3. hub display --------------------------------------------------------
print("\n3. hub display:")
for fn, delta in (("ev_cfg_tilt_minus", "-0.5f"),
                  ("ev_cfg_tilt_plus", " 0.5f")):
    assert f"_cfg_tilt_nudge({delta})" in DISP, f"{fn} does not step by half a degree"
assert "_cfg_slider_tilt = 0.0f;" in DISP, "the 0 button does not zero the angle"
print("   -/0/+ step by 0.5, and zero                          OK")

nudge = DISP[DISP.index("static void _cfg_tilt_nudge("):]
nudge = nudge[:nudge.index("\n}")]
assert "90.0f" in nudge and "-90.0f" in nudge, "the angle is unclamped"
assert "_send_cb" not in nudge, \
    "a button press sends immediately; six taps would be six packets, and it\n" \
    "    would not match how every other control on this screen behaves"
print("   a tap changes the value only, not the wire           OK")

# Apply carries it — but only once the angle is actually known.
apply = DISP[DISP.index("static void ev_send_config(lv_event_t *e) {"):]
apply = apply[:apply.index("\n}\n")]
assert "if (_cfg_tilt_known) {" in apply, \
    "Apply always sends a tilt; before the first CONFIG_REPORT that is a\n" \
    "    fabricated 0 and would level the rail"
assert "CMD_SET_ORIENTATION, p3, 3" in apply, "Apply never sends the 3-byte form"
assert "CMD_SET_ORIENTATION, &ori, 1" in apply, "the flags-only fallback is gone"
print("   Apply sends 3 bytes, or 1 if the angle is unknown    OK")

# Switching camera must forget it — the value on screen belongs to the old one.
sel = DISP[DISP.index("static void ev_cfg_cam_select("):]
sel = sel[:sel.index("\n}\n")]
assert "_cfg_tilt_known = false;" in sel, \
    "switching camera keeps the previous camera's angle on screen, and Apply\n" \
    "    would then write it to the new one"
assert sel.index("ev_send_config(nullptr);") < sel.index("_cfg_tilt_known = false;"), \
    "the old camera's config is saved AFTER its angle is forgotten"
print("   a camera switch saves, then forgets                  OK")

# ---- 4. and the report still feeds it --------------------------------------
print("\n4. what a CONFIG_REPORT does:")
assert "_cfg_slider_tilt = (float)t10 / 10.0f;" in DISP, \
    "the display no longer takes the angle from the mount"
assert "_cfg_tilt_known  = false;" in DISP, \
    "a 75-byte report from older firmware no longer marks the angle unknown"
print("   77 bytes sets it, 75 bytes leaves it unknown         OK")

print("\nALL CHECKS PASSED")
