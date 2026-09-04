"""The console's Focus button exists only when there is a camera to focus.

Asked for on 2026-08-27: a Focus button above the zoom slider on the hub
display's per-camera screen, present only when that mount has a Blackmagic
camera paired.

The display had no idea whether a camera was paired. The bit exists —
HEALTH_FLAG_BLE_LINK, set by the mount's own health report — but the hub only
ever relayed mount health to the clients and never read it, and the display
does not receive mount health at all.

It could not go in the mount status flags byte either: all eight bits are
spoken for. So it is appended to DISP_MSG_UPDATE_CAM as a fifth byte, which
also means it arrives with every status refresh and needs no change-tracking of
its own. A display or hub from before this reads or sends four bytes and the
button simply never appears — the two boards are flashed separately and have to
tolerate being out of step.

Hidden rather than greyed when nothing is paired: there is nothing to focus,
and a disabled button invites a press and then a question about why nothing
happened.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent

DISP = (REPO / "firmware/esp32_display/hub_display.cpp").read_text()
DHDR = (REPO / "firmware/esp32_display/hub_display.h").read_text()
DINO = (REPO / "firmware/esp32_display/esp32_display.ino").read_text()
HUB = (REPO / "firmware/esp32_hub_eth/esp32_hub_eth.ino").read_text()
UART = (REPO / "firmware/shared/disp_uart.h").read_text()
PROTO = (REPO / "firmware/shared/protocol.h").read_text()


def code_only(text):
    return "\n".join(l if l.find("//") < 0 else l[:l.find("//")]
                     for l in text.splitlines())


# ---- 1. the bit could not have gone anywhere else --------------------------
print("1. why a new byte:")
flags = re.findall(r"#define FLAG_\w+\s+0x([0-9A-Fa-f]{2})", PROTO)
used = 0
for f in flags:
    used |= int(f, 16)
assert used == 0xFF, \
    f"the mount status flags byte is no longer full (0x{used:02X}); a spare bit\n" \
    "    there would be cheaper than widening the display message"
print("   the mount status flags byte is full, all 8 bits     OK")
assert "#define DISP_CAM_BLE_LINKED" in UART, "the display-side flag is not defined"
print("   so a fifth UPDATE_CAM byte carries it               OK")

# ---- 2. the hub reads the bit it used only to relay ------------------------
print("\n2. where the hub gets it:")
assert "pkt.cmd == CMD_HEALTH" in HUB and "HEALTH_FLAG_BLE_LINK" in HUB, \
    "the hub does not read the BLE link bit out of mount health"
assert "pkt.payload[0] == HEALTH_NODE_BRIDGE" in HUB, \
    "the hub does not check WHOSE health it is reading — the hub, the display\n" \
    "    and the satellites all send the same record with different meanings"
assert "pkt.payload[19] & HEALTH_FLAG_BLE_LINK" in HUB, \
    "the flags byte is read from the wrong offset in the 24-byte record"
print("   from the bridge's own health, offset 19             OK")

# The offset has to match the struct, or it reads some other field entirely.
# Anchor on the LAST struct opener before the closer: a non-greedy match from
# the first one in the file spans every struct between them and sums their
# fields too, which reported the offset as 46.
end = PROTO.index("} PayloadHealth;")
fields = PROTO[PROTO.rindex("typedef struct __attribute__((packed)) {", 0, end):end]
sizes = {"uint8_t": 1, "int8_t": 1, "uint16_t": 2, "int16_t": 2,
         "uint32_t": 4, "int32_t": 4, "uint64_t": 8, "int64_t": 8}
off = 0
for line in fields.splitlines():
    m = re.match(r"\s*(u?int\d+_t)\s+(\w+)", line)
    if not m:
        continue
    if m.group(2) == "flags":
        break
    off += sizes[m.group(1)]
assert off == 19, f"PayloadHealth.flags is at offset {off}, the hub reads 19"
print(f"   which is where PayloadHealth.flags actually is      OK")

# ---- 3. both ends tolerate the other being older ---------------------------
print("\n3. a hub and a display flashed at different times:")
assert "uint8_t buf[5] = { mount_id, state, flags, (uint8_t)rssi, cf };" in HUB, \
    "the hub no longer sends the fifth byte"
assert "(len >= 5) ? d[4] : 0" in DINO, \
    "the display assumes the fifth byte is present; an older hub sends four"
assert "uint8_t cam_flags = 0);" in DHDR, \
    "the parameter has no default, so anything still calling the four-argument\n" \
    "    form stops compiling instead of behaving as it did"
print("   4 bytes -> no button; 5 bytes -> the flag           OK")

# ---- 4. the button appears and disappears with the camera ------------------
print("\n4. the button:")
disp = code_only(DISP)
assert '_det_focus_btn = make_button(_scr_detail, "FOCUS", C_SURF2, ev_detail_focus);' in disp, \
    "the Focus button is gone"
assert "lv_obj_add_flag(_det_focus_btn, LV_OBJ_FLAG_HIDDEN);" in disp, \
    "the button is not hidden at build time, so it shows before any camera has\n" \
    "    reported and disappears a moment later"
print("   built hidden                                       OK")

# Where it sits, computed rather than described. The line above used to SAY
# "above the zoom slider" and assert nothing of the kind, so the button could be
# moved anywhere and this file would still have congratulated it.
DEFS = {k: int(v) for k, v in
        re.findall(r"#define\s+(HSL_X|HSL_W|DET_BTN_W|DET_BTN_H|DET_BTN_Y)\s+(\d+)",
                   DISP)}
assert len(DEFS) == 5, f"the layout constants changed shape: {sorted(DEFS)}"


def geom(name, kind):
    m = re.search(rf"lv_obj_set_{kind}\({name},\s*([^,]+),\s*([^)]+)\);", disp)
    assert m, f"no lv_obj_set_{kind} for {name}"
    return tuple(int(eval(g.strip(), {}, DEFS)) for g in m.groups())


fw, fh = geom("_det_focus_btn", "size")
fx, fy = geom("_det_focus_btn", "pos")
cw, ch = geom("_det_clear_btn", "size")
cx, cy = geom("_det_clear_btn", "pos")
sw, sh = geom("_det_set_btn", "size")
_, sy  = geom("_det_set_btn", "pos")

assert (fw, fh) == (cw, ch) == (sw, sh), \
    f"FOCUS is {fw}x{fh}, CLEAR {cw}x{ch}, SET {sw}x{sh} — one row of buttons " \
    "should be\n    one size"
assert fy == cy == sy, \
    f"FOCUS sits at y={fy}, CLEAR/SET at y={cy}/{sy} — they do not line up"
print(f"   {fw}x{fh} at y={fy}, same as CLEAR and SET           OK")

# Centred on the ZOOM column below it, not on anything else.
zoom_centre  = DEFS["HSL_X"] + DEFS["HSL_W"] / 2
focus_centre = fx + fw / 2
assert abs(focus_centre - zoom_centre) < 1, \
    f"FOCUS is centred at x={focus_centre:.0f}, the ZOOM column at " \
    f"x={zoom_centre:.0f} —\n    it is the camera's control and should sit over " \
    "the camera's fader"
print(f"   centred at x={focus_centre:.0f} over the ZOOM column at "
      f"x={zoom_centre:.0f}      OK")

# And clear of the buttons it shares the row with.
assert fx + fw < cx, \
    f"FOCUS ends at x={fx + fw} and CLEAR starts at x={cx} — they overlap"
print(f"   {cx - (fx + fw)}px clear of CLEAR                            OK")

refresh = disp[disp.index("static void _detail_refresh_focus()"):]
refresh = refresh[:refresh.index("\n}")]
assert "_cam[_detail_cam].cam_linked" in refresh, \
    "the button's visibility is not driven by whether a camera is paired"
assert "LV_OBJ_FLAG_HIDDEN" in refresh, "it is not hidden, only styled"
print("   shown only while a camera is linked                 OK")

# It has to be refreshed on BOTH the things that can change the answer.
# In det_switch_cam(), not merely somewhere in the file — matched on the
# function body rather than on a line with its comment attached, since comments
# are stripped above.
switch = disp[disp.index("static void det_switch_cam(uint8_t idx)"):]
switch = switch[:switch.index("\n}")]
assert "_detail_refresh_focus();" in switch, \
    "switching camera on the detail screen keeps the previous camera's button\n" \
    "    state, so a mount with no camera shows one and vice versa"
upd = disp[disp.index("void hub_ui_update_cam(uint8_t mount_id,"):]
upd = upd[:upd.index("\n}\n")]
assert "_cam[i].cam_linked != was_linked" in upd and "_detail_refresh_focus();" in upd, \
    "a camera pairing or dropping while the screen is open does not update it"
print("   refreshed on camera switch and on link change       OK")

# ---- 5. and pressing it sends the camera's own bytes -----------------------
print("\n5. what a press sends:")
ev = disp[disp.index("static void ev_detail_focus(lv_event_t *)"):]
ev = ev[:ev.index("\n}")]
assert "if (!_cam[_detail_cam].cam_linked) return;" in ev, \
    "a press with no camera paired still sends; the button should be hidden but\n" \
    "    the handler must not depend on that being true"
assert "CMD_CAM_CONTROL" in ev, "the press does not use the verbatim camera pipe"
af = re.findall(r"0x([0-9A-Fa-f]{2})", ev)
assert [x.upper() for x in af[:12]] == ["FF", "04", "00", "00",
                                        "00", "01", "01", "00",
                                        "00", "00", "00", "00"], \
    "the autofocus bytes differ from the ones the PC app and the hub's OSC\n" \
    "    handler send; CMD_CAM_CONTROL is verbatim, so a difference here is a\n" \
    "    different command reaching the camera"
print("   the same 12 bytes the other surfaces send           OK")

# _detail_cam is unsigned; a < 0 guard would read as a check and never fire.
assert "_detail_cam < 0" not in disp, \
    "_detail_cam is unsigned, so a < 0 test is dead code dressed as a guard"
assert "_detail_cam >= 5" in ev, "nothing bounds the camera index"
print("   bounded by an index test that can actually fail     OK")

print("\nALL CHECKS PASSED")
