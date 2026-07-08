"""
Virtual on-screen keyboard control.

The deployment PC is a 21" touchscreen with no physical keyboard, so any text
entry (renaming a camera or a stored position while in Edit mode) needs the
Windows on-screen keyboard (osk.exe) shown for the duration of the input dialog.

Public API:
    show()   — launch the OSK (Windows only; no-op elsewhere)
    hide()   — close the OSK we launched (Windows only; no-op elsewhere)

Typical use wraps a modal input dialog:

    virtual_keyboard.show()
    try:
        text, ok = QInputDialog.getText(...)
    finally:
        virtual_keyboard.hide()

Because a QInputDialog accepts on Enter/Return (and on OK), the getText() call
returns as soon as the user commits, at which point hide() closes the keyboard —
satisfying "close when OK, Enter, or Return is pressed".
"""
from __future__ import annotations

import os
import sys
import logging

log = logging.getLogger(__name__)

# Full path to the Windows on-screen keyboard.  Using the explicit System32
# path (rather than relying on PATH) matches the approach already proven to
# work on this deployment.
_OSK_PATH = r"C:\Windows\System32\osk.exe"


def show() -> None:
    """Launch the Windows on-screen keyboard.  No-op on non-Windows platforms.

    osk.exe is single-instance and does not steal focus (it is an accessibility
    tool), so calling this while a modal dialog is open leaves keyboard focus on
    the dialog's input field.  Launch failures are logged, not raised — text
    entry must still work if a physical keyboard happens to be attached.
    """
    if sys.platform != "win32":
        return
    try:
        os.startfile(_OSK_PATH)   # type: ignore[attr-defined]  # Windows-only
    except Exception as e:
        log.warning("Could not launch on-screen keyboard (%s): %s", _OSK_PATH, e)


def hide() -> None:
    """Close the Windows on-screen keyboard.  No-op on non-Windows platforms.

    NOTE: `taskkill /F /IM osk.exe` is deliberately NOT used — it returns
    access-denied because of the on-screen keyboard's integrity level and leaves
    it on screen.  `wmic process where name="osk.exe" delete` closes it reliably
    (confirmed on the deployment PC).  Run through the shell (matching the
    validated command) but with a hidden window so nothing flashes on the kiosk.
    """
    if sys.platform != "win32":
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


def get_text(parent, title: str, prompt: str, text: str = "") -> tuple[str, bool]:
    """Touch-friendly drop-in replacement for QInputDialog.getText().

    Returns (entered_text, ok).  Shows the on-screen keyboard for the lifetime of
    the dialog and closes it as soon as the dialog commits.

    The important difference from QInputDialog: the line edit's returnPressed is
    wired *explicitly* to accept().  QInputDialog relies on implicit default-
    button activation, which the on-screen keyboard's synthetic Enter key does
    not reliably trigger — so its Enter would leave the dialog (and the keyboard)
    open.  With the explicit connection, tapping the OSK's Enter commits the
    dialog, exec() returns, and the keyboard is closed in the finally below.
    """
    from PyQt6.QtWidgets import (
        QApplication, QDialog, QVBoxLayout, QLabel, QLineEdit, QDialogButtonBox,
    )

    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setModal(True)
    dlg.setMinimumWidth(460)           # comfortable touch width

    lay = QVBoxLayout(dlg)
    lbl = QLabel(prompt)
    lbl.setWordWrap(True)
    lay.addWidget(lbl)

    edit = QLineEdit(text)
    edit.selectAll()
    edit.setMinimumHeight(44)          # touch target
    lay.addWidget(edit)

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
    edit.returnPressed.connect(dlg.accept)   # ← OSK Enter reliably commits

    # The OSK covers roughly the bottom half of the screen, so pin the dialog
    # near the top (centred horizontally) where it stays fully visible.
    dlg.adjustSize()
    screen = (parent.screen() if parent is not None else None) or QApplication.primaryScreen()
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
        hide()
    return edit.text(), accepted
