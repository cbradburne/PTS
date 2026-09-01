"""Taking the joystick during a look-at move hands over pan/tilt, not the rail.

Asked for on 2026-08-27: while the slider is travelling locked to a subject, a
joystick should move pan and tilt off the subject — dropping it, so every
client goes red — with the slider carrying on to the end of its travel. Then
re-selecting any subject re-aims and resumes the lock, still mid-travel.

At the time a joystick did nothing at all during a move. jogPanTilt() returned
at the top ("look-at owns pan/tilt") and both deselect sites in the .ino
explicitly excluded STATE_LOOK_AT_MOVE, so the commanded move was the one place
in the system that did not honour a rule everywhere else already followed:
moving pan or tilt by hand means "I am aiming somewhere else".

Three things have to hold together for this to work:

  the jog reaches the axes, and the subject is dropped

  the STATE stays LOOK_AT_MOVE, or the slider stops being a look-at move and
  nothing notices it arriving

  _updateLookAt() stops driving pan/tilt while there is no subject, or the
  tracker and the jog fight for the same two motors

And re-selecting has to ease from wherever the operator left the camera. There
is no previous subject to blend from — they dropped it — but the head is
pointing somewhere definite, and jumping the target from there is the same step
input the blend was built to remove.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent

CPP = (REPO / "firmware/teensy41_mount/MountMotion.cpp").read_text()
HDR = (REPO / "firmware/teensy41_mount/MountMotion.h").read_text()
INO = (REPO / "firmware/teensy41_mount/teensy41_mount.ino").read_text()


def code_only(text):
    return "\n".join(l if l.find("//") < 0 else l[:l.find("//")]
                     for l in text.splitlines())


def body(sig, end="\n}\n"):
    s = CPP[CPP.index(sig):]
    return code_only(s[:s.index(end)])


# ---- 1. the jog reaches the axes -------------------------------------------
print("1. a joystick during a look-at move:")
jog = body("void MountMotion::jogPanTilt(", end="\n}\n\n")
assert "if (_state == STATE_LOOK_AT_MOVE ||\n        _state == STATE_LOOK_AT_PRE_AIM) return;" not in jog, \
    "jogPanTilt still refuses outright during a look-at move, so the joystick\n" \
    "    does nothing at all while the slider is travelling"
assert "clearLaSubject();" in jog, \
    "taking pan/tilt no longer drops the subject, so every client keeps a green\n" \
    "    border on a subject the camera is not pointing at"
print("   reaches the axes, and drops the subject             OK")

# ---- 2. but the move keeps running -----------------------------------------
# If the state changed, the slider would stop being part of a look-at move and
# nothing would be watching for it to arrive.
print("\n2. what happens to the slider:")
assert "if (!in_la_move) _state = STATE_JOGGING;" in jog, \
    "the state is changed during a look-at move — the rail stops being a\n" \
    "    look-at move and nothing notices it reaching the end"
print("   state stays LOOK_AT_MOVE, so the rail carries on    OK")

# Releasing the stick must still stop the axes. An early return on a centred
# stick would leave them turning.
assert "} else if (_la_subject_id != 0xFF) {" in jog and "return;" in jog, \
    "a centred stick with a subject selected no longer returns early, so the\n" \
    "    stop code fights the tracker for the axes"
idx_else = jog.index("} else if (_la_subject_id != 0xFF) {")
idx_vel = jog.index("_jog_vel[AXIS_PAN]  = _applyOrientation")
assert idx_else < idx_vel, \
    "the guard sits after the jog is applied, so it cannot guard anything"
print("   a centred stick with no subject still stops them    OK")

# ---- 3. the tracker lets go of pan/tilt ------------------------------------
print("\n3. while the operator has the axes:")
upd = body("void MountMotion::_updateLookAt(")
assert "bool aiming = (_la_subject_id != 0xFF);" in upd, \
    "the tracker has no notion of 'no subject', so it aims at a stale one"
assert "if (aiming) {" in upd, "the drive calls are not guarded"
drive_at = upd.index("_driveTowardTarget(AXIS_PAN,")
guard_at = upd.index("if (aiming) {")
assert guard_at < drive_at, \
    "pan/tilt are driven before the guard, so the tracker and the jog both\n" \
    "    command the same two motors every tick"
print("   pan/tilt are not driven with no subject             OK")

# The slider-arrival check must still run — that is what ends the move.
assert "if (check_slider_arrival) {" in upd, "the arrival check has gone"
assert upd.index("if (check_slider_arrival) {") > guard_at, \
    "the arrival check now sits inside the aiming guard, so a move with the\n" \
    "    subject dropped would never end at all"
print("   the arrival check still runs, so the move ends      OK")

# And it must not wait for an aim that no longer exists.
assert "bool aim_there   = !aiming ||" in upd, \
    "with no subject the move waits for an aim to arrive that was never set,\n" \
    "    hanging until the timeout while the operator holds a deliberate frame"
print("   and does not wait for an aim nobody asked for       OK")

# ---- 4. re-selecting takes the axes back, smoothly -------------------------
print("\n4. re-selecting a subject:")
setter = body("void MountMotion::setLookAtSubject(")
assert "bool reacquiring = (tracking_now && _la_subject_id == 0xFF && _ref_set);" in setter, \
    "re-acquiring after manual control is not recognised, so the target jumps\n" \
    "    to the subject and the controller chases a step"
assert "_headAimAngles(&hpan, &htilt);" in setter, \
    "the blend does not start from where the head is actually pointing"
assert "switching = true;" in setter, \
    "the re-acquisition is not treated as a switch, so no blend is armed"
print("   eases from where the operator left the camera       OK")

# The jog bookkeeping has to be dropped or both believe they own the axes.
assert "_jog_vel[AXIS_PAN] = _jog_vel[AXIS_TILT] = 0;" in setter and \
       "_jog_dir[AXIS_PAN] = _jog_dir[AXIS_TILT] = 0;" in setter, \
    "the jog state survives re-selection, so the jog and the tracker both\n" \
    "    believe they hold pan and tilt"
print("   and takes the axes back from the jog                OK")

# ---- 5. the .ino tells the clients -----------------------------------------
# The mount dropping the subject is invisible unless the status goes out.
print("\n5. what the clients are told:")
for cmd, ax in (("case CMD_JOG:", "pan"), ("case CMD_MOVE_REL:", "d_pan")):
    h = INO[INO.index(cmd):]
    h = h[:h.index("\n        case ")]
    assert f"_cfg.look_at_mode && ({ax} != 0" in h, \
        f"{cmd} no longer deselects on a manual pan/tilt"
    assert "getState() != STATE_LOOK_AT_MOVE" not in h, \
        f"{cmd} excludes a look-at move again — the joystick would stop working\n" \
        "    for exactly the case this was asked for"
    assert "send_look_at_status();" in h, \
        f"{cmd} drops the subject without telling anyone, so the borders stay green"
print("   both sites deselect and broadcast, during a move    OK")

# The slider must never be a deselect trigger: sliding by hand is the one
# manual input that means "keep holding the subject".
conds = re.findall(r"if \(_cfg\.look_at_mode && \(([^)]*)\)\) \{", INO)
assert len(conds) == 2, f"expected two deselect sites, found {len(conds)}"
for c in conds:
    assert "slider" not in c, f"the slider deselects again: ({c.strip()})"
print("   the slider still does not deselect                  OK")

# ---- 6. and an ORDINARY joystick actually gets there -----------------------
# Everything above tests jogPanTilt(), which has been right for a while. None
# of it asked whether a joystick reaches it — and it did not. The .ino routes
# to jogPanTilt() only for axis_mask 0x03, which is CV tracking; a stick sends
# 0x0F and went to jog(), whose look-at branch handled zoom and returned. The
# subject was dropped (the .ino does that either way, so the borders went red)
# and the head did not move. Tested from the entry point the packet uses.
print("\n6. the path an ordinary joystick takes:")
jogfn = body("void MountMotion::jog(int16_t pan", end="\n}\n\n")
la_branch = jogfn[jogfn.index("if (_state == STATE_LOOK_AT_MOVE"):]
assert "jogPanTilt(pan, tilt, pt_preset);" in la_branch, \
    "jog()'s look-at branch does not hand pan/tilt to jogPanTilt(). A stick sends\n" \
    "    axis_mask 0x0F, which lands here — so pan and tilt are discarded and the\n" \
    "    joystick does nothing but turn the borders red."
assert la_branch.index("jogPanTilt(") < la_branch.index("return;"), \
    "the delegation is after the return, so it never runs"

# Not for the goto-zoom case: pan and tilt are zero by that branch's own
# definition, and jogPanTilt() would take _state to STATE_JOGGING and orphan
# the goto the branch exists to protect.
assert "if (!goto_zoom_only) jogPanTilt(" in la_branch, \
    "a zoom nudge during a GOTO now runs jogPanTilt(0, 0), which sets\n" \
    "    _state = STATE_JOGGING and abandons every axis the goto was steering"
print("   0x0F reaches jogPanTilt, 0x03 already did           OK")
print("   and a goto zoom nudge still does not                OK")

# ---- 7. dropping the subject lets go of the axes ---------------------------
# _updateLookAt() drives pan/tilt with an unbounded rotateAsync() and steers by
# re-issuing setMaxSpeed(). When `aiming` goes false it simply stops calling
# _driveTowardTarget(), so the last speed stays set and both axes keep turning.
# Nothing else stopped them: the jog paths only stop axes with _jog_dir[] set,
# and a tracked axis has none.
print("\n7. what happens to pan and tilt when the subject goes:")
assert "void    clearLaSubject();" in HDR, \
    "clearLaSubject is inline again — an inline that only clears four fields is\n" \
    "    what left both axes turning after the subject was dropped"
clr = body("void MountMotion::clearLaSubject()")
assert "stopAsync()" in clr, \
    "clearLaSubject does not stop the axes. _updateLookAt() leaves them in an\n" \
    "    unbounded rotateAsync(); dropping the subject just stops steering them."
assert "_jog_dir[i] == 0" in clr, \
    "it stops axes the operator has already claimed, which cancels the jog that\n" \
    "    is replacing the tracking"
assert "if (!had_subject) return;" in clr, \
    "it acts when there was no subject to drop. A held stick clears on every\n" \
    "    packet at 20 Hz, so this has to be the transition or the jog stutters."
assert clr.index("had_subject") < clr.index("stopAsync()"), \
    "the transition guard runs after the stop"
print("   stopped, once, and only if the tracker had them     OK")

# ---- 8. and a held stick cannot outlive the link ---------------------------
# _updateJog() and its watchdog only run in STATE_JOGGING, which a look-at move
# is not. Zoom had a dead-man here already because zoom was the only axis that
# could be held in this state; pan and tilt can be now.
print("\n8. the dead-man outside STATE_JOGGING:")
upd_fn = body("void MountMotion::update()")
wd = upd_fn[upd_fn.index("_state != STATE_JOGGING"):]
wd = wd[:wd.index("STATE_FINDING_LIMITS")]
for ax in ("AXIS_PAN", "AXIS_TILT", "AXIS_ZOOM"):
    assert ax in wd, \
        f"{ax} is not covered by the watchdog outside STATE_JOGGING. A held stick\n" \
        "    and a dead link would leave it turning with nothing left to stop it."
assert "_jog_dir[ax] == 0) continue;" in wd, \
    "the watchdog stops axes that are not being jogged"
print("   pan, tilt and zoom all covered                      OK")

print("\nALL CHECKS PASSED")
