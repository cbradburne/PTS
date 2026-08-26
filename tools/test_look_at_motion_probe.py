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
mm._la_req_t = {}
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
mm._la_series[5] = [(0.21, 1.0, 1.0)]
mm._la_moving[5] = True
mm._la_quiet[5]  = 1
mm._la_req_t[5]  = 0.0
mm._states = {5: St(MountState.IDLE)}
mm._poll_look_at_positions()
for name in ("_la_last", "_la_prev_v", "_la_series", "_la_moving",
             "_la_quiet", "_la_req_t"):
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

def summarise(pans, tilts=None):
    grab = _Grab()
    _mmmod.log.addHandler(grab)
    try:
        mm._la_series[9], mm._la_moving[9], mm._la_quiet[9] = [], False, 0
        ts = tilts if tilts is not None else [0.0] * len(pans)
        for v, t in zip(pans, ts):
            mm._la_series.setdefault(9, []).append((0.21, v, t))
            mm._la_check_settled(9, v, t)
    finally:
        _mmmod.log.removeHandler(grab)
    return [l for l in grab.lines if l.startswith("LA MOVE")]

# The question is no longer WHERE the biggest velocity step falls. A smoothstep
# peaks in acceleration at BOTH ends by definition, so once the shape is right
# the biggest step lands near an end every time and saying so reads as a fault
# that is not there. What matters is whether the motion is steeper than the
# curve of that size and length actually demands: 6 x travel / duration^2.
#
# The rig's own switch from 18:50 — flat out at 45, stopped inside one sample,
# tilt arcing — against the same axis at 19:32 with the angle blend in.
rect = summarise([20, 45, 45, 46, 46, 44, 45, 45, 43, -2, -2.2, -2.0])
assert rect, "the measured rectangle produced no summary at all"
ratio_bad = float(re.search(r"\(([\d.]+)x\)", rect[-1]).group(1))

bell = summarise([14.0, 19.4, 24.5, 28.9, 32.7, 35.7, 38.1, 39.8, 40.6, 40.4,
                  39.2, 36.8, 33.1, 27.7, 19.7, 7.2, 1.9, 1.9])
assert bell, "the measured bell produced no summary at all"
ratio_good = float(re.search(r"\(([\d.]+)x\)", bell[-1]).group(1))

print(f"   measured rectangle -> {ratio_bad:.2f}x the curve")
print(f"   measured bell      -> {ratio_good:.2f}x the curve")
assert ratio_good < 1.25, \
    f"a bell the camera actually followed reads as {ratio_good:.2f}x too steep;\n" \
    "    the metric would flag correct motion as a fault"
assert ratio_bad > ratio_good * 1.3, \
    "the metric no longer separates the rectangle from the bell"
print("   steepness separates them, and the bell reads clean   OK")

# The settling behind the move must not count: it adds half a second of span
# and no travel, which flatters the curve and makes good motion look steep.
assert "while move and abs(move[-1][1]) < self._LA_STOPPED_DPS:" in MM, \
    "the settle is included in the span again; a correct bell then reads ~30%\n" \
    "    steeper than it is"
print("   the settle is excluded from the measurement          OK")

# A tilt that reverses mid-move is the signature of blending position rather
# than angle. It must be called out by name, not left to be spotted by eye.
arc = summarise([20, 45, 45, 46, 46, 44, 45, 45, 43, -2, -2.2, -2.0],
                [2.7, 3.4, 4.4, 5.5, 5.7, 3.6, 0.5, -0.9, -1.9, -4.0, -1.9, -0.7])
assert "the aim is arcing" in arc[-1], "a reversing tilt is not reported"
straight = summarise([14, 20, 30, 38, 40, 38, 30, 20, 8, 2, 1.5, 1.8],
                     [1.3, 1.7, 2.1, 2.4, 2.4, 2.3, 1.9, 1.3, 0.6, 0.2, 0.1, 0.1])
assert "the aim is arcing" not in straight[-1], \
    "a monotonic tilt is reported as arcing"
print("   a reversing tilt is named, a monotonic one is not    OK")

print("   tracking at 1-5 deg/s -> silent                          OK")

# ---- 6. the sample is timed by the request, not the reply ------------------
# Arrival time is not sample time. The mount reads its position when it
# processes the request; everything after is transport, and transport jitter
# lands entirely in dt while the position delta stays honest. A reply 100 ms
# late reads 68% of true speed and the one behind it 190%.
#
# The first attempt at this dropped short samples and held the anchor, which
# only relocated the error: the next sample measured 420 ms of movement against
# a 310 ms gap. All four spikes in the 2026-08-26 18:50 log sat at dt 0.31
# against a 0.21 nominal -- that fix's signature, not the mount's motion.
print("\n5. how a sample is timed:")
log_src = MM[MM.index("def _log_look_at_sample"):]
log_src = log_src[:log_src.index("\n    # Speeds that count")]
assert "self._la_req_t.pop(mid, None)" in log_src, \
    "the sample is timed by arrival again; link jitter goes straight into the\n" \
    "    velocity and reads as spikes that were never real motion"
assert "time.monotonic()" not in log_src, \
    "arrival time is still being taken inside the logger"

poll_src = MM[MM.index("def _poll_look_at_positions"):]
poll_src = poll_src[:poll_src.index("\n    def ", 1)]
assert poll_src.index("self._la_req_t[mid] = time.monotonic()") \
     < poll_src.index("self._send(pkt_get_position(mid))"), \
    "the request is stamped after it is sent, which puts the send back in dt"
print("   timed from the request, where the clock has no jitter    OK")

# A reply with no outstanding request must not be timed at all, or a duplicate
# would be differenced against whatever anchor happened to be lying around.
assert "if now is None:" in log_src, \
    "an unsolicited or duplicate POSITION is still given a velocity"
print("   an unmatched reply is ignored rather than guessed        OK")

# The old drop-short-samples heuristic must be gone: it is what produced the
# dt 0.31 artifact, and leaving it in would fight the request timestamps.
assert "* 0.6" not in log_src, \
    "the short-sample drop is back; with request timestamps it is not only\n" \
    "    unnecessary but is itself what skewed the sample after each drop"
print("   the drop-short heuristic that caused it is gone          OK")

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
