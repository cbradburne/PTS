"""Building a dialog must never show a top-level window.

setVisible(True) on a widget that has no parent yet does not mean "be visible
once shown" — Qt SHOWS IT AS A TOP-LEVEL WINDOW there and then.  ConfigDialog
did this twice per camera tab:

    sl_box.setVisible(mc.has_slider)     # no parent yet -> a real window
    layout.addWidget(sl_box)             # re-parented, window vanishes

So up to ten windows were flashed onto the screen while Config was being built.
On macOS they land on the desktop Space and take the operator out of native
fullscreen — the bug that survived four fixes to the Config window's own flags,
because the offending windows were never the Config window.

Two things make it easy to miss, and both are why it went unnoticed for so long:

  * The window is TRANSIENT.  addWidget() re-parents it a line later, so
    counting top-level widgets after construction finds nothing.  It has to be
    caught with an event filter while it happens.
  * setVisible(FALSE) creates nothing, so a default config (sliders off) hides
    the bug entirely.  It only appears on a rig where mounts have sliders.

The rule: addWidget() first, setVisible() after.
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QObject, QEvent
from PyQt6.QtWidgets import QApplication, QWidget
app = QApplication.instance() or QApplication(sys.argv)

from comms.bridge import Bridge
from comms.mount_manager import MountManager
from config.mount_config import AppConfig
from ui.dialogs.config_dialog import ConfigDialog


class WindowSpy(QObject):
    """Records every top-level widget shown while installed."""
    def __init__(self):
        super().__init__()
        self.shown = []
    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Show and isinstance(obj, QWidget) and obj.isWindow():
            self.shown.append(f"{type(obj).__name__}({obj.title() if hasattr(obj,'title') else obj.windowTitle()!r})")
        return False


# has_slider MUST be True: setVisible(False) on a parentless widget creates
# nothing, so a default config would pass no matter how broken the code is.
cfg = AppConfig()
for mid in range(1, 6):
    cfg.mount(mid).has_slider = True
    cfg.mount(mid).lanc_zoom = False

spy = WindowSpy()
app.installEventFilter(spy)
dlg = ConfigDialog(cfg, MountManager(Bridge()), Bridge())
app.removeEventFilter(spy)

print(f"  top-level windows shown during construction: {len(spy.shown)}")
for w in spy.shown:
    print(f"     {w}")

ok = not spy.shown
print("\nRESULT:", "ALL PASS" if ok else
      "STRAY WINDOWS — a setVisible() runs before its addWidget()")
dlg.deleteLater()
sys.exit(0 if ok else 1)
