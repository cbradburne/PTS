"""
Camera Mount Controller — entry point.

Run with:
    python main.py
"""

# python3 -m pip install -r requirements.txt
# python3 -m pip install opencv-contrib-python
# python3 -m pip install ultralytics

# python -m pip install -r requirements.txt
# python -m pip install opencv-contrib-python
# python -m pip install ultralytics
# python -m pip install --force-reinstall opencv-contrib-python

from __future__ import annotations

import os
import sys
import logging
import logging.handlers
from pathlib import Path

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")   # no stdout banner

# cv2 and pygame each bundle their own libSDL2.  On macOS, the moment the
# second copy loads, the Objective-C runtime prints ~17 "Class SDLxxx is
# implemented in both…" warnings — written straight to file descriptor 2,
# bypassing Python, so import order and logging config can't stop them.
# Harmless for this app (nothing uses cv2's SDL-backed GUI; frames render via
# Qt), so load both libraries inside a brief OS-level stderr quiet window.
if sys.platform == "darwin":
    _stderr_fd = os.dup(2)
    _devnull   = os.open(os.devnull, os.O_WRONLY)
    os.dup2(_devnull, 2)
    try:
        import cv2     # noqa: F401
        import pygame  # noqa: F401  (cached — motion.joystick reuses it)
    finally:
        os.dup2(_stderr_fd, 2)
        os.close(_devnull)
        os.close(_stderr_fd)
else:
    import cv2  # noqa: F401

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

from comms.bridge import Bridge
from comms.mount_manager import MountManager
from config.mount_config import load_config
from config.position_store import PositionStore
from motion.joystick import JoystickHandler
from ui.main_window import MainWindow

# ── Logging: console + rotating file ────────────────────────────────────────
# The file persists comms diagnostics (HEALTH lines + the WEDGE host/hub verdict)
# so a wedge that happens unattended/overnight survives terminal scrollback.
# Temporary diagnostic scaffolding — once the USB wedge is root-caused this can
# revert to a plain console-only basicConfig.
_LOG_FMT = "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s"
_log_handlers: list[logging.Handler] = [logging.StreamHandler()]
try:
    _log_dir = Path(__file__).resolve().parent / "logs"
    _log_dir.mkdir(exist_ok=True)
    # 10 MB × 10 files ≈ a couple of weeks of 24/7 logs — enough to catch a
    # sporadic wedge — and utf-8 so the →/←/— glyphs write cleanly on Windows.
    _log_handlers.append(logging.handlers.RotatingFileHandler(
        _log_dir / "comms.log", maxBytes=10 * 1024 * 1024, backupCount=10,
        encoding="utf-8"))
except OSError as e:
    # A logging-setup failure must never stop the app launching.
    print(f"WARNING: could not open log file: {e}", file=sys.stderr)

logging.basicConfig(level=logging.INFO, format=_LOG_FMT, handlers=_log_handlers)
log = logging.getLogger(__name__)

def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # Dark palette
    from PyQt6.QtGui import QPalette, QColor
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window,          QColor("#121212"))
    palette.setColor(QPalette.ColorRole.WindowText,      QColor("#CFD8DC"))
    palette.setColor(QPalette.ColorRole.Base,            QColor("#1E1E1E"))
    palette.setColor(QPalette.ColorRole.AlternateBase,   QColor("#2A2A2A"))
    palette.setColor(QPalette.ColorRole.ToolTipBase,     QColor("#263238"))
    palette.setColor(QPalette.ColorRole.ToolTipText,     QColor("#CFD8DC"))
    palette.setColor(QPalette.ColorRole.Text,            QColor("#CFD8DC"))
    palette.setColor(QPalette.ColorRole.Button,          QColor("#263238"))
    palette.setColor(QPalette.ColorRole.ButtonText,      QColor("#CFD8DC"))
    palette.setColor(QPalette.ColorRole.BrightText,      QColor("#FFFFFF"))
    palette.setColor(QPalette.ColorRole.Link,            QColor("#4FC3F7"))
    palette.setColor(QPalette.ColorRole.Highlight,       QColor("#1565C0"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    #palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#A2A500"))
    app.setPalette(palette)

    # Core objects
    config   = load_config()
    from ui import virtual_keyboard
    virtual_keyboard.set_enabled(config.virtual_keyboard)
    store    = PositionStore()
    # Names (50 positions + 5 cameras): load the startup defaults from
    # ~/Documents/PTS/Default.json (created from the numeric/"Cam N" defaults
    # on first run).  See config/name_store.py.
    from config import name_store
    name_store.load_default(store, config)
    bridge   = Bridge()
    mm       = MountManager(bridge)
    joystick = JoystickHandler(deadzone=config.joystick_deadzone)
    joystick.init()

    # OSC control surface (Bitfocus Companion / QLab) — see docs/companion.md
    osc = None
    if config.osc_enabled:
        from comms.osc_server import OscServer
        osc = OscServer(mm, port=config.osc_port)
        osc.start()   # logs + returns False on bind failure; app runs regardless
        app.aboutToQuit.connect(osc.stop)

    window = MainWindow(config, bridge, mm, store, joystick)
    window.show()

    sys.exit(app.exec())

if __name__ == "__main__":
    main()