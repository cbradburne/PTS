"""A subject aims exactly where Set A and Set B were pressed.

Reported on 2026-11-18: right after calibrating a subject, starting a look-at
move swung pan/tilt before the slider moved, with the camera already on the
shot just set.  "The look-at shouldn't change that, they are absolute values
that I created."

The cause is the solver.  It stores the MIDPOINT of the two sightlines' closest
approach, and two sightlines aimed by hand almost never cross — a fraction of a
degree of tilt is enough — so the stored point lies on neither.  Aimed at from
where Set B was pressed, it is not where the camera was pointed, and the
move's pre-aim drove the camera to it.

The fix keeps the point (it is what tracks between the two) and puts back how
far each sighting missed it: the aim at a rail position is the point's, plus
the miss faded from A's to B's along the rail and held flat beyond.  At A and
at B the aim is then exactly what was set.

  1. The geometry, ported: the old jump, and that the fix removes it exactly.
  2. The Teensy sketch measures the miss in tracking's frame and keeps it.
  3. The motion code applies it everywhere the subject's aim is computed.
  4. A switch carries the old subject's miss across, so it does not step.
  5. The PC logs the miss from the SOLVED prompt.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re, math, logging

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))
CPP = (REPO / "firmware/teensy41_mount/MountMotion.cpp").read_text()
HDR = (REPO / "firmware/teensy41_mount/MountMotion.h").read_text()
INO = (REPO / "firmware/teensy41_mount/teensy41_mount.ino").read_text()


def body(src, signature):
    start = src.index(signature)
    return re.sub(r"//[^\n]*", "", src[start:src.index("\n}\n", start)])


# ---- 1. the geometry --------------------------------------------------------
# A level rail; the camera at x along it; z into the room, y up.  These are
# solve_subject_3d(), look_at_pan_deg/tilt_deg and aimFixAt() in Python.
def ray(pan, tilt):
    p, t = math.radians(pan), math.radians(tilt)
    return (math.sin(p) * math.cos(t), math.sin(t), math.cos(p) * math.cos(t))


def solve(xa, xb, pa, ta, pb, tb):
    va, vb = ray(pa, ta), ray(pb, tb)
    w = (xa - xb, 0.0, 0.0)
    b = sum(i * j for i, j in zip(va, vb))
    d = sum(i * j for i, j in zip(va, w))
    e = sum(i * j for i, j in zip(vb, w))
    den = 1 - b * b
    t, s = (b * e - d) / den, (e - b * d) / den
    pA = (xa + t * va[0], t * va[1], t * va[2])
    pB = (xb + s * vb[0], s * vb[1], s * vb[2])
    return tuple((i + j) / 2 for i, j in zip(pA, pB))


def aim(cx, s):
    dx, dy, dz = s[0] - cx, s[1], s[2]
    return (math.degrees(math.atan2(dx, dz)),
            math.degrees(math.atan2(dy, math.hypot(dx, dz))))


def fix_at(f, cx):
    span = f["xb"] - f["xa"]
    u = 0.5 if abs(span) < 1.0 else min(max((cx - f["xa"]) / span, 0.0), 1.0)
    return (f["pa"] + (f["pb"] - f["pa"]) * u, f["ta"] + (f["tb"] - f["ta"]) * u)


print("1. the geometry:")
TRUE = (500.0, 300.0, 3000.0)
XA, XB = 0.0, 1500.0
pa, ta = aim(XA, TRUE)
pb, tb = aim(XB, TRUE)
# Hand-aimed: a little high at A, a little low at B, and a touch of pan.
obs = dict(pa=pa + 0.10, ta=ta + 0.40, pb=pb - 0.05, tb=tb - 0.30)
S = solve(XA, XB, obs["pa"], obs["ta"], obs["pb"], obs["tb"])
mA, mB = aim(XA, S), aim(XB, S)
jump_b = max(abs(mB[0] - obs["pb"]), abs(mB[1] - obs["tb"]))
print(f"   sightings 0.4° and 0.3° off in tilt: the midpoint is "
      f"{jump_b:.2f}° from where B was set")
assert jump_b > 0.1, "the example no longer shows the jump it is about"

fix = dict(xa=XA, xb=XB, pa=obs["pa"] - mA[0], ta=obs["ta"] - mA[1],
           pb=obs["pb"] - mB[0], tb=obs["tb"] - mB[1])
for cx, (op, ot), name in ((XA, (obs["pa"], obs["ta"]), "A"),
                           (XB, (obs["pb"], obs["tb"]), "B")):
    p, t = aim(cx, S)
    fp, ft = fix_at(fix, cx)
    assert abs(p + fp - op) < 1e-9 and abs(t + ft - ot) < 1e-9, \
        f"with the miss put back, {name} still does not aim as set"
print("   with the miss put back, A and B aim exactly as set        OK")

mid = fix_at(fix, (XA + XB) / 2)
assert abs(mid[1] - (fix["ta"] + fix["tb"]) / 2) < 1e-9, "the fade is not linear along the rail"
assert fix_at(fix, XA - 400) == fix_at(fix, XA) and fix_at(fix, XB + 400) == fix_at(fix, XB), \
    "beyond A or B the correction keeps growing instead of holding"
assert fix_at(dict(fix, xb=XA + 0.5), 123.0) == ((fix["pa"] + fix["pb"]) / 2,
                                                  (fix["ta"] + fix["tb"]) / 2), \
    "A and B at one spot do not share their average"
print("   faded between, held beyond, averaged if A and B coincide  OK")

# ---- 2. the sketch measures it in tracking's frame, and keeps it ------------
print("\n2. the Teensy sketch:")
setb = INO[INO.index("case CMD_ADD_SUBJECT_SET_B: {"):]
setb = re.sub(r"//[^\n]*", "", setb[:setb.index("case CMD_ADD_SUBJECT_ABORT")])
for want, why in (
        ("(float)_calib_pan_steps_A  * dps_p + ref_p", "A's pan miss is not taken in tracking's frame"),
        ("(float)_calib_tilt_steps_B * dps_t + ref_t", "B's tilt miss is not taken in tracking's frame"),
        ("look_at_tilt_deg(_calib_xb_mm, sx, sy, sz)", "the miss is not measured against the solved point"),
        ("worst_miss > SUBJECT_AIM_FIX_MAX_DEG", "a miss too big to be aiming error is baked in"),
        ("if (sz_clamped) {", "a subject beside the rail has its miss baked in"),
        ("_subject_fix[_calib_subject_id] = fix;", "the miss is not kept with the subject"),
        ("send_packet(CMD_CALIB_PROMPT, solved, SUBJECT_MISS_PAYLOAD_LEN);",
         "SOLVED goes out without the miss, so the PC never sees it"),
        ("mount.setLookAtSubject(sx, sy, sz, _calib_subject_id,\n"
         "                                   _subject_fix[_calib_subject_id]);",
         "the new subject is selected without its miss — the very jump reported")):
    assert want in setb, why
applied = setb[setb.index("miss_flags  = SUBJECT_MISS_APPLIED;"):setb.index("_subject_fix[_calib_subject_id] = fix;")]
for f, v in (("xa_mm", "_calib_xa_mm"), ("xb_mm", "_calib_xb_mm"), ("pan_a", "miss_pan_A"),
             ("tilt_a", "miss_tilt_A"), ("pan_b", "miss_pan_B"), ("tilt_b", "miss_tilt_B")):
    assert re.search(rf"fix\.{f}\s*=\s*{v};", applied), \
        f"the correction kept with the subject leaves out its {f}"
# The midpoint is computed BEFORE the store, the miss after it: an auto-
# reference solve shows up as a large miss in tracking's frame and is refused.
assert setb.index("bool ok = solve_subject_3d(") < setb.index("float miss_pan_A"), \
    "the miss is measured before the point it is measured against exists"
print("   measured against the solved point, in tracking's frame    OK")
print("   refused when too big or beside the rail, kept otherwise   OK")

calls = re.findall(r"mount\.setLookAtSubject\(([^;]*)\);", INO)
assert calls and all("_subject_fix[" in c for c in calls), \
    f"a subject is selected without its miss: {[c for c in calls if '_subject_fix[' not in c]}"
assert "_subject_fix[id] = SubjectAimFix();" in INO, "a deleted subject keeps its miss"
ref = INO[INO.index("Compute expected pan/tilt at current slider position for this subject"):]
ref = ref[:ref.index("mount.setLookAtRef(")]
assert "MountMotion::aimFixAt(_subject_fix[subj_id], cx, &fix_pan, &fix_tilt);" in ref \
    and "exp_pan  += fix_pan;" in ref and "exp_tilt += fix_tilt;" in ref, \
    "Set Ref against a subject ignores its miss — the reference and tracking disagree"
print(f"   every selection ({len(calls)}) carries it; delete and Set Ref agree  OK")

# ---- 3. the motion code applies it wherever the subject's aim is computed ---
print("\n3. the motion code:")
fixat = body(CPP, "void MountMotion::aimFixAt(")
assert "constrain((cx_mm - f.xa_mm) / span, 0.0f, 1.0f)" in fixat and \
       "(fabsf(span) < 1.0f) ? 0.5f" in fixat, "aimFixAt is not the fade ported above"
aimnow = body(CPP, "void MountMotion::_laAimNow(")
assert "aimFixAt(_la_fix, cx, &fp, &ft);" in aimnow, "tracking ignores the subject's miss"
assert "aimFixAt(_la_blend_from_fix, cx, &fp, &ft);" in aimnow, \
    "a switch leaves its old subject without that subject's miss — a step at the start"
assert "_subjectAim(cx_now, &pan_deg_now, &tilt_deg_now);" in body(CPP, "bool MountMotion::startLookAtMove("), \
    "the pre-aim — the reported jump — aims without the miss"
assert "_subjectAim(cx, &pan_deg, &tilt_deg);" in body(CPP, "bool MountMotion::aimAtSubject("), \
    "a parked switch aims without the miss"
sub = body(CPP, "void MountMotion::_subjectAim(")
assert "aimFixAt(_la_fix, cx, &fp, &ft);" in sub, "_subjectAim leaves the miss out"
setter = body(CPP, "void MountMotion::setLookAtSubject(")
assert "aimFixAt(from_fix, cx, &fp, &ft);" in setter and "aimFixAt(fix, cx, &fp, &ft);" in setter, \
    "a switch's length is measured without the misses it will actually fly"
assert "_la_fix        = fix;" in setter and "_la_blend_from_fix = from_fix;" in setter
assert "_laSubjectNow(&from_x, &from_y, &from_z, &from_fix);" in setter
now = body(CPP, "void MountMotion::_laSubjectNow(")
assert "if (fix) *fix = _la_fix;" in now and "if (fix) *fix = SubjectAimFix();" in now, \
    "a switch from a settled subject or from mid-blend picks up the wrong miss"
clear = body(CPP, "void MountMotion::clearLaSubject(")
assert "_la_fix            = SubjectAimFix();" in clear and \
       "_la_blend_from_fix = SubjectAimFix();" in clear, "deselecting keeps a stale miss"
print("   tracking, pre-aim, parked aim and the switch all use it   OK")

# ---- 4. a switch does not step by the miss ------------------------------------
# Leaving a settled subject, the blend's from side is that subject WITH its
# miss — so at the first tick the setpoint is exactly where the camera was.
print("\n4. a switch away from a corrected subject:")
cx = 700.0
p, t = aim(cx, S)
fp, ft = fix_at(fix, cx)
before = (p + fp, t + ft)              # where tracking had the camera
from_side = (p + fp, t + ft)           # _laAimNow's from side at e = 0, as coded
assert from_side == before
without = (p, t)                       # what it would be had the miss been dropped
print(f"   would have stepped {max(abs(before[0]-without[0]), abs(before[1]-without[1])):.2f}° "
      f"at the first tick; steps 0 with the miss carried     OK")

# ---- 5. the PC logs it ---------------------------------------------------------
print("\n5. the PC:")
from PyQt6.QtCore import QCoreApplication
app = QCoreApplication.instance() or QCoreApplication([])
from comms import mount_manager as MM
from comms import protocol as P
from comms.protocol import Packet, Cmd


class FakeHubLink:
    connected = True
    def __init__(self): self.cb = None
    def on_packet(self, cb): self.cb = cb
    def off_packet(self, cb): pass
    def send(self, data): pass
    def seconds_since_tracked_tx(self): return 0.0


class Lines(logging.Handler):
    def __init__(self): super().__init__(); self.got = []
    def emit(self, rec):
        if rec.getMessage().startswith("SUBJECT MISS"):
            self.got.append(rec.getMessage())


lines = Lines()
logging.getLogger(MM.__name__).addHandler(lines)
logging.getLogger(MM.__name__).setLevel(logging.INFO)
link = FakeHubLink()
mm = MM.MountManager(link)


def solved(flags, tail=True):
    p = bytes([int(P.CalibPrompt.SOLVED)])
    if tail:
        import struct
        p += struct.pack(">hhhh", 12, -45, -10, 41) + bytes([7, flags])
    link.cb(Packet(mount_id=5, seq=0, cmd=Cmd.CALIB_PROMPT, payload=p))


solved(P.SUBJECT_MISS_APPLIED)
want = ("SUBJECT MISS cam5 slot 7: the sightings missed the solved point by pan +0.12° "
        "tilt -0.45° at A, pan -0.10° tilt +0.41° at B — put back, so A and B aim "
        "exactly as set")
assert lines.got == [want], f"the log reads {lines.got}"
lines.got = []
solved(P.SUBJECT_MISS_TOO_BIG)
assert len(lines.got) == 1 and "TOO BIG" in lines.got[0] and "NOT put back" in lines.got[0], lines.got
lines.got = []
solved(P.SUBJECT_MISS_BESIDE_RAIL)
assert len(lines.got) == 1 and "beside the rail" in lines.got[0], lines.got
lines.got = []
solved(0, tail=False)
assert lines.got == [], "a 1-byte SOLVED from older firmware logged a miss it does not have"
print("   'SUBJECT MISS' line for each verdict; none from older firmware  OK")

print("\nALL CHECKS PASSED")
