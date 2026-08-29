"""The subject-switch blend runs in ANGLE, not in position.

Measured on the rig on 2026-08-26, three switches on mount 1:

    net tilt move   actual tilt travel   overshoot
       -1.96 deg          5.11 deg        +3.15
       +3.10 deg          4.50 deg        +1.40
       -6.86 deg          7.08 deg        +0.22

The camera tilted out and came back on every one, worst when the net move was
smallest. The same log showed the pan peaking at 43-46 deg/s against the 39.8
the blend had sized its own duration for.

One cause. _laSubjectNow() interpolated the subject POINT from the old subject
to the new one and the tracker took atan2 of the result, so the aim walked a
straight chord between two subjects — and a chord passes nearer the camera than
either end:

  TILT: the aim point dips closer mid-move, and a nearer subject at the same
  height needs more tilt to look at. So the tilt bulges and returns. Largest
  when the two subjects are at similar height, because then the bulge is most
  of the movement rather than a detail on top of it.

  RATE: angular rate is v_perp / r. As r falls toward the middle of the chord
  the rate rises, and it does so exactly where the smoothstep already peaks.

Interpolating the two ANGLES instead makes the angular rate genuinely the
smoothstep the duration was computed from, and the tilt monotonic. Nothing is
given up: both endpoint angles are re-derived from the CURRENT rail position
every tick, which is the only reason the point form existed.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re, math
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent

CPP = (REPO / "firmware/teensy41_mount/MountMotion.cpp").read_text()


def code_only(text: str) -> str:
    out = []
    for line in text.splitlines():
        i = line.find("//")
        out.append(line if i < 0 else line[:i])
    return "\n".join(out)


def body(name, end="\n}\n"):
    s = CPP[CPP.index(name):]
    return s[:s.index(end)]


# ---- 1. the tracker asks for an angle, not a point -------------------------
print("1. what the tracking loop does:")
upd = code_only(body("// 2. Where the camera has to point"))
assert "_laAimNow(wx, wy, &pan_deg, &tilt_deg)" in upd, \
    "the tracker no longer blends in angle"
assert "atan2f" not in upd, \
    "the tracker takes atan2 of something again — if that something is a\n" \
    "    blended point the chord is back, and with it the tilt arc"
print("   calls _laAimNow, no atan2 of its own                   OK")

# ---- 2. and _laAimNow interpolates the two angles ---------------------------
print("\n2. how the blend is applied:")
aim = code_only(body("void MountMotion::_laAimNow("))
assert "*pan_deg  = pan_a  + dpan * e;" in aim and \
       "*tilt_deg = tilt_a + (tilt_b - tilt_a) * e;" in aim, \
    "the angles are not interpolated"
# Both ends re-derived here, or the rail stops being tracked during a switch.
assert aim.count("_aimFrom(") == 2, \
    "only one endpoint is re-derived per tick; the other is frozen and the\n" \
    "    rail would stop being tracked for the length of the blend"
assert "while (dpan > 180.0f)" in aim and "while (dpan < -180.0f)" in aim, \
    "the pan difference is not wrapped — a switch across the wrap would spin\n" \
    "    the head 358 degrees to make a 2 degree correction"
print("   both endpoints re-derived each tick, pan wrapped       OK")

# ---- 3. one definition of 'what angle looks at that point' ------------------
# Three copies of this geometry have been the source of two separate faults on
# this rig already.
print("\n3. where the geometry lives:")
n = code_only(CPP).count("atan2f(dx, dz)")
assert n == 1, f"the pan formula appears {n} times; it should exist once"
setter = code_only(body("void MountMotion::setLookAtSubject("))
assert "_aimFrom(from_x, from_y, from_z, wx, wy, &pan_a, &tilt_a);" in setter, \
    "setLookAtSubject computes the angles by hand again, so the travel it sizes\n" \
    "    the duration from can drift away from the angles actually flown"
print("   one _aimFrom(), used by the tracker and the sizer      OK")

# ---- 4. the numbers, both ways ----------------------------------------------
print("\n4. simulated, two subjects either side at similar height:")


def aim_at(s, w):
    dx, dy, dz = s[0] - w[0], s[1] - w[1], s[2]
    return (math.degrees(math.atan2(dx, dz)),
            math.degrees(math.atan2(dy, math.hypot(dx, dz))))


def smoothstep(t):
    return t * t * (3.0 - 2.0 * t)


W = (0.0, 0.0)
A = (-3.0, -1.4, 2.2)
B = (3.5, -1.6, 3.0)
N = 60
pa, ta = aim_at(A, W)
pb, tb = aim_at(B, W)

series = {"position": [], "angle": []}
for i in range(N + 1):
    e = smoothstep(i / N)
    P = (A[0] + (B[0] - A[0]) * e,
         A[1] + (B[1] - A[1]) * e,
         A[2] + (B[2] - A[2]) * e)
    series["position"].append(aim_at(P, W))
    series["angle"].append((pa + (pb - pa) * e, ta + (tb - ta) * e))

stats = {}
for name, ser in series.items():
    pans = [x[0] for x in ser]
    tilts = [x[1] for x in ser]
    over = max(max(tilts) - max(tilts[0], tilts[-1]),
               min(tilts[0], tilts[-1]) - min(tilts))
    rate = max(abs(pans[i + 1] - pans[i]) for i in range(N)) * N
    stats[name] = (tilts[-1] - tilts[0], over, rate / (1.5 * abs(pans[-1] - pans[0])))
    print(f"   {name:9} lerp: net tilt {stats[name][0]:+6.2f}°, "
          f"overshoot {over:+5.2f}°, peak rate {stats[name][2]:.2f}x design")

# Same destination either way — this changes the PATH, not where it ends up.
assert abs(stats["position"][0] - stats["angle"][0]) < 1e-6, \
    "the two forms no longer agree on the final angle; this was meant to change\n" \
    "    the path taken, not the place it arrives at"
print("   both land on the same angle                            OK")

assert stats["position"][1] > 1.0, \
    "the position lerp no longer overshoots; re-read this test, its premise moved"
assert abs(stats["angle"][1]) < 1e-6, \
    f"the angle lerp overshoots tilt by {stats['angle'][1]:.2f}° — it is linear\n" \
    "    between two endpoints and must be monotonic by construction"
print("   position lerp arcs, angle lerp does not                OK")

assert stats["position"][2] > 1.2, "the position lerp no longer over-speeds"
assert abs(stats["angle"][2] - 1.0) < 0.01, \
    f"the angle lerp peaks at {stats['angle'][2]:.2f}x the design rate; the whole\n" \
    "    point is that the curve's peak is what the duration was sized for"
print("   angle lerp peaks at exactly the designed rate          OK")

# ---- 5. switch-during-switch still starts from where we point ---------------
# setLookAtSubject needs a POINT for the new blend's origin. It must come from
# the current AIM, not from somewhere along the abandoned chord.
print("\n5. starting a switch during a switch:")
now = code_only(body("void MountMotion::_laSubjectNow("))
assert "_laAimNow(wx, wy, &pan_deg, &tilt_deg)" in now, \
    "the point handed to the next blend is not derived from the current aim"
# The projection itself now lives in _pointFromAim(), shared with the
# re-acquisition path. Check the property — that the aim is turned back into a
# point — not which function body the trigonometry sits in.
assert "_pointFromAim(wx, wy, pan_deg, tilt_deg, h, sx, sy, sz)" in now, \
    "the aim is not projected back out to a point"
proj = CPP[CPP.index("void MountMotion::_pointFromAim("):]
proj = proj[:proj.index("\n}")]
assert "sinf(pan_r)" in proj and "cosf(pan_r)" in proj and "tanf(tilt_r)" in proj, \
    "_pointFromAim no longer projects angles and a range into a point"
assert "(1.0f - e)" in now and "* e" in now, \
    "the range is not blended, so a switch during a switch would jump the\n" \
    "    subject nearer or further as well as sideways"
print("   reconstructed from the blended angle and range         OK")

print("\nALL CHECKS PASSED")
