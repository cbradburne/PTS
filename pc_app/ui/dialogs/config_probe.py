"""TEMPORARY diagnostic — bisecting the macOS fullscreen-Space bug.

Opening Config throws the operator out of the app's native-fullscreen Space.
Four separate fixes to how the window is created and shown changed nothing, and
these have each been ruled out by test rather than argument:

    the numeric keypad          disabled via its own setting — no change
    the launch context          VS Code terminal vs Terminal.app — no change
    an unbundled Python process CVWindow opens fine from the same process
    window flags / activate     Config now matches CVWindow exactly

So the cause is probably not the window at all, but something ConfigDialog DOES
while being built.  This opens a stripped-down stand-in instead, and adds the
real dialog's ingredients back one level at a time.

    PTS_CFGTEST=0 python3 main.py      ... then 1, 2, 3 ... until it breaks

Each level includes every level below it.  The first one that boots you out of
fullscreen is the culprit.  Delete this file and the hook in main_window.py
once we know.
"""
from __future__ import annotations

import os

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QPushButton, QTabWidget, QScrollArea,
    QFormLayout, QSpinBox, QGroupBox,
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont

# Ordered by suspicion, least likely first, so the first break is informative.
LEVELS = {
    0: "bare window — parented QWidget, Dialog flag, one label",
    1: "+ sizing: setMinimumSize, resize, and the self.screen() call",
    2: "+ QTabWidget with 7 tabs",
    3: "+ QScrollArea inside each tab",
    4: "+ 103 QSpinBoxes, the real count, spread across the tabs",
    5: "+ MountManager signal connections",
    6: "+ startup traffic: GET_CONFIG to every mount, mount-table request",
    7: "+ the numeric keypad",
}


def probe_level() -> int | None:
    """Requested level from PTS_CFGTEST, or None when unset (normal Config)."""
    raw = os.environ.get("PTS_CFGTEST")
    if raw is None or raw == "":
        return None
    try:
        return max(0, min(max(LEVELS), int(raw)))
    except ValueError:
        return None


class ConfigProbe(QWidget):
    """Stand-in for ConfigDialog. Same window recipe, adjustable innards."""

    accepted = pyqtSignal()
    finished = pyqtSignal(int)
    names_changed = pyqtSignal()

    def __init__(self, config, mount_manager, bridge, position_store=None,
                 parent=None, level: int = 0):
        # Identical to ConfigDialog and CVWindow — the window itself is not
        # what varies here.
        super().__init__(parent, Qt.WindowType.Dialog)
        self._level = level
        self._mm = mount_manager
        self._config = config
        self._result = 0
        self.setWindowTitle(f"Config PROBE — level {level}")

        note = f"LEVEL {level}: {LEVELS[level]}"
        print(f"[CFGTEST] {note}")

        vl = QVBoxLayout(self)
        title = QLabel(f"Config probe — level {level}")
        title.setFont(QFont("Arial", 15, QFont.Weight.Bold))
        vl.addWidget(title)
        for i in range(level + 1):
            row = QLabel(f"  {i}. {LEVELS[i]}")
            row.setStyleSheet("color:#7fbf72;" if i == level else "color:#8a8a8a;")
            vl.addWidget(row)
        vl.addWidget(QLabel(
            "\nDid opening this throw you out of fullscreen?\n"
            "No  → rerun with the next level up.\n"
            "Yes → this level's addition is the cause."))

        if level >= 1:
            self.setMinimumSize(680, 640)
            scr = self.screen()
            avail_h = scr.availableGeometry().height() if scr else 900
            self.resize(720, max(640, min(820, avail_h - 80)))

        if level >= 2:
            tabs = QTabWidget()
            spins_per_tab = 15          # 7 tabs x 15 ≈ the real 103
            for t in range(7):
                page = QWidget()
                form = QFormLayout(page)
                if level >= 4:
                    box = QGroupBox(f"Presets {t + 1}")
                    bf = QFormLayout(box)
                    for k in range(spins_per_tab):
                        sb = QSpinBox(); sb.setRange(1, 2000); sb.setValue(100 + k)
                        bf.addRow(f"Value {k + 1}:", sb)
                    form.addRow(box)
                else:
                    form.addRow(QLabel(f"tab {t + 1}"))
                if level >= 3:
                    sa = QScrollArea()
                    sa.setWidgetResizable(True)
                    sa.setFrameShape(QScrollArea.Shape.NoFrame)
                    sa.setWidget(page)
                    tabs.addTab(sa, f"Cam {t + 1}")
                else:
                    tabs.addTab(page, f"Cam {t + 1}")
            vl.addWidget(tabs, 1)

        close = QPushButton("Close")
        close.setFixedHeight(40)
        close.clicked.connect(self.reject)
        vl.addWidget(close)

        if level >= 5:
            self._mm.position_updated.connect(self._noop2)
            self._mm.config_report_received.connect(self._noop2)
            self._mm.mount_table_updated.connect(self._noop1)

        if level >= 6:
            for mid in range(1, 6):
                st = self._mm.state(mid)
                if st and getattr(st, "connected", False):
                    self._mm.send_get_config(mid)
            self._mm.request_mount_table()

        if level >= 7:
            from ui.numeric_keypad import NumericKeypad
            NumericKeypad.install(self)

    def _noop1(self, *_a):  pass
    def _noop2(self, *_a):  pass

    # ConfigDialog-compatible surface so main_window needs no special casing.
    def accept(self) -> None:
        self._result = 1
        self.accepted.emit()
        self.close()

    def reject(self) -> None:
        self._result = 0
        self.close()

    def closeEvent(self, event) -> None:
        self.finished.emit(self._result)
        super().closeEvent(event)
