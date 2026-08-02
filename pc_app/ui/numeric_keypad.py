"""On-screen numeric keypad for touchscreen setups.

The config screens are almost always driven by touch, where a QSpinBox is
close to unusable: the up/down arrows are a few pixels tall and there is no
hardware keyboard to type a value into.  This puts a large keypad beside the
dialog so speeds, accelerations and limits can be entered by thumb.

Design notes, in case this needs changing later:

* **Keys are delivered as real key events** to whatever widget has focus,
  rather than by calling setValue().  Qt's own validators then do the work —
  ranges, decimal places, suffixes and the wrapping behaviour of QSpinBox all
  keep working exactly as they do with a hardware keyboard, and this file
  needs to know nothing about any particular field.

* **Nothing here ever takes focus.**  Every button is NoFocus and the window
  is a Tool with WA_ShowWithoutActivating, because the moment the keypad
  steals focus the field it is meant to be typing into loses it, and the keys
  go nowhere.  This is the single most fragile property of the whole widget.

* **Enter advances to the next input**, skipping buttons, tabs and combo
  boxes, so a form can be filled top to bottom without touching anything else.

Usage: NumericKeypad.install(some_dialog) — one keypad per dialog, shown when
a numeric field inside it takes focus, hidden with Close or when the dialog
goes away.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QEvent, QTimer
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import (
    QWidget, QPushButton, QGridLayout, QVBoxLayout, QLabel,
    QAbstractSpinBox, QLineEdit, QApplication,
)

# Fields we drive.  QAbstractSpinBox covers QSpinBox and QDoubleSpinBox.
_EDITABLE = (QAbstractSpinBox, QLineEdit)

_BTN_CSS = """
QPushButton {
    background: #2A2A2A; color: #ECEFF1; border: 1px solid #3A3A3A;
    border-radius: 6px; font-size: 22px; font-weight: 600;
}
QPushButton:pressed { background: #3D5AFE; color: #FFFFFF; }
"""
_ACTION_CSS = """
QPushButton {
    background: #263238; color: #B0BEC5; border: 1px solid #3A3A3A;
    border-radius: 6px; font-size: 15px; font-weight: 600;
}
QPushButton:pressed { background: #3D5AFE; color: #FFFFFF; }
"""
_CLOSE_CSS = """
QPushButton {
    background: #37231F; color: #EF9A9A; border: 1px solid #5A2E28;
    border-radius: 6px; font-size: 15px; font-weight: 600;
}
QPushButton:pressed { background: #C62828; color: #FFFFFF; }
"""


class NumericKeypad(QWidget):
    """Frameless keypad that types into the focused numeric field."""

    KEY_PX = 64          # touch target — a fingertip is ~9 mm, this is bigger
    MARGIN = 12          # gap from the screen edge

    def __init__(self, owner: QWidget) -> None:
        # Tool + FramelessWindowHint keeps it off the taskbar and undecorated;
        # StaysOnTop keeps it above the dialog it serves.
        # PARENTED to the dialog, not None.  A parentless top-level window
        # cannot live in a macOS native-fullscreen Space, so the first time
        # this appeared macOS switched the operator to the desktop to show it —
        # with the Config sheet left correctly behind on the fullscreen Space.
        # An owned Tool window follows its parent's Space instead.
        super().__init__(owner,
                         Qt.WindowType.Tool
                         | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint
                         # Without this the keypad becomes the active window the
                         # instant it is shown.  The dialog is then inactive, so
                         # its spin box loses focus, QApplication.focusWidget()
                         # goes None, and every key lands nowhere — the keypad
                         # breaks the very thing it exists to serve.
                         # WA_ShowWithoutActivating alone does NOT prevent this.
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        # Belt and braces with the flag above: covers the show() path on
        # platforms that honour the attribute but not the window type.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowTitle("Keypad")

        self._owner = owner
        self._target: QWidget | None = None
        self._dismissed = False   # Close pressed — stay hidden until focus moves
        self._owner_shown_ms = 0  # see _on_focus_changed's opening grace

        self._build()

        # Focus tracking drives everything.
        app = QApplication.instance()
        app.focusChanged.connect(self._on_focus_changed)

        owner.installEventFilter(self)
        owner.destroyed.connect(self.deleteLater)
        # focusChanged alone is not enough: a dialog opens with its first field
        # already focused, so tapping that field changes nothing and emits no
        # signal — the keypad would never appear for the very first value the
        # user tries to edit.  Watch presses too, application-wide, and filter
        # to our own fields in the handler.
        app.installEventFilter(self)

    # ── construction ────────────────────────────────────────────────────
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        self.setStyleSheet("NumericKeypad { background: #161616; "
                           "border: 1px solid #3A3A3A; border-radius: 10px; }")

        self._caption = QLabel("Keypad")
        self._caption.setStyleSheet("color:#78909C; font-size:12px;")
        self._caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._caption)

        grid = QGridLayout()
        grid.setSpacing(6)
        keys = [("7", 0, 0), ("8", 0, 1), ("9", 0, 2),
                ("4", 1, 0), ("5", 1, 1), ("6", 1, 2),
                ("1", 2, 0), ("2", 2, 1), ("3", 2, 2),
                (".", 3, 0), ("0", 3, 1), ("⌫", 3, 2)]
        for text, r, c in keys:
            b = self._mk_button(text, _BTN_CSS)
            if text == "⌫":
                b.clicked.connect(lambda _, : self._send_key(Qt.Key.Key_Backspace))
            else:
                b.clicked.connect(lambda _, t=text: self._type(t))
            grid.addWidget(b, r, c)
        root.addLayout(grid)

        # Minus toggles sign — easier by touch than positioning a caret.
        row = QGridLayout()
        row.setSpacing(6)
        neg = self._mk_button("±", _ACTION_CSS, wide=False)
        neg.clicked.connect(self._toggle_sign)
        clr = self._mk_button("Clear", _ACTION_CSS, wide=True)
        clr.clicked.connect(self._clear)
        row.addWidget(neg, 0, 0)
        row.addWidget(clr, 0, 1, 1, 2)
        root.addLayout(row)

        nxt = self._mk_button("↵  Next field", _ACTION_CSS, wide=True)
        nxt.clicked.connect(self._next_field)
        nxt.setFixedHeight(self.KEY_PX - 10)
        root.addWidget(nxt)

        close = self._mk_button("Close", _CLOSE_CSS, wide=True)
        close.clicked.connect(self._on_close)
        close.setFixedHeight(self.KEY_PX - 18)
        root.addWidget(close)

        self.setFixedWidth(self.KEY_PX * 3 + 6 * 2 + 20)

    def _mk_button(self, text: str, css: str, wide: bool = False) -> QPushButton:
        b = QPushButton(text)
        # NoFocus is essential — see module docstring.
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setStyleSheet(css)
        b.setFixedHeight(self.KEY_PX)
        if not wide:
            b.setFixedWidth(self.KEY_PX)
        return b

    # ── focus tracking ──────────────────────────────────────────────────
    def _on_focus_changed(self, _old: QWidget | None, new: QWidget | None) -> None:
        target = self._resolve(new)
        if target is None:
            return                      # focus went somewhere we don't serve
        if target is not self._target:
            self._dismissed = False     # a different field — honour it again
        self._target = target
        self._caption.setText(self._label_for(target))
        if self._dismissed:
            return
        # Opening grace.  Showing the dialog auto-focuses its first spin box,
        # which is not the operator asking for a keypad — it popped up unbidden
        # every time Config was opened.  An explicit tap still shows it
        # immediately; only this focus-driven path waits.
        import time as _t
        if self._owner_shown_ms and (_t.monotonic() - self._owner_shown_ms) < 0.4:
            return
        self._show_beside_owner()

    def _current_target(self) -> QWidget | None:
        """The field to type into, resolved live wherever possible.

        focusChanged is the trigger for showing the keypad, but it is not
        trusted as the sole source of truth: it can be missed (a window
        activation change, a widget rebuilt under us) and then every keystroke
        would silently go to a stale field — the worst possible failure, since
        it edits the wrong value rather than doing nothing.  Ask the
        application what has focus right now, and only fall back to the
        remembered target when it can't say.
        """
        live = self._resolve(QApplication.focusWidget())
        if live is not None:
            self._target = live
            return live
        t = self._target
        if t is not None and t.isVisible() and t.isEnabled():
            return t
        return None

    def _resolve(self, w: QWidget | None) -> QWidget | None:
        """Map a focused widget to the field we should type into, or None.

        A QSpinBox focuses its internal QLineEdit, so walk up to the spin box
        when there is one — key events must go to the spin box for its
        validator and stepping to apply.
        """
        if w is None or not self._owner or not self._owner.isVisible():
            return None
        if self.isAncestorOf(w) or w is self:
            return None                 # our own buttons (shouldn't happen)
        node = w
        while node is not None:
            if isinstance(node, QAbstractSpinBox):
                return node if self._owner.isAncestorOf(node) else None
            node = node.parentWidget()
        if isinstance(w, QLineEdit) and self._owner.isAncestorOf(w):
            return w
        return None

    @staticmethod
    def _label_for(target: QWidget) -> str:
        """Best-effort caption so it's obvious which field is being edited."""
        from PyQt6.QtWidgets import QFormLayout
        parent = target.parentWidget()
        if parent is not None:
            lay = parent.layout()
            if isinstance(lay, QFormLayout):
                for r in range(lay.rowCount()):
                    item = lay.itemAt(r, QFormLayout.ItemRole.FieldRole)
                    if item is not None and item.widget() is target:
                        lbl = lay.itemAt(r, QFormLayout.ItemRole.LabelRole)
                        if lbl is not None and lbl.widget() is not None:
                            return lbl.widget().text()
        return "Keypad"

    # ── placement ───────────────────────────────────────────────────────
    def _show_beside_owner(self) -> None:
        self.adjustSize()
        screen = (self._owner.screen() or QApplication.primaryScreen())
        avail = screen.availableGeometry()

        # Right-hand edge of the screen the dialog is on, vertically centred —
        # "out of the way of the config window", as asked.
        x = avail.right() - self.width() - self.MARGIN
        y = avail.top() + max(0, (avail.height() - self.height()) // 2)

        # If the dialog reaches that far, sit just clear of its right edge
        # instead of covering it; if there's no room, fall back to the screen
        # edge and let it overlap rather than going off-screen.
        og = self._owner.frameGeometry()
        if og.right() + self.MARGIN + self.width() < avail.right():
            x = max(x, og.right() + self.MARGIN)

        self.move(x, max(avail.top(), y))
        if not self.isVisible():
            self.show()
        self.raise_()

        # WindowDoesNotAcceptFocus is honoured on Windows (WS_EX_NOACTIVATE) but
        # not by every platform or window manager.  Where it isn't, showing the
        # keypad makes IT the active window, the dialog goes inactive, its spin
        # box loses focus and keys land nowhere.  Hand activation straight back.
        #
        # Deferred to the event loop on purpose: activation is delivered as an
        # event, so immediately after show() the owner still *reports* itself
        # active and the check would pass while the theft is still in flight.
        QTimer.singleShot(0, self._restore_owner_activation)

    def event(self, ev):
        # The authoritative guard against activation theft.  Timing-based
        # attempts lose the race — activation arrives as an event, and can land
        # after any number of event-loop turns — so react to the fact itself:
        # if this window is ever made active, hand activation straight back.
        # Covers every platform and window manager that ignores
        # WindowDoesNotAcceptFocus, without depending on when they do it.
        if ev.type() == QEvent.Type.WindowActivate:
            self._restore_owner_activation()
        return super().event(ev)

    def _restore_owner_activation(self) -> None:
        if self._owner is None or not self._owner.isVisible():
            return
        if not self._owner.isActiveWindow():
            self._owner.activateWindow()
            t = self._target
            if t is not None and t.isVisible() and t.isEnabled():
                t.setFocus(Qt.FocusReason.OtherFocusReason)

    # ── key delivery ────────────────────────────────────────────────────
    def _send_key(self, key: Qt.Key, text: str = "") -> None:
        """Deliver a real key press/release pair to the focused field."""
        t = self._current_target()
        if t is None:
            return
        # Spin boxes route typing through their internal line edit.
        recv = t.lineEdit() if isinstance(t, QAbstractSpinBox) and t.lineEdit() else t
        for ev_type in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            QApplication.sendEvent(
                recv, QKeyEvent(ev_type, key, Qt.KeyboardModifier.NoModifier, text))

    def _type(self, ch: str) -> None:
        if ch == ".":
            # Only meaningful on a double spin box or a free-text field.
            t = self._current_target()
            if isinstance(t, QAbstractSpinBox) and not hasattr(t, "decimals"):
                return
            self._send_key(Qt.Key.Key_Period, ".")
            return
        self._send_key(Qt.Key(Qt.Key.Key_0 + int(ch)), ch)

    def _clear(self) -> None:
        t = self._current_target()
        if t is None:
            return
        recv = t.lineEdit() if isinstance(t, QAbstractSpinBox) and t.lineEdit() else t
        if isinstance(recv, QLineEdit):
            recv.selectAll()
        self._send_key(Qt.Key.Key_Delete)

    def _toggle_sign(self) -> None:
        """Flip the sign, for fields whose range allows it (e.g. offsets)."""
        t = self._current_target()
        if isinstance(t, QAbstractSpinBox) and hasattr(t, "value"):
            try:
                v = t.value()
                if -v >= t.minimum() and -v <= t.maximum():
                    t.setValue(-v)
            except Exception:
                pass
        elif isinstance(t, QLineEdit):
            s = t.text()
            t.setText(s[1:] if s.startswith("-") else "-" + s)

    def _next_field(self) -> None:
        """Commit the current value and focus the next input box.

        Walks the focus chain rather than sending Tab: a QSpinBox's internal
        line edit consumes Tab, and we want to skip past buttons and tabs to
        the next *input*, which is what makes filling a form by touch quick.
        """
        t = self._current_target()
        if t is None:
            return
        if isinstance(t, QAbstractSpinBox):
            t.interpretText()           # commit typed digits to the value

        # The chain terminates on `node is not t` (a full lap).  The step cap is
        # only a guard against a malformed chain, so it has to be well clear of
        # a real one: Settings holds ~103 spin boxes across its tabs, and with
        # their internal line edits, labels and buttons a single lap runs to
        # well over a thousand widgets.  At 500 the cap tripped mid-lap and
        # Enter stopped working on the last field of a tab.
        node = t.nextInFocusChain()
        seen = 0
        while node is not None and node is not t and seen < 20000:
            seen += 1
            if (isinstance(node, _EDITABLE)
                    and node.isVisible() and node.isEnabled()
                    and not node.isReadOnly()
                    and self._owner.isAncestorOf(node)
                    and node.focusPolicy() != Qt.FocusPolicy.NoFocus):
                # A spin box's own line edit is in the chain too — skip it and
                # let the spin box itself take focus.
                parent = node.parentWidget()
                if isinstance(parent, QAbstractSpinBox):
                    node = node.nextInFocusChain()
                    continue
                node.setFocus(Qt.FocusReason.TabFocusReason)
                if isinstance(node, (QAbstractSpinBox, QLineEdit)):
                    QTimer.singleShot(0, lambda n=node: self._select_all(n))
                return
            node = node.nextInFocusChain()

    @staticmethod
    def _select_all(w: QWidget) -> None:
        """Select the value so the first digit typed replaces it."""
        try:
            if isinstance(w, QAbstractSpinBox) and w.lineEdit():
                w.lineEdit().selectAll()
            elif isinstance(w, QLineEdit):
                w.selectAll()
        except Exception:
            pass

    # ── lifecycle ───────────────────────────────────────────────────────
    def _on_close(self) -> None:
        self._dismissed = True
        self.hide()

    def eventFilter(self, obj, event):
        et = event.type()
        if obj is self._owner and et == QEvent.Type.Show:
            import time as _t
            self._owner_shown_ms = _t.monotonic()
            return False
        if obj is self._owner and et in (QEvent.Type.Close, QEvent.Type.Hide):
            self.hide()
            return False
        # A press on one of our fields summons the keypad even when focus does
        # not move (the field was already focused).  Never swallow the event —
        # the field still needs it to place its caret.
        if et in (QEvent.Type.MouseButtonPress, QEvent.Type.TouchBegin):
            target = self._resolve(obj if isinstance(obj, QWidget) else None)
            if target is not None:
                self._target = target
                self._dismissed = False     # an explicit tap overrides Close
                self._caption.setText(self._label_for(target))
                self._show_beside_owner()
        return False

    def closeEvent(self, event):
        self._dismissed = True
        super().closeEvent(event)

    # ── entry point ─────────────────────────────────────────────────────
    @classmethod
    def install(cls, dialog: QWidget) -> "NumericKeypad | None":
        """Attach a keypad to `dialog`, unless one is already attached."""
        existing = getattr(dialog, "_numeric_keypad", None)
        if existing is not None:
            return existing
        kp = cls(dialog)
        dialog._numeric_keypad = kp     # keep a reference alive
        return kp
