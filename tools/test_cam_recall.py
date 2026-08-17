"""Close-and-reopen recall, for every control on the advanced panel.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

from PyQt6.QtWidgets import QApplication
app = QApplication([])

from comms.mount_manager import MountManager, MountState_
from comms.protocol import ISO_STEPS
from ui.dialogs.camera_advanced_dialog import CameraAdvancedDialog, _SHUTTERS

sent = []


class S:
    def connect(self, *a): pass


class M:
    """Real MountState + real cam_sent/cam_known, fake transport."""
    def __init__(self):
        self.cam_status_received = S()
        self._states = {i: MountState_(mount_id=i) for i in range(1, 6)}

    def state(self, m): return self._states[m]
    def cam_ble_state(self, m): return True
    def _send(self, pkt): sent.append(pkt)

    _note_cam       = MountManager._note_cam
    cam_known       = MountManager.cam_known
    send_cam_iso          = MountManager.send_cam_iso
    send_cam_nd           = MountManager.send_cam_nd
    send_cam_shutter_speed= MountManager.send_cam_shutter_speed
    send_cam_iris         = MountManager.send_cam_iris
    send_cam_zoom_norm    = MountManager.send_cam_zoom_norm
    send_cam_focus        = MountManager.send_cam_focus
    send_cam_lift         = MountManager.send_cam_lift
    send_cam_gamma        = MountManager.send_cam_gamma
    send_cam_gain_cc      = MountManager.send_cam_gain_cc
    send_cam_contrast     = MountManager.send_cam_contrast
    send_cam_hue_sat      = MountManager.send_cam_hue_sat
    send_cam_luma_mix     = MountManager.send_cam_luma_mix
    send_cam_cc_reset     = MountManager.send_cam_cc_reset
    send_cam_white_balance= MountManager.send_cam_white_balance
    def __getattr__(self, n):
        if n.startswith("send_cam_"):
            return lambda *a, **k: None
        raise AttributeError(n)


mm = M()

# ---- set every control on CAM 1 ------------------------------------------
d = CameraAdvancedDialog(mm, mount_id=1); d.resize(1500, 1000); d.show(); app.processEvents()
d._iso.set_value_of(1250)
d._shut.setCurrentIndex(_SHUTTERS.index(250))
d._nd.setValue(4.0)
d._wb.setValue(3200)
d._sliders["Tint"].setValue(-18)
d._iris.setValue(72); d._zoom.setValue(35); d._focus.setValue(61)
d._sliders["Contrast"].setValue(140); d._sliders["Pivot"].setValue(40)
d._sliders["Saturation"].setValue(160); d._sliders["Lum Mix"].setValue(75)
d._sliders["Hue"].setValue(-30)
d._w_lift.wheel.set_values(0.10, -0.05, -0.05, 0.02)
mm.send_cam_lift(1, 0.10, -0.05, -0.05, 0.02)
d._w_gain.wheel.set_values(1.20, 1.0, 0.80, 1.05)
mm.send_cam_gain_cc(1, 1.20, 1.0, 0.80, 1.05)
app.processEvents()

before = {
    "ISO": d._iso.value_of(),
    "Shutter":  d._shut.currentData(), "ND": d._nd.value(),
    "Balance":  d._wb.value(),         "Tint": d._sliders["Tint"].value(),
    "Iris": d._iris.value(), "Zoom": d._zoom.value(), "Focus": d._focus.value(),
    "Contrast": d._sliders["Contrast"].value(), "Pivot": d._sliders["Pivot"].value(),
    "Saturation": d._sliders["Saturation"].value(), "Lum Mix": d._sliders["Lum Mix"].value(),
    "Hue": d._sliders["Hue"].value(),
    "Lift wheel": d._w_lift.wheel.values(), "Gain wheel": d._w_gain.wheel.values(),
}
d.close()

# ---- reopen ---------------------------------------------------------------
d2 = CameraAdvancedDialog(mm, mount_id=1); d2.resize(1500, 1000); d2.show(); app.processEvents()
after = {
    "ISO": d2._iso.value_of(),
    "Shutter":  d2._shut.currentData(), "ND": d2._nd.value(),
    "Balance":  d2._wb.value(),         "Tint": d2._sliders["Tint"].value(),
    "Iris": d2._iris.value(), "Zoom": d2._zoom.value(), "Focus": d2._focus.value(),
    "Contrast": d2._sliders["Contrast"].value(), "Pivot": d2._sliders["Pivot"].value(),
    "Saturation": d2._sliders["Saturation"].value(), "Lum Mix": d2._sliders["Lum Mix"].value(),
    "Hue": d2._sliders["Hue"].value(),
    "Lift wheel": d2._w_lift.wheel.values(), "Gain wheel": d2._w_gain.wheel.values(),
}

print(f"{'control':<12} {'set to':>22}  {'on reopen':>22}")
bad = []
for k in before:
    ok = before[k] == after[k]
    if not ok:
        bad.append(k)
    print(f"{k:<12} {str(before[k]):>22}  {str(after[k]):>22}  {'' if ok else '<-- LOST'}")
assert not bad, f"lost on reopen: {bad}"

# ---- a camera report must override what we sent --------------------------
# Stamped, as the real receive path does: precedence is by recency now, so an
# unstamped report is one from before time began and correctly loses.
import time as _t
mm.state(1).cam_adv["luma_mix"] = 0.15
mm.state(1).cam_iso = 800
mm.state(1).cam_heard_at["luma_mix"] = _t.monotonic()
mm.state(1).cam_heard_at["iso"] = _t.monotonic()
d3 = CameraAdvancedDialog(mm, mount_id=1); d3.show(); app.processEvents()
print(f"\ncamera says lum mix 0.15 (we sent 0.75) -> panel shows {d3._sliders['Lum Mix'].value()/100:.2f}")
print(f"camera says ISO 800  (we sent 1250)  -> panel shows {d3._iso.value_of()}")
assert d3._sliders["Lum Mix"].value() == 15 and d3._iso.value_of() == 800, "camera report did not win"
# And the iris contract: the camera does NOT move that slider.
mm.state(1).cam_adv["iris"] = 0.15
mm.state(1).cam_heard_at["iris"] = _t.monotonic()
d3._touched.clear(); d3._show_known()
print(f"camera says iris 0.15 (we sent 0.72) -> slider stays at {d3._iris.value()/100:.2f}  <- by design")
assert d3._iris.value() == 72, "the camera moved the iris slider"

# ---- switching camera loads THAT camera, not the last one ----------------
mm.send_cam_iso(4, 3200)
d3._cam_btns[4].click(); app.processEvents()
print(f"\nswitch to Cam 4 -> ISO {d3._iso.value_of()}")
assert d3._iso.value_of() == 3200
d3._cam_btns[1].click(); app.processEvents()
print(f"back to Cam 1   -> ISO {d3._iso.value_of()}")
assert d3._iso.value_of() == 800

# ---- reset clears the remembered grade -----------------------------------
d3._reset_all(); app.processEvents()
d4 = CameraAdvancedDialog(mm, mount_id=1); d4.show(); app.processEvents()
print(f"\nafter Reset All, reopen -> lift {d4._w_lift.wheel.values()}, "
      f"gain wheel {d4._w_gain.wheel.values()}")
assert d4._w_lift.wheel.values() == (0.0, 0.0, 0.0, 0.0)
assert d4._w_gain.wheel.values() == (1.0, 1.0, 1.0, 1.0)

print("\nALL CHECKS PASSED")
