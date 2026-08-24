"""Calibrating without a session reference is refused, not silently wrong.

On 2026-08-24 two subjects were calibrated on mount 5 and solved to nearly 3 m
ABOVE the rail. The same rig, with a reference set, had solved to 1.3 m below
it. The cause was that no Set Ref had been done since the last flash.

The reference is RAM-only — _ref_set starts false in the constructor and is
never loaded from EEPROM — so a reboot or a firmware flash clears it. That is
exactly the moment someone is most likely to recalibrate.

Nothing stopped them. CMD_ADD_SUBJECT_START checked only the payload length, so
the calibration ran, the solve succeeded, the mount reported SOLVED and stored a
subject measured from wherever the head happened to sit at boot. The failure had
no symptom until the camera pointed at the wrong place.

The asymmetry is the bug: startLookAtMove() and aimAtSubject() have always
refused without a reference. The system would not USE a subject it had no frame
for, but would happily CREATE one.

NACK_NO_REF already existed in protocol.h and protocol.py, described as exactly
this, and had never been sent by anything.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

INO = (REPO / "firmware/teensy41_mount/teensy41_mount.ino").read_text()
CPP = (REPO / "firmware/teensy41_mount/MountMotion.cpp").read_text()
MM  = (REPO / "pc_app/comms/mount_manager.py").read_text()

# ---- 1. the reference really is lost on a reboot ---------------------------
print("1. why this happens at all:")
assert re.search(r"_pan_ref_deg\(0\.0f\), _tilt_ref_deg\(0\.0f\), _ref_set\(false\)", CPP), \
    "the reference no longer starts unset — re-read this test"
print("   _ref_set starts false in the constructor               OK")

# Nothing restores it at boot.  setLookAtRef() is the only way it becomes true,
# and the EEPROM load path must not call it — if it ever did, this whole class
# of fault would be gone and the guard below would be belt-and-braces.
load = INO[INO.index("static void eeprom_load("):]
load = load[:load.index("\n}\n")]
assert "setLookAtRef" not in load, \
    "the reference IS restored at boot now — this test's premise has changed"
setters = CPP.count("_ref_set      = true;")
assert setters == 1, f"_ref_set is set true in {setters} places; expected only setLookAtRef()"
print("   nothing restores it at boot — only Set Ref does        OK")

# ---- 2. calibration refuses without one ------------------------------------
print("\n2. starting a calibration:")
h = INO[INO.index("case CMD_ADD_SUBJECT_START:"):]
h = h[:h.index("\n        case ")]
assert "if (!mount.isRefSet()) {" in h, \
    "CMD_ADD_SUBJECT_START no longer checks for a reference — a calibration\n" \
    "    without one solves successfully and points nowhere"
assert "NACK_NO_REF" in h, "the refusal does not say WHY it was refused"

# The check must come before anything is changed, or an aborted calibration
# leaves state behind.
guard = h.index("if (!mount.isRefSet())")
state = h.index("_calib_subject_id =")
assert guard < state, "the reference check runs after calibration state is set up"
print("   refused with NACK_NO_REF, before any state changes     OK")

# ---- 3. and the look-at move names the same cause ---------------------------
print("\n3. starting a look-at move:")
la = INO[INO.index("[LookAt] FAILED to start"):]
la = la[:la.index("ESP_SERIAL.write(nack_buf, nack_len);")]
assert "mount.isRefSet() ? NACK_BUSY" in la and "NACK_NO_REF" in la, \
    "a look-at move refused for want of a reference still reports BUSY, which\n" \
    "    sends someone looking for a move in progress"
print("   reports NO_REF rather than BUSY when that is the cause OK")

# ---- 4. the app says what to do about it ------------------------------------
print("\n4. what reaches the log:")
assert "NackError.NO_REF" in MM, "the PC app does not distinguish NO_REF"
assert "run Set Ref (0/0)" in MM, \
    "the message does not say what to do — 'err=6' is not an instruction"
assert "cleared by a reboot or a firmware flash" in MM, \
    "the message does not say why the reference went missing, which is the part\n" \
    "    that makes it recognisable next time"
print("   names the fix and why it was needed                    OK")

# The generic path must survive for every other error.
assert 'f"NACK from mount {mid}: seq={n.nacked_seq} err={n.error}"' in MM, \
    "the generic NACK line was lost"
print("   other NACKs still reported as before                   OK")

# ---- 5. the code was already there, unused ---------------------------------
print("\n5. the error code:")
PROTO = (REPO / "firmware/shared/protocol.h").read_text()
assert "NACK_NO_REF        = 0x06" in PROTO, "NACK_NO_REF is gone from protocol.h"
from comms.protocol import NackError
assert int(NackError.NO_REF) == 0x06, "protocol.py disagrees about NO_REF"
print("   NACK_NO_REF = 0x06, both sides, and now actually sent  OK")

print("\nALL CHECKS PASSED")
