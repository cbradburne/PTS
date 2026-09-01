"""
Config dialog — per-mount settings.

Tabs:
  - General: COM port, CV mount assignment, joystick deadzone
  - Mount 1-5: orientation, speed presets, stall thresholds
"""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QTabWidget, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QFormLayout, QLabel, QLineEdit, QCheckBox, QSpinBox, QDoubleSpinBox,
    QPushButton, QComboBox, QGroupBox, QDialogButtonBox, QScrollArea,
    QRadioButton, QButtonGroup, QStackedWidget, QSizePolicy
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFontDatabase

from config.mount_config import AppConfig, SpeedPreset, save_config
from comms.bridge import Bridge
from comms.mount_manager import MountManager
from comms.protocol import AxisGroup, Axis, NUM_MOUNTS as _NUM_MOUNTS



def _scrollable(inner: QWidget) -> QScrollArea:
    """Wrap a tab's content so it scrolls instead of being squashed.

    Qt will happily shrink widgets below their natural height to fit a short
    dialog, which made the Homing/Paired-Mounts button rows overlap their own
    labels.  Inside a scroll area the content keeps its real size and the user
    scrolls instead — so the layout no longer depends on the window size.
    """
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)                      # follow width only
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    scroll.setWidget(inner)
    return scroll


# NOTE: a QWidget, NOT a QDialog, on purpose — a QDialog over a
# native-fullscreen main window lands on the desktop Space and drags the
# operator out of fullscreen.  We re-provide the small QDialog surface actually
# used (accept()/reject() + accepted/finished) so callers are unchanged.
#
# The claim that once stood here — that a QWidget with the Dialog flag "floats
# correctly on the fullscreen Space" — was WRONG.  It does the same thing, just
# less obviously, which is why this kept coming back.  See __init__ for what
# replaced it.
class ConfigDialog(QWidget):

    accepted = pyqtSignal()      # emitted on OK (after settings are applied)
    finished = pyqtSignal(int)   # emitted on any close: 1 = accepted, 0 = rejected
    names_changed = pyqtSignal() # emitted after a name set is Loaded

    def __init__(self, config: AppConfig, mount_manager: MountManager,
                 bridge: Bridge, position_store=None, parent=None):
        # Exactly what CVWindow uses, because CVWindow has never had the macOS
        # fullscreen problem: a parented QWidget with the Dialog flag, shown
        # with show() + raise_() and NO activateWindow().
        #
        # The Sheet flag that briefly lived here was introduced while
        # activateWindow() was still being called, so it was never tested
        # without it — and Sheet has real costs (attached to the parent,
        # cannot be moved).  Since the window that works uses Dialog, use
        # Dialog, and keep the difference to CVWindow at zero.
        super().__init__(parent, Qt.WindowType.Dialog)
        self._result = 0
        self._config  = config
        self._store   = position_store
        self._mm      = mount_manager
        self._bridge  = bridge
        self._tabs    = None   # set in _build
        # Track which mounts have had their CONFIG_REPORT applied this session.
        # Once a mount's report is applied we stop overwriting spinboxes so the
        # user's in-progress edits are never clobbered by a late CONFIG_REPORT
        # (e.g. the echo fired by CMD_SET_STALL_THRESHOLD or a tab-switch retry).
        self._config_fetched: set[int] = set()
        self.setWindowTitle("Settings")
        # The General tab's content is ~600x666.  The old 600x500 minimum was
        # exactly as wide as the content, so as soon as the vertical scrollbar
        # appeared it stole ~15 px and forced a horizontal one too.  Size the
        # floor to content + chrome (margins, tab frame, scrollbar).
        self.setMinimumSize(680, 640)
        # Open tall enough to show the tallest tab without scrolling at all —
        # but never taller than the screen it lands on.
        scr = self.screen()
        avail_h = scr.availableGeometry().height() if scr else 900
        self.resize(720, max(640, min(820, avail_h - 80)))
        # Live-position readout widgets, per mount — filled in by _build().
        self._build()
        # Touchscreen numeric entry — a keypad beside the dialog, shown when a
        # spin box takes focus.  Built after _build() so the fields exist.
        if self._config.numeric_keypad:
            from ui.numeric_keypad import NumericKeypad
            NumericKeypad.install(self)
        # Connect live-update signal — fires when a mount responds to CMD_GET_CONFIG
        self._mm.config_report_received.connect(self._on_config_report)
        # Request config from all online mounts immediately
        self._request_all_configs()
        # Pairing table (hub-owned): live-refresh + request an initial push
        self._mm.mount_table_updated.connect(self._refresh_mount_table)
        self._mm.mount_route_updated.connect(self._refresh_mount_route)
        # Names arrive on their own message and can land either side of the
        # route, so redraw on both rather than assuming an order.
        self._mm.sat_names_updated.connect(self._refresh_sat_names)
        self._refresh_mount_table(self._mm.mount_table())
        self._refresh_mount_route(self._mm.mount_route)
        self._mm.request_mount_table()   # hub answers with the table AND the routes

    # ── QDialog-compatible surface (this is a QWidget — see class note) ──
    def accept(self) -> None:
        self._result = 1
        self.accepted.emit()
        self.close()

    def reject(self) -> None:
        self._result = 0
        self.close()

    def closeEvent(self, event) -> None:
        # Fires for OK, Cancel, and the window close button alike.
        self.finished.emit(self._result)
        super().closeEvent(event)

    # ── Camera & position name sets ──────────────────────────────────────
    def _names_save(self) -> None:
        from PyQt6.QtWidgets import QFileDialog
        from config import name_store
        if self._store is None:
            return
        name_store.ensure_dir()
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Names", str(name_store.NAMES_DIR / "Names.json"),
            "Name sets (*.json)",
            options=QFileDialog.Option.DontUseNativeDialog)
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            name_store.save_to(path, self._store, self._config)
            self._names_status.setText(f"Saved → {path}")
        except Exception as e:
            self._names_status.setStyleSheet("color:#EF5350; font-size:11px;")
            self._names_status.setText(f"Save failed: {e}")

    def _names_load(self) -> None:
        from PyQt6.QtWidgets import QFileDialog
        from config import name_store
        if self._store is None:
            return
        name_store.ensure_dir()
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Names", str(name_store.NAMES_DIR),
            "Name sets (*.json)",
            options=QFileDialog.Option.DontUseNativeDialog)
        if not path:
            return
        try:
            name_store.load_from(path, self._store, self._config)
            name_store.save_temp(self._store, self._config)   # loaded set becomes the working copy
            self.names_changed.emit()                          # refresh grid + cam buttons
            self._names_status.setStyleSheet("color:#7fbf72; font-size:11px;")
            self._names_status.setText(f"Loaded ← {path}")
        except Exception as e:
            self._names_status.setStyleSheet("color:#EF5350; font-size:11px;")
            self._names_status.setText(f"Load failed: {e}")

    def _names_set_defaults(self) -> None:
        from config import name_store
        if self._store is None:
            return
        try:
            name_store.save_default(self._store, self._config)
            self._names_status.setStyleSheet("color:#7fbf72; font-size:11px;")
            self._names_status.setText(f"Current names set as defaults "
                                       f"({name_store.DEFAULT_PATH.name})")
        except Exception as e:
            self._names_status.setStyleSheet("color:#EF5350; font-size:11px;")
            self._names_status.setText(f"Set defaults failed: {e}")

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        self._tabs = QTabWidget()

        # Pre-populate each mount's MountConfig from the cached CONFIG_REPORT
        # (if one has been received since app start) so that the spinboxes are
        # initialised with the Teensy's live values before the tab is built.
        # Adding the mount to _config_fetched immediately prevents any late-arriving
        # CONFIG_REPORT response from resetting spinboxes the user has already edited.
        for mid in range(1, 6):
            cr = self._mm.state(mid).last_config_report
            if cr is None:
                continue
            mc = self._config.mount(mid)
            mc.has_slider    = cr.has_slider
            mc.pan_invert    = cr.pan_invert
            mc.tilt_invert   = cr.tilt_invert
            mc.slider_invert = cr.slider_invert
            mc.zoom_invert   = cr.zoom_invert
            mc.lanc_zoom     = cr.lanc_zoom
            mc.look_at_mode  = cr.look_at_mode
            for i, (spd, acc) in enumerate(cr.pt_presets):
                mc.pan_tilt_presets.set(i + 1, SpeedPreset(
                    max(1, min(720,  spd)), max(1, min(2000, acc))))
            for i, (spd, acc) in enumerate(cr.sl_presets):
                mc.slider_presets.set(i + 1, SpeedPreset(
                    max(1, min(1000, spd)), max(1, min(5000, acc))))
            zm_spd, zm_acc = cr.zm_preset
            mc.zoom_preset = SpeedPreset(max(1, min(360, zm_spd)), max(1, min(2000, zm_acc)))
            if cr.stall_threshold_slider > 0:
                mc.stall_threshold_slider = cr.stall_threshold_slider
            if cr.stall_threshold_zoom > 0:
                mc.stall_threshold_zoom = cr.stall_threshold_zoom
            # Do NOT add to _config_fetched here — the first live CONFIG_REPORT
            # that arrives after the dialog opens will still update the spinboxes
            # (this handles the case where the cache is slightly stale).
            # _config_fetched is only populated by _on_config_report itself.

        # Explicit tab-index → mount map, so adding/reordering tabs can't
        # silently make _on_tab_changed request config for the wrong camera.
        self._tab_mount: dict[int, int] = {}
        self._tabs.addTab(self._build_general_tab(), "General")
        self._tabs.addTab(self._build_mounts_tab(),  "Mounts")
        for mid in range(1, 6):
            idx = self._tabs.addTab(self._build_mount_tab(mid), f"Camera {mid}")
            self._tab_mount[idx] = mid

        # Request config when the user switches to a mount tab
        self._tabs.currentChanged.connect(self._on_tab_changed)

        layout.addWidget(self._tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------
    # Live config helpers
    # ------------------------------------------------------------------

    def _request_all_configs(self) -> None:
        """Ask every connected mount for its current config."""
        if not self._bridge.connected:
            return
        for mid in range(1, _NUM_MOUNTS + 1):
            if self._mm.state(mid).connected:
                self._mm.send_get_config(mid)

    def _on_tab_changed(self, index: int) -> None:
        """Request a camera's config when its tab is opened.

        Uses the explicit map built in _build() — General and Mounts simply
        aren't in it, so they no-op.
        """
        mount_id = self._tab_mount.get(index)
        if mount_id is None:
            return
        # Only request if we haven't already applied a CONFIG_REPORT for this mount.
        # Avoids clobbering in-progress edits when the user switches tabs.
        if mount_id not in self._config_fetched:
            if self._bridge.connected and self._mm.state(mount_id).connected:
                self._mm.send_get_config(mount_id)

    def _on_config_report(self, mount_id: int, cr) -> None:
        """Populate dialog fields with live data received from a mount.

        Only the FIRST CONFIG_REPORT per mount per dialog session is applied.
        Subsequent reports (echoed by CMD_SET_STALL_THRESHOLD, CMD_SET_ORIENTATION,
        or a tab-switch retry) are ignored so user edits are never clobbered.
        """
        if mount_id in self._config_fetched:
            return
        self._config_fetched.add(mount_id)
        key = f"m{mount_id}"
        # Orientation toggles
        has_slider_cb = getattr(self, f"_{key}_has_slider", None)
        pan_inv_cb    = getattr(self, f"_{key}_pan_inv",    None)
        tilt_inv_cb   = getattr(self, f"_{key}_tilt_inv",   None)
        slider_inv_cb = getattr(self, f"_{key}_slider_inv", None)
        zoom_inv_cb   = getattr(self, f"_{key}_zoom_inv",   None)
        if has_slider_cb is not None:
            has_slider_cb.setChecked(cr.has_slider)
        if pan_inv_cb is not None:
            pan_inv_cb.setChecked(cr.pan_invert)
        if tilt_inv_cb is not None:
            tilt_inv_cb.setChecked(cr.tilt_invert)
        if slider_inv_cb is not None:
            slider_inv_cb.setChecked(cr.slider_invert)
        if zoom_inv_cb is not None:
            zoom_inv_cb.setChecked(cr.zoom_invert)
        lanc_zoom_cb  = getattr(self, f"_{key}_lanc_zoom",  None)
        if lanc_zoom_cb is not None:
            lanc_zoom_cb.setChecked(cr.lanc_zoom)
        look_at_mode_cb = getattr(self, f"_{key}_look_at_mode", None)
        if look_at_mode_cb is not None:
            look_at_mode_cb.setChecked(cr.look_at_mode)
            look_at_mode_cb.setEnabled(cr.has_slider)
        tilt_spin = getattr(self, f"_{key}_slider_tilt", None)
        if tilt_spin is not None:
            # From the MOUNT, so the box shows what is actually in force.
            tilt_spin.setValue(getattr(cr, "slider_tilt_deg", 0.0))
        margin_spin = getattr(self, f"_{key}_end_margin", None)
        # 0 means the mount is on firmware from before the field existed; keep
        # what is on screen rather than showing a rail with no margin at all.
        if margin_spin is not None and getattr(cr, "slider_end_margin_mm", 0):
            margin_spin.setValue(cr.slider_end_margin_mm)
        # PT speed presets
        pt_rows = getattr(self, f"_{key}_pt_rows", None)
        if pt_rows is not None:
            for i, (spd_spin, acc_spin) in enumerate(pt_rows):
                if i < len(cr.pt_presets):
                    spd, acc = cr.pt_presets[i]
                    spd_spin.setValue(max(spd_spin.minimum(),
                                         min(spd_spin.maximum(), spd)))
                    acc_spin.setValue(max(acc_spin.minimum(),
                                         min(acc_spin.maximum(), acc)))
        # SL speed presets
        sl_rows = getattr(self, f"_{key}_sl_rows", None)
        if sl_rows is not None:
            for i, (spd_spin, acc_spin) in enumerate(sl_rows):
                if i < len(cr.sl_presets):
                    spd, acc = cr.sl_presets[i]
                    spd_spin.setValue(max(spd_spin.minimum(),
                                         min(spd_spin.maximum(), spd)))
                    acc_spin.setValue(max(acc_spin.minimum(),
                                         min(acc_spin.maximum(), acc)))
        # ZM speed preset
        zm_spd_spin = getattr(self, f"_{key}_zm_spd", None)
        zm_acc_spin = getattr(self, f"_{key}_zm_acc", None)
        if zm_spd_spin is not None and zm_acc_spin is not None:
            zm_spd, zm_acc = cr.zm_preset
            zm_spd_spin.setValue(max(zm_spd_spin.minimum(),
                                     min(zm_spd_spin.maximum(), zm_spd)))
            zm_acc_spin.setValue(max(zm_acc_spin.minimum(),
                                     min(zm_acc_spin.maximum(), zm_acc)))
        # StallGuard thresholds — only update if mount reported non-zero values.
        # Also keep mc in sync so _open_find_limits() and the next dialog open
        # start with the correct live value rather than the JSON default.
        sg_sl_spin = getattr(self, f"_{key}_sg_slider", None)
        sg_zm_spin = getattr(self, f"_{key}_sg_zoom",   None)
        mc = self._config.mount(mount_id)
        if sg_sl_spin is not None and cr.stall_threshold_slider > 0:
            sg_sl_spin.setValue(cr.stall_threshold_slider)
            mc.stall_threshold_slider = cr.stall_threshold_slider
        if sg_zm_spin is not None and cr.stall_threshold_zoom > 0:
            sg_zm_spin.setValue(cr.stall_threshold_zoom)
            mc.stall_threshold_zoom = cr.stall_threshold_zoom

    # ------------------------------------------------------------------
    # General tab
    # ------------------------------------------------------------------

    def _build_general_tab(self) -> QWidget:
        # Form goes in its own widget so the outer layout can park all leftover
        # vertical space in a stretch at the BOTTOM.  Otherwise the form shares
        # the slack out among its rows — which inflated the Hub Connection box
        # (a big gap under Port) while squeezing the names box below it.
        w     = QWidget()
        outer = QVBoxLayout(w)
        outer.setContentsMargins(16, 16, 16, 16)
        inner = QWidget()
        form  = QFormLayout(inner)
        form.setSpacing(10)
        form.setContentsMargins(0, 0, 0, 0)

        # ---- Connection mode ----
        mode_box = QGroupBox("Hub Connection")
        mode_layout = QVBoxLayout(mode_box)

        self._tcp_radio    = QRadioButton("WiFi TCP  (ESP32 hub — production)")
        self._serial_radio = QRadioButton("USB Serial  (direct ESP32 — bench/dev)")
        self._tcp_radio.setChecked(self._config.bridge_mode == "tcp")
        self._serial_radio.setChecked(self._config.bridge_mode == "serial")

        mode_layout.addWidget(self._tcp_radio)
        mode_layout.addWidget(self._serial_radio)

        # Stacked pages: TCP settings / Serial settings
        self._conn_stack = QStackedWidget()

        # Page 0 — TCP
        tcp_page = QWidget()
        tcp_form = QFormLayout(tcp_page)
        tcp_form.setContentsMargins(0, 4, 0, 0)
        self._host_edit = QLineEdit(self._config.bridge_host)
        self._host_edit.setPlaceholderText("192.168.4.1")
        self._tcp_port_spin = QSpinBox()
        self._tcp_port_spin.setRange(1, 65535)
        self._tcp_port_spin.setValue(self._config.bridge_tcp_port)
        tcp_form.addRow("Hub IP:", self._host_edit)
        tcp_form.addRow("Port:", self._tcp_port_spin)
        self._conn_stack.addWidget(tcp_page)   # index 0

        # Page 1 — Serial
        serial_page = QWidget()
        serial_form = QFormLayout(serial_page)
        serial_form.setContentsMargins(0, 4, 0, 0)
        port_row = QHBoxLayout()
        self._port_combo = QComboBox()
        self._refresh_ports_btn = QPushButton("Refresh")
        self._refresh_ports_btn.clicked.connect(self._refresh_ports)
        self._refresh_ports()
        port_row.addWidget(self._port_combo)
        port_row.addWidget(self._refresh_ports_btn)
        serial_form.addRow("Serial port:", port_row)
        self._conn_stack.addWidget(serial_page)  # index 1

        # Don't let the stack grow past the taller of its two pages — by default
        # a QStackedWidget expands vertically and absorbs the tab's spare space.
        self._conn_stack.setSizePolicy(QSizePolicy.Policy.Preferred,
                                       QSizePolicy.Policy.Fixed)
        self._conn_stack.setCurrentIndex(0 if self._config.bridge_mode == "tcp" else 1)
        self._tcp_radio.toggled.connect(
            lambda checked: self._conn_stack.setCurrentIndex(0 if checked else 1))

        mode_layout.addWidget(self._conn_stack)
        form.addRow(mode_box)

        # ---- CV mount ----
        self._cv_mount_spin = QSpinBox()
        self._cv_mount_spin.setRange(1, 5)
        self._cv_mount_spin.setValue(self._config.cv_mount_id)
        form.addRow("CV tracking mount:", self._cv_mount_spin)

        # ---- CV performance (see tools/cv_benchmark.py) ----
        # The correlation tracker runs every tick (30 Hz) while YOLO runs ~3x/s,
        # so the tracker choice dominates CPU cost on modest machines.
        self._cv_tracker_combo = QComboBox()
        for label, key in (("MOSSE  (fastest — recommended)", "mosse"),
                           ("MedianFlow  (fast)",             "medianflow"),
                           ("KCF  (slower)",                  "kcf"),
                           ("CSRT  (most accurate, slowest)", "csrt")):
            self._cv_tracker_combo.addItem(label, key)
        idx = self._cv_tracker_combo.findData(self._config.cv_tracker)
        self._cv_tracker_combo.setCurrentIndex(idx if idx >= 0 else 0)
        form.addRow("CV tracker:", self._cv_tracker_combo)

        self._cv_size_combo = QComboBox()
        for sz in (256, 320, 416, 512, 640):
            self._cv_size_combo.addItem(
                f"{sz} px" + ("  (recommended)" if sz == 416 else ""), sz)
        idx = self._cv_size_combo.findData(self._config.cv_detect_size)
        self._cv_size_combo.setCurrentIndex(idx if idx >= 0 else 2)
        form.addRow("CV detect size:", self._cv_size_combo)

        # ---- Joystick deadzone ----
        self._deadzone_spin = QDoubleSpinBox()
        self._deadzone_spin.setRange(0.0, 0.5)
        self._deadzone_spin.setSingleStep(0.01)
        self._deadzone_spin.setValue(self._config.joystick_deadzone)
        form.addRow("Joystick deadzone:", self._deadzone_spin)

        # ---- On-screen keyboard (touchscreen setups) ----
        self._osk_check = QCheckBox(
            "Show an on-screen keyboard for text entry (touchscreen, no keyboard)")
        self._osk_check.setChecked(self._config.virtual_keyboard)
        form.addRow("Virtual keyboard:", self._osk_check)

        self._keypad_check = QCheckBox(
            "Show a numeric keypad beside this window when a value is selected")
        self._keypad_check.setChecked(self._config.numeric_keypad)
        form.addRow("Numeric keypad:", self._keypad_check)

        # ---- Camera & position names (save / load / defaults) ----
        names_box = QGroupBox("Camera && Position Names")
        names_vl  = QVBoxLayout(names_box)
        names_note = QLabel(
            "The 5 camera names and 50 position names load from Default.json at "
            "startup.  Editing a name updates the working copy only — use these "
            "buttons to save, load, or set the startup defaults.\nFiles live in "
            "your Documents/PTS folder.")
        names_note.setWordWrap(True)
        names_vl.addWidget(names_note)
        names_row = QHBoxLayout()
        save_names_btn     = QPushButton("Save…")
        load_names_btn     = QPushButton("Load…")
        defaults_names_btn = QPushButton("Set as Defaults")
        save_names_btn.clicked.connect(self._names_save)
        load_names_btn.clicked.connect(self._names_load)
        defaults_names_btn.clicked.connect(self._names_set_defaults)
        for b in (save_names_btn, load_names_btn, defaults_names_btn):
            names_row.addWidget(b)
        names_vl.addLayout(names_row)
        self._names_status = QLabel("")
        self._names_status.setStyleSheet("color:#7fbf72; font-size:11px;")
        names_vl.addWidget(self._names_status)
        form.addRow(names_box)

        # ---- Slider / Zoom / Ref grid (all 5 cameras) ----
        # "&&" — a lone '&' is a Qt mnemonic marker and renders as an underscore.
        ops_box = QGroupBox("Homing && Reference")
        ops_vl  = QVBoxLayout(ops_box)
        ops_note = QLabel(
            "Home: drives the axis to its end stop, zeroes position, then backs off.\n"
            "Ref: zeroes the look-at angular reference at the camera's current position."
        )
        ops_note.setWordWrap(True)
        ops_vl.addWidget(ops_note)

        grid = QGridLayout()
        grid.setSpacing(6)

        # Header — camera labels
        for col, mid in enumerate(range(1, 6), start=1):
            lbl = QLabel(self._config.mount_label(mid))
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("font-weight: bold;")
            grid.addWidget(lbl, 0, col)

        # Row labels
        for row, text in enumerate(("Slider Home", "Zoom Home", "Ref"), start=1):
            lbl = QLabel(text)
            lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(lbl, row, 0)

        # Buttons
        for col, mid in enumerate(range(1, 6), start=1):
            sl_btn = QPushButton("Home")
            zm_btn = QPushButton("Home")
            ref_btn = QPushButton("Ref")
            for btn in (sl_btn, zm_btn, ref_btn):
                btn.setFixedHeight(32)
            sl_btn.clicked.connect(lambda checked, m=mid: self._do_home_slider(m))
            zm_btn.clicked.connect(lambda checked, m=mid: self._do_home_zoom(m))
            ref_btn.clicked.connect(lambda checked, m=mid: self._do_manual_ref(m))
            grid.addWidget(sl_btn,  1, col)
            grid.addWidget(zm_btn,  2, col)
            grid.addWidget(ref_btn, 3, col)

        ops_vl.addLayout(grid)
        form.addRow(ops_box)

        outer.addWidget(inner)
        outer.addStretch()      # spare space collects here, below Homing & Reference

        # Scroll rather than squash: without this the tab's groups get crushed
        # below their natural height when the dialog is short (the Home/Ref
        # buttons overlap their own labels).
        return _scrollable(w)

    # ------------------------------------------------------------------
    # Mounts tab — the hub's pairing table (view / forget)
    # ------------------------------------------------------------------

    def _build_mounts_tab(self) -> QWidget:
        w  = QWidget()
        vl = QVBoxLayout(w)
        vl.setContentsMargins(16, 16, 16, 16)
        vl.setSpacing(10)

        pair_box = QGroupBox("Paired Mounts")
        pair_vl  = QVBoxLayout(pair_box)
        pair_note = QLabel(
            "The hub owns the pairing table; this shows its live state (nothing "
            "is stored here).  Forget frees a slot — a live mount re-pairs itself "
            "within ~5 s, so use it for a retired unit.  A same-number conflict "
            "pops a Replace / Ignore prompt.")
        pair_note.setWordWrap(True)
        pair_vl.addWidget(pair_note)

        pair_grid = QGridLayout()
        pair_grid.setSpacing(8)
        # Ask Qt for the platform's real fixed-width face.  A CSS
        # "font-family: monospace" is not a family that exists on macOS, and
        # sends Qt off building font-family aliases (the startup warning).
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        self._pair_mac_lbls:    dict[int, QLabel]      = {}
        self._pair_via_lbls: dict = {}
        self._pair_forget_btns: dict[int, QPushButton] = {}
        for row, mid in enumerate(range(1, 6)):
            cam_lbl = QLabel(self._config.mount_label(mid))
            cam_lbl.setStyleSheet("font-weight: bold;")
            cam_lbl.setMinimumHeight(30)
            mac_lbl = QLabel("—")
            mac_lbl.setFont(mono)
            mac_lbl.setStyleSheet("color: #8a97a8;")
            mac_lbl.setMinimumHeight(30)
            # How the hub reaches this mount.  Blank when it is on the hub's
            # own radio, which is the ordinary case and needs no decoration.
            via_lbl = QLabel("")
            via_lbl.setStyleSheet("color: #8a97a8; font-size: 11px;")
            via_lbl.setMinimumHeight(30)
            forget_btn = QPushButton("Forget")
            forget_btn.setFixedHeight(30)
            forget_btn.clicked.connect(lambda checked, m=mid: self._mm.send_pair_forget(m))
            pair_grid.addWidget(cam_lbl,    row, 0)
            pair_grid.addWidget(mac_lbl,    row, 1)
            pair_grid.addWidget(via_lbl,    row, 2)
            pair_grid.addWidget(forget_btn, row, 3)
            self._pair_mac_lbls[mid]    = mac_lbl
            self._pair_via_lbls[mid]    = via_lbl
            self._pair_forget_btns[mid] = forget_btn
        pair_grid.setColumnStretch(1, 1)
        pair_vl.addLayout(pair_grid)

        vl.addWidget(pair_box)
        vl.addStretch()
        return _scrollable(w)

    @staticmethod
    def _fmt_mac(mac: bytes) -> str:
        return ":".join(f"{b:02x}" for b in mac)

    def _refresh_mount_route(self, route: list) -> None:
        """Show which satellite relays each mount, if any.

        Without this the RSSI in the log is unreadable: it is measured wherever
        the frame arrived, so a mount reads -40 with a satellite beside it and
        -85 when that satellite drops and it falls back to the hub — the same
        mount, unmoved, with nothing else on screen to explain the change."""
        self._route = list(route)
        for mid in range(1, 6):
            via = route[mid - 1] if len(route) >= mid else 0
            lbl = self._pair_via_lbls.get(mid)
            if lbl is not None:
                # "via Foyer" beats "via SAT 2": the slot number is TCP accept
                # order and tells nobody which box in the building to go and
                # look at.  mount_manager falls back to the number for a
                # satellite that has not given a name.
                lbl.setText(f"via {self._mm.sat_label(via)}" if via else "")

    def _refresh_sat_names(self, _names: dict) -> None:
        """A satellite named itself — relabel the rows already on screen."""
        self._refresh_mount_route(getattr(self, "_route", []))

    def _refresh_mount_table(self, table: list) -> None:
        """Update the Paired Mounts rows from the hub's table (5 × 6-byte MAC)."""
        for mid in range(1, 6):
            mac   = table[mid - 1] if len(table) >= mid else b"\x00" * 6
            bound = any(mac)
            self._pair_mac_lbls[mid].setText(self._fmt_mac(mac) if bound else "— unpaired —")
            self._pair_forget_btns[mid].setVisible(bound)

    def _do_home_slider(self, mount_id: int) -> None:
        key   = f"m{mount_id}"
        sg_sl = getattr(self, f"_{key}_sg_slider", None)
        mc    = self._config.mount(mount_id)
        thresh = sg_sl.value() if sg_sl is not None else mc.stall_threshold_slider
        self._mm.send_find_home(mount_id, Axis.SLIDER, thresh)

    def _do_home_zoom(self, mount_id: int) -> None:
        key   = f"m{mount_id}"
        sg_zm = getattr(self, f"_{key}_sg_zoom", None)
        mc    = self._config.mount(mount_id)
        thresh = sg_zm.value() if sg_zm is not None else mc.stall_threshold_zoom
        self._mm.send_find_home(mount_id, Axis.ZOOM, thresh)

    def _do_manual_ref(self, mount_id: int) -> None:
        self._mm.send_set_ref(mount_id, 0xFF)

    def _open_find_limits(self, mount_id: int, axis: Axis) -> None:
        from ui.dialogs.find_limits_dialog import FindLimitsDialog
        mc  = self._config.mount(mount_id)
        key = f"m{mount_id}"
        # Read from the live spinbox — _on_config_report() keeps it in sync with
        # whatever the mount last reported, so the dialog gets the real live
        # threshold rather than the possibly-stale JSON default.  This matters
        # more since the slider run current went to 2000 mA: StallGuard is
        # tuned against a given current, so a stale threshold finds the wrong end.
        if axis == Axis.SLIDER:
            sg = getattr(self, f"_{key}_sg_slider", None)
            threshold = sg.value() if sg is not None else mc.stall_threshold_slider
        else:
            sg = getattr(self, f"_{key}_sg_zoom", None)
            threshold = sg.value() if sg is not None else mc.stall_threshold_zoom
        dlg = FindLimitsDialog(mount_id, self._mm, axis,
                               stall_threshold=threshold, parent=self)
        # Window-modal → sheet on macOS, so this sub-dialog doesn't animate the
        # app out of fullscreen the way application-modal exec() does.
        import sys
        if sys.platform != "win32":
            from PyQt6.QtCore import Qt
            dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.exec()

    def _refresh_ports(self) -> None:
        _EXCLUDE = ("debug", "bluetooth")
        ports = [p for p in Bridge.list_ports()
                 if not any(kw in p.lower() for kw in _EXCLUDE)]
        self._port_combo.clear()
        self._port_combo.addItems(ports)
        if self._config.bridge_port in ports:
            self._port_combo.setCurrentText(self._config.bridge_port)

    # ------------------------------------------------------------------
    # Per-mount tab
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Live position readout
    # ------------------------------------------------------------------

    def _build_mount_tab(self, mount_id: int) -> QWidget:
        mc = self._config.mount(mount_id)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setSpacing(12)
        layout.setContentsMargins(12, 12, 12, 12)


        # Orientation / hardware
        orientation_box = QGroupBox("Orientation / Hardware")
        ol = QVBoxLayout(orientation_box)
        has_slider_cb = QCheckBox("Has slider axis")
        pan_inv_cb    = QCheckBox("Invert pan direction (mount is back-to-front)")
        tilt_inv_cb   = QCheckBox("Invert tilt direction (+tilt physically moves camera down)")
        slider_inv_cb = QCheckBox("Invert slider direction (left/right swapped)")
        zoom_inv_cb   = QCheckBox("Invert zoom direction (lens zooms opposite way)")
        lanc_zoom_cb     = QCheckBox("LANC Zoom (serial control — position not stored/recalled)")
        look_at_mode_cb  = QCheckBox("Look-at mode (3D triangulation — subjects 1-8 + slider arrows)")
        has_slider_cb.setChecked(mc.has_slider)
        pan_inv_cb.setChecked(mc.pan_invert)
        tilt_inv_cb.setChecked(mc.tilt_invert)
        slider_inv_cb.setChecked(mc.slider_invert)
        zoom_inv_cb.setChecked(mc.zoom_invert)
        lanc_zoom_cb.setChecked(mc.lanc_zoom)
        look_at_mode_cb.setChecked(mc.look_at_mode)
        # Look-at mode only makes sense when there's a slider
        look_at_mode_cb.setEnabled(mc.has_slider)
        has_slider_cb.toggled.connect(look_at_mode_cb.setEnabled)
        ol.addWidget(has_slider_cb)
        ol.addWidget(pan_inv_cb)
        ol.addWidget(tilt_inv_cb)
        ol.addWidget(slider_inv_cb)
        ol.addWidget(zoom_inv_cb)
        ol.addWidget(lanc_zoom_cb)
        ol.addWidget(look_at_mode_cb)

        # ── Slider tilt ──────────────────────────────────────────────────
        # The look-at solver used to place both calibration viewpoints on one
        # horizontal line at equal height.  A rail on a slope breaks that: at
        # 21 degrees the camera climbs 358 mm for every metre it travels, so a
        # 2 m calibration baseline separates the two viewpoints by 717 mm
        # VERTICALLY.  Two rays anchored at the wrong heights do not cross at
        # the subject, so no lock is possible — and even a good subject would
        # drift several degrees as the slider ran.
        tilt_row = QWidget()
        tilt_hl  = QHBoxLayout(tilt_row)
        tilt_hl.setContentsMargins(0, 0, 0, 0)
        slider_tilt_spin = QDoubleSpinBox()
        slider_tilt_spin.setRange(-90.0, 90.0)
        slider_tilt_spin.setDecimals(1)
        slider_tilt_spin.setSingleStep(0.5)
        slider_tilt_spin.setSuffix(" °")
        slider_tilt_spin.setValue(getattr(mc, "slider_tilt_deg", 0.0))
        slider_tilt_spin.setToolTip(
            "Inclination of the slider rail. 0 is level.\n"
            "Positive = the rail rises as the slider position increases.\n"
            "Used by look-at triangulation; a wrong value here puts the\n"
            "subject in the wrong place.")
        tilt_hl.addWidget(QLabel("Slider tilt:"))
        tilt_hl.addWidget(slider_tilt_spin)
        tilt_hl.addSpacing(16)

        # End margin. A stall is noticed late — the threshold is desensitised so
        # the carriage will not stall part-way along a tilted rail — so the
        # position recorded at an end is already inside that stop, and driving
        # back to it grinds. This holds the usable rail back from BOTH stalls.
        #
        # A setting rather than a constant because the right value is found by
        # trying one and listening, and as a constant every attempt cost a board
        # removal, a re-home, a ref 0/0 and a recalibration.
        end_margin_spin = QSpinBox()
        end_margin_spin.setRange(3, 200)
        end_margin_spin.setSingleStep(5)
        end_margin_spin.setSuffix(" mm")
        end_margin_spin.setValue(getattr(mc, "slider_end_margin_mm", 30))
        end_margin_spin.setToolTip(
            "How far the usable rail is held back from EACH end stop.\n"
            "The stall is noticed late, so the position found by Find Limits\n"
            "is already inside the stop; without this every move to a limit\n"
            "grinds against it.\n\n"
            "Costs twice this much travel in total. Lower it until you hear\n"
            "the carriage touch, then go back up. Takes effect on the next\n"
            "Find Limits.")
        tilt_hl.addWidget(QLabel("End margin:"))
        tilt_hl.addWidget(end_margin_spin)
        tilt_hl.addStretch()
        ol.addWidget(tilt_row)
        # Only meaningful with a slider — same rule as look-at mode, and shown
        # rather than merely disabled so a mount without a rail is not asked a
        # question that has no answer.
        tilt_row.setVisible(has_slider_cb.isChecked())
        has_slider_cb.toggled.connect(tilt_row.setVisible)
        layout.addWidget(orientation_box)

        # Speed presets — Pan/Tilt
        pt_box  = QGroupBox("Pan / Tilt Speed Presets")
        pt_form = QFormLayout(pt_box)
        pt_rows: list[tuple[QSpinBox, QSpinBox]] = []
        for i in range(1, 5):
            sp       = mc.pan_tilt_presets.get(i)
            spd_spin = QSpinBox(); spd_spin.setRange(1, 720); spd_spin.setValue(sp.max_speed)
            acc_spin = QSpinBox(); acc_spin.setRange(1, 2000); acc_spin.setValue(sp.accel)
            row_w    = QWidget()
            row_hl   = QHBoxLayout(row_w); row_hl.setContentsMargins(0,0,0,0)
            row_hl.addWidget(QLabel("Speed (deg/s):")); row_hl.addWidget(spd_spin)
            row_hl.addWidget(QLabel("  Accel (deg/s²):")); row_hl.addWidget(acc_spin)
            pt_form.addRow(f"Preset {i}:", row_w)
            pt_rows.append((spd_spin, acc_spin))
        layout.addWidget(pt_box)

        # Speed presets — Slider (hidden when no slider)
        sl_box  = QGroupBox("Slider Speed Presets")
        sl_form = QFormLayout(sl_box)
        sl_rows: list[tuple[QSpinBox, QSpinBox]] = []
        for i in range(1, 5):
            sp       = mc.slider_presets.get(i)
            spd_spin = QSpinBox(); spd_spin.setRange(1, 1000); spd_spin.setValue(sp.max_speed)
            acc_spin = QSpinBox(); acc_spin.setRange(1, 5000); acc_spin.setValue(sp.accel)
            row_w    = QWidget()
            row_hl   = QHBoxLayout(row_w); row_hl.setContentsMargins(0,0,0,0)
            row_hl.addWidget(QLabel("Speed (mm/s):")); row_hl.addWidget(spd_spin)
            row_hl.addWidget(QLabel("  Accel (mm/s²):")); row_hl.addWidget(acc_spin)
            sl_form.addRow(f"Preset {i}:", row_w)
            sl_rows.append((spd_spin, acc_spin))
        has_slider_cb.toggled.connect(sl_box.setVisible)
        layout.addWidget(sl_box)
        # setVisible only AFTER addWidget.  On a widget that has no parent yet,
        # setVisible(True) does not "make it visible later" — it SHOWS IT AS A
        # TOP-LEVEL WINDOW.  Five camera tabs meant a burst of stray windows
        # during construction, and on macOS they land on the desktop Space and
        # take the operator with them.  That is the fullscreen bug: nothing to
        # do with the Config window's own flags, which is why four fixes to
        # those changed nothing.
        sl_box.setVisible(mc.has_slider)

        # Speed preset — Zoom (single independent preset)
        zm_box  = QGroupBox("Zoom Speed Preset")
        zm_form = QFormLayout(zm_box)
        zm_spd  = QSpinBox(); zm_spd.setRange(1, 360); zm_spd.setValue(mc.zoom_preset.max_speed)
        zm_acc  = QSpinBox(); zm_acc.setRange(1, 2000); zm_acc.setValue(mc.zoom_preset.accel)
        zm_row  = QWidget()
        zm_hl   = QHBoxLayout(zm_row); zm_hl.setContentsMargins(0,0,0,0)
        zm_hl.addWidget(QLabel("Speed (deg/s):")); zm_hl.addWidget(zm_spd)
        zm_hl.addWidget(QLabel("  Accel (deg/s²):")); zm_hl.addWidget(zm_acc)
        zm_form.addRow("Preset:", zm_row)
        layout.addWidget(zm_box)

        # StallGuard thresholds
        sg_box  = QGroupBox("StallGuard Thresholds (0=sensitive, 255=coarse)")
        sg_form = QFormLayout(sg_box)
        sg_slider = QSpinBox(); sg_slider.setRange(0, 255); sg_slider.setValue(mc.stall_threshold_slider)
        sg_zoom   = QSpinBox(); sg_zoom.setRange(0, 255);   sg_zoom.setValue(mc.stall_threshold_zoom)
        sg_form.addRow("Slider:", sg_slider)
        sg_form.addRow("Zoom:",   sg_zoom)
        layout.addWidget(sg_box)

        # Find Limits — at the bottom of each camera tab
        limits_box  = QGroupBox("Find Limits")
        limits_vl   = QVBoxLayout(limits_box)
        limits_note = QLabel(
            "Drive slider and zoom to their end stops (stall) to calibrate the axis "
            "range.  Required before using look-at tracking."
        )
        limits_note.setWordWrap(True)
        limits_vl.addWidget(limits_note)
        # One button per axis, side by side, matching the web app's extended
        # config — the axis picker inside the dialog was an extra step for a
        # choice the operator has already made by the time they click.
        mid_capture = mount_id
        btn_row = QHBoxLayout()
        slider_btn = QPushButton("Slider")
        zoom_btn   = QPushButton("Zoom")
        for b, ax in ((slider_btn, Axis.SLIDER), (zoom_btn, Axis.ZOOM)):
            b.setFixedHeight(36)
            b.clicked.connect(
                lambda _, m=mid_capture, a=ax: self._open_find_limits(m, a))
            btn_row.addWidget(b)
        limits_vl.addLayout(btn_row)

        # Slider button follows the hardware checkbox; zoom is meaningless
        # under LANC, where the camera drives its own lens and reports no
        # step count to find limits on.
        slider_btn.setVisible(mc.has_slider)
        zoom_btn.setVisible(not mc.lanc_zoom)
        has_slider_cb.toggled.connect(slider_btn.setVisible)
        lanc_zoom_cb.toggled.connect(lambda on: zoom_btn.setVisible(not on))

        # Derive from the checkbox STATE, not from the buttons' isVisible():
        # during _build() nothing is shown yet, so isVisible() is False for
        # every child regardless of its own flag, and reading it here hid the
        # whole box permanently.
        def _sync_limits_box(_=None, scb=has_slider_cb, lcb=lanc_zoom_cb,
                             box=limits_box):
            box.setVisible(scb.isChecked() or not lcb.isChecked())
        has_slider_cb.toggled.connect(_sync_limits_box)
        lanc_zoom_cb.toggled.connect(_sync_limits_box)
        layout.addWidget(limits_box)
        _sync_limits_box()          # after addWidget — see the note above


        layout.addStretch()
        scroll.setWidget(inner)

        # Store refs for _apply()
        key = f"m{mount_id}"
        setattr(self, f"_{key}_has_slider",   has_slider_cb)
        setattr(self, f"_{key}_pan_inv",      pan_inv_cb)
        setattr(self, f"_{key}_tilt_inv",     tilt_inv_cb)
        setattr(self, f"_{key}_slider_inv",   slider_inv_cb)
        setattr(self, f"_{key}_zoom_inv",     zoom_inv_cb)
        setattr(self, f"_{key}_lanc_zoom",    lanc_zoom_cb)
        setattr(self, f"_{key}_look_at_mode", look_at_mode_cb)
        setattr(self, f"_{key}_slider_tilt", slider_tilt_spin)
        setattr(self, f"_{key}_end_margin", end_margin_spin)
        setattr(self, f"_{key}_pt_rows",    pt_rows)
        setattr(self, f"_{key}_sl_rows",    sl_rows)
        setattr(self, f"_{key}_zm_spd",     zm_spd)
        setattr(self, f"_{key}_zm_acc",     zm_acc)
        setattr(self, f"_{key}_sg_slider",  sg_slider)
        setattr(self, f"_{key}_sg_zoom",    sg_zoom)

        return scroll

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    def _apply(self) -> None:
        # General — connection
        if self._tcp_radio.isChecked():
            self._config.bridge_mode     = "tcp"
            self._config.bridge_host     = self._host_edit.text().strip() or "192.168.4.1"
            self._config.bridge_tcp_port = self._tcp_port_spin.value()
        else:
            self._config.bridge_mode = "serial"
            port = self._port_combo.currentText()
            if port:
                self._config.bridge_port = port

        self._config.cv_mount_id       = self._cv_mount_spin.value()
        self._config.cv_tracker        = self._cv_tracker_combo.currentData()
        self._config.cv_detect_size    = int(self._cv_size_combo.currentData())
        self._config.joystick_deadzone = self._deadzone_spin.value()

        self._config.virtual_keyboard  = self._osk_check.isChecked()
        from ui import virtual_keyboard
        virtual_keyboard.set_enabled(self._config.virtual_keyboard)  # live
        # Numeric keypad — apply live too, so the effect is visible immediately
        # rather than only on the next time Settings is opened.
        self._config.numeric_keypad = self._keypad_check.isChecked()
        kp = getattr(self, "_numeric_keypad", None)
        if self._config.numeric_keypad and kp is None:
            from ui.numeric_keypad import NumericKeypad
            NumericKeypad.install(self)
        elif not self._config.numeric_keypad and kp is not None:
            kp.hide()
            kp.deleteLater()
            self._numeric_keypad = None

        # Per-mount — only apply settings for connected cameras.
        # Unconnected cameras have no authoritative values (checkboxes show stale
        # local defaults), so writing them would corrupt the grid state.
        from comms.protocol import Axis as _Axis
        for mount_id in range(1, 6):
            if not self._mm.state(mount_id).connected:
                continue

            key = f"m{mount_id}"
            mc  = self._config.mount(mount_id)

            has_slider   = getattr(self, f"_{key}_has_slider").isChecked()
            pan_inv      = getattr(self, f"_{key}_pan_inv").isChecked()
            tilt_inv     = getattr(self, f"_{key}_tilt_inv").isChecked()
            slider_inv   = getattr(self, f"_{key}_slider_inv").isChecked()
            zoom_inv     = getattr(self, f"_{key}_zoom_inv").isChecked()
            lanc_zoom    = getattr(self, f"_{key}_lanc_zoom").isChecked()
            look_at_mode = getattr(self, f"_{key}_look_at_mode").isChecked()

            mc.has_slider    = has_slider
            mc.pan_invert    = pan_inv
            mc.tilt_invert   = tilt_inv
            mc.slider_invert = slider_inv
            mc.zoom_invert   = zoom_inv
            mc.lanc_zoom     = lanc_zoom
            mc.look_at_mode  = look_at_mode

            pt_rows = getattr(self, f"_{key}_pt_rows")
            for i, (spd, acc) in enumerate(pt_rows):
                mc.pan_tilt_presets.set(i + 1, SpeedPreset(spd.value(), acc.value()))

            sl_rows = getattr(self, f"_{key}_sl_rows")
            for i, (spd, acc) in enumerate(sl_rows):
                mc.slider_presets.set(i + 1, SpeedPreset(spd.value(), acc.value()))

            zm_spd = getattr(self, f"_{key}_zm_spd").value()
            zm_acc = getattr(self, f"_{key}_zm_acc").value()
            mc.zoom_preset = SpeedPreset(zm_spd, zm_acc)

            mc.stall_threshold_slider = getattr(self, f"_{key}_sg_slider").value()
            mc.stall_threshold_zoom   = getattr(self, f"_{key}_sg_zoom").value()

            # Orientation FIRST — each command causes the Teensy to echo a full
            # CONFIG_REPORT.  If orientation (which carries look_at_mode, has_slider,
            # inversion flags) were sent last, the echoes from save_speeds and the
            # stall-threshold commands would carry the *old* orientation values and
            # revert the PC-app grid immediately after the dialog closes.  Sending
            # orientation first means every subsequent echo already carries the new
            # orientation, so no intermediate revert occurs.
            slider_tilt = getattr(self, f"_{key}_slider_tilt").value()
            end_margin  = getattr(self, f"_{key}_end_margin").value()
            mc.slider_tilt_deg       = slider_tilt
            mc.slider_end_margin_mm  = end_margin
            self._mm.send_set_orientation(mount_id, pan_inv, slider_inv,
                                          has_slider, zoom_inv, lanc_zoom,
                                          tilt_inv, look_at_mode, slider_tilt,
                                          end_margin)

            # Speed presets — its CONFIG_REPORT echo (carrying the new PT/SL
            # values) will arrive at any subsequently-opened config dialog before
            # the stall-threshold echoes do.  This prevents the stall-threshold
            # echoes (which carry the *old* PT speeds, because save_speeds hasn't
            # run on the Teensy yet) from clobbering the spinboxes in the new dialog.
            pt = [(mc.pan_tilt_presets.get(i).max_speed,
                   mc.pan_tilt_presets.get(i).accel) for i in range(1, 5)]
            sl = [(mc.slider_presets.get(i).max_speed,
                   mc.slider_presets.get(i).accel)   for i in range(1, 5)]
            zm = (mc.zoom_preset.max_speed, mc.zoom_preset.accel)
            self._mm.send_save_speeds(mount_id, pt, sl, zm)

            # Stall thresholds (one command each → save_full_config on Teensy)
            self._mm.send_set_stall_threshold(mount_id, _Axis.SLIDER,
                                              mc.stall_threshold_slider)
            self._mm.send_set_stall_threshold(mount_id, _Axis.ZOOM,
                                              mc.stall_threshold_zoom)

        save_config(self._config)
        self.accept()
