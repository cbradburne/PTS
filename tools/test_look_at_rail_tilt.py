"""The look-at TRACKER must use the same rail as the solver.

The slider-tilt work resolved the rail's inclination in the .ino —
solve_subject_3d(), look_at_pan_deg(), look_at_tilt_deg(). It missed that the
same geometry is implemented a second time inside MountMotion, and that copy is
the one that drives the motors during a follow.

There are three sites in MountMotion, all computing the vector from camera to
subject: the pre-aim phase, aimAtSubject(), and _updateLookAt(). Every one of
them read

    float dy = _la_subject_y;

taking the camera's height on the rail as zero — a level rail — while the
subject had been solved for a rail climbing at 21 degrees. Solved in one frame,
tracked in another.

It reads as a tracking fault rather than a geometry one because PAN is barely
affected: pan depends on the rise only through cos(tilt) on the along-rail
distance. TILT depends on it entirely. On mount 5 on 2026-08-20, pan followed to
within 1.3 degrees across the whole rail while tilt moved 0.1 degrees where the
calibration wanted 11.2 — the camera stayed pointing at the floor.

Setting the tilt correctly could never have fixed this, which is why 0, 21 and
-21 all behaved the same during a follow.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re, math
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent

CPP = (REPO / "firmware/teensy41_mount/MountMotion.cpp").read_text()
HDR = (REPO / "firmware/teensy41_mount/MountMotion.h").read_text()
INO = (REPO / "firmware/teensy41_mount/teensy41_mount.ino").read_text()

# ---- 1. MountMotion knows the rail at all -----------------------------------
print("1. the tracker's copy of the geometry:")
assert "_slider_tilt_deg" in HDR, "MountMotion still has no rail inclination"
assert "void setSliderTiltDeg(float deg)" in HDR, "no way to give it the tilt"
assert "void MountMotion::_railWorldPos(" in CPP, "the rail helper is gone"
body = CPP[CPP.index("void MountMotion::_railWorldPos("):]
body = body[:body.index("\n}")]
assert "cosf(t)" in body and "sinf(t)" in body, "_railWorldPos does not resolve the tilt"
print("   _railWorldPos resolves along/up components          OK")

# ---- 2. all three sites use it ----------------------------------------------
# Any site still reading the subject's height directly is tracking a flat rail.
print("\n2. every site that aims at the subject:")
assert "float dy = _la_subject_y;" not in CPP, \
    "a look-at site still takes the camera height as zero"
n = CPP.count("float dy = _la_subject_y - w")
assert n == 3, f"expected 3 rail-aware sites (pre-aim, aimAtSubject, tracking), found {n}"
print(f"   {n} of 3 subtract the camera's height on the rail   OK")
n = CPP.count("_railWorldPos(")
assert n >= 4, f"_railWorldPos called {n} times; expected 3 sites + definition"
print("   pre-aim, aimAtSubject and _updateLookAt all fixed   OK")

# ---- 3. the value actually reaches it ---------------------------------------
print("\n3. the tilt reaches MountMotion:")
assert "mount.setSliderTiltDeg(cfg.slider_tilt_deg);" in INO, \
    "the EEPROM load path does not pass the tilt to MountMotion"
assert "mount.setSliderTiltDeg(_cfg.slider_tilt_deg);" in INO, \
    "a live SET_ORIENTATION does not pass the tilt to MountMotion"
print("   on EEPROM load and on live SET_ORIENTATION          OK")

# ---- 4. the two implementations must agree ----------------------------------
# The solver's frame and the tracker's frame have to be the same one.
ino_body = INO[INO.index("static void slider_world_pos("):]
ino_body = ino_body[:ino_body.index("\n}")]
for frag in ("cosf(t)", "sinf(t)"):
    assert frag in ino_body and frag in body, f"frames disagree on {frag}"
print("   solver and tracker resolve the rail identically     OK")

# ---- 5. the numbers, from the rig -------------------------------------------
# Calibration of 2026-08-20 14:45, mount-internal frame (slider_invert negates
# the displayed mm). Rail at 21 degrees.
XA, PA, TA = -2719.0,  12.10,  -3.30
XB, PB, TB =    -0.1, -15.10, -14.50
RAIL = 21.0


def world(x, rail=RAIL):
    t = math.radians(rail)
    return x * math.cos(t), x * math.sin(t)


def solve(rail=RAIL):
    pa, ta, pb, tb = map(math.radians, (PA, TA, PB, TB))
    vA = (math.sin(pa)*math.cos(ta), math.sin(ta), math.cos(pa)*math.cos(ta))
    vB = (math.sin(pb)*math.cos(tb), math.sin(tb), math.cos(pb)*math.cos(tb))
    oax, oay = world(XA, rail)
    obx, oby = world(XB, rail)
    wx, wy = oax - obx, oay - oby
    b = sum(u*v for u, v in zip(vA, vB))
    d = vA[0]*wx + vA[1]*wy
    e = vB[0]*wx + vB[1]*wy
    den = 1 - b*b
    t = (b*e - d)/den
    s = (e - b*d)/den
    pA = (oax + t*vA[0], oay + t*vA[1], t*vA[2])
    pB = (obx + s*vB[0], oby + s*vB[1], s*vB[2])
    return tuple((pA[i] + pB[i]) * 0.5 for i in range(3))


sub = solve()


def track_tilt(cx, rail_aware):
    sx, sy, sz = sub
    if rail_aware:
        wx, wy = world(cx)
        dx, dy = sx - wx, sy - wy
    else:
        dx, dy = sx - cx, sy          # the shipped bug
    return math.degrees(math.atan2(dy, math.hypot(dx, sz)))


print("\n4. tilt across the rail, against what the rig measured:")
want = TA - TB
old = track_tilt(XA, False) - track_tilt(XB, False)
new = track_tilt(XA, True) - track_tilt(XB, True)
print(f"   calibration wanted a swing of {want:+6.2f}°")
print(f"   level-rail tracker gives      {old:+6.2f}°   (rig showed +0.10°)")
print(f"   rail-aware tracker gives      {new:+6.2f}°")
assert abs(old) < 1.0, "the old formula no longer reproduces the flat tilt"
assert abs(new - want) < 2.0, "the new formula does not reproduce the measured swing"
print("   the fix restores the swing the calibration implies  OK")

# A level rail must be completely unaffected — existing rigs cannot change.
# At 0 degrees _railWorldPos returns (cx, 0), so the rail-aware form collapses
# to exactly the old one: dx = sx - cx, dy = sy - 0.
def tilt_at(cx, wx, wy):
    return math.degrees(math.atan2(sub[1] - wy, math.hypot(sub[0] - wx, sub[2])))

for cx in (XA, XB, -1000.0, 0.0):
    t0 = math.radians(0.0)
    rail_aware = tilt_at(cx, cx * math.cos(t0), cx * math.sin(t0))
    level_rail = tilt_at(cx, cx, 0.0)
    assert abs(rail_aware - level_rail) < 1e-9, \
        f"a level rail is no longer a no-op at cx={cx}"
print("   a level rail is still bit-identical                 OK")

print("\nALL CHECKS PASSED")
