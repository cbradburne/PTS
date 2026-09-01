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
# The state exclusions this used to match on are gone: on 2026-08-27 the rule was
# extended to apply DURING a look-at move too, so taking the joystick mid-move
# hands pan/tilt to the operator and drops the subject while the rail carries on.
# What this file is about is unchanged and is the part still checked here — the
# SLIDER must never be a deselect trigger, because sliding by hand is the one
# manual input that means "keep holding the subject".
print("1. what drops the selected subject:")
conds = re.findall(r"if \(_cfg\.look_at_mode && \(([^)]*)\)\) \{", INO)
assert len(conds) == 2, \
    f"expected the CMD_JOG and CMD_MOVE_REL deselect sites, found {len(conds)}"
for c in conds:
    assert "slider" not in c, f"the slider is a deselect trigger again: ({c.strip()})"
    assert "pan" in c and "tilt" in c, f"pan/tilt no longer deselect: ({c.strip()})"
print("   both sites drop on pan/tilt only                   OK")

# And no state test may creep back in: excluding a look-at move is what made a
# joystick do nothing at all while the slider was travelling.
for c in conds:
    assert "getState" not in c, \
        "a state test is back in the deselect condition — if it excludes a\n" \
        "    look-at move again, the joystick stops working during one"
print("   and during a look-at move as well as outside one    OK")

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

# ---- 5. and a slider nudge tracks too, not just a joystick slide -----------
# The joystick tracked; the Move panel's slider buttons did not. Same feature,
# different command: a MOVE_REL went through moveTo() into STATE_MOVING_TO_POS,
# where _updateGoto() holds every axis at the target it was handed — and for a
# slider-only move pan and tilt were handed the position they already had. The
# camera slid down the rail with the subject walking out of frame.
#
# Rather than a second copy of the tracking gate, a slider-only nudge with a
# subject selected now starts a real look-at move to a relative destination.
print("\n5. a slider nudge from the Move panel:")
rel = INO[INO.index("case CMD_MOVE_REL: {"):]
rel = rel[:rel.index("\n        }")]
assert "startLookAtMove" in rel, \
    "a slider MOVE_REL no longer starts a look-at move — pan and tilt will sit\n" \
    "    still while the rail moves, which is the bug this is about"

# The routing decision, run against the cases it has to separate. The condition
# is lifted from the source and evaluated, so it is the real logic being tested
# and not a restatement of it.
m = re.search(r"if \((d_pan == 0 && d_tilt == 0 && d_slider != 0 &&.*?"
              r"hasLimits\(AXIS_SLIDER\))\) \{", rel, re.S)
assert m, "the tracking condition changed shape — check it still separates the cases below"
expr = re.sub(r"\s+", " ", m.group(1))
py = (expr.replace("&&", "and")
          .replace("_cfg.look_at_mode", "look_at")
          .replace("mount.getLaSubjectId()", "subject")
          .replace("mount.isRefSet()", "ref_set")
          .replace("mount.hasLimits(AXIS_SLIDER)", "limits")
          .replace("0xFF", "0xFF"))

CASES = [
    # d_pan d_tilt d_slider look_at subject ref limits  -> tracked
    ((0, 0,  1600, True,  3, True,  True),  True,  "a slider nudge with a subject"),
    ((0, 0, -1600, True,  3, True,  True),  True,  "the other way"),
    ((0, 0,  1600, True,  0, True,  True),  True,  "subject 0 is a subject"),
    ((0, 0,  1600, True, 0xFF, True, True), False, "no subject selected"),
    ((0, 0,  1600, False, 3, True,  True),  False, "look-at mode off"),
    ((0, 0,  1600, True,  3, False, True),  False, "no reference set"),
    ((0, 0,  1600, True,  3, True,  False), False, "slider limits not found"),
    ((320, 0, 1600, True, 3, True,  True),  False, "pan moved too — that is aiming"),
    ((0, 320, 1600, True, 3, True,  True),  False, "tilt moved too"),
    ((320, 0,    0, True, 3, True,  True),  False, "a pan-only nudge"),
    ((0, 0,      0, True, 3, True,  True),  False, "nothing moved"),
]
for (d_pan, d_tilt, d_slider, look_at, subject, ref_set, limits), want, what in CASES:
    got = bool(eval(py, {}, dict(d_pan=d_pan, d_tilt=d_tilt, d_slider=d_slider,
                                 look_at=look_at, subject=subject,
                                 ref_set=ref_set, limits=limits)))
    assert got == want, \
        f"{what}: the nudge is {'tracked' if got else 'not tracked'}, expected " \
        f"{'tracked' if want else 'not tracked'}"
print(f"   {len(CASES)} cases route correctly                       OK")

# A pan or tilt nudge must still land as a plain move — and still drop the
# subject, which is section 1's rule and the reason those cases are excluded.
assert "if (!tracked)" in rel and "mount.moveRel(" in rel, \
    "the plain relative move is gone — everything that is not tracked would do nothing"
assert rel.index("startLookAtMove") < rel.index("if (!tracked)"), \
    "the fallback runs before the attempt"
print("   anything else still does a plain relative move    OK")

# It has to be the same entry point every other look-at move uses, or the
# tracking gate exists twice and the two will drift.
assert "LOOK_AT_MAX_DEG_S" in rel, \
    "the nudge asks for a different pan/tilt speed limit than every other look-at move"
starts = len(re.findall(r"mount\.startLookAtMove\(", INO))
assert starts == 2, \
    f"{starts} call sites for startLookAtMove — expected the rail-end command and " \
    "the nudge"
print("   same entry point and speed cap as a rail-end move  OK")

# Tapping the same button twice must not restart the move. startLookAtMove()
# opens with emergencyStop() — a hard stop, no decel ramp — which on a live
# shot is a visible jerk. A move already running is retargeted instead.
assert "mount.getState() == STATE_LOOK_AT_MOVE" in rel, \
    "a second nudge restarts the move, and startLookAtMove() hard-stops the rail\n" \
    "    on the way in"
assert "mount.moveSliderTo(" in rel, "a nudge during a move has nothing to retarget with"
assert rel.index("mount.moveSliderTo(") < rel.index("mount.startLookAtMove("), \
    "the retarget branch is not the one taken while a move is already running"
la_move = CPP[CPP.index("void MountMotion::moveSliderTo("):]
la_move = la_move[:la_move.index("\n}")]
assert not re.search(r"^\s*_state\s*=[^=]", la_move, re.M), \
    "moveSliderTo() assigns _state — retargeting would drop out of the look-at\n" \
    "    move it is meant to extend, and tracking would stop mid-rail"
assert "_goto_target[AXIS_SLIDER]" in la_move, \
    "moveSliderTo() no longer records the destination the arrival test reads"
print("   a second tap retargets instead of hard-stopping    OK")

print("\nALL CHECKS PASSED")
