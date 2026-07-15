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

import sys
import logging
import logging.handlers
from pathlib import Path

# cv2 must be imported before pygame to avoid duplicate SDL2 library warnings
# on macOS — both packages bundle libSDL2 and the first one loaded wins.
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
    store    = PositionStore()
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