"""TEMPORARY — the look-at motion probe, and its blast radius.

Switching subject mid-move reads as abrupt. Two fixes were reasoned from the
firmware and neither changed what the operator sees: the setpoint blend, then
the slew-cap timing. Both were correct about what the code does. Neither was
measured, because there is no position data anywhere in the system — CMD_POSITION
is answered on request and nothing asks.

So this asks. At 5 Hz, ONLY while a mount is in a look-at state, logging pan and
tilt with the angular velocity between samples. Velocity rather than position
because abruptness IS velocity — a position series has to be differenced by hand
before it says anything.

What this file guards is the blast radius, because the last diagnostic to go in
here polled every connected mount and left the tap open for six seconds after
motion stopped:

  it must ask only while a look-at is actually running
  it must stop asking the moment it is not
  it must be one flag away from silence

Delete this file with the diagnostic.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

MM = (REPO / "pc_app/comms/mount_manager.py").read_text()

from PyQt6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])

from comms.mount_manager import (MountManager, LOOK_AT_DIAGNOSTIC,
                                 LOOK_AT_POLL_MS)
from comms.protocol import MountState

# ---- 1. it samples fast enough to mean anything -----------------------------
print("1. sample rate:")
hz = 1000.0 / LOOK_AT_POLL_MS
assert hz >= 5.0, f"{hz:.0f} Hz cannot resolve a ~1.6 s move"
print(f"   {hz:.0f} Hz — about {1.6 * hz:.0f} samples across a 1.6 s blend   OK")

# The old position logging downsampled to 1 Hz, which is why it could not
# answer this even when positions were flowing.
plog = (REPO / "pc_app/comms/position_log.py").read_text()
assert "SAMPLE_INTERVAL_S = 1.0" in plog, "position_log's rate changed"
assert hz > 1.0, "this samples no faster than the logging that already failed"
print("   faster than position_log's 1 Hz, which could not         OK")

# ---- 2. and asks only while a look-at is running ---------------------------
print("\n2. when it asks:")
poll = MM[MM.index("def _poll_look_at_positions"):]
poll = poll[:poll.index("\n    def ", 1)]
assert "if not LOOK_AT_DIAGNOSTIC:" in poll, "the poller is not gated on the flag"
assert "st.state in self._LOOK_AT_STATES" in poll, \
    "the poller asks regardless of what the mount is doing"
assert "if not st.connected:" in poll, "it asks mounts that are not there"
print("   only a connected mount in a look-at state               OK")

# Nothing keeps the tap open past the move. The zoom diagnostic had a 6 s tail
# and that tail was most of its traffic.
assert "_POS_TAIL_S" not in MM and "_pos_until" not in MM, \
    "a tail window is back — traffic would outlast the thing being measured"
print("   no tail — asking stops when the move does               OK")

# ---- 3. the velocity anchor is dropped between moves -----------------------
# Otherwise the first sample of the next switch reports a velocity measured
# across the gap since the last one, which would look like a huge spike.
assert "del self._la_last[mid]" in poll, \
    "the previous sample survives the end of a move — the next move's first\n" \
    "    velocity would be computed across the gap between them"
print("   anchor dropped at the end of a move                     OK")

# ---- 4. it runs, and only for the right state ------------------------------
print("\n3. driven directly:")
mm = MountManager.__new__(MountManager)
mm._la_last = {}
mm._la_prev_v, mm._la_series, mm._la_moving, mm._la_quiet = {}, {}, {}, {}
sent: list[int] = []
mm._send = lambda pkt: sent.append(1)

class St:
    def __init__(self, state, connected=True):
        self.state, self.connected = state, connected

for state, name, want in ((MountState.LOOK_AT_MOVE,    "LOOK_AT_MOVE   ", True),
                          (MountState.LOOK_AT_PRE_AIM, "LOOK_AT_PRE_AIM", True),
                          (MountState.IDLE,            "IDLE           ", False),
                          (MountState.JOGGING,         "JOGGING        ", False),
                          (MountState.MOVING_TO_POS,   "MOVING_TO_POS  ", False)):
    sent.clear()
    mm._states = {5: St(state)}
    mm._poll_look_at_positions()
    got = len(sent) > 0
    assert got == want, f"{name}: asked={got}, expected {want}"
    print(f"   {name} -> {'asks' if got else 'silent'}                        OK")

sent.clear()
mm._states = {5: St(MountState.LOOK_AT_MOVE, connected=False)}
mm._poll_look_at_positions()
assert not sent, "asked a disconnected mount"
print("   disconnected            -> silent                        OK")

# The end-of-move cleanup touches every per-mount dict the probe keeps. If one
# of them is ever missed the tidy-up raises instead of tidying — and it raises
# inside the poll timer, where the traceback is easy to miss and the probe just
# quietly stops sampling.
mm._states = {5: St(MountState.LOOK_AT_MOVE)}
mm._la_last[5]   = (0.0, 0.0, 0.0)
mm._la_prev_v[5] = (0.0, 0.0)
mm._la_series[5] = [(1.0, 1.0)]
mm._la_moving[5] = True
mm._la_quiet[5]  = 1
mm._states = {5: St(MountState.IDLE)}
mm._poll_look_at_positions()
for name in ("_la_last", "_la_prev_v", "_la_series", "_la_moving", "_la_quiet"):
    assert 5 not in getattr(mm, name), f"{name} survives the end of a move"
print("   every per-mount dict cleared, without raising            OK")

# ---- 5. the summary tells the shapes apart ---------------------------------
# The rectangle was only obvious as a series. Whatever the ease out really is,
# it will be too — so the probe prints the whole move on one line and names the
# biggest single step. These are the shapes it has to distinguish, and the
# reading has to be right or the next round of work is misdirected again.
print("\n4. the shape it reports:")
import logging
from comms import mount_manager as _mmmod

class _Grab(logging.Handler):
    def __init__(self): super().__init__(); self.lines = []
    def emit(self, r): self.lines.append(r.getMessage())

def summarise(pans):
    grab = _Grab()
    _mmmod.log.addHandler(grab)
    try:
        mm._la_series[9], mm._la_moving[9], mm._la_quiet[9] = [], False, 0
        for v in pans:
            mm._la_series.setdefault(9, []).append((v, 0.0))
            mm._la_check_settled(9, v, 0.0)
    finally:
        _mmmod.log.removeHandler(grab)
    return [l for l in grab.lines if l.startswith("LA MOVE")]

# Switch 3 exactly as the rig produced it on 2026-08-26, spike removed: flat
# out at 45, then stopped inside one sample.
rect = summarise([20, 45, 45, 46, 46, 44, 45, 45, 43, -2, -2.2, -2.0])
assert rect, "the measured rectangle produced no summary at all"
assert "during the ease out" in rect[-1], \
    "the rig's own 45 deg/s drop is not reported as an ease-out step"
print("   measured switch     -> step named, at the ease out       OK")

# What it should look like instead: same 45 peak, shaped.
bell = summarise([4, 15, 30, 42, 45, 42, 30, 15, 6, 2, 1.5, 1.8])
assert bell and "during the ease in" in bell[-1], \
    "a smooth bell is reported as having an ease-out step; the reading is wrong\n" \
    "    and would send the next fix to the wrong end of the move"
print("   smooth bell         -> no ease-out step                  OK")

# Tracking through a slider move — the thing that kept the summary silent when
# the thresholds assumed a move ends with the camera standing still.
assert not summarise([1.5, 2.1, 2.8, 3.2, 4.7, 1.1, 2.2, 3.1]), \
    "ordinary tracking is summarised as a move; the log fills with non-moves"
print("   tracking at 1-5 deg/s -> silent                          OK")

# ---- 6. the bunched-reply artifact ------------------------------------------
# Every switch in the 2026-08-26 log carried one spike to double speed, and
# every one had dt 0.10 against a 0.21 nominal: two replies landing together,
# not motion. A fake 45 deg/s step is exactly the shape being hunted.
print("\n5. bunched replies:")
log_src = MM[MM.index("def _log_look_at_sample"):]
log_src = log_src[:log_src.index("\n    # Speeds that count")]
assert "LOOK_AT_POLL_MS / 1000.0) * 0.6" in log_src, \
    "a sample timed over a fraction of the poll interval is still logged, and\n" \
    "    it reads as a spike to double speed"
anchor = log_src.index("self._la_last[mid] = (now, pos.pan_deg, pos.tilt_deg)",
                       log_src.index("dt = now - last[0]"))
assert log_src.index("if dt < (LOOK_AT_POLL_MS") < anchor, \
    "the anchor is advanced before the short sample is rejected, so the NEXT\n" \
    "    sample measures from it too and the artifact survives anyway"
print("   short sample dropped, anchor held for the next one       OK")

# ---- 7. one flag away from silence -----------------------------------------
print("\n6. removing it:")
assert MM.count("LOOK_AT_DIAGNOSTIC") >= 4, \
    "the flag no longer guards every entry point"
for guard in ("if not LOOK_AT_DIAGNOSTIC:", "if LOOK_AT_DIAGNOSTIC:"):
    assert guard in MM, f"missing guard form: {guard}"
assert "REMOVE once the profile has been read" in MM, \
    "the flag no longer says it is temporary"
print("   flag guards the timer, the poll and the logging         OK")
print("   and says in the source that it is temporary             OK")

print("\nALL CHECKS PASSED")
