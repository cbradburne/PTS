"""Limit-find must start gently, and must not travel far unable to see a stall.

Two numbers pull against each other and it is easy to move one and make the
other quietly worse.

  ACCELERATION has to be low enough that the slider starts from a dead stop
  UPHILL, at 75% current, without juddering. On 2026-08-24 two of three limit
  finds halted that way at 40 mm/s² — a figure chosen when the rail was level.

  The SETTLE GUARD has to outlast the acceleration ramp, because StallGuard
  means nothing while the axis is still accelerating. A static_assert in the
  firmware enforces that much.

What nothing enforced is the consequence: the settle guard is a window in which
a stall CANNOT be detected, so the carriage travels through it blind. Halve the
acceleration on its own and the ramp doubles, the settle has to grow to match,
and the blind window grows with it — a change that reads as "gentler" while
making the mount MORE likely to grind into a stop it started near.

Easing the speed alongside the acceleration is what avoids that. This file
checks the blind distance directly, in millimetres, so the trade cannot be lost.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent

CPP = (REPO / "firmware/teensy41_mount/MountMotion.cpp").read_text()


def const(name: str) -> int:
    m = re.search(rf"{name}\s*=\s*(\d+);", CPP)
    assert m, f"{name} not found"
    return int(m.group(1))


MM_PER_STEP = 40.0 / (32 * 200)          # SLIDER_MM_PER_REV / (µsteps × full steps)

speed  = const("LIMIT_FIND_SPEED")
accel  = const("LIMIT_FIND_ACCEL")
settle = const("LIMIT_STALL_SETTLE_MS")

v_mm, a_mm = speed * MM_PER_STEP, accel * MM_PER_STEP
ramp_s     = v_mm / a_mm
blind_s    = settle / 1000.0

print("1. the limit-find ramp:")
print(f"   speed {v_mm:5.1f} mm/s   accel {a_mm:5.1f} mm/s²   ramp {ramp_s:.2f} s")

# ---- 1. gentle enough to start uphill --------------------------------------
assert a_mm <= 20.0, \
    f"limit-find accelerates at {a_mm:.0f} mm/s²; the slider is observed to judder\n" \
    f"    starting uphill above 20 mm/s²"
print(f"   {a_mm:.0f} mm/s² — within what starts cleanly uphill      OK")

# ---- 2. fast enough for StallGuard to mean anything ------------------------
rpm = speed / (32 * 200) * 60
assert rpm >= 25.0, \
    f"{rpm:.0f} RPM is too close to the ~20 RPM below which SG_RESULT is unusable"
print(f"   {rpm:.0f} RPM — clear of the ~20 RPM StallGuard floor    OK")

# ---- 3. the settle guard outlasts the ramp ---------------------------------
assert blind_s > ramp_s, \
    f"settle {blind_s:.2f} s does not outlast the {ramp_s:.2f} s ramp — StallGuard\n" \
    f"    would watch while the axis is still accelerating"
assert "static_assert(LIMIT_STALL_SETTLE_MS" in CPP, \
    "the firmware no longer enforces settle > ramp at compile time"
print(f"   settle {blind_s:.2f} s > ramp {ramp_s:.2f} s, and asserted in C++   OK")

# ---- 4. and the blind window has not grown ---------------------------------
# Distance covered while a stall cannot be detected. Start near a stop and this
# is how much rail the carriage can grind through without noticing.
if blind_s > ramp_s:
    blind_mm = 0.5 * a_mm * ramp_s**2 + (blind_s - ramp_s) * v_mm
else:
    blind_mm = 0.5 * a_mm * blind_s**2

print(f"\n2. travel with StallGuard blind: {blind_mm:.0f} mm")
assert blind_mm <= 30.0, \
    f"{blind_mm:.0f} mm travelled before a stall can be seen. The previous\n" \
    f"    settings managed 42 mm and that was already the thing being fixed;\n" \
    f"    if the accel came down without easing the speed, this is the cost."
print(f"   under 30 mm — better than the 42 mm it replaced      OK")

# The pairing is the whole point: a future edit that drops accel alone will
# fail the check above rather than passing quietly.
print(f"   speed and accel eased together, not accel alone      OK")

print("\nALL CHECKS PASSED")
