"""Sliding by hand keeps the subject; only pan or tilt drops it.

In look-at mode the operator can push the camera along the rail and the
triangulation should hold the subject centred — that is the whole point of the
mode. Instead a manual slide dropped the subject and left pan and tilt where
they were.

Two separate causes, and fixing either alone leaves the feature broken:

  The slider was in the deselect condition, alongside pan and tilt, in both the
  CMD_JOG and CMD_MOVE_REL handlers. Moving the slider is not aiming somewhere
  else; moving pan or tilt is. Only those two mean "I am pointing at something
  different now".

  _updateLookAt() — the thing that actually drives pan and tilt at the subject —
  runs only in STATE_LOOK_AT_MOVE, which is the state a COMMANDED slider move
  (slot 9/10) puts the mount in. A manual jog puts it in STATE_JOGGING, where
  nothing was tracking.

The tracking call cannot simply be reused as-is: _updateLookAt() ends with a
test for the slider having arrived at _goto_target[AXIS_SLIDER], and calls
stopLookAtMove() when it has. During a hand-driven slide there is no commanded
destination, so that test would fire against a stale target and end the tracking
immediately.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent

INO = (REPO / "firmware/teensy41_mount/teensy41_mount.ino").read_text()
CPP = (REPO / "firmware/teensy41_mount/MountMotion.cpp").read_text()
HDR = (REPO / "firmware/teensy41_mount/MountMotion.h").read_text()

# ---- 1. only pan or tilt deselects ------------------------------------------
print("1. what drops the selected subject:")
conds = re.findall(r"if \(_cfg\.look_at_mode &&\s*\n\s*mount\.getState\(\) != STATE_LOOK_AT_MOVE &&\s*\n"
                   r"\s*mount\.getState\(\) != STATE_LOOK_AT_PRE_AIM &&\s*\n\s*\(([^)]*)\)\) \{", INO)
assert len(conds) == 2, f"expected the CMD_JOG and CMD_MOVE_REL deselect sites, found {len(conds)}"
for c in conds:
    assert "slider" not in c, f"the slider is still a deselect trigger: ({c.strip()})"
    assert "pan" in c and "tilt" in c, f"pan/tilt no longer deselect: ({c.strip()})"
print(f"   both sites drop on pan/tilt only                   OK")

# The subject must still be dropped by a manual pan/tilt — that half is the
# operator saying they are aiming elsewhere, and it is what turns the UI red.
assert INO.count("mount.clearLaSubject();") >= 2, \
    "the deselect on manual pan/tilt has gone entirely"
print("   a manual pan or tilt still drops it                OK")

# ---- 2. pan/tilt actually track during a hand-driven slide ------------------
print("\n2. tracking while the operator owns the slider:")
assert "void     _updateLookAt(bool check_slider_arrival = true);" in HDR, \
    "the look-at controller cannot be run without its arrival test"

upd = CPP[CPP.index("void MountMotion::update()"):]
upd = upd[:upd.index("\n}")]
assert "_state == STATE_JOGGING" in upd and "_updateLookAt(false)" in upd, \
    "nothing tracks the subject during a jog"
m = re.search(r"else if \(_state == STATE_JOGGING && _look_at_mode && _ref_set &&\s*\n\s*"
              r"_la_subject_id != 0xFF &&\s*\n\s*"
              r"_jog_vel\[AXIS_PAN\] == 0 && _jog_vel\[AXIS_TILT\] == 0\) \{", upd)
assert m, "the tracking guard changed shape"
print("   runs in STATE_JOGGING with a subject selected      OK")
print("   only while pan and tilt are themselves idle        OK")

# ---- 2b. and it is INERT with look-at switched off -------------------------
# The guard must ask the mode itself.  A subject id survives a mode change, so
# it cannot stand in for the mode: a subject selected before look-at was turned
# off would otherwise still satisfy the check, and a plain slider jog in
# standard mode would drive pan and tilt at a stale target.
print("\n2b. with look-at disabled:")
assert "_look_at_mode &&" in upd, \
    "the tracking branch does not check look-at mode — a stale subject id would fire it"
assert "void    setLookAtMode(bool on)" in HDR, "MountMotion cannot be told the mode"
assert "mount.setLookAtMode(cfg.look_at_mode);" in INO, \
    "the mode is not pushed to MountMotion on EEPROM load"
assert "mount.setLookAtMode(look_at_mode);" in INO, \
    "the mode is not pushed to MountMotion when it changes"
print("   the branch is gated on the mode, not on a proxy    OK")

# Turning look-at off must drop the selection, like the slots and subjects
# that block already wipes for the same staleness reason.
blk = INO[INO.index("if (look_at_changed) {"):]
blk = blk[:blk.index("send_subject_list();")]
assert "mount.clearLaSubject();" in blk, \
    "the selected subject survives a mode change — it must be cleared with it"
print("   switching the mode off clears the selection        OK")

# Both deselect sites, and every look-at helper, stay inside look-at mode.
for helper in ("slider_world_pos", "look_at_pan_deg", "look_at_tilt_deg",
               "solve_subject_3d"):
    assert helper in INO, f"{helper} vanished"
print("   the geometry helpers are look-at only              OK")

# ---- 3. the arrival test must not fire on a hand-driven slide ---------------
la = CPP[CPP.index("void MountMotion::_updateLookAt("):]
la = la[:la.index("\n}\n")]
assert "if (check_slider_arrival) {" in la, \
    "the end-of-move test is unconditional again — a manual slide would end at once"
arrival = la[la.index("if (check_slider_arrival) {"):]
assert "stopLookAtMove();" in arrival, "the arrival test no longer ends a commanded move"
assert la.index("_driveTowardTarget(AXIS_PAN,") < la.index("if (check_slider_arrival) {"), \
    "pan/tilt are no longer driven before the arrival test — tracking would be skipped"
print("\n3. the end-of-move test:")
print("   guarded, and still ends a COMMANDED move           OK")
print("   pan/tilt driven before it either way               OK")

# ---- 4. the controller still only owns pan and tilt ------------------------
# If it ever drove the slider, it would fight the operator's own jog.
driven = set(re.findall(r"_driveTowardTarget\((AXIS_\w+)", la))
assert driven == {"AXIS_PAN", "AXIS_TILT"}, \
    f"the look-at controller drives {driven}; during a manual slide it must own pan/tilt only"
print("\n4. it drives pan and tilt only — no fight for the")
print("   slider the operator is holding                     OK")

print("\nALL CHECKS PASSED")
