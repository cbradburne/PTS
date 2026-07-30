import sys, os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, ".")
from PyQt6.QtWidgets import (QApplication, QWidget, QFormLayout, QSpinBox,
                             QDoubleSpinBox, QLineEdit, QPushButton)
from PyQt6.QtCore import Qt
from ui.numeric_keypad import NumericKeypad

app = QApplication(sys.argv)

dlg = QWidget(); form = QFormLayout(dlg)
speed = QSpinBox(); speed.setRange(1, 720); speed.setValue(100)
accel = QSpinBox(); accel.setRange(1, 2000); accel.setValue(500)
dz    = QDoubleSpinBox(); dz.setRange(0.0, 1.0); dz.setDecimals(2); dz.setValue(0.08)
name  = QLineEdit("Cam 1")
btn   = QPushButton("Not an input")
form.addRow("Speed (deg/s):", speed)
form.addRow("Accel (deg/s2):", accel)
form.addRow("Joystick deadzone:", dz)
form.addRow("Name:", name)
form.addRow("Button:", btn)
dlg.show(); dlg.activateWindow(); app.processEvents()

kp = NumericKeypad.install(dlg)
fails = []
def check(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label:44s} got={got!r} want={want!r}")

print("1. Focus a spin box -> keypad appears, captioned with its label")
btn.setFocus(); app.processEvents()          # park focus off the fields first
kp.hide(); kp._dismissed = False
speed.setFocus(); app.processEvents()
check("keypad visible", kp.isVisible(), True)
check("caption from QFormLayout label", kp._caption.text(), "Speed (deg/s):")
check("target resolved to the spin box", kp._target is speed, True)

print("2. Typing digits goes into the field (Qt validator applies)")
speed.lineEdit().selectAll()
for d in "250": kp._type(d)
speed.interpretText()
check("typed 250", speed.value(), 250)

print("3. Range enforced by the spin box's own validator (max 720)")
speed.lineEdit().selectAll()
for d in "999": kp._type(d)
speed.interpretText()
# The third 9 is REJECTED (999 > 720) and never reaches the field, leaving 99 —
# identical to typing 999 on a hardware keyboard.  Verified against raw
# QKeyEvents to confirm this is Qt's behaviour and not ours.
check("999 -> 99, third keystroke refused", speed.value(), 99)
speed.lineEdit().selectAll()
for d in "700": kp._type(d)
speed.interpretText()
check("700 accepted (in range)", speed.value(), 700)

print("4. Backspace and Clear")
accel.setFocus(); app.processEvents()
accel.lineEdit().selectAll()
for d in "150": kp._type(d)
kp._send_key(Qt.Key.Key_Backspace); accel.interpretText()
check("150 backspace -> 15", accel.value(), 15)
kp._clear()
for d in "42": kp._type(d)
accel.interpretText()
check("clear then 42", accel.value(), 42)

print("5. Decimal point only where it means something")
dz.setFocus(); app.processEvents()
dz.lineEdit().selectAll()
for ch in "0.25": kp._type(ch)
dz.interpretText()
check("double spin accepts 0.25", dz.value(), 0.25)
speed.setFocus(); app.processEvents()
before = speed.value()
kp._type(".")
speed.interpretText()
check("int spin ignores '.'", speed.value(), before)

print("6. Next field skips the button, lands on the next input")
speed.setFocus(); app.processEvents()
kp._next_field(); app.processEvents()
check("speed -> accel", app.focusWidget() in (accel, accel.lineEdit()), True)
kp._next_field(); app.processEvents()
check("accel -> deadzone", app.focusWidget() in (dz, dz.lineEdit()), True)
kp._next_field(); app.processEvents()
check("deadzone -> name (skips button)", app.focusWidget() is name, True)

print("7. Keypad buttons never take focus (the fragile bit)")
nofocus = [b.focusPolicy() == Qt.FocusPolicy.NoFocus
           for b in kp.findChildren(QPushButton)]
check("all buttons NoFocus", all(nofocus), True)
check("button count", len(nofocus) >= 16, True)

print("8. Close hides it; refocusing the SAME field leaves it hidden")
name.setFocus(); app.processEvents()
kp._on_close()
check("hidden after Close", kp.isVisible(), False)
name.clearFocus(); name.setFocus(); app.processEvents()
check("same field does not re-summon", kp.isVisible(), False)
print("   ...but a different field does")
speed.setFocus(); app.processEvents()
check("different field re-summons", kp.isVisible(), True)

print("9. Dialog closing hides the keypad")
dlg.close(); app.processEvents()
check("hidden with owner", kp.isVisible(), False)

print()
print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
