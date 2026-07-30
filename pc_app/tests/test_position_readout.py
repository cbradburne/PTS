import sys, os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, ".")
from PyQt6.QtWidgets import QApplication, QTabWidget, QCheckBox
from config.mount_config import AppConfig
from comms.bridge import Bridge
from comms.mount_manager import MountManager
from comms.protocol import PositionPayload
from ui.dialogs.config_dialog import ConfigDialog

app = QApplication(sys.argv)
mm  = MountManager(Bridge())
cfg = AppConfig()
cfg.mount(1).has_slider = True;  cfg.mount(1).lanc_zoom = False   # slider + stepper zoom
cfg.mount(2).has_slider = False; cfg.mount(2).lanc_zoom = False   # no slider
cfg.mount(3).has_slider = True;  cfg.mount(3).lanc_zoom = True    # LANC zoom
dlg = ConfigDialog(cfg, mm, Bridge()); dlg.show(); app.processEvents()
tabs = dlg.findChildren(QTabWidget)[0]

fails = []
def chk(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label:48s} got={got!r}")

def show_mount(mid):
    """Select the tab owning this mount's labels — rows on a hidden tab
    report isVisible()==False regardless of our own show/hide."""
    lbl = dlg._pos_labels[mid]['pan']
    for i in range(tabs.count()):
        if tabs.widget(i).isAncestorOf(lbl):
            tabs.setCurrentIndex(i); app.processEvents(); return tabs.tabText(i)
    raise AssertionError("no tab owns mount %d" % mid)

print("Rows shown per hardware config:")
for mid, want_sl, want_zm, desc in ((1, True, True,  "slider + stepper zoom"),
                                    (2, False, True, "no slider"),
                                    (3, True, False, "LANC zoom")):
    name = show_mount(mid); L = dlg._pos_labels[mid]
    chk(f"{name} ({desc}): pan",    L['pan'].isVisible(),    True)
    chk(f"{name} ({desc}): tilt",   L['tilt'].isVisible(),   True)
    chk(f"{name} ({desc}): slider", L['slider'].isVisible(), want_sl)
    chk(f"{name} ({desc}): zoom",   L['zoom'].isVisible(),   want_zm)

print("\nLive values (mount 1, slider in motion):")
show_mount(1)
mm.position_updated.emit(1, PositionPayload(12.5, -3.25, 1487.5, 4200, 0b0100))
app.processEvents()
L = dlg._pos_labels[1]
for k in ("pan","tilt","slider","zoom"):
    print(f"      {k:6s} = {L[k].text()!r}")
chk("pan in degrees",        "12.5 °"    in L['pan'].text(),    True)
chk("slider in mm",          "1487.5 mm" in L['slider'].text(), True)
chk("zoom in steps",         "4200 steps" in L['zoom'].text(),  True)
chk("moving axis marked",    "▸" in L['slider'].text(),         True)
chk("still axis unmarked",   "▸" in L['pan'].text(),            False)
chk("moving axis coloured",  L['slider'].styleSheet() != "",    True)

mm.position_updated.emit(1, PositionPayload(12.5, -3.25, 1487.5, 4200, 0))
app.processEvents()
chk("marker cleared when stopped", "▸" in L['slider'].text(),   False)
chk("colour cleared when stopped", L['slider'].styleSheet(),    "")

print("\nToggling hardware checkboxes updates the readout live:")
show_mount(1)
cb = [c for c in tabs.currentWidget().findChildren(QCheckBox)
      if c.text().startswith("Has slider")][0]
cb.setChecked(False); app.processEvents()
chk("untick Has slider -> row hidden", L['slider'].isVisible(), False)
cb.setChecked(True);  app.processEvents()
chk("retick Has slider -> row back",   L['slider'].isVisible(), True)
lz = [c for c in tabs.currentWidget().findChildren(QCheckBox)
      if c.text().startswith("LANC")][0]
lz.setChecked(True); app.processEvents()
chk("tick LANC -> zoom row hidden",    L['zoom'].isVisible(),   False)
lz.setChecked(False); app.processEvents()
chk("untick LANC -> zoom row back",    L['zoom'].isVisible(),   True)

print("\nOther mounts are unaffected by mount 1's packets:")
chk("mount 2 still shows placeholder", dlg._pos_labels[2]['pan'].text(), "—")

print()
print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
dlg.close()
sys.exit(1 if fails else 0)
