"""A green look-at arrow means "parked at that end" — and stops when it is not.

The slot 9/10 arrows go green to assert that the slider is parked at that end of
the rail. All three clients used to LATCH that from "a look-at move just
finished here", because target_slot reports only a move IN PROGRESS and nothing
in STATUS said where the slider came to rest.

A latch can only notice what the client itself watched. Slide the rail by hand —
which, since manual sliding began keeping the subject tracked, is an ordinary
thing to do mid-shot — and the arrow went on claiming the end. Slide it from the
hub display and the PC app and web app both kept a green arrow with the slider
mid-rail.

The two fallbacks that propped this up were worse than the latch. FLAG_AT_MIN_LIMIT
and FLAG_AT_MAX_LIMIT are set inside the jog loop by whichever axis hits a limit
and cleared by whichever axis is checked last, so they say nothing reliable about
the slider specifically.

The mount knows. In look-at mode slots 8 and 9 hold no stored position — they ARE
the arrows — so their slot_at_mask bits are free to carry "the slider is parked at
that end", reported on every STATUS through the same mask that drives every other
border. Every client derives the arrow from it and none of them latch.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent

PROTO = (REPO / "firmware/shared/protocol.h").read_text()
PY    = (REPO / "pc_app/comms/protocol.py").read_text()
INO   = (REPO / "firmware/teensy41_mount/teensy41_mount.ino").read_text()
MW    = (REPO / "pc_app/ui/main_window.py").read_text()
WEB   = (REPO / "firmware/esp32_hub/web_app.h").read_text()
DISP  = (REPO / "firmware/esp32_display/hub_display.cpp").read_text()

# ---- 1. one canonical meaning for the two bits ------------------------------
print("1. the fact itself:")
for name, val in (("SLOT_LA_LEFT_END", 8), ("SLOT_LA_RIGHT_END", 9)):
    assert re.search(rf"#define {name}\s+{val}", PROTO), f"{name} missing from protocol.h"
    assert re.search(rf"^{name} = {val}", PY, re.M), f"{name} missing from protocol.py"
print("   slots 8/9 named in protocol.h and mirrored in .py  OK")

# They must be the SAME slots the arrows command, or the arrow that takes you to
# an end is not the one that lights when you arrive.
assert re.search(r"#define TARGET_SLOT_LA_MIN\s+8", PROTO)
assert re.search(r"#define TARGET_SLOT_LA_MAX\s+9", PROTO)
print("   and they match TARGET_SLOT_LA_MIN / _MAX           OK")

# ---- 2. the mount reports it -----------------------------------------------
print("\n2. the mount:")
fn = INO[INO.index("static void update_slot_at_mask()"):]
fn = fn[:fn.index("\n}")]
assert "_cfg.look_at_mode" in fn, "the end bits are set outside look-at mode too"
assert "mount.hasLimits(AXIS_SLIDER)" in fn, "the ends are reported without known limits"
assert "1u << 8" in fn and "1u << 9" in fn, "the end bits are not set"
print("   sets bits 8/9, only in look-at mode with limits    OK")

# The invert mapping must match the one CMD_START_LOOK_AT_MOVE uses.
assert "_cfg.slider_invert ? phys_max : phys_min" in fn, "left end ignores slider_invert"
assert "_cfg.slider_invert ? phys_min : phys_max" in fn, "right end ignores slider_invert"
cmd = INO[INO.index("bool go_to_phys_max"):][:200]
assert "(_cfg.slider_invert) ? (direction == 0) : (direction == 1)" in cmd, \
    "the command path's invert mapping changed — the two must agree"
print("   the invert mapping agrees with the command path    OK")

# "At the end" is a region, not a 0.5 mm target.
m = re.search(r"#define AT_END_THRESHOLD_SZ\s+(\d+)", INO)
assert m, "no dedicated end threshold"
end_thr = int(m.group(1))
pos_thr = int(re.search(r"#define AT_POS_THRESHOLD_SZ\s+(\d+)", INO).group(1))
assert end_thr > pos_thr, "the end threshold is no looser than the stored-position one"
print(f"   end window {end_thr} steps vs {pos_thr} for a stored slot   OK")

# ---- 3. no client latches any more -----------------------------------------
print("\n3. the three clients:")
arrows = MW[MW.index("if st.look_at_mode and st.has_slider:"):]
arrows = arrows[:arrows.index("# ---- Run sequence")]
assert "SLOT_LA_LEFT_END" in arrows and "SLOT_LA_RIGHT_END" in arrows, \
    "the PC app does not read the end bits"
assert "cur_arrow ==" not in arrows, "the PC app still latches from its own previous state"
assert "self._set_la_arrow(mount_id, None)" in arrows, \
    "the PC app has no path back to grey"
print("   PC app derives it, and can return to grey          OK")

web = WEB[WEB.index("if (camIsLookAt(mountId)) {"):]
web = web[:web.index("refreshCamBtns();")]
assert "cs.slotAt & (1 << SLOT_LA_LEFT_END)" in web, "the web app does not read the end bits"
assert "cs.laArrow === 'left'" not in web, "the web app still latches from its own state"
assert "FLAG_AT_MIN_LIMIT" not in web, "the web app still falls back to the unreliable flags"
print("   web app derives it, no AT_MIN/AT_MAX fallback      OK")

disp = DISP[DISP.index("int8_t want;"):]
disp = disp[:disp.index("_la_refresh_pending = true;")]
assert "slot_at & (1u << SLOT_LA_LEFT_END)" in disp, "the display does not read the end bits"
assert "_la_arrow_state[i] == 0)" not in disp, "the display still latches from its own state"
assert "want = -1" in disp, "the display has no path back to grey"
print("   hub display derives it, and can return to grey     OK")

# The display's two props for the old latch must be gone with it.
assert "if (cam_is_look_at(i) && _la_arrow_state[i] == -1) {" not in DISP, \
    "the display still seeds green from AT_MIN/AT_MAX on boot"
assert "Manual slider jog while arrow is in \"moving\" state" not in DISP, \
    "the display still special-cases a jog to clear a stale moving arrow"
print("   and both of its old workarounds are gone           OK")

# ---- 4. nothing clears it locally any more ---------------------------------
# A second source of truth for one fact is what caused the earlier round of this.
assert "slider_jogged" not in MW, "the PC app still has a local jog-clears-green path"
assert "slider_jogged" not in (REPO / "pc_app/comms/mount_manager.py").read_text(), \
    "the slider_jogged signal survived"
print("\n4. no client second-guesses the mount                OK")

print("\nALL CHECKS PASSED")
