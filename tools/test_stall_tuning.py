"""StallGuard tuning: which way the threshold goes, what the readout shows,
and what a find does when it never sees a stall.

On 2026-11-09 the zoom's limit find "either stops too soon or hits the end of
travel and keeps going", and three things stood between the operator and a
threshold that worked:

  1. The PC app said "0=sensitive, 255=coarse".  On the TMC2209 it is the
     other way round: a stall is SG_RESULT <= 2 x SGTHRS, and SG_RESULT FALLS as
     the load rises, so a higher number trips at a lighter load.  Following the
     label moved the threshold the wrong way every time.
  2. The Teensy's serial readout printed the box's number doubled as "thr",
     ignoring the zoom's divide-by-5 — 160 for the default 80, where the chip
     trips at 32.  A reading of SG=60 looked well inside a stall when the chip
     saw nothing.
  3. A find that missed the stop pushed against it until somebody pressed Stop:
     the only backstop was a 33-minute timeout.  The zoom now gives up after
     LIMIT_FIND_MAX_TURNS, says so, and clears its limits; a timeout is a
     failure too, where it used to be recorded as the end.

The PC side is driven rather than read: the config dialog and the Find Limits
dialog are built and asked what they show.  The firmware side can only be read
here, so those checks pin the shape that makes it true.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

CPP = (REPO / "firmware/teensy41_mount/MountMotion.cpp").read_text()
HDR = (REPO / "firmware/teensy41_mount/MountMotion.h").read_text()
INO = (REPO / "firmware/teensy41_mount/teensy41_mount.ino").read_text()


def body(src, signature):
    """The text of one function, from its signature to its closing brace."""
    start = src.index(signature)
    return src[start:src.index("\n}\n", start)]


def block(src, label):
    """One case of a switch, up to its break."""
    start = src.index(label)
    return src[start:src.index("break;", start)]


from PyQt6.QtWidgets import QApplication, QGroupBox
from PyQt6.QtCore import QObject, pyqtSignal
app = QApplication.instance() or QApplication([])

from comms.protocol import Axis

# ---- 1. the direction, as the operator is told it ---------------------------
print("1. which way the threshold goes:")
from config.mount_config import load_config
from ui.dialogs.config_dialog import ConfigDialog


class _Any:
    def __call__(self, *a, **k):  return None
    def connect(self, *a, **k):   return None
    def disconnect(self, *a, **k): return None
    def emit(self, *a, **k):      return None


class _St:
    last_config_report = None
    active_pt_preset = 2; active_sl_preset = 2
    slider_min = None; slider_max = None; connected = False


class _MM(QObject):
    limits_found           = pyqtSignal(int, int, int, int)
    config_report_received = pyqtSignal(int, object)
    mount_status_updated   = pyqtSignal(int)
    def state(self, m):    return _St()
    def mount_table(self): return [b"\x00" * 6] * 5
    def sat_names(self):   return {}
    def __getattr__(self, n): return _Any()


mm = _MM(); mm.mount_route = [0] * 5
DLG = ConfigDialog(load_config(), mm, _MM())
app.processEvents()

sg_zoom   = getattr(DLG, "_m1_sg_zoom")
sg_slider = getattr(DLG, "_m1_sg_slider")
box = sg_zoom.parent()
while box is not None and not isinstance(box, QGroupBox):
    box = box.parent()
title = box.title()
assert "0=sensitive" not in title and "coarse" not in title, \
    f"the threshold box still says low numbers are sensitive: {title!r}"
assert "higher = more sensitive" in title, \
    f"the threshold box does not say which way is more sensitive: {title!r}"
print(f"   box title: {title!r}   OK")

for name, w in (("slider", sg_slider), ("zoom", sg_zoom)):
    tip = w.toolTip()
    assert "Stops before the end → lower it" in tip and \
           "keeps going → raise it" in tip, \
        f"the {name} threshold's tooltip does not say which way to move it: {tip!r}"
assert "steps of 5" in sg_zoom.toolTip(), \
    "the zoom tooltip does not say its threshold only changes in steps of 5"
assert "steps of 5" not in sg_slider.toolTip(), \
    "the slider tooltip claims the zoom's divide-by-5"
print("   tooltips: lower if it stops short, raise if it runs on   OK")

# The firmware's own description of the API agrees with the label.
doc = HDR[:HDR.index("void setStallThreshold(")]
doc = doc[doc.rindex("// StallGuard threshold for limit finding"):]
assert "higher = MORE sensitive" in doc.splitlines()[0], \
    f"MountMotion.h's setStallThreshold comment reads {doc.splitlines()[0]!r}"
print("   MountMotion.h says the same                            OK")

# ---- 2. the serial readout shows the chip's trip point ----------------------
print("\n2. the Teensy's readout:")
lf = body(CPP, "void MountMotion::_updateLimitFind()")
assert "_stall_threshold[idx] * 2" not in lf, \
    "the readout still prints the box's threshold doubled — five times the real\n" \
    "    trip point on the zoom, which divides it by 5 first"
assert re.search(r"trip\s*=\s*\(uint16_t\)_lf_sgthrs \* 2;", lf), \
    "the readout's trip point is not the value written to the chip, doubled"
apply = body(CPP, "void MountMotion::_applyStallThreshold(")
assert "_tmc[idx]->SGTHRS(_lf_sgthrs);" in apply, \
    "_lf_sgthrs is not the value written to SGTHRS, so the readout can disagree\n" \
    "    with the chip"
print("   prints 2 x the SGTHRS actually in the chip              OK")

# ---- 3. a find that never stalls gives up, and says so ----------------------
print("\n3. a find that sees no stall:")
caps = re.search(r"LIMIT_FIND_MAX_TURNS\[4\]\s*=\s*\{([^}]*)\}", CPP)
assert caps, "LIMIT_FIND_MAX_TURNS is gone"
turns = [float(x.strip().rstrip("f")) for x in caps.group(1).split(",")]
zoom_cap = turns[3]
assert 0 < zoom_cap <= 10, \
    f"the zoom's travel cap is {zoom_cap} turns — 0 means none at all, and much\n" \
    "    over ten is minutes of grinding at find speed"
assert turns[2] == 0, "the slider has gained a travel cap — deliberately?"
print(f"   the zoom gives up after {zoom_cap:g} motor turns              OK")

begin = body(CPP, "void MountMotion::_beginLimitSeek(")
assert "10000000L" not in begin and "_limitSeekTarget(axis, go_positive)" in begin, \
    "the first leg is not sent to its capped target"
assert "_lf_failed   = false;" in begin, \
    "a new find does not clear the last one's failure"
backoff = block(lf, "case LimitFindState::BACKING_OFF_MIN:")
assert "10000000L" not in backoff and "_limitSeekTarget(ax, go_max_positive)" in backoff, \
    "the second leg is not sent to its capped target"
print("   both legs are sent no further than the cap             OK")

assert "bool gave_up = !stalled && (timed_out || !_stepper[idx]->isMoving);" in lf, \
    "a leg that stops without a stall, or times out, is no longer 'gave up'"
for leg in ("MOVING_TO_MIN", "MOVING_TO_MAX"):
    b = block(lf, f"case LimitFindState::{leg}:")
    assert re.search(r"if \(gave_up\) \{\s*_abandonLimitFind\(\);\s*\} else if \(stalled\) \{", b), \
        f"the {leg} leg does not abandon the find when it gave up — a timeout\n" \
        "    used to be recorded as the end, wherever the axis happened to be"
    found = "setPosition(0)" if leg == "MOVING_TO_MIN" else "setLimits("
    assert b.index("_abandonLimitFind();") < b.index(found), \
        f"the {leg} leg records an end before deciding whether it found one"
print("   no stall -> no end recorded, on either leg              OK")

ab = body(CPP, "void MountMotion::_abandonLimitFind()")
for want, why in (("_limits_set[idx] = false;", "the old limits survive a failed find"),
                  ("_lf_failed   = true;", "the failure is not flagged for the callbacks"),
                  ("_lf_cb(ax, 0, 0);", "the app is not told the limits are gone"),
                  ("_restoreDriverAfterFind(ax);", "the driver is left on find current"),
                  ("_state       = STATE_IDLE;", "the mount is left in FINDING_LIMITS")):
    assert want in ab, f"_abandonLimitFind: {why}"
print("   it clears the limits and reports 0/0                   OK")

found_cb = body(INO, "static void on_limits_found(")
fail_at  = found_cb.index("if (mount.limitFindFailed())")
set_at   = found_cb.index("_cfg.limits_set[(int)axis] = true;")
assert fail_at < set_at and "return;" in found_cb[fail_at:set_at], \
    "on_limits_found stores a failed find as limits_set = true, 0..0"
assert "send_limits_gone(axis);" in found_cb[fail_at:set_at]
home_cb = body(INO, "static void on_home_complete(")
assert "if (mount.limitFindFailed()) send_limits_gone(axis);" in home_cb, \
    "a home that found no stop does not tell the app"
assert home_cb.index("send_limits_gone") < home_cb.index("CMD_HOME_COMPLETE"), \
    "HOME_COMPLETE goes before the failure, so the display re-enables first"
print("   EEPROM forgets them too, and a failed home says so     OK")

estop = body(CPP, "void MountMotion::emergencyStop()")
assert "_restoreDriverAfterFind(_lf_axis);" in estop, \
    "a find cancelled from the app leaves the axis on its reduced find current"
print("   Stop mid-find puts the full current back               OK")

# ---- 4. and the app shows it as a failure -----------------------------------
print("\n4. what the PC shows:")
from ui.dialogs.find_limits_dialog import FindLimitsDialog


class _CR:
    slider_end_margin_mm = 0


class _FSt:
    last_config_report = _CR()


class _FMM(QObject):
    limits_found = pyqtSignal(int, int, int, int)
    def send_find_limits(self, *a): pass
    def send_e_stop(self, *a): pass
    def state(self, m): return _FSt()


fmm = _FMM()
dlg = FindLimitsDialog(1, fmm, Axis.ZOOM, stall_threshold=80)
dlg._running = True
fmm.limits_found.emit(1, int(Axis.ZOOM), 0, 0)
app.processEvents()
status, result = dlg._status.text(), dlg._result.text()
assert "no end stop found" in status, \
    f"a find that found no end reads as {status!r}"
assert "Travel" not in result and "raise the" in result and "(80)" in result, \
    f"the failure does not say what to change, or with what: {result!r}"
assert not dlg._running, "the dialog still thinks the find is running"
print(f"   {status}  {result[:60]}…   OK")

dlg._running = True
fmm.limits_found.emit(1, int(Axis.ZOOM), 400, 9200)
app.processEvents()
assert dlg._status.text() == "Done." and "Travel: 8800 steps" in dlg._result.text(), \
    f"a real result after a failure reads {dlg._status.text()!r} / {dlg._result.text()!r}"
assert "#4CAF50" in dlg._result.styleSheet(), \
    "a real result after a failure is still in the failure's colour"
print("   a good run afterwards reads as one, in green           OK")

import ui.main_window as mw


class _Mc:
    zoom_min = 400; zoom_max = 9200; slider_min = 1; slider_max = 2


class _Cfg:
    def __init__(self): self.mc = _Mc()
    def mount(self, i): return self.mc


class _Label:
    text = ""
    def setText(self, t): self.text = t


class _Win:
    _config = _Cfg()
    _status_label = _Label()


saved = []
mw.save_config = lambda cfg: saved.append(cfg)
win = _Win()
mw.MainWindow._on_limits_found(win, 1, int(Axis.ZOOM), 0, 0)
assert win._config.mc.zoom_min is None and win._config.mc.zoom_max is None, \
    "the PC's saved zoom range survives a find that cleared it on the mount"
assert "no end stop found" in win._status_label.text, \
    f"the status line reads {win._status_label.text!r}"
assert "0 → 0" not in win._status_label.text
assert saved, "the cleared range is not saved"
print(f"   status line: {win._status_label.text[:52]}…   OK")

print("\nALL CHECKS PASSED")
