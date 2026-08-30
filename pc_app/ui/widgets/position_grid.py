"""
PositionGrid — 5-row × 10-column position recall/store grid.

Each row is colour-coded to its camera.  Each position button border shows:
  Grey         — no position stored (slot empty on mount)
  Red          — position stored, camera NOT currently there
  Green        — position stored, camera IS currently there (mount reports AT_POSITION)
  Yellow/dark  — camera is actively moving to this position (1 Hz flash)

Slot occupancy and AT_POSITION state come from the mount (slot_occupied_mask /
slot_at_mask in STATUS packets / STATE_REPORT).  Coordinates are never held
locally — they live in the mount's volatile RAM.

Right side of each row: two RotaryDial widgets (Pan/Tilt speed, Slide speed).

Two modes:
  MOVE — tap a button to recall that position on that camera
  SET  — tap a button to store current camera position there (sends CMD_STORE_POS)
  LABEL_EDIT — tap a button to rename its label (right-click always works for this)
"""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QPushButton,
    QLabel, QSizePolicy, QInputDialog, QMessageBox,
    QStyleOptionButton, QStylePainter, QStyle
)
from PyQt6.QtCore import pyqtSignal, Qt, QTimer
from PyQt6.QtGui import QColor, QPalette, QPaintEvent, QPainter

from .rotary_dial import RotaryDial
from config.position_store import PositionStore
from ui import virtual_keyboard


# ---------------------------------------------------------------------------
# Word-wrapping button
# ---------------------------------------------------------------------------

class WrappingButton(QPushButton):
    def paintEvent(self, event: QPaintEvent) -> None:
        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        text = opt.text
        opt.text = ""
        p = QStylePainter(self)
        p.drawControl(QStyle.ControlElement.CE_PushButton, opt)
        if text:
            content_rect = self.style().subElementRect(
                QStyle.SubElement.SE_PushButtonContents, opt, self)
            p.setPen(self.palette().color(QPalette.ColorRole.ButtonText))
            p.drawText(
                content_rect,
                int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap),
                text,
            )


# ---------------------------------------------------------------------------
# Camera colour palette
# ---------------------------------------------------------------------------

# row_bg = the per-camera row strip.  Kept brighter than the original near-black
# tints so an active (online) camera's strip reads more vividly against the
# overlay-dimmed offline rows — while staying darker than btn_bg below it.
CAM_COLORS = {
    1: {"row_bg": "#112B16", "btn_bg": "#1B3A1D", "btn_text": "#A5D6A7", "accent": QColor("#4CAF50")},
    2: {"row_bg": "#112338", "btn_bg": "#1A2E45", "btn_text": "#90CAF9", "accent": QColor("#4A7DA8")},
    3: {"row_bg": "#2A2200", "btn_bg": "#332B00", "btn_text": "#D4B800", "accent": QColor("#A89200")},
    4: {"row_bg": "#00231F", "btn_bg": "#002E2A", "btn_text": "#80CBC4", "accent": QColor("#00796B")},
    5: {"row_bg": "#230F34", "btn_bg": "#2E1040", "btn_text": "#CE93D8", "accent": QColor("#8B44A8")},
}

BORDER_EMPTY          = "#4A4A4A"
BORDER_AT             = "#4CAF50"
BORDER_NOT_AT         = "#F44336"
BORDER_FLASH_A        = "#FFC107"
BORDER_FLASH_B        = "#2A2A2A"
BORDER_CLEAR_SELECTED = "#FF9800"   # orange — slot selected for deletion

# ---------------------------------------------------------------------------
# Mode constants
# ---------------------------------------------------------------------------

MODE_MOVE       = "move"
MODE_LABEL_EDIT = "label_edit"
MODE_SET        = "set"
MODE_CLEAR      = "clear"


# Metrics scale with the button, so the grid looks the same on any screen.
#
# These were fixed at 22px radius / 24px font against a 130x120 button, which
# is right on a 1920x1080 screen and wrong on every other one: the app runs
# full screen, so a display with more logical pixels got the same size grid in
# a bigger space and it read as small and over-spaced.  Set from the height the
# row actually gets — see PositionGrid._relayout().
# The button is not the whole row: it floats in the camera's coloured band,
# and that band is as much of the look as the button is.  120 in a 178px row on
# the screen this was designed against.
_BTN_H_FRAC      = 120.0 / 178.0
# The button's own shape, and the dials beside it, both as originally drawn.
# Without the aspect cap the buttons simply absorbed every spare pixel of width
# and came out stretched — wider than tall, which they never were — while the
# dials were squeezed down onto their 72px floor.
_BTN_ASPECT      = 130.0 / 120.0
_DIAL_FRAC       = 0.78            # of button height
_DIAL_GAP_FRAC   = 0.10
_BTN_RADIUS_FRAC = 22.0 / 120.0    # of the BUTTON height, as originally tuned
_BTN_FONT_FRAC   = 24.0 / 120.0
_BTN_BORDER_FRAC =  8.0 / 120.0


def _btn_stylesheet(bg: str, text: str, border: str, border_w: int = 8,
                    radius: int = 22, font_px: int = 24) -> str:
    return f"""
        QPushButton {{
            background: {bg};
            color: {text};
            border: {border_w}px solid {border};
            border-radius: {radius}px;
            font-size: {font_px}px;
            font-weight: bold;
            padding: 2px;
        }}
        QPushButton:pressed {{
            background: #111111;
        }}
    """


# ---------------------------------------------------------------------------
# Inactive mount overlay
# ---------------------------------------------------------------------------

class _InactiveOverlay(QWidget):
    """
    Semi-transparent screen that sits on top of a camera row while the mount
    is offline.  Blocks mouse events so controls beneath cannot be activated.
    Removed automatically once the mount connects.
    """
    _FILL = QColor(0, 0, 0, 150)   # ~59 % opaque black — controls still visible

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        # Visible by default — mount is disconnected on startup
        self.setVisible(True)

    def paintEvent(self, event: QPaintEvent) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.fillRect(self.rect(), self._FILL)


class _RowContainer(QWidget):
    """
    Thin wrapper that stacks the camera row widget with a floating
    _InactiveOverlay child.  The overlay covers the full row geometry
    and is kept in sync via resizeEvent.
    """

    def __init__(self, row: QWidget, parent=None) -> None:
        super().__init__(parent)
        vl = QVBoxLayout(self)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)
        vl.addWidget(row)

        self._overlay = _InactiveOverlay(self)
        self._overlay.raise_()          # always on top of the row widget

    # keep overlay covering the full container
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._overlay.setGeometry(self.rect())
        self._overlay.raise_()

    @property
    def overlay(self) -> _InactiveOverlay:
        return self._overlay


class PositionGrid(QWidget):
    """
    Signals:
        store_requested(mount_id, slot)     — SET mode: send CMD_STORE_POS to mount
        recall_requested(mount_id, slot)    — MOVE mode: send CMD_GOTO to mount
        clear_requested(mount_id, slot)     — user deleted slot: send CMD_CLEAR_POS
        label_edited(mount_id, slot, label) — user renamed a button (label only)
        pt_preset_changed(mount_id, preset)
        sl_preset_changed(mount_id, preset)
    """

    store_requested    = pyqtSignal(int, int)
    recall_requested   = pyqtSignal(int, int)
    clear_requested    = pyqtSignal(int, int)
    label_edited       = pyqtSignal(int, int, str)
    pt_preset_changed  = pyqtSignal(int, int)
    sl_preset_changed  = pyqtSignal(int, int)
    # Emitted whenever a slot's clear-selection state changes (mount_id, slot, selected)
    clear_selection_changed = pyqtSignal(int, int, bool)

    # v2 — slider-equipped mounts
    calibrate_subject_requested = pyqtSignal(int, int)  # mount_id, subject_slot (0-7)
    look_at_subject_selected    = pyqtSignal(int, int)  # mount_id, subject_slot — activate look-at
    slider_jog_started          = pyqtSignal(int, int)  # mount_id, direction (+1 right / -1 left)
    slider_jog_stopped          = pyqtSignal(int)       # mount_id

    def __init__(self, position_store: PositionStore, parent=None):
        super().__init__(parent)
        self._store  = position_store
        self._mode   = MODE_MOVE
        self._active = 1

        # Live layout metrics.  Seeded with the values the grid was designed at
        # so the very first paint is right even before any resize arrives, then
        # recomputed from the height actually granted — see _relayout().
        self._row_layouts: dict[int, object] = {}
        self._row_h   = self._REF_ROW_H
        self._radius  = round(self._REF_ROW_H * _BTN_RADIUS_FRAC)
        self._font_px = round(self._REF_ROW_H * _BTN_FONT_FRAC)
        self._border  = round(self._REF_ROW_H * _BTN_BORDER_FRAC)

        # Slot state from mount (bitmasks, bits 0-9)
        self._slot_occupied: dict[int, int] = {mid: 0 for mid in range(1, 6)}
        self._slot_at:       dict[int, int] = {mid: 0 for mid in range(1, 6)}
        # Every mount starts offline, so a freshly-opened app looks exactly like
        # a disconnect rather than a third, half-lit state of its own.
        self._connected:     dict[int, bool] = {mid: False for mid in range(1, 6)}
        # The presets the mount actually has active.  Held separately from the
        # dials because an offline row shows 0 while these keep the real value:
        # main_window compares get_*_preset() against the mount's reported
        # preset to decide whether to send a change, and a blanked DISPLAY must
        # not be mistaken for the mount having changed speed.
        self._pt_preset:     dict[int, int]  = {mid: 0 for mid in range(1, 6)}
        self._sl_preset:     dict[int, int]  = {mid: 0 for mid in range(1, 6)}

        # Which slot each mount is moving toward (None = not moving)
        self._target_slot: dict[int, int | None] = {mid: None for mid in range(1, 6)}

        # Slots selected for clearing in MODE_CLEAR (set of slot indices per mount)
        self._clear_selected: dict[int, set[int]] = {mid: set() for mid in range(1, 6)}

        # v2 — slider mounts repurpose buttons 0-7 as subjects, 8-9 as jog arrows
        self._has_slider:       dict[int, bool] = {mid: False for mid in range(1, 6)}
        self._look_at_mode:     dict[int, bool] = {mid: False for mid in range(1, 6)}
        self._subjects:         dict[int, list] = {mid: [None] * 8 for mid in range(1, 6)}
        self._active_la_subj:   dict[int, int]  = {mid: -1 for mid in range(1, 6)}

        # Arrow button (slots 8/9) visual state for look-at moves.
        # None             → both grey  (idle, not at a limit)
        # 'left_moving'    → ◀ flashes yellow  (look-at move toward min running)
        # 'right_moving'   → ▶ flashes yellow  (look-at move toward max running)
        # 'left_done'      → ◀ green  (slider arrived at / is at min limit)
        # 'right_done'     → ▶ green  (slider arrived at / is at max limit)
        self._la_arrow_state: dict[int, str | None] = {mid: None for mid in range(1, 6)}

        self._flash_on = False
        self._flash_timer = QTimer(self)
        self._flash_timer.setInterval(500)
        self._flash_timer.timeout.connect(self._on_flash)
        self._flash_timer.start()

        self._buttons: dict[tuple[int, int], QPushButton] = {}
        self._pt_dials: dict[int, RotaryDial] = {}
        self._sl_dials: dict[int, RotaryDial] = {}
        self._row_containers: dict[int, _RowContainer] = {}

        self._build()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_mode(self, mode: str) -> None:
        self._mode = mode

    def set_active_mount(self, mount_id: int) -> None:
        self._active = mount_id
        self._refresh_all_borders()

    def update_slot_masks(self, mount_id: int,
                          occupied_mask: int, at_mask: int) -> None:
        """Call when STATUS / STATE_REPORT arrives with updated slot bitmasks."""
        self._slot_occupied[mount_id] = occupied_mask
        self._slot_at[mount_id]       = at_mask
        self._refresh_row_borders(mount_id)
        self._refresh_row_labels(mount_id)

    def mark_subject_deleted(self, mount_id: int, subject_id: int) -> None:
        """Optimistically remove a look-at subject after a local delete.
        Refreshes the button to grey immediately without waiting for the subject list echo."""
        if subject_id < len(self._subjects[mount_id]):
            self._subjects[mount_id][subject_id] = None
        # If this was the active look-at target, deselect it so the border goes grey.
        if self._active_la_subj[mount_id] == subject_id:
            self._active_la_subj[mount_id] = -1
        self.refresh_button(mount_id, subject_id)

    def set_target_slot(self, mount_id: int, slot: int | None) -> None:
        self._target_slot[mount_id] = slot
        self._refresh_row_borders(mount_id)

    def refresh_button(self, mount_id: int, slot: int) -> None:
        btn = self._buttons.get((mount_id, slot))
        if not btn:
            return
        self._apply_border(btn, mount_id, slot)
        # Label depends on look-at mode — must not overwrite ◄/► with "9"/"10".
        if self._look_at_mode[mount_id]:
            if slot == 8:
                btn.setText("◀")
            elif slot == 9:
                btn.setText("▶")
            else:
                lbl = self._store.get_label(mount_id, slot)
                btn.setText(lbl or str(slot + 1))
        else:
            occupied = bool(self._slot_occupied[mount_id] & (1 << slot))
            lbl      = self._store.get_label(mount_id, slot)
            btn.setText((lbl or str(slot + 1)) if occupied else str(slot + 1))

    def set_la_arrow_state(self, mount_id: int,
                           state: "str | None") -> None:
        """Update the ◀/▶ border state for a look-at mount and redraw the buttons.

        state values:
          None           – both arrows grey  (idle, not at a limit)
          'left_moving'  – ◀ flashes yellow  (look-at move toward min running)
          'right_moving' – ▶ flashes yellow  (look-at move toward max running)
          'left_done'    – ◀ green  (slider at / arrived at min limit)
          'right_done'   – ▶ green  (slider at / arrived at max limit)
        """
        if self._la_arrow_state[mount_id] == state:
            return
        self._la_arrow_state[mount_id] = state
        self.refresh_button(mount_id, 8)
        self.refresh_button(mount_id, 9)

    def get_clear_selected(self) -> dict[int, set[int]]:
        """Return a copy of the current clear-mode selection (mount_id → set of slots)."""
        return {mid: set(s) for mid, s in self._clear_selected.items()}

    def reset_clear_selection(self) -> None:
        """Clear all selections and refresh borders (called when exiting clear mode)."""
        for mid in range(1, 6):
            self._clear_selected[mid].clear()
        self._refresh_all_borders()

    def set_mount_connected(self, mount_id: int, connected: bool) -> None:
        """Show/hide the inactive overlay, and blank the row while it is offline."""
        self._connected[mount_id] = connected
        # Repaint either way: coming online reveals the masks that arrived while
        # offline, going offline blanks whatever was on screen.
        self._refresh_row_borders(mount_id)
        if connected:
            # Reveal the speeds that arrived while the row was blanked.
            self._pt_dials[mount_id].preset = self._pt_preset[mount_id]
            self._sl_dials[mount_id].preset = self._sl_preset[mount_id]
        else:
            self._pt_dials[mount_id].preset = 0
            self._sl_dials[mount_id].preset = 0
            # Clear arrow flash state so stale yellow/green doesn't persist after
            # a reconnect.  State will be re-derived from the first STATUS packet.
            self.set_la_arrow_state(mount_id, None)
        container = self._row_containers.get(mount_id)
        if container:
            container.overlay.setVisible(not connected)

    def set_pt_preset(self, mount_id: int, preset: int) -> None:
        self._pt_preset[mount_id] = preset
        if self._connected.get(mount_id, False):
            self._pt_dials[mount_id].preset = preset

    def set_sl_preset(self, mount_id: int, preset: int) -> None:
        self._sl_preset[mount_id] = preset
        if self._connected.get(mount_id, False):
            self._sl_dials[mount_id].preset = preset

    def get_pt_preset(self, mount_id: int) -> int:
        return self._pt_preset[mount_id]

    def get_sl_preset(self, mount_id: int) -> int:
        return self._sl_preset[mount_id]

    # v2 — slider / subject awareness --------------------------------

    def set_has_slider(self, mount_id: int, has_slider: bool) -> None:
        """Switch between position-slot mode and subject/arrow mode for one row."""
        # Grey out the slider speed dial when this mount has no slider (done
        # before the change guard so it always reflects the current flag).
        sl_dial = self._sl_dials.get(mount_id)
        if sl_dial is not None:
            sl_dial.setEnabled(has_slider)
        if self._has_slider[mount_id] == has_slider:
            return
        self._has_slider[mount_id] = has_slider
        # Refresh all 10 buttons in that row
        for slot in range(10):
            btn = self._buttons.get((mount_id, slot))
            if btn:
                self._apply_border(btn, mount_id, slot)
        self._refresh_row_labels(mount_id)

    def set_look_at_mode(self, mount_id: int, look_at_mode: bool) -> bool:
        """Switch between look-at subject mode and standard position-slot mode.

        Returns True if the mode actually changed (and this therefore cleared
        the cached subject state below).  Callers mirroring that state need to
        know: this is called on every CONFIG_REPORT, which arrives every few
        seconds from the GET_CONFIG poll, not just when something changes.
        """
        if self._look_at_mode[mount_id] == look_at_mode:
            return False
        # Clear stale subject state so it doesn't linger if look-at is re-enabled.
        self._active_la_subj[mount_id] = -1
        self._subjects[mount_id] = [None] * 8
        self._look_at_mode[mount_id] = look_at_mode
        for slot in range(10):
            btn = self._buttons.get((mount_id, slot))
            if btn:
                self._apply_border(btn, mount_id, slot)
        self._refresh_row_labels(mount_id)
        return True

    def _has_subject(self, mount_id: int, slot: int) -> bool:
        """Is a look-at subject stored in this slot?

        Taken from the slot_occupied bitmask, not the cached subject list. On a
        look-at camera those bits ARE the stored subjects (see the hub's own
        comment at esp32_hub.ino:826), and STATUS re-broadcasts them every
        100 ms — so a dropped or discarded packet self-corrects on the next one.

        The subject list is a one-shot reply to CMD_GET_SUBJECTS: lose that copy
        and nothing refills it. set_look_at_mode() discards it whenever the mode
        changes, which left every slot grey while the mount genuinely held the
        subjects and look-at moves ran correctly — the web app and the hub
        display, both reading the bitmask, showed them properly throughout.

        Nothing in this widget reads the subject list for display any more —
        names come from the label store. It is still cached (set_subjects) for
        callers that want the coordinates, so it stays, but no on-screen state
        depends on a one-shot packet arriving and surviving.
        """
        return bool(self._slot_occupied[mount_id] & (1 << slot))

    def set_subjects(self, mount_id: int, subjects: list) -> None:
        """Update the cached subject list for a slider mount (list of SubjectRecord or None)."""
        self._subjects[mount_id] = list(subjects[:8]) + [None] * max(0, 8 - len(subjects))
        if self._look_at_mode[mount_id]:
            self._refresh_row_labels(mount_id)
            self._refresh_row_borders(mount_id)

    def set_active_la_subject(self, mount_id: int, slot: int) -> None:
        """Highlight the selected look-at subject (-1 = none).  Refreshes borders."""
        self._active_la_subj[mount_id] = slot
        if self._look_at_mode[mount_id]:
            self._refresh_row_borders(mount_id)

    # ------------------------------------------------------------------
    # Build layout
    # ------------------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(6, 6, 6, 6)
        for mid in range(1, 6):
            row = self._build_camera_row(mid)
            container = _RowContainer(row)
            self._row_containers[mid] = container
            root.addWidget(container)

    # Never narrower than this, whatever the screen: below it the two-digit
    # labels start to crowd their border radius.
    _BTN_MIN_W = 64
    # The proportions the layout was designed at, and the size the row is given
    # on the 1920x1080 screen it was tuned on.  _relayout() scales from these,
    # so that screen is unchanged by construction and every other one matches it.
    _REF_ROW_H = 120

    def _relayout(self) -> None:
        """Size the rows, and the type inside them, from the height available.

        The app runs full screen, so the height it gets is the screen's. Fixed
        pixel sizes therefore meant the grid occupied a different FRACTION of
        the screen on every machine: right on the display it was tuned on,
        small and over-spaced on a larger one. Everything here is derived from
        one number instead, so the proportions hold everywhere.
        """
        if not self._row_containers:
            return
        m = self.layout().contentsMargins()
        avail = self.height() - m.top() - m.bottom() \
                - self.layout().spacing() * (len(self._row_containers) - 1)
        row_h = max(self._MIN_ROW_H, avail // len(self._row_containers))
        if row_h == self._row_h:
            return                      # nothing moved; don't churn stylesheets
        self._row_h   = row_h
        btn_h         = max(self._MIN_BTN_H, round(row_h * _BTN_H_FRAC))
        self._radius  = max(4, round(btn_h * _BTN_RADIUS_FRAC))
        self._font_px = max(9, round(btn_h * _BTN_FONT_FRAC))
        self._border  = max(2, round(btn_h * _BTN_BORDER_FRAC))

        # A MAXIMUM, not a fixed height.  The minimum stays small, so the grid
        # can still shrink; the maximum stops the button swelling to fill the
        # band it is supposed to float in.
        btn_w = round(btn_h * _BTN_ASPECT)
        for btn in self._buttons.values():
            btn.setMaximumHeight(btn_h)
            btn.setMaximumWidth(btn_w)

        # The dials scale with the row too.  They had a 72px minimum and no
        # maximum, so with the buttons expanding they were pushed to that floor
        # and sat almost touching.
        # A MAXIMUM again, for the same reason as the buttons: setFixedSize
        # here made the row's minimum height the dial's size, so after being
        # shown on a 4K screen the grid could not shrink and a 1366x768 window
        # kept 215px rows.  Every constraint in this method has to be an upper
        # bound, or the layout ratchets.
        dial = max(48, round(btn_h * _DIAL_FRAC))
        for d in list(self._pt_dials.values()) + list(self._sl_dials.values()):
            d.setMaximumSize(dial, dial)
        for hl in self._row_layouts.values():
            hl.setSpacing(max(3, round(btn_h * _DIAL_GAP_FRAC)))

        for mid in list(self._row_containers):
            self._refresh_row_borders(mid)

    _MIN_ROW_H = 56     # below this the digits stop being readable across a room
    _MIN_BTN_H = 38

    def resizeEvent(self, event):        # type: ignore[override]
        super().resizeEvent(event)
        self._relayout()

    def _btn_target_width(self) -> int:
        return 130

    def _build_camera_row(self, mount_id: int) -> QWidget:
        col = CAM_COLORS[mount_id]
        row = QWidget()
        row.setStyleSheet(f"background: {col['row_bg']}; border-radius: 6px;")
        hl  = QHBoxLayout(row)
        hl.setSpacing(3)
        hl.setContentsMargins(4, 4, 4, 4)
        self._row_layouts[mount_id] = hl

        for slot in range(10):
            btn = WrappingButton(str(slot + 1))
            # Expanding, not fixed: the ten buttons and the two dials divide
            # whatever width the row has, so the grid fills the screen the same
            # way whatever its resolution.  A fixed width left the surplus as
            # gaps, which is why a bigger display looked sparse rather than
            # bigger.  The minimum keeps the digits legible on a small one.
            btn.setMinimumSize(self._BTN_MIN_W, self._MIN_BTN_H)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding,
                              QSizePolicy.Policy.Expanding)
            hl.setAlignment(btn, Qt.AlignmentFlag.AlignVCenter)
            self._apply_border(btn, mount_id, slot)
            btn.clicked.connect(self._make_click_handler(mount_id, slot))
            btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            btn.customContextMenuRequested.connect(
                self._make_edit_handler(mount_id, slot))
            # Arrow-mode: press/release for continuous slider jog (only active
            # when _has_slider[mount_id] is True and slot ∈ {8, 9})
            btn.pressed.connect(self._make_arrow_press(mount_id, slot))
            btn.released.connect(self._make_arrow_release(mount_id, slot))
            self._buttons[(mount_id, slot)] = btn
            # Stretch 1, same as the spacer below.  An Expanding policy alone
            # loses to any item that has an explicit stretch factor, so without
            # this the spacer took every spare pixel and the buttons collapsed
            # onto their minimum width.  With it they grow first, stop at their
            # aspect cap, and the remainder goes to the spacer.
            hl.addWidget(btn, 1)

        # A stretch, not a fixed gap: with the buttons capped to their own
        # aspect, whatever width is left over collects HERE rather than being
        # shared out as wider buttons.  It also keeps the dials against the
        # right-hand edge, which is where they have always sat.
        hl.addStretch(1)

        accent  = col["accent"]
        pt_dial = RotaryDial(accent_colour=accent)
        sl_dial = RotaryDial(accent_colour=accent)
        pt_dial.preset_changed.connect(self._make_pt_handler(mount_id))
        sl_dial.preset_changed.connect(self._make_sl_handler(mount_id))
        self._pt_dials[mount_id] = pt_dial
        self._sl_dials[mount_id] = sl_dial
        # Slider dial starts greyed until a mount reports it has a slider
        # (set_has_slider, driven by CONFIG_REPORT / config).
        sl_dial.setEnabled(self._has_slider[mount_id])
        # Stretch 1, like the buttons: an Expanding policy with no stretch
        # factor loses to the spacer and the dial collapses onto its minimum.
        for d in (sl_dial, pt_dial):
            d.setSizePolicy(QSizePolicy.Policy.Expanding,
                            QSizePolicy.Policy.Expanding)
        hl.addWidget(sl_dial, 1)   # Slider  — left
        hl.addWidget(pt_dial, 1)   # Pan/Tilt — right

        return row

    # ------------------------------------------------------------------
    # Button state / border
    # ------------------------------------------------------------------

    def _apply_border(self, btn: QPushButton, mount_id: int, slot: int) -> None:
        col = CAM_COLORS[mount_id]

        # An offline mount shows nothing.  The overlay is only ~59% opaque, so
        # anything painted underneath stays legible through it, and STATUS is
        # relayed by the hub whether or not this app considers the mount
        # connected — so a row could sit dimmed while displaying real stored
        # positions and a real speed.  Half-lit is the one state the operator
        # cannot read: it looks like a mount they can drive.
        #
        # The masks are still stored by update_slot_masks(); only the drawing
        # waits.  set_mount_connected(True) repaints from them immediately.
        if not self._connected.get(mount_id, False):
            btn.setStyleSheet(_btn_stylesheet(col["btn_bg"], col["btn_text"],
                                              BORDER_EMPTY,
                                          radius=self._radius, font_px=self._font_px))
            return

        # Arrow buttons (slots 8-9 on look-at mounts) — state-driven border
        if self._look_at_mode[mount_id] and slot >= 8:
            arr = self._la_arrow_state[mount_id]
            is_left  = (slot == 8)
            if is_left:
                if arr == 'left_moving':
                    border = BORDER_FLASH_A if self._flash_on else BORDER_FLASH_B
                elif arr == 'left_done':
                    border = BORDER_AT
                else:
                    border = "#37474F"
            else:  # slot == 9 (▶)
                if arr == 'right_moving':
                    border = BORDER_FLASH_A if self._flash_on else BORDER_FLASH_B
                elif arr == 'right_done':
                    border = BORDER_AT
                else:
                    border = "#37474F"
            btn.setStyleSheet(_btn_stylesheet(col["btn_bg"], col["btn_text"], border, border_w=4,
                                          radius=self._radius, font_px=self._font_px))
            return

        # Subject buttons (slots 0-7 on look-at mounts)
        # Border colour only — background never changes (same as non-look-at slots).
        # Green = stored + looking at, Red = stored + not looking at, Grey = empty.
        if self._look_at_mode[mount_id] and slot < 8:
            has_subject  = self._has_subject(mount_id, slot)
            is_active_la = has_subject and self._active_la_subj[mount_id] == slot
            if is_active_la:
                border = BORDER_AT        # green — stored + camera currently looking at
            elif has_subject:
                border = BORDER_NOT_AT    # red   — stored + camera not looking at
            else:
                border = BORDER_EMPTY     # grey  — no subject stored
            btn.setStyleSheet(_btn_stylesheet(col["btn_bg"], col["btn_text"], border,
                                          radius=self._radius, font_px=self._font_px))
            return

        # Normal position-slot buttons
        occupied = bool(self._slot_occupied[mount_id] & (1 << slot))
        at_pos   = bool(self._slot_at[mount_id]       & (1 << slot))

        if self._mode == MODE_CLEAR and slot in self._clear_selected[mount_id]:
            border = BORDER_CLEAR_SELECTED
        elif not occupied:
            border = BORDER_EMPTY
        elif self._target_slot[mount_id] == slot:
            border = BORDER_FLASH_A if self._flash_on else BORDER_FLASH_B
        elif at_pos:
            border = BORDER_AT
        else:
            border = BORDER_NOT_AT

        btn.setStyleSheet(_btn_stylesheet(col["btn_bg"], col["btn_text"], border,
                                          radius=self._radius, font_px=self._font_px))

    def _refresh_row_borders(self, mount_id: int) -> None:
        for slot in range(10):
            btn = self._buttons.get((mount_id, slot))
            if btn:
                self._apply_border(btn, mount_id, slot)

    def _refresh_row_labels(self, mount_id: int) -> None:
        for slot in range(10):
            btn = self._buttons.get((mount_id, slot))
            if not btn:
                continue
            if self._look_at_mode[mount_id]:
                if slot == 8:
                    btn.setText("◀")
                elif slot == 9:
                    btn.setText("▶")
                else:
                    lbl = self._store.get_label(mount_id, slot)
                    btn.setText(lbl or str(slot + 1))
            else:
                occupied = bool(self._slot_occupied[mount_id] & (1 << slot))
                lbl      = self._store.get_label(mount_id, slot)
                btn.setText((lbl or str(slot + 1)) if occupied else str(slot + 1))

    def refresh_row_labels(self, mount_id: int) -> None:
        """Public entry point — re-apply button labels for one row (look-at aware)."""
        self._refresh_row_labels(mount_id)

    def _refresh_all_borders(self) -> None:
        for mid in range(1, 6):
            self._refresh_row_borders(mid)

    # ------------------------------------------------------------------
    # Flash timer
    # ------------------------------------------------------------------

    def _on_flash(self) -> None:
        self._flash_on = not self._flash_on
        for mid in range(1, 6):
            if self._target_slot[mid] is not None or \
               self._la_arrow_state[mid] in ('left_moving', 'right_moving'):
                self._refresh_row_borders(mid)

    # ------------------------------------------------------------------
    # Click handlers
    # ------------------------------------------------------------------

    def _make_click_handler(self, mount_id: int, slot: int):
        def handler():
            # Arrow buttons (slots 8-9) are handled by pressed/released — ignore click
            if self._look_at_mode[mount_id] and slot >= 8:
                return

            # Subject buttons (slots 0-7 on look-at mounts)
            if self._look_at_mode[mount_id]:
                has_subj = self._has_subject(mount_id, slot)
                if self._mode == MODE_CLEAR:
                    if has_subj:
                        self.clear_requested.emit(mount_id, slot)
                elif self._mode == MODE_LABEL_EDIT:
                    self._edit_label(mount_id, slot, has_subj)
                elif self._mode == MODE_SET:
                    self.calibrate_subject_requested.emit(mount_id, slot)
                elif self._mode == MODE_MOVE:
                    self.look_at_subject_selected.emit(mount_id, slot)
                return

            # Normal position-slot behaviour
            occupied = bool(self._slot_occupied[mount_id] & (1 << slot))
            if self._mode == MODE_CLEAR:
                # Immediate delete — only act on occupied slots.
                if occupied:
                    self.clear_requested.emit(mount_id, slot)
            elif self._mode == MODE_MOVE:
                if occupied:
                    self.recall_requested.emit(mount_id, slot)
            elif self._mode == MODE_SET:
                self.store_requested.emit(mount_id, slot)
            elif self._mode == MODE_LABEL_EDIT:
                self._edit_label(mount_id, slot, occupied)
        return handler

    def _make_arrow_press(self, mount_id: int, slot: int):
        def handler():
            if not self._has_slider[mount_id] or slot not in (8, 9):
                return
            direction = -1 if slot == 8 else +1
            self.slider_jog_started.emit(mount_id, direction)
        return handler

    def _make_arrow_release(self, mount_id: int, slot: int):
        def handler():
            if not self._has_slider[mount_id] or slot not in (8, 9):
                return
            self.slider_jog_stopped.emit(mount_id)
        return handler

    def _make_edit_handler(self, mount_id: int, slot: int):
        def handler(_pos):
            occupied = bool(self._slot_occupied[mount_id] & (1 << slot))
            self._edit_label(mount_id, slot, occupied)
        return handler

    def _edit_label(self, mount_id: int, slot: int, occupied: bool) -> None:
        cur = self._store.get_label(mount_id, slot)
        # Touchscreen-only PC: get_text() shows the on-screen keyboard for the
        # dialog and closes it as soon as it commits (OK / Enter / Return).
        text, ok = virtual_keyboard.get_text(
            self, "Edit Label",
            f"Label for Camera {mount_id}, Position {slot + 1}:\n"
            f"(Clear to delete slot)",
            cur if occupied else "")
        if not ok:
            return
        if text.strip() == "" and occupied:
            # Blank input on an occupied slot → confirm delete
            reply = QMessageBox.question(
                self, "Delete slot?",
                f"Clear Camera {mount_id} position {slot + 1} from the mount?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.Yes:
                self._store.set_label(mount_id, slot, str(slot + 1))
                self.clear_requested.emit(mount_id, slot)
        else:
            # Never store an empty string — fall back to the numeric default.
            label = text.strip() or str(slot + 1)
            self._store.set_label(mount_id, slot, label)
            self.refresh_button(mount_id, slot)
            self.label_edited.emit(mount_id, slot, label)

    def _make_pt_handler(self, mount_id: int):
        def handler(preset: int):
            self.pt_preset_changed.emit(mount_id, preset)
        return handler

    def _make_sl_handler(self, mount_id: int):
        def handler(preset: int):
            # Never act on the slider dial for a mount with no slider (the dial
            # is disabled too, so this is belt-and-braces).
            if not self._has_slider[mount_id]:
                return
            self.sl_preset_changed.emit(mount_id, preset)
        return handler
