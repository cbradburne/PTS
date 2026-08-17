"""The 0.8s operator hold: a control stays the operator's while they are using
it, and goes back to following the camera once they stop.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))
import time

from PyQt6.QtWidgets import QApplication
app = QApplication([])
from ui.dialogs.camera_advanced_dialog import CameraAdvancedDialog, _HOLD_S

sent = []
# what the camera claims to hold — deliberately different from what we send
cam = {"wb": 3200, "tint": 25, "adv": {"hue_sat": (0.0, 1.0),
                                       "contrast": (0.5, 1.0), "luma_mix": 0.10,
                                       "nd": 0.0}}


class S:
    def connect(self, *a): pass


class M:
    def __init__(self): self.cam_status_received = S()
    def state(self, m):
        return type("St", (), {"cam_wb": cam["wb"], "cam_tint": cam["tint"],
                               "cam_adv": cam["adv"]})()

    def cam_known(self, m):
        """What the real MountManager.cam_known would return for this camera."""
        return {**cam["adv"], "white_balance": cam["wb"], "tint": cam["tint"]}
    def cam_ble_state(self, m): return True
    def __getattr__(self, n):
        if n.startswith("send_cam_"):
            return lambda *a, **k: sent.append((n, a))
        raise AttributeError(n)


d = CameraAdvancedDialog(M(), mount_id=1)
d.resize(1180, 720); d.show(); app.processEvents()

print("Camera insists: wb=3200 tint=25 iris=0.10\n")

# ---- 1. the fight: click the arrow, camera answers with its stale value ----
d._wb.setValue(5600)
d._wb.stepBy(1); d._wb.stepBy(1); d._wb.stepBy(1)      # three clicks of "up"
after_clicks = d._wb.value()
d._show_known()                                     # camera report lands
print(f"1. three arrow clicks -> {after_clicks} K")
print(f"   camera reports 3200 immediately after -> box now {d._wb.value()} K")
assert d._wb.value() == after_clicks, "camera dragged the value back"

# ---- 2. it releases after the hold ---------------------------------------
time.sleep(_HOLD_S + 0.15)
d._show_known()
print(f"2. after {_HOLD_S}s idle, camera reports again -> {d._wb.value()} K")
assert d._wb.value() == 3200, "panel never resumed following the camera"

# ---- 3. a slider drag is not interrupted ---------------------------------
# Lum Mix rather than Iris: the iris slider deliberately no longer follows
# the camera at all, so it cannot show a hold-off being respected.
iris = d._sliders["Lum Mix"]
iris.setValue(50)
for step in range(50, 71, 5):        # a drag, with a report arriving mid-way
    iris.setValue(step)
    d._show_known()              # camera keeps insisting on 10
print(f"3. dragged Lum Mix 50->70 with reports throughout -> {iris.value()}")
assert iris.value() == 70, f"slider jumped mid-drag to {iris.value()}"
time.sleep(_HOLD_S + 0.15)
d._show_known()
print(f"   after release + {_HOLD_S}s -> {iris.value()} (camera's 0.10)")
assert iris.value() == 10

# ---- 4. tint, the one that was jumping -----------------------------------
tint = d._sliders["Tint"]
tint.setValue(-30)
for _ in range(5):
    d._show_known()
print(f"4. tint set to -30, five camera reports -> {tint.value()}")
assert tint.value() == -30, "tint dragged back by the camera"
time.sleep(_HOLD_S + 0.15)
d._show_known()
print(f"   after {_HOLD_S}s -> {tint.value()} (camera's 25)")
assert tint.value() == 25

# ---- 5. typing is not overwritten ----------------------------------------
d._wb.setFocus()
print(f"5. keyboardTracking off: {not d._wb.keyboardTracking()}  accelerated: {d._wb.isAccelerated()}")
assert not d._wb.keyboardTracking()
d._wb.lineEdit().setText("7200 K")       # mid-typing, no valueChanged yet
d._show_known()
print(f"   typing '7200' while camera reports 3200 -> field shows '{d._wb.lineEdit().text()}'")
assert "7200" in d._wb.lineEdit().text(), "camera overwrote the text being typed"
d._wb.interpretText()
print(f"   committed -> {d._wb.value()} K")
assert d._wb.value() == 7200

# ---- 6. the panel's own updates must not extend the hold ------------------
d._touched.clear()
d._show_known()                      # pure camera update, no user input
held = [w for w in d._touched]
print(f"6. camera-driven update recorded {len(held)} touches (must be 0)")
assert not held, "camera updates are marking themselves as user activity"

print("\nALL CHECKS PASSED")
