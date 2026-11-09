"""A limit find's StallGuard readings reach the PC's Find Limits dialog.

Tuning a stall threshold is choosing a trip point between two readings: the
lowest SG_RESULT while the axis moves freely, and the reading when it pushes
against its end.  Until 2026-11-09 both were only on the Teensy's USB serial,
inside the mount.  Now the Teensy samples SG_RESULT every 50 ms while a leg
moves, keeps the lowest (leaving out the last 150 ms — the approach to the end)
and the last, and sends them with LIMITS_FOUND; the PC logs them and the dialog
shows them.

  1. sg_readout.h, compiled and run here: which samples count as "lowest".
  2. The Teensy fills the report from it and sends it with every result.
  3. The PC keeps it for the dialog and writes it to comms.log.
  4. The dialog says what each leg did and what to change.
  5. A 9-byte LIMITS_FOUND (older Teensy firmware) still works, with no readings.

The wire format itself is held by tools/check_protocol.py, which builds the
message in C and decodes it in Python.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re, shutil, subprocess, tempfile, struct, logging

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))
SHARED = REPO / "firmware" / "shared"
CPP = (REPO / "firmware/teensy41_mount/MountMotion.cpp").read_text()
INO = (REPO / "firmware/teensy41_mount/teensy41_mount.ino").read_text()


def body(src, signature):
    start = src.index(signature)
    return src[start:src.index("\n}\n", start)]


# ---- 1. which samples count ---------------------------------------------------
print("1. sg_readout.h:")
cxx = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
if cxx:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="sgreadout-"))
    (tmp / "h.cpp").write_text(r"""
#include <cstdio>
#include "sg_readout.h"
int main() {
    SgReadout r;
    sg_readout_reset(&r);
    char op; unsigned v;
    while (scanf(" %c", &op) == 1) {
        if (op == 'a' && scanf("%u", &v) == 1) sg_readout_add(&r, (uint16_t)v);
        else if (op == 'r') sg_readout_reset(&r);
        else if (op == 'p') printf("%u %u\n", r.lowest, r.last);
    }
}
""")
    r = subprocess.run([cxx, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(SHARED),
                        str(tmp / "h.cpp"), "-o", str(tmp / "h")],
                       capture_output=True, text=True)
    assert r.returncode == 0, "sg_readout.h does not compile cleanly:\n" + r.stderr[:1500]
    NONE = 0xFFFF
    cases = [  # (samples, want lowest, want last, what)
        ([], NONE, NONE, "no samples: nothing to report"),
        ([80, 70, 75], NONE, 75,
         "three samples are all the ending, so no lowest yet (a stall that quick\n"
         "      was not travel)"),
        ([100, 90, 95, 30, 20, 10], 90, 10,
         "the last three (the approach to the stop) are left out of the lowest"),
        ([100, 40, 100, 100, 100, 100], 40,
         100, "a dip mid-travel counts: that is a stiff spot"),
        ([60, 61, 62, 63, 64, 65, 66], 60, 66, "the lowest is the lowest, not the first"),
    ]
    script = []
    for samples, *_ in cases:
        script += ["r"] + [f"a {s}" for s in samples] + ["p"]
    out = subprocess.run([str(tmp / "h")], input="\n".join(script) + "\n",
                         capture_output=True, text=True, check=True).stdout.splitlines()
    for (samples, lo, last, what), got in zip(cases, out):
        g_lo, g_last = (int(x) for x in got.split())
        assert (g_lo, g_last) == (lo, last), \
            f"{what}: {samples} gave lowest {g_lo}, last {g_last}; want {lo}, {last}"
        print(f"   {what:<66} OK")
    shutil.rmtree(tmp, ignore_errors=True)
else:
    print("   no C++ compiler: the helper is not run here")

# ---- 2. the Teensy fills the report and sends it ----------------------------
print("\n2. the Teensy:")
lf = body(CPP, "void MountMotion::_updateLimitFind()")
assert "millis() - _lf_last_sample >= SG_READOUT_SAMPLE_MS" in lf, \
    "SG_RESULT is not sampled on the readout's own period"
assert re.search(r"if \(moving_leg && stall_settled\) sg_readout_add\(&_lf_sg, _lf_sg_now\);", lf), \
    "samples are taken outside a moving leg, or before the settle"
print("   samples every 50 ms, moving and settled only             OK")

begin = body(CPP, "void MountMotion::_beginLimitSeek(")
for want, why in (("_lf_report = LimitFindReport{};", "the last find's report is not cleared"),
                  ("_lf_report.threshold       = _stall_threshold[(int)axis];",
                   "the report does not carry the threshold the find used"),
                  ("_lf_report.usteps_per_turn = (uint16_t)lfUStepsPerTurn(axis);",
                   "the report does not say how big a motor turn is"),
                  ("leg.outcome   = LIMIT_LEG_NOT_RUN;", "an unrun leg is not marked so")):
    assert want in begin, why
rep = body(CPP, "void MountMotion::_reportLeg(")
for want, why in (("LimitLegReport &r = _lf_report.leg[leg];", "the leg is not written to the report"),
                  ("r.outcome   = stalled ? LIMIT_LEG_STALL : timed_out ? LIMIT_LEG_TIMEOUT : LIMIT_LEG_CAP;",
                   "the leg's outcome is not recorded as it happened"),
                  ("r.sg_lowest = _lf_sg.lowest;", "the lowest reading is not reported"),
                  ("r.sg_last   = _lf_sg.last;", "the last reading is not reported"),
                  ("r.trip      = trip;", "the trip point is not reported")):
    assert want in rep, why
start = body(CPP, "void MountMotion::_startLegReadout(")
assert "sg_readout_reset(&_lf_sg);" in start, "a leg starts with the last leg's samples"
assert "static_assert(SG_READOUT_NONE == LIMIT_SG_NONE" in CPP
print("   each leg's outcome, distance and readings go in the report OK")

send = body(INO, "static void send_limits_found(")
assert "build_limits_found_report(" in send and "&mount.limitFindReport()" in send, \
    "LIMITS_FOUND is sent without the readings"
assert "build_limits_found(buf" not in INO, \
    "something still sends the 9-byte LIMITS_FOUND without readings"
assert "send_limits_found(axis, 0, 0);" in body(INO, "static void send_limits_gone("), \
    "a failed find goes out without its readings — the time they matter most"
assert "send_limits_found(axis, min_steps, max_steps);" in body(INO, "static void on_limits_found("), \
    "a found range goes out without its readings"
print("   sent with every result, failed or found                  OK")

# ---- 3. the PC keeps them and logs them --------------------------------------
print("\n3. the PC's mount manager:")
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QObject, pyqtSignal
app = QApplication.instance() or QApplication([])
from comms import mount_manager as MM
from comms import protocol as P
from comms.protocol import Packet, Cmd, Axis


class FakeHubLink:
    connected = True
    def __init__(self): self.cb = None
    def on_packet(self, cb): self.cb = cb
    def off_packet(self, cb): pass
    def send(self, data): pass
    def seconds_since_tracked_tx(self): return 0.0


class Lines(logging.Handler):
    def __init__(self): super().__init__(); self.got = []
    def emit(self, rec):
        if rec.getMessage().startswith("LIMIT FIND"):
            self.got.append(rec.getMessage())


def payload(axis, lo, hi, legs=None, threshold=80, per_turn=6400):
    p = struct.pack(">Bii", int(axis), lo, hi)
    if legs is None:
        return p
    p += struct.pack(">BH", threshold, per_turn)
    for leg in legs:
        p += struct.pack(">BIHHH", *leg)
    return p


lines = Lines()
logging.getLogger(MM.__name__).addHandler(lines)
logging.getLogger(MM.__name__).setLevel(logging.INFO)   # as comms.log records it
link = FakeHubLink()
mm = MM.MountManager(link)
seen_at_signal = []
mm.limits_found.connect(lambda m, a, lo, hi: seen_at_signal.append(mm.state(m).last_limit_find))

FAILED = [(P.LIMIT_LEG_STALL, 9088, 61, 58, 32),
          (P.LIMIT_LEG_CAP, 38400, P.LIMIT_SG_NONE, 40, 32)]
link.cb(Packet(mount_id=2, seq=0, cmd=Cmd.LIMITS_FOUND, payload=payload(Axis.ZOOM, 0, 0, FAILED)))
st = mm.state(2).last_limit_find
assert st is not None and st.threshold == 80 and len(st.legs) == 2, st
assert (st.legs[1].outcome, st.legs[1].sg_lowest, st.legs[1].sg_last) == (P.LIMIT_LEG_CAP, None, 40)
assert seen_at_signal and seen_at_signal[-1] is st, \
    "the readings are stored after limits_found fires, so the dialog reads the last find's"
print("   stored before the signal, so the dialog reads this find's   OK")

want = ("LIMIT FIND cam2 zoom: threshold 80 — home end stalled after 1.42 turns "
        "(lowest SG 61, last 58, trips at 32); far end no stall, travel cap reached, "
        "after 6.00 turns (lowest SG -, last 40, trips at 32) — no end found, limits cleared")
assert lines.got == [want], f"the log line reads:\n    {lines.got}\nwant:\n    {[want]}"
print(f"   comms.log: {want[:60]}…  OK")

# Older firmware: 9 bytes, no readings, no LIMIT FIND line, nothing stale kept.
lines.got = []
link.cb(Packet(mount_id=2, seq=1, cmd=Cmd.LIMITS_FOUND, payload=payload(Axis.ZOOM, 400, 9200)))
assert mm.state(2).last_limit_find is None, \
    "a 9-byte result leaves the previous find's readings in place"
assert lines.got == [], f"a 9-byte result logged readings it does not have: {lines.got}"
assert (mm.state(2).zoom_min, mm.state(2).zoom_max) == (400, 9200)
print("   a 9-byte result from older firmware: limits, no readings   OK")

# ---- 4. the dialog shows them -------------------------------------------------
print("\n4. the Find Limits dialog:")
from ui.dialogs.find_limits_dialog import FindLimitsDialog


class _CR:
    slider_end_margin_mm = 0


class _St:
    last_config_report = _CR()
    last_limit_find = None


class _MM(QObject):
    limits_found = pyqtSignal(int, int, int, int)
    def __init__(self): super().__init__(); self.st = _St()
    def send_find_limits(self, *a): pass
    def send_e_stop(self, *a): pass
    def state(self, m): return self.st


def run(axis, lo, hi, legs, threshold=80):
    fmm = _MM()
    fmm.st.last_limit_find = None if legs is None else \
        P.decode_limits_found(payload(axis, lo, hi, legs, threshold)).report
    dlg = FindLimitsDialog(1, fmm, axis, stall_threshold=threshold)
    dlg._running = True
    fmm.limits_found.emit(1, int(axis), lo, hi)
    app.processEvents()
    return dlg._readings.text()


text = run(Axis.ZOOM, 0, 0, FAILED)
for want in ("Home end: stalled after 1.42 turns — lowest SG while moving 61, trips at 32.",
             "Far end: no stall in 6.00 turns (its travel limit) — lowest SG -, last 40, "
             "trips at 32.",
             "Threshold 80. If it was pushing against its end, SG read about 40 there: "
             "raise the threshold until the trip point is above that."):
    assert want in text, f"missing {want!r} from:\n{text}"
print("   a failed find: both legs, and the reading to beat at the end OK")

FOUND = [(P.LIMIT_LEG_STALL, 9088, 61, 58, 32), (P.LIMIT_LEG_STALL, 8832, 55, 30, 32)]
text = run(Axis.ZOOM, 400, 8432, FOUND)
assert "Far end: stalled after 1.38 turns — lowest SG while moving 55, trips at 32." in text, text
assert "If an end stopped short, lower it" in text and "pushing against" not in text, text
print("   a found range: stalls, and what to do if one stopped short    OK")

# The rail in millimetres, like the result line above it; a leg that never ran
# is left out; a stall too quick to sample says so rather than printing 65535.
RAIL = [(P.LIMIT_LEG_STALL, 160 * 25, P.LIMIT_SG_NONE, 70, 120), (P.LIMIT_LEG_NOT_RUN, 0, 0xFFFF, 0xFFFF, 0)]
text = run(Axis.SLIDER, 0, 0, RAIL, threshold=60)
assert "Home end: stalled after 25 mm — too soon for a reading, trips at 120." in text, text
assert "Far end" not in text and "65535" not in text, text
print("   slider in mm, unrun leg left out, no 65535                   OK")

assert run(Axis.ZOOM, 400, 9200, None) == "", "readings invented for older firmware"
print("   nothing shown when the mount sends none                     OK")

print("\nALL CHECKS PASSED")
