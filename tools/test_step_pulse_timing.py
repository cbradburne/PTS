"""The step ISR must have time to run between one pulse and the next.

Reported on 2026-08-26: the pan motor audibly skips a step or two during a
subject switch. The operator's own diagnosis, and it was right: computational
latency, the same class of fault as the delayMicroseconds(5)-per-step that was
removed from rotISR() earlier.

The step pin is driven by a hardware timer. Each step is two ISR invocations —
one to raise the pin, one to lower it — and everything the driver has to do
between the pulse FALLING and the next one RISING has to fit in whatever is
left of the period. rotISR() does a sqrtf, an updateFrequency division and
doStep(), with up to three other motors' ISRs competing for the same core.

Stock TeensyStep4 asks for an 8 us pulse:

    stpTimer->setPulseParams(8, stepPin);

At vMaxMax = 100,000 steps/s the period is 10 us, so that left 1.89 us. The
STEP line was HIGH 81% of the time. Anything that overran cost a step, and
these axes have no encoder, so nothing afterwards knows the mount's idea of its
own position has moved — it just silently stops matching the world.

A TMC2209 needs about 100 ns of STEP high time and about 20 ns of DIR setup.
Both waits were hundreds of times longer than the hardware asks for, and both
were being paid out of a budget measured in microseconds.

This checks the timing arithmetic against the library as it is actually
compiled — the copy in libraries/, which is what tools/build.sh passes to
arduino-cli, NOT whatever happens to be installed in the user's Arduino folder.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
LIB = REPO / "libraries/TeensyStep4/src"

BASE_C = (LIB / "stepperbase.cpp").read_text()
BASE_H = (LIB / "stepperbase.h").read_text()
TMR = (LIB / "timers/Teensy4/TMR/TMR.h").read_text()
STEPPER_H = (LIB / "stepper.h").read_text()

# ---- 1. the library the build actually uses --------------------------------
print("1. which copy this is:")
build = (REPO / "tools/build.sh").read_text()
assert 'LIBS="$REPO/libraries"' in build and '--libraries "$LIBS"' in build, \
    "build.sh no longer compiles against libraries/ — this file is checking a\n" \
    "    copy that does not reach the firmware"
print("   libraries/, which build.sh passes to arduino-cli           OK")


def number(pattern, text, what):
    m = re.search(pattern, text)
    assert m, f"{what} not found"
    return float(m.group(1).replace("'", ""))


# ---- 2. the numbers that set the budget ------------------------------------
print("\n2. the timing:")
PRESCALE = number(r"prescale\s*=\s*(\d+)", TMR, "timer prescale")
VMAXMAX = number(r"vMaxMax\s*=\s*([\d']+)", STEPPER_H, "vMaxMax")
PULSE_US = number(r"STEP_PULSE_US\s*=\s*([\d.]+)f?", BASE_C, "STEP_PULSE_US")
DIR_US = number(r"DIR_SETTLE_US\s*=\s*([\d.]+)", BASE_H, "DIR_SETTLE_US")

# setPulseParams(): pulsewidth = width_us * (150.0f / prescale) + 0.5, in ticks
tick_us = PRESCALE / 150.0
pulse_ticks = int(PULSE_US * (150.0 / PRESCALE) + 0.5)
pulse_us = pulse_ticks * tick_us
period_us = 1e6 / VMAXMAX
slack_us = period_us - pulse_us

print(f"   timer tick {tick_us * 1000:.1f} ns, pulse {pulse_ticks} ticks "
      f"= {pulse_us:.2f} us")
print(f"   at vMaxMax {VMAXMAX:,.0f}/s the period is {period_us:.2f} us")
print(f"   leaving {slack_us:.2f} us for the ISR")

# The stock 8 us is what this exists to prevent coming back.
assert PULSE_US < 8.0, \
    "the stock 8 us pulse is back; at the top step rate that leaves 1.89 us\n" \
    "    for the whole step ISR and the motor loses steps"
assert slack_us > period_us / 2, \
    f"the pulse takes {pulse_us / period_us:.0%} of the period; the ISR needs\n" \
    "    the majority of it, not the remainder"
print("   the ISR gets the majority of the period                    OK")

# ---- 3. and still long enough for the driver -------------------------------
# Shortening it is only safe while it stays well clear of what a TMC2209 needs.
print("\n3. still within the driver's spec:")
TMC_STEP_MIN_US = 0.1      # TMC2209 minimum STEP high time, ~100 ns
TMC_DIR_MIN_US = 0.02      # TMC2209 DIR-to-STEP setup, ~20 ns
assert pulse_us > TMC_STEP_MIN_US * 5, \
    f"a {pulse_us:.2f} us pulse is too close to the TMC2209's {TMC_STEP_MIN_US} us\n" \
    "    minimum — steps would be missed at the driver instead of the timer"
assert DIR_US > TMC_DIR_MIN_US * 5, "the DIR settle is below the driver's spec"
print(f"   STEP high {pulse_us:.2f} us vs {TMC_STEP_MIN_US} us needed "
      f"({pulse_us / TMC_STEP_MIN_US:.0f}x)      OK")
print(f"   DIR setup {DIR_US:.2f} us vs {TMC_DIR_MIN_US} us needed "
      f"({DIR_US / TMC_DIR_MIN_US:.0f}x)      OK")

# ---- 4. the busy-wait inside the ISR fits in the budget --------------------
# delayMicroseconds() blocks every interrupt, so on a direction change it is
# spent out of the same window the ISR itself needs.
print("\n4. the DIR wait, which runs inside the ISR:")
assert "delayMicroseconds(DIR_SETTLE_US)" in BASE_H, \
    "rotISR's DIR wait is not the named constant any more"
assert DIR_US < slack_us / 2, \
    f"a {DIR_US} us busy-wait is {DIR_US / slack_us:.0%} of the {slack_us:.2f} us\n" \
    "    the ISR has; a direction change would cost a step on its own"
print(f"   {DIR_US:.0f} us out of {slack_us:.2f} us = {DIR_US / slack_us:.0%} "
      f"of the budget                OK")

# It must still only fire on an actual direction change. Per-step was the
# original fault and is the one thing that must not come back.
def code_only(text: str) -> str:
    """Drop // comments. The comment above the guard explains the old
    per-step delayMicroseconds(5), and a scan that cannot tell an explanation
    from a call finds the explanation first and reports a fault that is not
    there."""
    out = []
    for line in text.splitlines():
        i = line.find("//")
        out.append(line if i < 0 else line[:i])
    return "\n".join(out)


rot = code_only(BASE_H[BASE_H.index("void StepperBase::rotISR()"):])
rot = rot[:rot.index("void StepperBase::", 10)]
guard = rot.index("if (new_dir != dir)")
wait = rot.index("delayMicroseconds(")
assert guard < wait, \
    "the DIR wait is no longer inside the direction-changed guard — it would\n" \
    "    run on every step again, which is the fault that started all of this"
print("   still only on an actual direction change                   OK")

# ---- 5. the installed copy, if there is one, agrees ------------------------
# Editing one and building the other is a whole afternoon.
print("\n5. the installed copy:")
inst = pathlib.Path.home() / "Documents/Arduino/libraries/TeensyStep4/src"
if inst.exists():
    drift = [n for n in ("stepperbase.cpp", "stepperbase.h")
             if (inst / n).read_text() != (LIB / n).read_text()]
    assert not drift, \
        f"{', '.join(drift)} differs from libraries/. The build uses libraries/,\n" \
        "    so the rig would get one and anything reading the other would lie."
    print("   byte-identical to libraries/                               OK")
else:
    print("   (not installed here — nothing to diverge)")

print("\nALL CHECKS PASSED")
