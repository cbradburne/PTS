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
    QRadioButton, QButtonGroup, QStackedWidget
)
from PyQt6.QtCore import Qt, pyqtSignal

from config.mount_config import AppConfig, SpeedPreset, save_config
from comms.bridge import Bridge
from comms.mount_manager import MountManager
from comms.protocol import AxisGroup, Axis, NUM_MOUNTS as _NUM_MOUNTS


# NOTE: this is a QWidget, NOT a QDialog, on purpose.  On macOS a QDialog
# opened over a native-fullscreen main window is placed on the *desktop* Space
# (dragging the operator out of fullscreen), whereas a plain QWidget with the
# Dialog window flag floats correctly on the fullscreen Space — exactly like
# the CV window.  We re-provide the tiny QDialog surface we actually use
# (accept()/reject() + accepted/finished signals) so callers are unchanged.
class ConfigDialog(QWidget):

    accepted = pyqtSignal()      # emitted on OK (after settings are applied)
    finished = pyqtSignal(int)   # emitted on any close: 1 = accepted, 0 = rejected
    names_changed = pyqtSignal() # emitted after a name set is Loaded

    def __init__(self, config: AppConfig, mount_manager: MountManager,
                 bridge: Bridge, position_store=None, parent=None):
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
        self.setMinimumSize(600, 500)
        self._build()
        # Connect live-update signal — fires when a mount responds to CMD_GET_CONFIG
        self._mm.config_report_received.connect(self._on_config_report)
        # Request config from all online mounts immediately
        self._request_all_configs()
        # Pairing table (hub-owned): live-refresh + request an initial push
        self._mm.mount_table_updated.connect(self._refresh_mount_table)
        self._refresh_mount_table(self._mm.mount_table())
        self._mm.request_mount_table()

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

        self._tabs.addTab(self._build_general_tab(), "General")
        for mid in range(1, 6):
            self._tabs.addTab(self._build_mount_tab(mid), f"Camera {mid}")

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
        """Tab 0 = General; tabs 1-5 = Camera 1-5."""
        if index < 1:
            return
        mount_id = index   # tab 1 → mount 1, etc.
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
        w = QWidget()
        form = QFormLayout(w)
        form.setSpacing(10)
        form.setContentsMargins(16, 16, 16, 16)

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
        ops_box = QGroupBox("Homing & Reference")
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

        # ---- Paired mounts (hub pairing table: view / forget) ----
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
        pair_grid.setSpacing(6)
        self._pair_mac_lbls:    dict[int, QLabel]      = {}
        self._pair_forget_btns: dict[int, QPushButton] = {}
        for row, mid in enumerate(range(1, 6)):
            cam_lbl = QLabel(self._config.mount_label(mid))
            cam_lbl.setStyleSheet("font-weight: bold;")
            mac_lbl = QLabel("—")
            mac_lbl.setStyleSheet("font-family: monospace; color: #8a97a8;")
            forget_btn = QPushButton("Forget")
            forget_btn.setFixedHeight(28)
            forget_btn.clicked.connect(lambda checked, m=mid: self._mm.send_pair_forget(m))
            pair_grid.addWidget(cam_lbl,    row, 0)
            pair_grid.addWidget(mac_lbl,    row, 1)
            pair_grid.addWidget(forget_btn, row, 2)
            self._pair_mac_lbls[mid]    = mac_lbl
            self._pair_forget_btns[mid] = forget_btn
        pair_grid.setColumnStretch(1, 1)
        pair_vl.addLayout(pair_grid)
        form.addRow(pair_box)

        return w

    @staticmethod
    def _fmt_mac(mac: bytes) -> str:
        return ":".join(f"{b:02x}" for b in mac)

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

    def _open_find_limits(self, mount_id: int) -> None:
        from ui.dialogs.find_limits_dialog import FindLimitsDialog
        mc  = self._config.mount(mount_id)
        key = f"m{mount_id}"
        # Read from live spinbox widgets — _on_config_report() keeps these in sync
        # with whatever the mount last reported, so FindLimitsDialog gets the real
        # live thresholds rather than the possibly-stale JSON default.
        sg_sl = getattr(self, f"_{key}_sg_slider", None)
        sg_zm = getattr(self, f"_{key}_sg_zoom",   None)
        thresh_sl = sg_sl.value() if sg_sl is not None else mc.stall_threshold_slider
        thresh_zm = sg_zm.value() if sg_zm is not None else mc.stall_threshold_zoom
        dlg = FindLimitsDialog(mount_id, self._mm, self,
                               has_slider=mc.has_slider,
                               lanc_zoom=mc.lanc_zoom,
                               stall_threshold_slider=thresh_sl,
                               stall_threshold_zoom=thresh_zm)
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
        sl_box.setVisible(mc.has_slider)
        has_slider_cb.toggled.connect(sl_box.setVisible)
        layout.addWidget(sl_box)

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
        find_limits_btn = QPushButton("Find Limits…")
        find_limits_btn.setFixedHeight(36)
        mid_capture = mount_id
        find_limits_btn.clicked.connect(lambda: self._open_find_limits(mid_capture))
        limits_vl.addWidget(find_limits_btn)
        limits_box.setVisible(mc.has_slider)
        has_slider_cb.toggled.connect(limits_box.setVisible)
        layout.addWidget(limits_box)

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
        self._config.joystick_deadzone = self._deadzone_spin.value()

        self._config.virtual_keyboard  = self._osk_check.isChecked()
        from ui import virtual_keyboard
        virtual_keyboard.set_enabled(self._config.virtual_keyboard)  # live

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
            self._mm.send_set_orientation(mount_id, pan_inv, slider_inv,
                                          has_slider, zoom_inv, lanc_zoom,
                                          tilt_inv, look_at_mode)

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
