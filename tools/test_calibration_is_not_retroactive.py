"""Calibrating a subject must not move the frame the others were stored in.

Reported on 2026-08-25: reference the mount, set A and B for one subject, and
that move "looks amazing". Calibrate a second subject and the first one is
"pretty much lost".

Two mechanisms were rewriting global state on every SET_B:

  the pan/tilt deg-per-step scale, refined from the solved geometry
  the pan/tilt REFERENCE, re-derived from the SET_B position

Both are global: every subject is aimed at through them. And subjects are stored
as solved 3D POINTS, not as the observations behind them, so moving either
re-reads all of them against different arithmetic and nothing can re-derive
them. The second calibration therefore invalidates the first.

The reference version was also circular — the solve that produced the new
reference had itself run on the old one, so its own error was fed back in as
fact and applied to everything already stored. Its comment offered "all subjects
share this one global reference" as the benefit. It is the defect.

Neither was even self-consistent for the subject being calibrated: the solve
runs first, the writes happen after, so that subject was stored in one frame and
tracked in another. Small enough to look right, which is why only the SECOND
calibration showed it.

The operator's argument is the one the code now follows. The reference is set
once, deliberately, with the camera level and square to the rail. The A and B
observations are facts measured against it. A later subject is not evidence that
an earlier one was measured wrongly.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent

INO = (REPO / "firmware/teensy41_mount/teensy41_mount.ino").read_text()

setb = INO[INO.index("case CMD_ADD_SUBJECT_SET_B:"):]
setb = setb[:setb.index("\n        case ")]

# ---- 1. a calibration changes no global scale ------------------------------
print("1. what SET_B writes:")
assert "_cfg.pan_deg_per_step" not in setb and "_cfg.tilt_deg_per_step" not in setb, \
    "SET_B writes deg/step again — every previously stored subject is re-read\n" \
    "    against the new scale and none of them can be re-derived"
assert "setDegPerStep" not in setb, "SET_B pushes a new scale to MountMotion again"
print("   no deg/step refinement                             OK")

def code_only(text: str) -> str:
    """Drop // comments. The explanation of why the reference is no longer set
    here names the function, and a test that cannot tell an explanation from a
    call would fail on its own documentation."""
    out = []
    for line in text.splitlines():
        i = line.find("//")
        out.append(line if i < 0 else line[:i])
    return "\n".join(out)


assert "setLookAtRef" not in code_only(setb), \
    "SET_B sets the look-at reference again — that re-aims every stored subject"
print("   no reference recalibration                         OK")

# It must still STORE the subject, which is the whole point of the command.
assert "rec.x = sx;  rec.y = sy;  rec.z = sz;" in setb, "SET_B no longer stores the subject"
assert "_slot_occupied |= (1u << _calib_subject_id);" in setb, \
    "the subject is not marked occupied"
print("   still solves and stores the subject                 OK")

# ---- 2. the manual reference still works -----------------------------------
print("\n2. the manual reference:")
ref = INO[INO.index("case CMD_SET_REF:"):]
ref = ref[:ref.index("\n        case ")]
assert "mount.setLookAtRef(pan_ref, tilt_ref);" in ref, \
    "CMD_SET_REF no longer sets the reference — nothing would"
print("   CMD_SET_REF is the only thing that sets it         OK")
n = code_only(INO).count("mount.setLookAtRef(")
assert n == 1, f"setLookAtRef called from {n} places; only Set Ref may call it"
print("   and it is called from exactly one place            OK")

# ---- 3. deg/step comes from the drive geometry -----------------------------
print("\n3. where deg/step comes from:")
load = INO[INO.index("static void eeprom_load("):]
load = load[:load.index("\n}\n")]
assert "cfg.pan_deg_per_step  = NOMINAL_PAN_DEG_PER_STEP;" in load, \
    "the stored deg/step is used again — a mount calibrated under the old\n" \
    "    firmware is still carrying a bent scale in EEPROM"
assert "mount.setDegPerStep(NOMINAL_PAN_DEG_PER_STEP, NOMINAL_TILT_DEG_PER_STEP);" in load, \
    "the nominal scale is not pushed to MountMotion at boot"
print("   always nominal, so an old bent EEPROM value is ignored  OK")

# The nominal is exact by construction, which is why it needs no refining.
HDR = (REPO / "firmware/teensy41_mount/MountMotion.h").read_text()
assert "MOTOR_DEG_PER_STEP_PT / (MICROSTEPS_PAN  * GEAR_RATIO_PAN)" in HDR, \
    "the nominal is no longer derived from the drive geometry"
driven = int(re.search(r"#define PAN_DRIVEN_TEETH\s+(\d+)", HDR).group(1))
driver = int(re.search(r"#define PAN_DRIVER_TEETH\s+(\d+)", HDR).group(1))
print(f"   from tooth counts {driven}/{driver} = {driven/driver}, exact       OK")

# ---- 4. the disagreement is still reported ---------------------------------
# Removing the correction should not remove the information. If the implied
# scale is far from nominal that means the tooth counts are wrong, and that is
# worth saying even though it must not be acted on automatically.
print("\n4. what happens to the measurement:")
assert "DPS_REPORT_TOL" in setb, "the implied scale is no longer compared to nominal"
assert "NOT applied" in setb, \
    "the report does not say it is only a report — someone will read it as a change"
assert "PAN_DRIVEN_TEETH / " in setb or "PAN_DRIVEN_TEETH" in setb, \
    "the report does not point at what to fix if it persists"
print("   still computed, printed, and marked NOT applied     OK")
print("   and names the constants to check                    OK")

# ---- 5. no message claims otherwise ----------------------------------------
print("\n5. what the log says:")
assert "ref (auto-updated)" not in INO, \
    "a log line still says the reference was auto-updated; it is not"
assert "against ref (unchanged)" in INO, "the corrected wording is gone"
print("   nothing claims the reference was updated           OK")

print("\nALL CHECKS PASSED")
