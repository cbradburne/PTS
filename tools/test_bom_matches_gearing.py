"""The bill of materials must list the pulleys the firmware is geared for.

The slider's drive pulley is SLIDER_PULLEY_TEETH in MountMotion.h, and the
motion maths uses it directly: one motor revolution advances the carriage by
teeth x 2 mm, because GT2 is a 2 mm pitch by definition. Fit a different pulley
and every slider distance is wrong by the ratio — a 36-tooth in place of a
20-tooth sends the carriage 1.8x further than commanded.

The BOM said 36 tooth. The firmware says 20, and the firmware is what the rig
actually does, so anyone building from that parts list would have got a slider
that overshot every move and no obvious reason why. Caught by the operator, not
by anything here.

This is the same shape as the protocol checker: two copies of one fact in
different languages, drifting silently. The difference is that the wire format
breaks loudly and a wrong pulley just makes the machine subtly wrong.

Not every tooth count is a purchase. The pan ring is 270 teeth — about 172 mm
across — and is printed as part of the PT mount, so it is listed here as
deliberately absent rather than missing.

Run directly, or via tools/run_tests.sh with the rest.
"""
import pathlib, re

REPO = pathlib.Path(__file__).resolve().parent.parent
MOTION = (REPO / "firmware/teensy41_mount/MountMotion.h").read_text()
BOM    = (REPO / "docs/manual/bom.html").read_text()

# Tooth counts that are PRINTED, not bought — keep the reason with the number.
PRINTED = {270: "pan ring, ~172 mm across, printed as part of the PT mount"}


def teeth(name):
    m = re.search(r"#define\s+%s\s+(\d+)" % name, MOTION)
    assert m, f"{name} is gone from MountMotion.h — this test is now checking nothing"
    return int(m.group(1))


print("1. what the firmware is geared for:")
counts = {n: teeth(n) for n in ("SLIDER_PULLEY_TEETH", "PAN_DRIVEN_TEETH",
                                "PAN_DRIVER_TEETH", "TILT_DRIVEN_TEETH",
                                "TILT_DRIVER_TEETH")}
for n, v in counts.items():
    print("   %-22s %3d" % (n, v))

# GT2 is a profile, not a choice: 2 mm pitch is what makes it GT2. If that
# constant ever moves, the slider arithmetic below stops meaning what it says.
pitch = re.search(r"GT2_BELT_PITCH_MM\s*=\s*([\d.]+)f", MOTION)
assert pitch and float(pitch.group(1)) == 2.0, \
    "GT2 pitch is no longer 2.0 mm, which is not a thing GT2 can be"

print("\n2. every bought tooth count is in the BOM:")
listed = {int(t) for t in re.findall(r"GT2 Pulley - (\d+) tooth", BOM)}
assert listed, "no GT2 pulleys found in the BOM at all — has the markup changed?"
print("   BOM lists:", ", ".join(str(t) for t in sorted(listed)))

for name, n in sorted(counts.items(), key=lambda kv: kv[1]):
    if n in PRINTED:
        assert n not in listed, \
            f"{n} teeth ({name}) is in the BOM as a purchase, but it is {PRINTED[n]}"
        print(f"   {n:>3} ({name}) printed, correctly not a line item   OK")
        continue
    assert n in listed, (
        f"{name} is {n} teeth and the BOM does not list a {n}-tooth pulley.\n"
        f"    The firmware is what the rig does, so a parts list that disagrees\n"
        f"    builds a machine that moves the wrong distance. BOM has: "
        f"{sorted(listed)}")
    print(f"   {n:>3} ({name}) listed                            OK")

# The slider is the one where a wrong pulley is silently wrong rather than
# obviously wrong, so it gets its arithmetic checked too.
print("\n3. the slider's travel per revolution:")
mm = counts["SLIDER_PULLEY_TEETH"] * 2.0
assert f"GT2 Pulley - {counts['SLIDER_PULLEY_TEETH']} tooth" in BOM, \
    "the slider pulley in the BOM is not the one the firmware is geared for"
print(f"   {counts['SLIDER_PULLEY_TEETH']} teeth x 2 mm = {mm:.0f} mm per motor revolution   OK")

print("\nALL CHECKS PASSED")
