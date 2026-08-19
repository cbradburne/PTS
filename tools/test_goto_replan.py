"""A mid-move recall plans the same way as a fresh one.

moveTo() synchronises the axes so they land together. retargetTo() — the path
taken when a slot is pressed while a move is already running — did neither: it
handed each axis its full preset speed and never recomputed the P-loop's decel
window at all.

Two consequences, both seen on the rig:

  Pressing the same slot twice behaved differently depending only on whether the
  mount happened to still be moving, because the two presses took different code
  paths with different arithmetic.

  The decel window carried over from the previous move. _updateGoto() steers on
  `factor = err / _goto_decel_dist`, and decel_dist = v²/2a — so an axis
  retargeted from a sync-scaled 109 steps/s up to its full 711 kept a window
  sized for a seventh of the speed, roughly forty times too small. It ran flat
  out until far past the point it should have begun slowing, and overshot.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
import re, shutil, subprocess, tempfile

CPP = (REPO / "firmware/teensy41_mount/MountMotion.cpp").read_text()


def func(name: str) -> str:
    i = CPP.index(f"void MountMotion::{name}(")
    j = CPP.index("\n}", i)
    return CPP[i:j]


MOVE, RETARGET = func("moveTo"), func("retargetTo")

# ---- 1. retargetTo must now do the two things it never did ------------------
print("1. retargetTo plans like moveTo:")
assert "t_move" in RETARGET, "retargetTo still computes no move time"
assert re.search(r"float k\s*=\s*\(dist_st\[i\] / preset_spd\) / t_move", RETARGET), \
    "retargetTo does not synchronise the axes"
print("   synchronises all four axes                         OK")

assert "_goto_decel_dist[i]" in RETARGET, \
    "retargetTo still leaves the decel window from the previous move"
assert re.search(r"\(actual_spd \* actual_spd\) / \(2\.f \* actual_acc\)", RETARGET), \
    "the decel window is not derived from the speed actually set"
print("   recomputes the decel window from that speed        OK")

# Both paths must use the SAME expressions, or they drift apart again.
for frag in ("(dist_st[i] / preset_spd) / t_move",
             "(actual_spd * actual_spd) / (2.f * actual_acc)",
             "fmaxf(move_dist, (float)(GOTO_ARRIVE_STEPS * 2))"):
    assert frag in MOVE and frag in RETARGET, f"paths disagree on: {frag}"
print("   both paths share the same arithmetic               OK")


# ---- 1b. the motor ceiling is the PRESET; sync lives in the factor ---------
# Setting a new max on a turning motor only takes effect via rotateAsync(), and
# rotateAsync() here takes a POSITIVE max because direction is carried by
# overrideSpeed's sign — so calling it on an axis travelling negative would
# fling it the other way for a tick.  Keeping the ceiling at the preset and
# scaling the factor means a retarget never touches a moving motor at all.
UPDATE = func("_updateGoto")
print("\n1b. where the synchronisation is applied:")
assert "_goto_spd_scale[i]" in UPDATE, "the sync ratio is not applied in the P-loop"
assert re.search(r"constrain\(factor, -1\.0f, 1\.0f\) \* _goto_spd_scale\[i\]", UPDATE), \
    "the factor is not scaled by the axis's share of the move"
print("   overrideSpeed factor carries the sync ratio         OK")

for fn, name in ((MOVE, "moveTo"), (RETARGET, "retargetTo")):
    assert "_goto_spd_scale[i]" in fn, f"{name} does not record the sync ratio"
    assert "_goto_max_spd_st[i]  = (uint32_t)preset_spd;" in fn, \
        f"{name} still puts a scaled speed in the motor ceiling"
print("   both paths set the ceiling to the preset            OK")

# retargetTo must not restart an axis that is already turning.
assert "_goto_dir[i] == 0 && dist_st[i]" in RETARGET, \
    "retargetTo restarts axes unconditionally — it must only start PARKED ones"
print("   a turning axis is never re-launched mid-move        OK")

# Any decel window must be sized from the speed actually travelled, everywhere
# it is computed — including _updateGoto's restart branch, which reads the
# ceiling and would otherwise size for a speed the axis never reaches.
assert "_goto_max_spd_st[i] * _goto_spd_scale[i]" in UPDATE, \
    "the restart branch sizes its decel window from the unscaled ceiling"
print("   restart branch sizes its window from actual speed   OK")

# ---- 2. the arithmetic itself ----------------------------------------------
if not shutil.which("c++"):
    print("\n2. no C++ compiler — skipping the numeric check.")
    print("\nALL CHECKS PASSED")
    raise SystemExit(0)

HARNESS = r"""
#include <cstdio>
#include <cmath>
#include <cstring>
#define GOTO_ARRIVE_STEPS 10

struct Plan { float spd[4], acc[4], decel[4]; float t_move; };

// The shared planner, as both paths now implement it.
static Plan plan(const float dist[4], const float pspd[4], const float pacc[4]) {
    Plan p{}; p.t_move = 0.f;
    for (int i = 0; i < 4; i++)
        if (dist[i] > (float)GOTO_ARRIVE_STEPS) {
            float t = dist[i] / pspd[i];
            if (t > p.t_move) p.t_move = t;
        }
    for (int i = 0; i < 4; i++) {
        if (p.t_move > 0.f && dist[i] > (float)GOTO_ARRIVE_STEPS) {
            float k = (dist[i] / pspd[i]) / p.t_move;
            p.spd[i] = fmaxf(1.f, pspd[i] * k);
            p.acc[i] = fmaxf(1.f, pacc[i] * k);
        } else { p.spd[i] = pspd[i]; p.acc[i] = pacc[i]; }
        float full = (p.acc[i] > 0.f) ? (p.spd[i]*p.spd[i])/(2.f*p.acc[i]) : p.spd[i];
        p.decel[i] = (dist[i] > (float)GOTO_ARRIVE_STEPS && dist[i] < full)
                     ? fmaxf(dist[i], (float)(GOTO_ARRIVE_STEPS*2)) : full;
    }
    return p;
}

int main() {
    // The 16:22 recall from the rig: pan dominant, zoom short, slider long.
    float dist[4] = { 213952.f, 65495.f, 58926.f, 502.f };
    float pspd[4] = {  53333.f, 21550.f, 12800.f, 711.f };
    float pacc[4] = {  53333.f, 21550.f, 12800.f, 711.f };
    Plan a = plan(dist, pspd, pacc);

    int bad = 0;
    // Every moving axis must finish in the same time.
    for (int i = 0; i < 4; i++) {
        if (dist[i] <= GOTO_ARRIVE_STEPS) continue;
        float t = dist[i] / a.spd[i];
        if (fabsf(t - a.t_move) > 0.01f) { bad++; printf("DIFF axis %d t=%.3f vs %.3f\n", i, t, a.t_move); }
    }
    printf("t_move %.3fs; axis times equal: %s\n", a.t_move, bad ? "NO" : "yes");

    // The decel window must track the speed actually set.  The old bug was a
    // window sized for a different speed: check the v^2 relationship holds.
    for (int i = 0; i < 4; i++) {
        if (dist[i] <= GOTO_ARRIVE_STEPS) continue;
        float want = (a.spd[i]*a.spd[i])/(2.f*a.acc[i]);
        float got  = a.decel[i];
        bool capped = (dist[i] < want);
        if (!capped && fabsf(got - want) > 0.5f) { bad++; printf("DIFF decel axis %d %.1f vs %.1f\n", i, got, want); }
    }
    printf("decel windows match their speeds: %s\n", bad ? "NO" : "yes");

    // And the bug it replaced: a window computed for 109 steps/s, used at 711.
    float slow = 109.f, fast = 711.f, acc = 711.f;
    float w_slow = (slow*slow)/(2.f*acc), w_fast = (fast*fast)/(2.f*acc);
    printf("stale-window error would have been %.0fx too small\n", w_fast / w_slow);
    return bad;
}
"""
d = pathlib.Path(tempfile.mkdtemp())
(d / "g.cpp").write_text(HARNESS)
r = subprocess.run(["c++", "-std=c++17", "-O2", "-o", str(d / "g"), str(d / "g.cpp")],
                   capture_output=True, text=True)
assert r.returncode == 0, f"harness did not compile:\n{r.stderr[:700]}"
r = subprocess.run([str(d / "g")], capture_output=True, text=True)
print("\n2. the planner arithmetic:")
for line in r.stdout.strip().splitlines():
    print(f"   {line}")
assert r.returncode == 0, "the planner does not synchronise or sizes the window wrongly"

print("\nALL CHECKS PASSED")
