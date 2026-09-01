"""Switching subject mid-move eases across instead of stepping.

Selecting a new subject moved the TARGET in a single step and left the
controller to chase the discontinuity. However gently the P-loop was tuned —
and it is tuned carefully, with brake factors and a slew grace period — it was
still reacting to a jump: near-full slew speed almost at once, then a braking
curve into the new aim. It looked mechanical because it was: a step input.

The second half of the stiffness was that pan and tilt are separate P-loops with
separate errors, so whichever had less to travel arrived first and the move read
as two axis motions rather than one arc.

Easing the SETPOINT fixes both without touching the controller. The subject
position is interpolated old -> new on a smoothstep (3t^2 - 2t^3), whose
derivative is zero at both ends, so the aim eases out of the old subject and
into the new one. Both axes come from the same moving point, so they stay
coordinated for free.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re, math
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent

CPP = (REPO / "firmware/teensy41_mount/MountMotion.cpp").read_text()
HDR = (REPO / "firmware/teensy41_mount/MountMotion.h").read_text()

# ---- 1. the blend exists and is a smoothstep --------------------------------
# The curve now lives in _laBlendPhase() and is applied to the ANGLES rather
# than to the subject point — see test_look_at_angle_blend.py for why. The
# easing itself is unchanged and is still what this file is about.
print("1. the eased setpoint:")
assert "float MountMotion::_laBlendPhase(" in CPP, "the blend evaluator is gone"
body = CPP[CPP.index("float MountMotion::_laBlendPhase("):]
body = body[:body.index("\n}")]
assert "t * t * (3.0f - 2.0f * t)" in body, \
    "the easing is not a smoothstep — a linear ramp still starts and stops abruptly"
print("   smoothstep: zero velocity at both ends              OK")

# Pan and tilt must move on ONE phase, or they arrive at different times and
# the move reads as two axis motions instead of one arc.
aim = CPP[CPP.index("void MountMotion::_laAimNow("):]
aim = aim[:aim.index("\n}")]
assert aim.count("_laBlendPhase()") == 1 and "* e;" in aim, \
    "pan and tilt no longer share one phase; they would land separately"
assert "*pan_deg  = pan_a  + dpan * e;" in aim, "pan is not blended"
assert "*tilt_deg = tilt_a + (tilt_b - tilt_a) * e;" in aim, "tilt is not blended"
print("   pan and tilt share one phase, so they land together  OK")

# Once the blend is over it must read exactly the new subject's angles, not an
# extrapolation one tick past the end of the curve.
assert body.count("return 1.0f;") == 2, \
    "the phase does not settle on exactly 1.0 for both 'no blend' and 'expired'"
assert "if (e >= 1.0f) {" in aim and "*pan_deg = pan_b;  *tilt_deg = tilt_b;" in aim, \
    "a finished blend does not short-circuit to the new subject's own angles"
print("   settles exactly on the new subject                 OK")

# ---- 2. the controller follows the blended ANGLE ----------------------------
# It used to follow a blended POINT and take atan2 of it, which walked the aim
# along a chord and made the tilt arc. See test_look_at_angle_blend.py.
print("\n2. what the controller follows:")
la = CPP[CPP.index("void MountMotion::_updateLookAt("):]
la = la[:la.index("\n}\n")]
assert "_laAimNow(wx, wy, &pan_deg, &tilt_deg);" in la, \
    "the tracking loop does not use the blended angles, so the blend does nothing"
assert "atan2f" not in la, \
    "the tracking loop computes an angle of its own again; if that is from a\n" \
    "    blended point then the chord — and the tilt arc — are back"
print("   _updateLookAt follows the blended angle            OK")

# ---- 3. it only arms on a genuine mid-track switch --------------------------
print("\n3. when it arms:")
setter = CPP[CPP.index("void MountMotion::setLookAtSubject("):]
setter = setter[:setter.index("\n}\n")]
assert "subject_id != _la_subject_id" in setter, \
    "re-selecting the SAME subject would start a blend from itself"
assert "_la_subject_id != 0xFF" in setter, \
    "the first selection would blend from a subject that was never set"
assert "_look_at_mode &&" in setter, "the blend can arm with look-at disabled"
assert "if (!switching) {" in setter and \
       re.search(r"_la_blend_ms\s*=\s*0;", setter), \
    "a first selection does not cancel any previous blend"
# and it must drop the blend's speed cap with it, or the next non-blended aim
# would run under a cap derived from a switch that is over.
assert re.search(r"_la_blend_brake\s*=\s*0\.0f;", setter), \
    "a first selection leaves the previous blend's speed cap in force"
print("   only when already tracking a DIFFERENT subject     OK")

# A switch during a switch must continue from where the aim actually is.
# Written as an if/else since re-acquisition joined it, so match the call
# rather than the one-line form it used to have.
assert "_laSubjectNow(&from_x, &from_y, &from_z);" in setter and \
       "if (switching) {" in setter, \
    "a switch mid-blend restarts from the old subject — the aim would snap back"
# _laSubjectNow now reconstructs that point from the live AIM rather than from
# a position lerp, so it is still the place the camera is actually looking.
now_fn = CPP[CPP.index("void MountMotion::_laSubjectNow("):]
now_fn = now_fn[:now_fn.index("\n}")]
assert "_laAimNow(" in now_fn, \
    "the hand-off point is no longer derived from where the camera is pointing"
print("   a switch mid-blend continues from the live aim     OK")

# clearLaSubject() moved from the header into MountMotion.cpp when it gained
# the job of STOPPING pan and tilt as well as forgetting the subject — the
# tracker leaves both axes in an unbounded rotateAsync(), so clearing the state
# alone left them turning.
clear_la = CPP[CPP.index("void MountMotion::clearLaSubject()"):]
# Whitespace normalised: an aligned "_la_blend_ms    = 0;" is the same statement
# as an unaligned one, and a test that cannot tell them apart fails on a tidy-up.
clear_la = re.sub(r"[ \t]+", " ", clear_la[:clear_la.index("\n}")])
assert "_la_blend_ms = 0;" in clear_la, \
    "dropping the subject does not cancel the blend"
print("   deselecting cancels it                             OK")

# ---- 4. duration scales with the turn ---------------------------------------
# NOTE: above about 58 degrees the MAX_MS clamp no longer has the last word —
# the duration is extended so the curve's peak fits under what the axis can
# actually deliver. That rule and its numbers are checked in
# test_look_at_speed_ceiling.py; this section is only about the per-degree
# scaling below that point, so it stops at turns the clamp still governs.
print("\n4. duration:")
assert "travel * LOOK_AT_BLEND_MS_PER_DEG" in setter, \
    "the duration no longer scales with how far the camera must turn"
assert "LOOK_AT_BLEND_MIN_MS" in setter and "LOOK_AT_BLEND_MAX_MS" in setter, \
    "the duration is unclamped"
per_deg = float(re.search(r"#define LOOK_AT_BLEND_MS_PER_DEG\s+([\d.]+)f", HDR).group(1))
lo = int(re.search(r"#define LOOK_AT_BLEND_MIN_MS\s+(\d+)", HDR).group(1))
hi = int(re.search(r"#define LOOK_AT_BLEND_MAX_MS\s+(\d+)", HDR).group(1))
assert lo < hi, "the duration clamp is inverted"
for deg in (2, 10, 30, 40, 55):
    ms = min(max(deg * per_deg, lo), hi)
    print(f"   {deg:>3}° turn -> {ms:>6.0f} ms")
print("   scales with the turn, clamped both ends            OK")

# ---- 5. what the motion actually looks like ---------------------------------
# The controller is a P-loop on the error to the setpoint. Feed it a step and
# the demanded velocity is discontinuous at t=0; feed it a smoothstep and the
# demand starts and ends at rest.
print("\n5. the aim's own velocity through a 40° switch:")
TRAVEL, DUR, DT = 40.0, 40.0 * per_deg / 1000.0, 0.02


def aim(t, eased):
    if t <= 0:
        return 0.0
    if t >= DUR:
        return TRAVEL
    if not eased:
        return TRAVEL          # step: there the instant it is asked for
    u = t / DUR
    return TRAVEL * (u * u * (3.0 - 2.0 * u))


for eased, label in ((False, "step    "), (True, "smoothstep")):
    vs = []
    t = -DT
    while t < DUR + 4 * DT:
        vs.append((aim(t + DT, eased) - aim(t, eased)) / DT)
        t += DT
    peak = max(vs)
    start = vs[1]
    print(f"   {label}: demand jumps to {start:7.1f}°/s at t=0, "
          f"peak {peak:7.1f}°/s")

step_v = (aim(DT, False) - aim(0.0, False)) / DT
ease_v = (aim(DT, True) - aim(0.0, True)) / DT
assert step_v > 100 * ease_v, "the step no longer shows a velocity discontinuity"
assert ease_v < 5.0, "the eased setpoint does not start from rest"
ease_end = (aim(DUR, True) - aim(DUR - DT, True)) / DT
assert ease_end < 5.0, "the eased setpoint does not finish at rest"
print("   eased demand starts AND ends at rest               OK")
print(f"   peak is 1.5x the average, as smoothstep implies "
      f"({TRAVEL/DUR:.0f}°/s avg)")

print("\nALL CHECKS PASSED")
