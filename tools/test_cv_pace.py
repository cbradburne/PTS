"""CV tracking closes the last few pixels — and does not overshoot doing it.

2026-10-01, the operator's first tracking test on the new loop: the mount kept
up with them walking, but "the last bit" took seconds.  The drive asked for
error^1.5 of the preset's top speed, and the Teensy then puts every jog
through the joystick's curve, 0.3x + 0.7x^3, soft in the middle for a thumb.
Together, a speed that died away near the target: simulated, a 40 px error was
still more than 8 px out after 30 s.  The drive now asks for a speed
proportional to the error, undoes the joystick's curve, and never asks for more
than the mount could stop from.

WHAT THIS TEST IS PROTECTING.

  the curve undone       the PC's copy of the Teensy's joystick curve is the
                         firmware's (MountMotion.cpp), and its inverse is exact
  proportional           near the target the speed falls in proportion to the
                         error, the same in deg/s whatever the preset, the same
                         for a pixel of tilt as for a pixel of pan; nothing is
                         sent inside the deadband
  the last bit closes    simulated, mount and delay: 40 px closes to 8 in under
                         3 s at the operator's framing (the old law: not in 10)
  and does not overshoot a tight shot with a long delay, steps of 40, 100 and
                         400 px: less than 30 px past (without the braking
                         cap, 75-150 px)
  the CV window          hands over the mount's configured presets; its gain
                         sliders reach the loop while tracking
  the approach, logged   the CV TIMING line's distance from the target is to the
                         target the operator set; crossing it counts overshoots,
                         not pose jitter
  joystick untouched     only CV's jogs are reshaped — the joystick's dispatcher
                         still sends its stick as it always did

Run directly, or via tools/run_tests.sh with the rest.
"""
import math
import os
import pathlib
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

from PyQt6.QtWidgets import QApplication                        # noqa: E402

app = QApplication.instance() or QApplication([])

import cv.tracking_loop as tl                                   # noqa: E402

FRAME_W = 1280

# ---- 1. the joystick curve, as the Teensy has it, undone exactly -------------------
print("1. the Teensy's joystick curve:")
fw = (REPO / "firmware/teensy41_mount/MountMotion.cpp").read_text()
m = re.search(r"JOG_EXPO_STRENGTH\s*=\s*([\d.]+)f?\s*;", fw)
assert m, "JOG_EXPO_STRENGTH not found in MountMotion.cpp"
assert abs(float(m.group(1)) - tl.JOG_EXPO_STRENGTH) < 1e-9, \
    f"the Teensy's joystick curve is {m.group(1)}, the PC undoes {tl.JOG_EXPO_STRENGTH} — " \
    "CV would ask for one speed and get another"
body = re.search(r"jogExpo\(float x\)\s*\{(.*?)\n\}", fw, re.S)
assert body and re.search(r"\(1\.0f\s*-\s*JOG_EXPO_STRENGTH\)\s*\*\s*x", body.group(1)) and \
    re.search(r"JOG_EXPO_STRENGTH\s*\*\s*x\s*\*\s*x\s*\*\s*x", body.group(1)), \
    "jogExpo is no longer (1-k)x + kx^3 — the PC's inverse undoes a curve the Teensy no longer has"
for s in [i / 200 for i in range(201)]:
    x = tl.jog_for_speed(s)
    assert 0.0 <= x <= 1.0 and abs(tl.jog_expo(x) - s) < 1e-9, (s, x, tl.jog_expo(x))
print(f"   {tl.JOG_EXPO_STRENGTH} in the firmware and here; the inverse exact over 0-1   OK")


def speed(jog, vmax):
    """deg/s the Teensy runs a jog at."""
    return math.copysign(tl.jog_expo(abs(jog) / tl.MAX_JOG_VALUE) * vmax, jog)


# ---- 2. proportional, preset-free, square pixels, a deadband ------------------------
print("\n2. the speed asked for:")
dead = tl.DEADBAND_FRACTION * FRAME_W
assert tl.axis_jog(dead - 0.5, 2.0, FRAME_W, 5, 5) == 0 and \
    tl.axis_jog(-(dead - 0.5), 2.0, FRAME_W, 5, 5) == 0, "a jog inside the deadband"
v40 = speed(tl.axis_jog(40, 2.0, FRAME_W, 5, 5), 5)
v20 = speed(tl.axis_jog(20, 2.0, FRAME_W, 5, 5), 5)
want = (40 - dead) / (20 - dead)
assert abs(v40 / v20 - want) < 0.05 * want, \
    f"40 px ran {v40:.3f} deg/s and 20 px {v20:.3f}: {v40 / v20:.1f}x, not {want:.1f}x — " \
    "the speed is not falling in proportion to the error near the target"
v40_p3 = speed(tl.axis_jog(40, 2.0, FRAME_W, 10, 10), 10)
assert abs(v40_p3 - v40) < 0.03 * v40, \
    f"40 px ran {v40:.3f} deg/s at preset 2 and {v40_p3:.3f} at preset 3 — the near-target " \
    "speed should not depend on the preset, only the top speed should"
assert tl.axis_jog(-40, 2.0, FRAME_W, 5, 5) == -tl.axis_jog(40, 2.0, FRAME_W, 5, 5), "not symmetric"


class St:
    active_pt_preset = 2


class FakeMM:
    def __init__(self):
        self.jogs = []

    def state(self, m):
        return St()

    def send_jog(self, mid, pan, tilt, *a, **k):
        self.jogs.append((pan, tilt))


class Cap:
    def get_frame(self):
        return None


class NoYolo:
    available, _imgsz, model_name = False, 320, "none"

    def detect(self, frame):
        return []


loop = tl.TrackingLoop(FakeMM(), Cap(), detector=NoYolo())
loop._timer.stop()
loop._frame_w, loop._frame_h = FRAME_W, 720
loop._target_cx = loop._target_cy = 0.0
loop._drive(50.0, 50.0)                      # 50 px right of the target, 50 px below
pan, tilt = loop._mm.jogs[-1]
assert pan > 0 and pan == -tilt, \
    f"50 px of pan and 50 px of tilt jogged {pan} and {-tilt}: tilt was measured against the " \
    "frame's height, 1.8x as hard as pan for the same error"
# ...at the preset the loop is told the mount runs, not a guess.
loop.set_pt_preset_speeds(lambda mid, p: (10.0, 10.0))
loop._jog_due = 0.0
loop._drive(50.0, 0.0)
assert loop._mm.jogs[-1][0] == tl.axis_jog(50.0, 2.0, FRAME_W, 10.0, 10.0) != pan, \
    f"told the preset runs at 10 deg/s, the drive jogged {loop._mm.jogs[-1][0]} — as if it " \
    "were still 5"
loop.shutdown()
print(f"   in proportion ({v40:.2f} deg/s at 40 px, {v20:.2f} at 20, either preset); a pixel of\n"
      "   tilt the same as a pixel of pan; nothing inside the deadband   OK")

# ---- 3. the last bit closes, and nothing sails past: a simulated mount -------------
# The pan axis: the loop jogs at 20 Hz on what it saw, a jog acts 30 ms later
# — `lat` from the picture to the motor in all — and the motor ramps to each
# new speed at the preset's acceleration (MountMotion.cpp's rotateAsync,
# overrideSpeed and stopAsync).
print("\n3. a simulated mount:")


def old_law(e_px, vmax, accel):
    n = max(-1.0, min(1.0, e_px / (FRAME_W / 2)))
    return int(max(-1000, min(1000, math.copysign(abs(n) ** 1.5, n) * 1000 * 2.0)))


def new_law(e_px, vmax, accel):
    return tl.axis_jog(e_px, 2.0, FRAME_W, vmax, accel)


def simulate(law, step_px, pxd, vmax, accel, lat, secs):
    """Returns (seconds until within 8 px for good or None, worst overshoot px)."""
    dt, th, v, t = 0.001, 0.0, 0.0, 0.0
    target = step_px / pxd
    seen, pending, jog, next_jog = [], [], 0, 0.0
    last_out, over = 0.0, 0.0
    while t < secs:
        e = (target - th) * pxd
        seen.append(e)
        if t >= next_jog:
            next_jog += 0.05
            k = max(0, len(seen) - 1 - int((lat - 0.03) / dt))
            pending.append((t + 0.03, law(seen[k], vmax, accel)))
        while pending and pending[0][0] <= t:
            jog = pending.pop(0)[1]
        want = speed(jog, vmax) if jog else 0.0
        v += max(-accel * dt, min(accel * dt, want - v))
        th += v * dt
        if abs(e) > 8:
            last_out = t
        if e < 0:
            over = max(over, -e)
        t += dt
    settled = None if last_out >= secs - dt * 2 else last_out
    return settled, over


s_new, _ = simulate(new_law, 40, 70, 5, 5, 0.13, 10)
s_old, _ = simulate(old_law, 40, 70, 5, 5, 0.13, 10)
assert s_new is not None and s_new < 3.0, \
    f"40 px took {s_new} s to close to 8 at 70 px/deg — the last bit is slow again"
assert s_old is None, "the old law closed 40 px inside 10 s, so this test no longer shows " \
                      "the fault it was written for"
worst = 0.0
for vmax, accel in ((5, 5), (10, 10)):
    for step in (40, 100, 400):
        settled, over = simulate(new_law, step, 150, vmax, accel, 0.25, 12)
        assert settled is not None, f"a {step} px step never settled at preset {vmax} deg/s"
        worst = max(worst, over)
assert worst < 30, \
    f"on a tight shot (150 px/deg) with a 0.25 s delay the mount sailed {worst:.0f} px past — " \
    "it was asked for speeds it could not stop from"
print(f"   40 px closes in {s_new:.1f} s (the old law: not in 10); a tight shot with a long\n"
      f"   delay goes at most {worst:.0f} px past   OK")

# ---- 4. the CV window: the mount's presets, live sliders -------------------------
print("\n4. the CV window:")
from config.mount_config import AppConfig, SpeedPreset         # noqa: E402
from ui.cv_window import CVWindow                               # noqa: E402
import cv.capture                                               # noqa: E402

# The window lists capture devices as it is built, opening each in turn; a
# test has no business with the cameras.
cv.capture.CaptureSource.list_devices = staticmethod(lambda max_test=5: [])

cfg = AppConfig()
cfg.mount(1).pan_tilt_presets.set(3, SpeedPreset(12, 7))
win = CVWindow(1, FakeMM(), cfg)
win._starting = True
win._on_feed_opened(True, NoYolo())          # the feed's open, as its worker reports it
lp = win._tracking_loop
assert lp is not None, "the feed opened and no tracking loop was made"
assert lp._pt_preset_speed(1, 3) == (12.0, 7.0), \
    f"the loop runs on {lp._pt_preset_speed(1, 3)} for preset 3, not the mount's configured " \
    "(12, 7) — it would ask for speeds against the wrong top speed"
win._pan_gain.setValue(35)
assert lp._gain_pan == 3.5, f"moving the pan slider while tracking left the loop at {lp._gain_pan}"
win._tilt_gain.setValue(15)
assert lp._gain_tilt == 1.5, f"moving the tilt slider while tracking left the loop at {lp._gain_tilt}"
win._stop_feed()
win.close()
print("   the configured preset (12 deg/s, 7 deg/s^2) handed to the loop; sliders live   OK")

# ---- 4b. what the CV TIMING line counts as crossing the target --------------------
# Neither jogs nor the mount's position are logged, so the line is the only
# record of how the mount closes on a person: an overshoot shows as the aim
# crossing the target.  Pose jitter about it must not.
print("\n4b. crossing the target:")
lp2 = tl.TrackingLoop(FakeMM(), Cap(), detector=NoYolo())
lp2._timer.stop()
lp2._frame_w, lp2._frame_h = FRAME_W, 720
for ex in (40, 25, 8, -6, 7, -8):            # closing in, then jitter at the target
    lp2._note_aim(ex, 0.0)
assert lp2._stats.crossings == 0, \
    f"a few pixels of jitter about the target counted as {lp2._stats.crossings} crossings"
for ex in (-30, -40, 20):                    # past it, and back
    lp2._note_aim(ex, 0.0)
assert lp2._stats.crossings == 2, \
    f"past the target and back again counted {lp2._stats.crossings} crossings, not 2"
lp2._note_aim(20.0, -30.0)
lp2._note_aim(20.0, 30.0)                    # tilt crosses too
assert lp2._stats.crossings == 3, "a crossing in tilt was not counted"
lp2.set_target(100.0, -50.0)                 # the operator dragged the target
lp2._anchor_half_h, lp2._head_off = 150.0, None
lp2._drive_on((600, 200, 80, 300))           # aims 15% down the box: (640, 245)
want = math.hypot(640 - (640 + 100), 245 - (360 - 50))
assert abs(lp2._stats.aim_err[-1] - want) < 0.5, \
    f"the aim was {want:.0f} px from the target the operator set; the line would say " \
    f"{lp2._stats.aim_err[-1]:.0f}"
lp2.shutdown()
print("   jitter at the target: none; past it and back: two; tilt counts too   OK")

# ---- 5. the joystick is not reshaped ------------------------------------------------
print("\n5. the joystick:")
disp = (REPO / "pc_app/motion/command_dispatcher.py").read_text()
assert "jog_for_speed" not in disp and "axis_jog" not in disp and "tracking_loop" not in disp, \
    "the joystick's dispatcher now goes through CV's drive law"
print("   the dispatcher sends its stick as before; only CV's jogs are reshaped   OK")

print("\nALL CHECKS PASSED")
