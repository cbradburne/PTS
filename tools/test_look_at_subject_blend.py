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
print("1. the eased setpoint:")
assert "void MountMotion::_laSubjectNow(" in CPP, "the blend evaluator is gone"
body = CPP[CPP.index("void MountMotion::_laSubjectNow("):]
body = body[:body.index("\n}\n")]
assert "t * t * (3.0f - 2.0f * t)" in body, \
    "the easing is not a smoothstep — a linear ramp still starts and stops abruptly"
print("   smoothstep: zero velocity at both ends              OK")

# It must interpolate all three coordinates, or the aim travels through a
# point that is not on the path between the two subjects.
for i, ax in enumerate("xyz"):
    assert f"_la_blend_from[{i}] + (_la_subject_{ax} - _la_blend_from[{i}]) * e" in body, \
        f"the {ax} coordinate is not blended"
print("   all three coordinates interpolated together        OK")

# Once the blend is over it must read exactly the subject, not an extrapolation.
assert body.count("*sx = _la_subject_x;") == 2, \
    "the blend does not settle exactly on the subject when it expires"
print("   settles exactly on the new subject                 OK")

# ---- 2. the controller aims at the blended point ----------------------------
print("\n2. what the controller follows:")
la = CPP[CPP.index("void MountMotion::_updateLookAt("):]
la = la[:la.index("\n}\n")]
assert "_laSubjectNow(&sx_now, &sy_now, &sz_now);" in la, \
    "the tracking loop still reads the raw subject, so the blend does nothing"
assert "float dx = sx_now - wx;" in la and "float dy = sy_now - wy;" in la, \
    "the camera-to-subject vector is not built from the blended point"
assert "_la_subject_x" not in la, \
    "the tracking loop still references the raw subject somewhere"
print("   _updateLookAt aims at the blended point            OK")

# ---- 3. it only arms on a genuine mid-track switch --------------------------
print("\n3. when it arms:")
setter = CPP[CPP.index("void MountMotion::setLookAtSubject("):]
setter = setter[:setter.index("\n}\n")]
assert "subject_id != _la_subject_id" in setter, \
    "re-selecting the SAME subject would start a blend from itself"
assert "_la_subject_id != 0xFF" in setter, \
    "the first selection would blend from a subject that was never set"
assert "_look_at_mode &&" in setter, "the blend can arm with look-at disabled"
assert "if (!switching) {" in setter and "_la_blend_ms = 0;" in setter, \
    "a first selection does not cancel any previous blend"
print("   only when already tracking a DIFFERENT subject     OK")

# A switch during a switch must continue from where the aim actually is.
assert "if (switching) _laSubjectNow(&from_x, &from_y, &from_z);" in setter, \
    "a switch mid-blend restarts from the old subject — the aim would snap back"
print("   a switch mid-blend continues from the live aim     OK")

assert "_la_blend_ms = 0;" in HDR, "dropping the subject does not cancel the blend"
print("   deselecting cancels it                             OK")

# ---- 4. duration scales with the turn ---------------------------------------
print("\n4. duration:")
assert "travel * LOOK_AT_BLEND_MS_PER_DEG" in setter, \
    "the duration no longer scales with how far the camera must turn"
assert "LOOK_AT_BLEND_MIN_MS" in setter and "LOOK_AT_BLEND_MAX_MS" in setter, \
    "the duration is unclamped"
per_deg = float(re.search(r"#define LOOK_AT_BLEND_MS_PER_DEG\s+([\d.]+)f", HDR).group(1))
lo = int(re.search(r"#define LOOK_AT_BLEND_MIN_MS\s+(\d+)", HDR).group(1))
hi = int(re.search(r"#define LOOK_AT_BLEND_MAX_MS\s+(\d+)", HDR).group(1))
assert lo < hi, "the duration clamp is inverted"
for deg in (2, 10, 30, 60, 120):
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
