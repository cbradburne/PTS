"""The look-at calibration popup shows the carriage while the slider travels.

Find Limits showed the travel animation and the look-at "moving to far end"
popup did not. They are the same situation — the mount is moving, reports no
position, and the operator can only wait — so the same picture belongs in both.

There were two calibration UIs and only one had it. SubjectCalibrationDialog had
the animation all along, but nothing could reach it: the SubjectGrid that opened
it was never instantiated anywhere, and both had been broken against the current
API since the initial commit. Both have since been deleted.
main_window._CalibPopup, which is what pressing a subject slot actually opens,
was the one in use and was built without an animation.

The animation is REMOVED when the slider arrives, not parked. This popup is
small and the operator's next job is to aim the camera; a carriage sitting in
the middle of it with nothing left to report is just something to look at.

Unlike most checks here this one builds the real dialog and drives its state
machine, so it tests behaviour rather than the source text.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

from PyQt6.QtWidgets import QApplication
from comms.protocol import CalibPrompt

app = QApplication.instance() or QApplication([])

from ui.main_window import _CalibPopup


class StubMM:
    """Only what the popup calls on it."""
    def __init__(self):
        self.calls = []
    def send_get_subjects(self, mid):        self.calls.append(("get_subjects", mid))
    def send_add_subject_set_b(self, mid):   self.calls.append(("set_b", mid))
    def send_add_subject_abort(self, mid):   self.calls.append(("abort", mid))


def popup():
    return _CalibPopup(5, "Lectern", StubMM())


def state(d):
    """(hidden, timer running) — 'removed' has to mean both."""
    return d._anim.isHidden(), d._anim._timer.isActive()


# ---- 1. it opens already travelling ----------------------------------------
print("1. when the popup opens:")
d = popup()
hidden, running = state(d)
assert not hidden, "the animation is hidden the moment the popup opens"
assert running, "the animation is not running — the slider is already travelling"
print("   carriage visible and moving                        OK")

# The popup exists BECAUSE the move started, so it must not sit still waiting
# for a MOVING_TO_B prompt that has probably already been and gone.
print("   does not wait for a prompt it may have missed      OK")

# ---- 2. it keeps running while the slider travels --------------------------
print("\n2. while the slider travels:")
d.update_prompt(int(CalibPrompt.MOVING_TO_B))
hidden, running = state(d)
assert not hidden and running, "MOVING_TO_B does not show a moving carriage"
print("   MOVING_TO_B: visible and moving                    OK")

# ---- 3. and is REMOVED on arrival ------------------------------------------
print("\n3. when the slider arrives:")
d.update_prompt(int(CalibPrompt.WAIT_SET_B))
hidden, running = state(d)
assert hidden, "WAIT_SET_B still shows the carriage — it should be removed"
assert not running, "the animation timer still runs behind a hidden widget"
print("   WAIT_SET_B: removed, and the timer stopped         OK")

# The Set button is the operator's next action, and must be live.
assert d._set_btn.isEnabled(), "Set is not enabled at WAIT_SET_B"
assert d._can_set, "the popup will ignore a press of Set"
print("   Set is enabled — nothing else changed              OK")

# ---- 4. every other end state also removes it ------------------------------
print("\n4. the states after the move:")
for prompt, label in ((CalibPrompt.SOLVED, "SOLVED"), (CalibPrompt.ERROR, "ERROR ")):
    d = popup()
    assert not state(d)[0], "precondition: the animation starts visible"
    d.update_prompt(int(prompt))
    hidden, running = state(d)
    assert hidden and not running, f"{label} leaves the carriage on screen"
    print(f"   {label}: removed                                OK")

# Pressing Set puts it into "Solving…", which is also not travelling.
d = popup()
d.update_prompt(int(CalibPrompt.WAIT_SET_B))
d._on_set()
assert state(d)[0], "the carriage returns while solving"
assert d._mm.calls[-1] == ("set_b", 5), "Set no longer sends the observation"
print("   Solving…: removed, and Set still sends set_b       OK")

# ---- 5. this is the only calibration UI ------------------------------------
# The second one and the panel that opened it are gone. If either comes back,
# the two need to agree about the picture — an operator who learns one rule and
# then meets another has been given a worse UI than either on its own.
print("\n5. no second calibration UI:")
for gone in ("pc_app/ui/widgets/subject_grid.py",
             "pc_app/ui/dialogs/subject_calibration_dialog.py"):
    assert not (REPO / gone).exists(), f"{gone} is back — make it agree with this one"
print("   _CalibPopup is the only one                        OK")

print("\nALL CHECKS PASSED")
