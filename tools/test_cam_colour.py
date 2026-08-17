"""Colour-correction maths and the wheel widget: encoding, rim behaviour,
puck round-trip, and layout geometry.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))
import math

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt
app = QApplication([])

from comms import protocol as P
from ui.widgets.colour_wheel import ColourWheel
from ui.dialogs.camera_advanced_dialog import CameraAdvancedDialog

sent = []


class FakeMM:
    from PyQt6.QtCore import pyqtSignal
    def __init__(self):
        class S:
            def connect(self, *a): pass
        self.cam_status_received = S()
    def state(self, m): return type("St", (), {"cam_wb": None, "cam_tint": None, "cam_adv": {}})()
    def cam_ble_state(self, m): return True
    def __getattr__(self, name):
        if name.startswith("send_cam_"):
            def f(*a, **k):
                sent.append((name, a, k))
                # Route through the REAL encoder so a bad value raises here.
                fn = getattr(P, "pkt_" + name[5:], None) or getattr(P, "pkt_cam_" + name[9:], None)
                if fn: fn(*a, **k)
            return f
        raise AttributeError(name)


# ---- 1. the crash: white balance signature -------------------------------
from comms.mount_manager import MountManager
import inspect
sig = inspect.signature(MountManager.send_cam_white_balance)
print("1. send_cam_white_balance", sig)
assert "tint" in sig.parameters, "tint param missing"

# real encoder with negative tint
pkt = P.pkt_cam_white_balance(1, 5600, -37)
dec = P.decode_cam_status(pkt[P.HEADER_LEN:] if hasattr(P, "HEADER_LEN") else pkt)
print("   negative tint encodes ok, len", len(pkt))

# ---- 2. gain wheel neutral ------------------------------------------------
g = ColourWheel("Gain", span=1.0, centre=1.0, lo=0.0, hi=16.0)
g.resize(200, 260)
print("2. gain at rest      ", tuple(round(v, 3) for v in g.values()))
assert g.values() == (1.0, 1.0, 1.0, 1.0), "gain neutral is not unity"

lift = ColourWheel("Lift", span=0.5, centre=0.0, lo=-2.0, hi=2.0)
print("   lift at rest      ", lift.values())
assert lift.values() == (0.0, 0.0, 0.0, 0.0)

# drag the gain puck all the way round; nothing may go negative or dark
c, rad = g._ring()


class Ev:
    def __init__(self, x, y): self._p = type("P", (), {"x": lambda s: x, "y": lambda s: y})()
    def position(self): return self._p


worst = 99.0
for deg in range(0, 360, 15):
    a = math.radians(deg)
    g._apply(Ev(c.x() + rad * 0.86 * math.cos(a), c.y() + rad * 0.86 * math.sin(a)))
    r, gg, b, y = g.values()
    worst = min(worst, r, gg, b)
    assert r >= 0.0 and gg >= 0.0 and b >= 0.0, f"negative gain at {deg}deg: {g.values()}"
    assert max(r, gg, b) > 0.5, f"all channels dark at {deg}deg: {g.values()}"
print(f"   24 directions at rim: lowest channel {worst:.3f} (was ~0.0 = black)")

# mean should stay at the neutral -> a tint, not a level change
g._apply(Ev(c.x() + rad * 0.5, c.y()))
r, gg, b, y = g.values()
print(f"   mid-drag mean {(r+gg+b)/3:.4f} (neutral 1.0), values {r:.2f} {gg:.2f} {b:.2f}")

# ---- 3. puck round-trips --------------------------------------------------
for deg in (0, 45, 130, 200, 305):
    a = math.radians(deg)
    px, py = c.x() + rad * 0.6 * math.cos(a), c.y() + rad * 0.6 * math.sin(a)
    g._apply(Ev(px, py))
    back = g._puck()
    err = math.hypot(back.x() - px, back.y() - py)
    assert err < 2.0, f"puck round-trip off by {err:.1f}px at {deg}deg"
print("3. puck round-trip     max error < 2px over 5 angles")

# ---- 4. master strip sits under the ring ---------------------------------
for h in (240, 320, 460):
    g.resize(210, h)
    cc, rr = g._ring()
    gap = g._strip_top() - (cc.y() + rr)
    print(f"4. h={h:3d}  ring bottom {cc.y()+rr:6.1f}  strip {g._strip_top():4d}  gap {gap:.1f}px")
    assert 0 < gap < 12, "master strip drifted away from the wheel"

# ---- 5. the dialog: build, tint, layout ----------------------------------
d = CameraAdvancedDialog(FakeMM(), mount_id=1)
d.resize(1180, 720)
d.show()
app.processEvents()

sent.clear()
d._sliders["Tint"].setValue(-12)
app.processEvents()
print("5. tint -> ", sent)
assert sent and sent[0][0] == "send_cam_white_balance", "tint sent nothing"
assert sent[0][1] == (1, 5600, -12), f"wrong wb args {sent[0][1]}"

d._sliders["Tint"].setValue(3); d._sliders["Tint"].setValue(0)
d._wb.setValue(6500)
app.processEvents()
print("   step up/down + balance ok, %d packets" % len(sent))

names = list(d._sliders)
print("   sliders (%d):" % len(names), names)
assert len(names) == 6, "expected six sliders"


def cx(w):
    return w.mapTo(d, w.rect().center()).x()


cols = {"Lift": (d._w_lift, ["Contrast", "Pivot"]),
        "Gamma": (d._w_gamma, ["Saturation", "Lum Mix"]),
        "Gain": (d._w_gain, ["Hue", "Tint"])}
for title, (wheel, members) in cols.items():
    wx = cx(wheel)
    for m in members:
        off = abs(cx(d._sliders[m]) - wx)
        print(f"   {title:6s} centre {wx:4d} | {m:10s} centre {cx(d._sliders[m]):4d}  off by {off}px")
        assert off <= 2, f"{m} not centred under {title}"

# the sliders must sit BELOW their wheel, in the given order
for title, (wheel, members) in cols.items():
    wb = wheel.mapTo(d, wheel.rect().bottomLeft()).y()
    ys = [d._sliders[m].mapTo(d, d._sliders[m].rect().topLeft()).y() for m in members]
    assert ys[0] > wb and ys[1] > ys[0], f"{title} column out of order"
print("   all six sit below their wheel, in reference order")

# Tint must be gone from the LEFT camera panel — it is legitimately in the
# Gain column now, so scope the search by x position.
from PyQt6.QtWidgets import QLabel
split = cx(d._w_lift) - d._w_lift.width() // 2      # left edge of the wheels
tints = [(w.text(), w.mapTo(d, w.rect().center()).x())
         for w in d.findChildren(QLabel) if w.text() == "Tint"]
print("   'Tint' labels at x:", [x for _t, x in tints], " (camera panel ends ~%d)" % split)
assert all(x > split for _t, x in tints), "Tint still on the left"
assert len(tints) == 1, "expected exactly one Tint label"

sent.clear()
d._sliders["Tint"].setValue(-12)
app.processEvents()
print("   tint slider ->", sent[-1])
assert sent[-1] == ("send_cam_white_balance", (1, 6500, -12), {}), sent[-1]

sent.clear()
d._w_gain.wheel._apply(Ev(*[v + 30 for v in (d._w_gain.wheel._ring()[0].x(),
                                             d._w_gain.wheel._ring()[0].y())]))
app.processEvents()
print("   gain wheel drag ->", sent[0][0], tuple(round(v, 3) for v in sent[0][1][1:]))

sent.clear()
d._reset_all()
print("   reset all -> gain wheel now", d._w_gain.wheel.values())
assert d._w_gain.wheel.values() == (1.0, 1.0, 1.0, 1.0)

print("\nALL CHECKS PASSED")
