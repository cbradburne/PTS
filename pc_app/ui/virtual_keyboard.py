"""
Virtual on-screen keyboard for text entry on touchscreen setups.

All label/name entry in the app goes through get_text() below.  When the
on-screen keyboard is enabled (Config → "On-screen keyboard", persisted as
`virtual_keyboard` in config.json), text entry works with no physical
keyboard attached:

  Windows      — launches the system osk.exe beside the dialog (proven on
                 the deployment touchscreen PC), closed again on commit.
  macOS/Linux  — a keyboard panel is embedded INSIDE the entry dialog
                 itself.  Neither OS offers a dependable way to summon its
                 native on-screen keyboard programmatically (macOS has no
                 public API; Linux varies by desktop), and an embedded
                 panel needs no OS integration at all.

When disabled, get_text() is just a normal styled input dialog — for
setups with a real keyboard and mouse.

Enabled state: set_enabled() is called at startup from main.py and live
from the config dialog when the checkbox changes.
"""
from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger(__name__)

# Full path to the Windows on-screen keyboard.  Using the explicit System32
# path (rather than relying on PATH) matches the approach already proven to
# work on this deployment.
_OSK_PATH = r"C:\Windows\System32\osk.exe"

_enabled = True


def set_enabled(on: bool) -> None:
    """Enable/disable the on-screen keyboard (config checkbox)."""
    global _enabled
    _enabled = bool(on)


def is_enabled() -> bool:
    return _enabled


# ---------------------------------------------------------------------------
# Windows: system osk.exe
# ---------------------------------------------------------------------------

def show() -> None:
    """Launch the Windows on-screen keyboard.  No-op unless enabled + win32.

    osk.exe is single-instance and does not steal focus (it is an accessibility
    tool), so calling this while a modal dialog is open leaves keyboard focus on
    the dialog's input field.  Launch failures are logged, not raised — text
    entry must still work if a physical keyboard happens to be attached.
    """
    if not _enabled or sys.platform != "win32":
        return
    try:
        os.startfile(_OSK_PATH)   # type: ignore[attr-defined]  # Windows-only
    except Exception as e:
        log.warning("Could not launch on-screen keyboard (%s): %s", _OSK_PATH, e)


def hide() -> None:
    """Close the Windows on-screen keyboard.  No-op unless enabled + win32.

    NOTE: `taskkill /F /IM osk.exe` is deliberately NOT used — it returns
    access-denied because of the on-screen keyboard's integrity level and leaves
    it on screen.  `wmic process where name="osk.exe" delete` closes it reliably
    (confirmed on the deployment PC).  Run through the shell (matching the
    validated command) but with a hidden window so nothing flashes on the kiosk.
    """
    if not _enabled or sys.platform != "win32":
        return
    try:
        import subprocess
        subprocess.run(
            'wmic process where name="osk.exe" delete',
            shell=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        log.debug("On-screen keyboard close (wmic) failed: %s", e)


# ---------------------------------------------------------------------------
# macOS / Linux: keyboard panel embedded in the dialog
# ---------------------------------------------------------------------------

_KEY_ROWS = [
    list("1234567890") + ["⌫"],
    list("qwertyuiop"),
    list("asdfghjkl") + ["-"],
    ["⇧"] + list("zxcvbnm") + ["_", "."],
]


def _add_embedded_keyboard(layout, edit) -> None:
    """Build a touch keyboard into `layout`, typing into QLineEdit `edit`.

    Every key uses NoFocus policy so the line edit keeps focus and caret —
    taps insert at the caret exactly like a hardware keyboard.  Shift is
    one-shot (phone-style): next letter is uppercase, then it releases.
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QGridLayout, QPushButton, QWidget

    panel = QWidget()
    grid = QGridLayout(panel)
    grid.setSpacing(5)
    grid.setContentsMargins(0, 8, 0, 0)

    letter_keys: list[QPushButton] = []
    shift_btn: QPushButton | None = None

    def refresh_case(shifted: bool) -> None:
        for b in letter_keys:
            b.setText(b.property("ch").upper() if shifted else b.property("ch"))

    def on_key(ch: str) -> None:
        nonlocal shift_btn
        shifted = shift_btn is not None and shift_btn.isChecked()
        edit.insert(ch.upper() if (shifted and ch.isalpha()) else ch)
        if shifted and ch.isalpha():          # one-shot shift
            shift_btn.setChecked(False)
            refresh_case(False)

    for r, row in enumerate(_KEY_ROWS):
        # centre shorter rows with a leading half-column offset
        col = 0
        for ch in row:
            btn = QPushButton(ch)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setMinimumSize(44, 44)
            if ch == "⌫":
                btn.clicked.connect(edit.backspace)
            elif ch == "⇧":
                btn.setCheckable(True)
                shift_btn = btn
                btn.toggled.connect(refresh_case)
            else:
                btn.setProperty("ch", ch)
                if ch.isalpha():
                    letter_keys.append(btn)
                btn.clicked.connect(lambda _=False, c=ch: on_key(c))
            grid.addWidget(btn, r, col)
            col += 1

    space = QPushButton("space")
    space.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    space.setMinimumHeight(44)
    space.clicked.connect(lambda: edit.insert(" "))
    grid.addWidget(space, len(_KEY_ROWS), 2, 1, 7)

    layout.addWidget(panel)


# ---------------------------------------------------------------------------
# The shared text-entry dialog
# ---------------------------------------------------------------------------

def get_text(parent, title: str, prompt: str, text: str = "") -> tuple[str, bool]:
    """Touch-friendly drop-in replacement for QInputDialog.getText().

    Returns (entered_text, ok).  Behaviour by platform/config:
      - keyboard disabled: plain input dialog
      - Windows + enabled: system osk.exe shown for the dialog's lifetime,
        dialog pinned near the top (the OSK covers the lower half)
      - macOS/Linux + enabled: keyboard panel embedded in the dialog

    The line edit's returnPressed is wired *explicitly* to accept() —
    QInputDialog's implicit default-button activation is not reliably
    triggered by synthetic (on-screen) Enter keys.
    """
    from PyQt6.QtWidgets import (
        QApplication, QDialog, QVBoxLayout, QLabel, QLineEdit, QDialogButtonBox,
    )

    use_osk      = _enabled and sys.platform == "win32"
    use_embedded = _enabled and sys.platform != "win32"

    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setModal(True)
    dlg.setMinimumWidth(560 if use_embedded else 460)

    lay = QVBoxLayout(dlg)
    lbl = QLabel(prompt)
    lbl.setWordWrap(True)
    lay.addWidget(lbl)

    edit = QLineEdit(text)
    edit.selectAll()
    edit.setMinimumHeight(44)          # touch target
    lay.addWidget(edit)

    if use_embedded:
        _add_embedded_keyboard(lay, edit)

    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    lay.addWidget(buttons)
    ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
    cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
    for b in (ok_btn, cancel_btn):
        b.setMinimumHeight(48)
        b.setMinimumWidth(120)
    ok_btn.setDefault(True)
    ok_btn.setAutoDefault(True)

    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    edit.returnPressed.connect(dlg.accept)   # ← on-screen Enter reliably commits

    if use_osk:
        # The OSK covers roughly the bottom half of the screen, so pin the
        # dialog near the top (centred horizontally) where it stays visible.
        dlg.adjustSize()
        screen = (parent.screen() if parent is not None else None) \
                 or QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            x = avail.x() + (avail.width() - dlg.width()) // 2
            y = avail.y() + avail.height() // 12      # ~8% down from the top
            dlg.move(x, y)
        show()

    edit.setFocus()
    try:
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
    finally:
        if use_osk:
            hide()
    return edit.text(), accepted
