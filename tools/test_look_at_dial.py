"""The pan/tilt dial sets how fast a look-at subject switch, and the swing
before a look-at move, turn the camera — and tracking never falls behind.

Asked on 2026-11-18, after the Foyer event: switching subject mid look-at
"was too fast to use on a live camera. Could the pan-tilt speed control dial
control the subject change speed also?"  It could not: a switch mid-move was a
fixed 55 ms per degree — 18 deg/s average, 27 peak, whatever the dial said —
while the same switch with the slider parked has always run at the dial.  The
swing to the subject before a move was worse, the axis's full 47 deg/s.

And the operator's worry: "too slow a pan speed may result in the pan never
reaching the subject depending on slide speed and subject position."  The
answer is to let the dial time only the SWITCH.  The rail moving still turns
the camera at whatever rate the geometry demands, on top of the switch, so
during a switch the controller keeps the speed cap and the acceleration that
tracking has.  The switch always lands, at the time the dial sets, because both
ends of the blend are recomputed from the rail position every tick.

  1. The law: the blend is as long as the dial's speed AND acceleration need.
  2. The Teensy sketch keeps the motion code's copy of the dial current.
  3. The swing before a move runs at the dial, pan and tilt landing together.
  4. A model of the controller, closed-loop, on a deliberately hard case: with
     tracking's allowances it follows and lands; without them it falls behind.

Run directly, or via tools/run_tests.sh with the rest.
"""
import pathlib, re, math

REPO = pathlib.Path(__file__).resolve().parent.parent
CPP = (REPO / "firmware/teensy41_mount/MountMotion.cpp").read_text()
HDR = (REPO / "firmware/teensy41_mount/MountMotion.h").read_text()
INO = (REPO / "firmware/teensy41_mount/teensy41_mount.ino").read_text()


def body(src, signature):
    start = src.index(signature)
    return re.sub(r"//[^\n]*", "", src[start:src.index("\n}\n", start)])


def define(name):
    m = re.search(rf"#define {name}\s+([\d.]+)f?", HDR)
    assert m, f"{name} not found"
    return float(m.group(1))


PRESETS = [(float(v), float(a)) for v, a in re.findall(
    r"\{\s*(\d+)\s*,\s*(\d+)\s*\}",
    re.search(r"DEFAULT_PT_PRESETS\[5\]\s*=\s*\{(.*?)\};", CPP, re.S).group(1))][1:]
assert len(PRESETS) == 4, "the default pan/tilt presets are not where this test looks"


def switch_s(d, v, a):
    """_switchMs, in seconds."""
    return max(1.5 * d / v, math.sqrt(6.0 * d / a))


# ---- 1. the law ---------------------------------------------------------------
print("1. how long a switch takes at the dial:")
sw = body(CPP, "float MountMotion::_switchMs(")
for want in ("const SpeedPreset &dial = _pt_presets[_la_pt_preset];",
             "float t_speed = 1.5f * travel / v;",
             "float t_accel = sqrtf(6.0f * travel / a);",
             "return 1000.0f * fmaxf(t_speed, t_accel);"):
    assert want in sw, f"_switchMs is not the law this test models: missing {want!r}"
assert "if (v <= 0.0f || a <= 0.0f) {" in sw and "travel * LOOK_AT_BLEND_MS_PER_DEG" in sw, \
    "a dial with no usable speed divides by zero instead of falling back"
for n, (v, a) in enumerate(PRESETS, 1):
    for d in (1, 5, 15, 40, 90):
        t = switch_s(d, v, a)
        peak_v, peak_a = 1.5 * d / t, 6.0 * d / (t * t)
        assert peak_v <= v + 1e-9, f"dial {n}, {d}°: peaks at {peak_v:.2f} deg/s"
        assert peak_a <= a + 1e-9, f"dial {n}, {d}°: accelerates at {peak_a:.2f} deg/s^2"
        assert abs(peak_v - v) < 1e-9 or abs(peak_a - a) < 1e-9, \
            f"dial {n}, {d}°: longer than either limit needs"
    print(f"   dial {n} ({v:.0f} deg/s, {a:.0f} deg/s^2): a 15° switch takes "
          f"{switch_s(15, v, a):.1f} s (was {15 * define('LOOK_AT_BLEND_MS_PER_DEG') / 1000:.2f})")
print("   never faster than the dial's speed or acceleration, and no slower  OK")

# ---- 2. the sketch keeps the dial current ------------------------------------
print("\n2. the Teensy sketch:")
assigns = [m.start() for m in re.finditer(r"\b_active_pt_preset\s*=\s*(?!=)", INO)]
decl = INO.index("static uint8_t _active_pt_preset = 2;")
for at in assigns:
    if at == decl + len("static uint8_t "):
        continue
    tail = INO[at:at + 400]
    assert "mount.setActivePtPreset(_active_pt_preset);" in tail[:tail.find("break;")], \
        "the dial is changed somewhere without telling the motion code"
setup = INO[INO.index("    eeprom_load();\n"):]
assert "mount.setActivePtPreset(_active_pt_preset);" in setup[:600], \
    "at boot the motion code's dial is left to two defaults agreeing"
assert "_la_pt_preset = constrain(preset, 1, 4);" in HDR, "the dial is not bounded 1-4"
print("   mirrored on every change and at boot                         OK")

# ---- 3. the swing before a move ------------------------------------------------
print("\n3. the swing to the subject before a move:")
start = body(CPP, "bool MountMotion::startLookAtMove(")
pre = start[start.index("PRE-AIM skipped"):start.index("_state = STATE_LOOK_AT_PRE_AIM;")]
for want, why in (
        ("const SpeedPreset &dial = _pt_presets[_la_pt_preset];", "the swing does not read the dial"),
        ("fminf(v_deg / _pan_deg_per_step,  _la_max_steps_s[0])", "the swing can ask past the axis ceiling"),
        ("setMaxSpeed((int32_t)fmaxf(1.0f, pan_v * k_pan))", "pan's speed is not synced"),
        ("setAcceleration((uint32_t)fmaxf(1.0f, pan_a * k_pan))", "pan's acceleration is not synced"),
        ("setAcceleration((uint32_t)fmaxf(1.0f, tilt_a * k_tilt))", "tilt's acceleration is not synced")):
    assert want in pre, why
assert "* 3.0f" not in pre and "_la_max_steps_s[0];" not in pre, \
    "the swing is the old full-speed snap again"


def trapezoid_s(d, v, a):
    return d / v + v / a if d >= v * v / a else 2.0 * math.sqrt(d / a)


v, a = PRESETS[2]                        # dial 3
for pan_d, tilt_d in ((30.0, 4.0), (2.0, 9.0), (12.0, 12.0)):
    t_move = max(pan_d / v, tilt_d / v)
    k_p, k_t = (pan_d / v) / t_move, (tilt_d / v) / t_move
    tp, tt = trapezoid_s(pan_d, v * k_p, a * k_p), trapezoid_s(tilt_d, v * k_t, a * k_t)
    assert abs(tp - tt) < 1e-9, f"pan {pan_d}°/tilt {tilt_d}° land {abs(tp - tt):.3f} s apart"
print("   at the dial, synced: pan and tilt land together               OK")

# ---- 4. closed loop: tracking keeps up through a slow switch ------------------
# A model of _driveTowardTarget on the pan axis, at its 20 ms tick, with the
# stepper ramping at the acceleration it is given.  The case is chosen to be
# hard: subjects 0.7-0.8 m from the rail, the slider at 80 mm/s, and a dial-2
# switch from one AHEAD to one BEHIND, so the switch and the tracking turn the
# camera the same way and add.  Tracking alone peaks near 6 deg/s here; a
# 3 m subject at the same speed needs under 2.
print("\n4. closed loop, slow switch on a fast slide:")
drive = CPP[CPP.index("void MountMotion::_driveTowardTarget("):]
drive = drive[:drive.index("\n}\n")]
FAST = float(re.search(r"LOOK_AT_BRAKE_FACTOR\s*=\s*([\d.]+)f", drive).group(1))
SLOW = float(re.search(r"LOOK_AT_SLEW_BRAKE_FACTOR\s*=\s*([\d.]+)f", drive).group(1))
BOOST = float(re.search(r"LOOK_AT_SLEW_ACCEL_BOOST\s*=\s*([\d.]+)f", drive).group(1))
assert "float blend_brake  = fmaxf(_la_blend_brake, LOOK_AT_BRAKE_FACTOR);" in drive
DPS = 0.9 / (256 * (270 / 36))
MAX = define("AXIS_MAX_STEPS_S") * DPS
KP = define("LOOK_AT_KP_STEPS") * DPS
HEAD, AHEAD = define("LOOK_AT_BLEND_HEADROOM"), define("LOOK_AT_ACCEL_HEADROOM")
TRACK_MIN, SETTLE = define("LOOK_AT_TRACK_ACCEL_MIN"), define("LOOK_AT_SLEW_SETTLE_MS") / 1000
MIN_S = define("LOOK_AT_BLEND_MIN_MS") / 1000


def run(floors, dial, s1, s2, v_slide, x0, t_switch, extra=4.0):
    pan = lambda s, x: math.degrees(math.atan2(s[0] - x, s[1]))
    x_at = lambda t: x0 + v_slide * t
    x_sw = x_at(t_switch)
    travel = abs(pan(s2, x_sw) - pan(s1, x_sw))
    T = max(switch_s(travel, *dial), MIN_S)
    peak = 1.5 * travel / T
    blend_cap = min(peak * HEAD / MAX, 1.0)
    acc_lim = AHEAD * 6.0 * travel / (T * T)
    if floors:
        blend_cap = max(blend_cap, FAST)
        acc_lim = max(acc_lim, TRACK_MIN)

    def setpoint(t):
        x = x_at(t)
        if t < t_switch:
            return pan(s1, x)
        u = min((t - t_switch) / T, 1.0)
        e = u * u * (3 - 2 * u)
        return pan(s1, x) + e * (pan(s2, x) - pan(s1, x))

    cam, vel, t, dt = setpoint(0.0), 0.0, 0.0, 0.001
    # start already tracking, moving with the subject
    vel = (setpoint(0.001) - setpoint(0.0)) / 0.001
    worst, cmd, stepper_acc, end = 0.0, vel, 1.0, t_switch + T + extra
    while t < end:
        if round(t * 1000) % 20 == 0:          # the 20 ms control tick
            err = setpoint(t) - cam
            in_switch = t >= t_switch
            if not in_switch:
                cap, lim, slew = FAST, None, abs(err) / DPS > 1250
            else:
                since = t - (t_switch + T)
                if since < 0:
                    cap = blend_cap
                else:
                    fixed = SLOW if since < SETTLE else FAST
                    u = min(since / SETTLE, 1.0)
                    cap = blend_cap + (fixed - blend_cap) * u * u * (3 - 2 * u) \
                        if since < SETTLE else fixed
                lim, slew = acc_lim, since < SETTLE or abs(err) / DPS > 1250
            vmaxb = MAX * cap
            acc = vmaxb * MAX / (2 * KP)
            if lim is not None and acc > lim:
                acc = lim
            stepper_acc = acc * BOOST if slew else acc
            v_p = abs(err) / KP * MAX
            v_sqrt = min(math.sqrt(2 * acc * abs(err)), vmaxb)
            cmd = math.copysign(min(v_p, v_sqrt), err)
        step = stepper_acc * dt                # the motor ramps to the command
        vel = cmd if abs(cmd - vel) <= step else vel + math.copysign(step, cmd - vel)
        cam += vel * dt
        t += dt
        if t >= t_switch:
            worst = max(worst, abs(setpoint(t) - cam))
    return travel, T, worst, abs(setpoint(t) - cam)


S1, S2 = (300.0, 800.0), (-400.0, 700.0)     # ahead-right, then behind-left
DIAL2 = PRESETS[1]
travel, T, worst_new, final_new = run(True,  DIAL2, S1, S2, 80.0, -400.0, 5.0)
_,      _, worst_old, final_old = run(False, DIAL2, S1, S2, 80.0, -400.0, 5.0)
print(f"   {travel:.0f}° switch at dial 2 takes {T:.1f} s while the rail moves "
      f"{80 * T / 1000:.1f} m")
print(f"   with tracking's allowances:    worst following error {worst_new:5.2f}°, "
      f"then {final_new:.3f}° off")
print(f"   capped at the dial's peak only: worst following error {worst_old:5.2f}°, "
      f"then {final_old:.3f}° off")
# The P-loop on its own trails anything moving by speed / 20 per second of
# gain — about half a degree here — so that is the bar, not zero.
assert worst_new < 0.6, \
    f"the camera trails the switch by {worst_new:.2f}° even with tracking's room"
assert final_new < 0.05, f"it does not land on the new subject ({final_new:.3f}° off)"
assert worst_old > 4 * worst_new and worst_old > 1.0, \
    "without the allowances it no longer falls behind — this case no longer\n" \
    "    shows why they are there"
print("   follows the whole way and lands; without the allowances it trails  OK")

# And the ordinary case, the Foyer's: subjects ~3 m out, a fast dial.
travel, T, worst, final = run(True, PRESETS[3], (860.0, 3495.0), (-97.0, 3165.0),
                              40.0, -800.0, 4.0)
assert worst < 0.6 and final < 0.05, (worst, final)
print(f"   the Foyer's subjects at dial 4: {travel:.0f}° in {T:.1f} s, "
      f"worst {worst:.2f}°                OK")

print("\nALL CHECKS PASSED")
