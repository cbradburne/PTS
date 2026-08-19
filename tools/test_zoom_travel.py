"""Zoom travel logging: make a creeping zoom axis visible in the log.

Reported on 2026-08-19: recalling slot 10, pan/tilt/slider arrived and stopped
while ZOOM kept creeping — the move never completed, the border stayed yellow,
and pressing the slot again sprang zoom back the other way. Entirely visible on
the rig, entirely invisible in the log.

The data had been arriving the whole time. The mount sends CMD_POSITION at 5 Hz
while moving carrying zoom_steps and a per-axis moving_mask, and mount_manager
already decoded it. Nothing logged it.

What matters here is not that a line appears but WHICH line: the diagnostic
value is in "zoom is the only axis still moving", and in the step delta, because
position alone cannot say whether zoom is approaching its target, running past
it, or oscillating.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))
import logging, io, time

from PyQt6.QtWidgets import QApplication
_app = QApplication([])

from comms.mount_manager import MountManager
from comms.protocol import PositionPayload

PAN, TILT, SLIDER, ZOOM = 1, 2, 4, 8

_buf = io.StringIO()
_h = logging.StreamHandler(_buf)
_h.setFormatter(logging.Formatter("%(levelname)s|%(message)s"))
_lg = logging.getLogger("comms.mount_manager")
_lg.handlers, _lg.propagate = [_h], False
_lg.setLevel(logging.INFO)


def fresh():
    mm = MountManager.__new__(MountManager)
    mm._zoom_travel = {}
    return mm


def pos(z, mask):
    return PositionPayload(pan_deg=0.0, tilt_deg=0.0, slider_mm=0.0,
                           zoom_steps=z, moving_mask=mask)


def feed(mm, prev, cur, gap=0.6):
    time.sleep(gap)
    _buf.truncate(0); _buf.seek(0)
    mm._note_zoom_travel(5, prev, cur)
    return _buf.getvalue().strip()


# ---- 1. zoom alone is the finding, and is raised to WARNING ----------------
print("1. the reported fault:")
mm = fresh()
feed(mm, pos(12000, PAN | TILT | SLIDER | ZOOM), pos(12210, PAN | TILT | SLIDER | ZOOM))
out = feed(mm, pos(12420, PAN | TILT | ZOOM), pos(12600, ZOOM))
assert out.startswith("WARNING"), out
assert "ZOOM ALONE" in out and "cam5" in out, out
print(f"   {out.split('|', 1)[1]}")

out = feed(mm, pos(12600, ZOOM), pos(12615, ZOOM))
assert "+15" in out, f"the step delta is missing: {out}"
assert "ZOOM ALONE for" in out, out
print(f"   {out.split('|', 1)[1]}")
print("   zoom-alone warns, and carries the delta            OK")

# The delta must be SIGNED — an unsigned number cannot distinguish a zoom
# closing on its target from one running past it, which is the whole question.
out = feed(mm, pos(12615, ZOOM), pos(12600, ZOOM))
assert "-15" in out, f"a reversal must show as negative: {out}"
print("   a reversal reads as negative                       OK")

# ---- 2. moving WITH other axes is ordinary, and stays at INFO --------------
print("\n2. ordinary movement:")
mm2 = fresh()
out = feed(mm2, pos(100, PAN | TILT | ZOOM), pos(300, PAN | TILT | ZOOM))
assert out.startswith("INFO"), out
assert "also moving" in out and "pan" in out and "tilt" in out, out
print(f"   {out.split('|', 1)[1]}")
print("   normal travel stays INFO and names the other axes  OK")

# ---- 3. the end of a creep must be recorded --------------------------------
# A creep that simply stops and is never accounted for leaves the log implying
# it never ended.
print("\n3. the end:")
mm3 = fresh()
feed(mm3, pos(500, ZOOM), pos(520, ZOOM))
out = feed(mm3, pos(520, ZOOM), pos(400, 0))
assert "stopped at 400 steps" in out, out
assert "alone" in out, out
print(f"   {out.split('|', 1)[1]}")
print("   the stop is logged, with where it ended up         OK")

# ---- 4. it must not become a firehose --------------------------------------
# 5 Hz across five mounts is 25 lines a second and would bury everything else.
print("\n4. rate:")
mm4 = fresh()
feed(mm4, pos(0, ZOOM), pos(10, ZOOM))          # primes the 2 Hz gate
quiet = feed(mm4, pos(10, ZOOM), pos(20, ZOOM), gap=0.05)
assert quiet == "", f"a sample 50 ms later should be suppressed: {quiet!r}"
loud = feed(mm4, pos(20, ZOOM), pos(30, ZOOM), gap=0.6)
assert loud, "a sample 600 ms later should print"
print("   2 Hz: 50 ms sample suppressed, 600 ms printed      OK")

# A mount at rest must say nothing at all — CMD_POSITION still arrives at 1 Hz.
# Primed with one packet first, because the liveness line legitimately fires on
# the very first sample from each mount.
mm5 = fresh()
mm5._note_zoom_travel(5, None, pos(900, 0))
assert feed(mm5, pos(900, 0), pos(900, 0)) == "", "a stationary mount must be silent"
print("   a stationary mount is silent                       OK")

# ---- 5. it must prove telemetry is arriving --------------------------------
# The first version of this logged nothing on a rig where the fault reproduced
# every time, and there was no way to tell "zoom is behaving" from "these
# packets never reach me".  Everything else here is conditional; this is not.
print("\n5. liveness:")
mm6 = fresh()
_buf.truncate(0); _buf.seek(0)
mm6._note_zoom_travel(5, None, pos(12000, 0))
first = _buf.getvalue().strip()
assert "telemetry arriving" in first and "moving_mask" in first, first
print(f"   {first.split('|', 1)[1]}")
_buf.truncate(0); _buf.seek(0)
mm6._note_zoom_travel(5, pos(12000, 0), pos(12000, 0))
assert "telemetry arriving" not in _buf.getvalue(), "the liveness line repeats"
print("   said once per mount, then quiet                    OK")

# ---- 6. movement the mount does not admit to -------------------------------
# The mask comes from _stepper[i]->isMoving, and keying the whole instrument on
# it makes it blind if an axis can travel without that flag.  A changing step
# count is ground truth; a disagreement between the two IS the finding.
print("\n6. zoom moving while the mask says stopped:")
mm7 = fresh()
mm7._note_zoom_travel(5, None, pos(12600, 0))
out = feed(mm7, pos(12600, 0), pos(12615, 0))   # stopped -> stopped, still moving
assert out.startswith("WARNING"), out
assert "+15" in out and "STOPPED" in out and "0x00" in out, out
print(f"   {out.split('|', 1)[1]}")
print("   a creeping zoom is caught even if unreported       OK")

# The tail of an ordinary move must NOT be reported as unadmitted travel: the
# last sample always has the mask cleared and a position that still changed.
mm9 = fresh()
mm9._note_zoom_travel(5, None, pos(500, ZOOM))
out = feed(mm9, pos(520, ZOOM), pos(400, 0))
assert "without reporting" not in out, f"a normal arrival was flagged as a fault: {out}"
print("   a normal arrival is not flagged                    OK")

# ...but a stationary axis must still say nothing.
mm8 = fresh()
mm8._note_zoom_travel(5, None, pos(900, 0))
assert feed(mm8, pos(900, 0), pos(900, 0)) == "", "no movement must stay silent"
print("   a genuinely stopped zoom stays silent              OK")

print("\nALL CHECKS PASSED")
