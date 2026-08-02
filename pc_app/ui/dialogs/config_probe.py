"""TEMPORARY diagnostic — bisecting the macOS fullscreen-Space bug.

Opening Config throws the operator out of the app's native-fullscreen Space.
Four separate fixes to how the window is created and shown changed nothing, and
these have each been ruled out by test rather than argument:

    the numeric keypad          disabled via its own setting — no change
    the launch context          VS Code terminal vs Terminal.app — no change
    an unbundled Python process CVWindow opens fine from the same process
    window flags / activate     Config now matches CVWindow exactly

This opens a stripped-down stand-in instead and adds the real dialog's
ingredients back one level at a time.

    PTS_CFGTEST=0 python3 main.py      ... then 1, 2, 3 ... until it breaks

Each level includes every level below it, so the first one that boots you out
names the cause.

WHERE THE BISECT STANDS
  levels 0-7   all CLEAN — tabs, scroll areas, 103 spin boxes, signals,
               startup traffic and the keypad are all innocent
  PTS_CFGSKIP  skipping every side effect in the REAL dialog did NOT help,
               so it is not something the dialog DOES
  levels 8-10  the widget types the probe had been missing

If 8-10 are also clean, the probe now contains everything the real dialog has
and still does not reproduce it — which would point at _build()'s structure or
ordering rather than any single ingredient, and the next step is to bisect the
real _build() itself rather than keep rebuilding an imitation of it.

Delete this file and the hook in main_window.py once we know.
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
    # Levels 0-7 all came back CLEAN, and skipping every side effect in the
    # real dialog did NOT help — so the cause is a widget type the probe never
    # created.  Ordered most-suspicious first now, to find it in fewer runs.
    8: "+ 3 QComboBox — the only widget here that owns a native popup window",
    9: "+ QDialogButtonBox (standard buttons, queries the platform style)",
    10: "+ QStackedWidget + QRadioButton + QCheckBox + QLineEdit",
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

        if level >= 8:
            # Three, as in the real dialog: port, CV tracker, CV size.  A
            # QComboBox creates a popup window of its own, which is the one
            # thing in the real dialog that could plausibly interact with a
            # macOS fullscreen Space.
            from PyQt6.QtWidgets import QComboBox
            cbox = QGroupBox("Combo boxes")
            cf = QFormLayout(cbox)
            for name, items in (("Port:", ["/dev/cu.usbmodem2301", "/dev/cu.debug-console"]),
                                ("Tracker:", ["CSRT", "MOSSE", "KCF"]),
                                ("Size:", ["320", "416", "640"])):
                cb = QComboBox(); cb.addItems(items)
                cf.addRow(name, cb)
            vl.addWidget(cbox)

        if level >= 9:
            from PyQt6.QtWidgets import QDialogButtonBox
            bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                  QDialogButtonBox.StandardButton.Cancel)
            bb.accepted.connect(self.accept)
            bb.rejected.connect(self.reject)
            vl.addWidget(bb)

        if level >= 10:
            from PyQt6.QtWidgets import (QStackedWidget, QRadioButton,
                                         QCheckBox, QLineEdit)
            sbox = QGroupBox("Remaining widget types")
            sf = QFormLayout(sbox)
            st = QStackedWidget()
            for t in ("one", "two"):
                pg = QWidget(); QFormLayout(pg).addRow(QLabel(t))
                st.addWidget(pg)
            sf.addRow("Stacked:", st)
            sf.addRow("Radio A:", QRadioButton("serial"))
            sf.addRow("Radio B:", QRadioButton("tcp"))
            sf.addRow("Check:",   QCheckBox("has slider"))
            sf.addRow("Text:",    QLineEdit("Cam 1"))
            vl.addWidget(sbox)

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
