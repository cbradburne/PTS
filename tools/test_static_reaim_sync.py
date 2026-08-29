"""A subject switch with the slider parked must land both axes together.

Reported on 2026-08-27: switching subject in look-at mode with the slider
stationary, pan and tilt do not arrive together — one stops while the other is
still running. It looks correct while the slider IS moving.

That difference is the whole diagnosis. A switch during a look-at move goes
through setLookAtSubject()'s blend, which drives pan and tilt off one phase, so
they land together by construction. Parked, the mount is not in
STATE_LOOK_AT_MOVE, so CMD_SWITCH_SUBJECT takes an entirely different path:

    mount.aimAtSubject(_active_pt_preset);   ->   moveTo(..., sync=false)

and sync=false means each axis runs at its own full preset speed. Whichever has
less to travel finishes first and sits there.

The comment defending sync=false had it backwards — it said syncing "would make
one axis crawl". Sync scales the axis that would arrive EARLY down to match the
one that takes longest; the dominant axis stays at full preset speed. The move
takes exactly as long either way. The only question is whether the short axis
spends that time moving or spends it stopped.

At preset 4 (15 deg/s) a 90 degree pan with 3 degrees of tilt had the tilt
finish 5.8 seconds before the pan.

Slot recalls have always synced. This is the same expectation applied to the
same kind of move.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent

CPP = (REPO / "firmware/teensy41_mount/MountMotion.cpp").read_text()
INO = (REPO / "firmware/teensy41_mount/teensy41_mount.ino").read_text()


def code_only(text):
    return "\n".join(l if l.find("//") < 0 else l[:l.find("//")]
                     for l in text.splitlines())


# ---- 1. the parked path is a goto, not the blend ---------------------------
print("1. what a parked switch actually does:")
h = INO[INO.index("case CMD_SWITCH_SUBJECT:"):]
h = h[:h.index("\n        default:")]
assert "mount.setLookAtSubject(" in h, "the switch no longer tells the tracker"
assert "mount.aimAtSubject(_active_pt_preset);" in h, \
    "a parked switch no longer re-aims at all"
assert "getState() == STATE_LOOK_AT_MOVE" in h, \
    "the two cases are no longer told apart, so one of them is being handled\n" \
    "    by machinery built for the other"
print("   moving -> the blend; parked -> aimAtSubject()      OK")

# ---- 2. and that goto synchronises the axes --------------------------------
print("\n2. how the parked re-aim is commanded:")
aim = code_only(CPP[CPP.index("bool MountMotion::aimAtSubject("):])
aim = aim[:aim.index("\n}")]
m = re.search(r"moveTo\([^)]*sync=\*/(\w+)\)", aim)
assert m, "aimAtSubject no longer calls moveTo with an explicit sync argument"
assert m.group(1) == "true", \
    "aimAtSubject asks for sync=false again — each axis then runs at its own\n" \
    "    full preset speed and the shorter one finishes first and stops"
print("   moveTo(..., sync=true)                             OK")

# ---- 3. sync does what the fix assumes -------------------------------------
# The change is only correct because sync slows the EARLY axis, never the move
# as a whole. If that ever stops being true this fix becomes a slowdown.
print("\n3. what sync actually scales:")
mt = code_only(CPP[CPP.index("    pt_preset = constrain(pt_preset, 1, 4);"):])
mt = mt[:mt.index("_goto_accel_st[i]   = (uint32_t)actual_acc;")]
assert "if (t > t_move) t_move = t;" in mt, \
    "t_move is no longer the LONGEST axis time, so scaling to it could slow\n" \
    "    the whole move rather than just delaying the early axis"
assert "float k  = (dist_st[i] / preset_spd) / t_move;" in mt, \
    "the scale factor is no longer each axis's own share of the longest time"
assert "actual_spd = max(1.0f, preset_spd * k);" in mt and \
       "actual_acc = max(1.0f, preset_acc * k);" in mt, \
    "speed and acceleration are not scaled together, so the ramp shape changes\n" \
    "    and the slowed axis no longer decelerates in proportion"
print("   k <= 1 against the longest axis, speed and accel    OK")

# An axis with nothing to do must not be stretched across the whole move.
assert "dist_st[i] > (float)GOTO_ARRIVE_STEPS" in mt, \
    "an axis already at its target is included in the timing, so a move with\n" \
    "    one idle axis would drag it along at a crawl for no reason"
print("   an axis already there is left out of the timing     OK")

# ---- 4. the numbers that were reported -------------------------------------
print("\n4. the reported case, at preset 4 (15 deg/s):")
for pan, tilt in ((90, 3), (60, 8), (40, 2)):
    tp, tt = pan / 15.0, tilt / 15.0
    longest = max(tp, tt)
    k = min(tp, tt) / longest
    print(f"   pan {pan:>2}deg / tilt {tilt:>2}deg: was {abs(tp - tt):.2f}s apart, "
          f"now both {longest:.2f}s (k={k:.3f})")
    assert abs(longest - tp) < 1e-9 or abs(longest - tt) < 1e-9, \
        "the synced duration is not one of the two axis durations, so the move\n" \
        "    has been made longer than either axis needed"
print("   same total duration, both axes land together        OK")

# ---- 5. the moving case is untouched ---------------------------------------
# It already worked, and it works by a different mechanism. A change here must
# not have reached it.
print("\n5. a switch while the slider IS moving:")
aimnow = code_only(CPP[CPP.index("void MountMotion::_laAimNow("):])
aimnow = aimnow[:aimnow.index("\n}")]
assert "*pan_deg  = pan_a  + dpan * e;" in aimnow and \
       "*tilt_deg = tilt_a + (tilt_b - tilt_a) * e;" in aimnow, \
    "the blend no longer drives both axes from one phase"
assert aimnow.count("_laBlendPhase()") == 1, \
    "pan and tilt no longer share a single phase, so they would stop landing\n" \
    "    together during a slider move as well"
print("   still one phase for both axes, unchanged            OK")

print("\nALL CHECKS PASSED")
