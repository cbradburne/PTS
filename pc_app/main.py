"""
Camera Mount Controller — entry point.

Run with:
    python main.py
"""

# pip install -r requirements.txt
# opt/homebrew/bin/python3 -m pip install -r requirements.txt

# python3 -m pip install opencv-python
# python3 -m pip install ultralytics

# source '/Users/col/New CC v2/bin/activate' 
# python3 "/Users/col/New CC v2/pc_app/main.py"

from __future__ import annotations

import sys
import logging

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
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

    window = MainWindow(config, bridge, mm, store, joystick)
    window.show()

    sys.exit(app.exec())

if __name__ == "__main__":
    main()