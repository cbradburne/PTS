"""
MountManager — central state and command routing for all 5 mounts.

Responsibilities:
  - Maintain live state (position, connection, motion state) per mount
  - Route outgoing packets through the Bridge
  - Parse incoming STATUS / LIMITS_FOUND / ACK / NACK packets
  - Emit Qt signals for UI updates
  - Heartbeat: ping all mounts every second; mark disconnected if no pong
  - Joystick override: cancel GOTO if joystick moves while mount is MOVING_TO_POS
"""
from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field as dc_field
from typing import Optional, Callable

from PyQt6.QtCore import QObject, pyqtSignal, QTimer

from .protocol import (
    Packet, Cmd, MountState, Axis, AxisGroup,
    decode_status, decode_limits_found, decode_home_complete,
    decode_ack, decode_nack, decode_pong,
    decode_state_report, decode_config_report,
    StatusPayload, LimitsFoundPayload, StateReportPayload, ConfigReportPayload,
    pkt_jog, pkt_goto, pkt_save_pos, pkt_set_speed_preset,
    pkt_set_limits, pkt_find_limits, pkt_find_home, pkt_set_orientation,
    pkt_e_stop, pkt_get_status, pkt_ping,
    pkt_get_state, pkt_store_pos, pkt_clear_pos,
    pkt_set_active_preset, pkt_save_speeds, pkt_goto_slot, pkt_move_rel,
    pkt_get_config, pkt_set_stall_threshold, pkt_cam_autofocus,
    MOUNT_BROADCAST, NUM_MOUNTS, NUM_SLOTS,
    # v2 — look-at tracking
    decode_subject_list, decode_look_at_status, decode_ref_confirmed,
    decode_calib_prompt, decode_position,
    SubjectRecord, LookAtStatusPayload, RefConfirmedPayload, CalibPrompt,
    pkt_get_subjects, pkt_add_subject_start, pkt_add_subject_set_a,
    pkt_add_subject_set_b, pkt_add_subject_abort, pkt_delete_subject,
    pkt_set_ref, pkt_set_slider_move, pkt_start_look_at_move,
    pkt_switch_subject,
    MAX_SUBJECTS,
    # pairing management (hub mount-table view / set / clear)
    pkt_get_mount_table, pkt_pair_decide, pkt_pair_forget,
    decode_mount_table, decode_mount_route, decode_pair_conflict, PairConflictPayload,
)



from .bridge import Bridge

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_MS  = 1000
HEARTBEAT_TIMEOUT_MS   = 3000   # mark disconnected after this

# Idle probe: if no ACK-tracked command has been sent for this long, send a
# read-only GET_CONFIG to each connected mount.  GET_CONFIG IS ack-tracked, so
# it exercises the PC→hub command path and lets the bridge detect (and recover
# from) a wedge even when the user isn't operating the system — instead of the
# wedge only surfacing the next time the user presses a button.
IDLE_PROBE_INTERVAL_S  = 3.0


@dataclass
class MountState_:
    """Live state for one mount."""
    mount_id:    int
    connected:   bool = False
    last_pong_ms: float = 0.0

    # Motion
    state:       MountState = MountState.IDLE
    flags:       int = 0

    # Hardware config (read from STATUS flags)
    has_slider:   bool = True
    look_at_mode: bool = False

    # Limits
    slider_min:  Optional[int] = None
    slider_max:  Optional[int] = None
    zoom_min:    Optional[int] = None
    zoom_max:    Optional[int] = None

    # Active speed presets (from STATUS extended fields / STATE_REPORT)
    active_pt_preset: int = 2
    active_sl_preset: int = 2

    # Position slot state (from STATUS bitmasks / STATE_REPORT)
    slot_occupied_mask: int = 0   # bits 0-9
    slot_at_mask:       int = 0   # bits 0-9
    target_slot:        int = 0xFF  # slot being moved to (0xFF = none)
    slots: list = dc_field(default_factory=lambda: [0] * NUM_SLOTS)

    # v2 — look-at tracking
    subjects:          list = dc_field(default_factory=lambda: [None] * MAX_SUBJECTS)
    look_at_status:    Optional[LookAtStatusPayload] = None
    active_subject_id: int = 0xFF   # currently tracked subject (0xFF = none)

    # Cached last CONFIG_REPORT — used to pre-populate the config dialog so
    # user edits are never overwritten by a late-arriving response.
    last_config_report: Optional[object] = None   # ConfigReportPayload

    # Live axis positions (CMD_POSITION — 5 Hz moving / 1 Hz at rest).
    # None until the first POSITION packet arrives from this mount.
    position: Optional[object] = None   # PositionPayload


class MountManager(QObject):
    """
    Signals emitted via direct cross-thread emission (Qt auto-connection queues
    connect them directly without cross-thread marshalling.
    """

    # mount_id (1-5)
    mount_status_updated  = pyqtSignal(int)       # new STATUS packet processed
    mount_connected       = pyqtSignal(int)
    mount_disconnected    = pyqtSignal(int)
    limits_found          = pyqtSignal(int, int, int, int)  # mount_id, axis, min, max
    home_complete         = pyqtSignal(int, int)            # mount_id, axis
    nack_received         = pyqtSignal(int, int, int)       # mount_id, seq, error
    state_report_received = pyqtSignal(int)        # mount_id — state fully synced
    config_report_received = pyqtSignal(int, object)  # mount_id, ConfigReportPayload

    # v2 — look-at tracking
    subject_list_received  = pyqtSignal(int)           # mount_id — subjects updated
    look_at_status_updated = pyqtSignal(int)           # mount_id — LookAtStatusPayload updated
    calib_prompt_received  = pyqtSignal(int, int)      # mount_id, CalibPrompt value
    ref_confirmed          = pyqtSignal(int, float, float)  # mount_id, pan_deg, tilt_deg
    la_move_dir_received   = pyqtSignal(int, int)      # mount_id, direction (0=◀, 1=▶, 0xFF=stopped)
    position_updated       = pyqtSignal(int, object)   # mount_id, PositionPayload

    # Pairing management (hub-owned mount table; nothing stored locally)
    mount_table_updated    = pyqtSignal(list)          # [5 × 6-byte MAC]; all-zero = unbound
    mount_route_updated    = pyqtSignal(list)          # [5 × int]; 0 = direct, N = via satellite N
    pair_conflict          = pyqtSignal(object)        # PairConflictPayload; cam 0 = dismiss

    def __init__(self, bridge: Bridge, parent=None):
        super().__init__(parent)
        self._bridge = bridge
        self._states: dict[int, MountState_] = {
            i: MountState_(mount_id=i) for i in range(1, NUM_MOUNTS + 1)
        }
        # Last mount table pushed by the hub (5 × 6-byte MAC; all-zero = unbound).
        # Mirror only — the hub is the source of truth.
        self._mount_table: list[bytes] = [b"\x00" * 6 for _ in range(NUM_MOUNTS)]
        # 0 = the hub reaches that cam directly, N = relayed by satellite N.
        self._mount_route: list[int] = [0] * NUM_MOUNTS

        bridge.on_packet(self._on_packet)
        self.destroyed.connect(lambda: bridge.off_packet(self._on_packet))

        # Heartbeat timer
        self._hb_timer = QTimer(self)
        self._hb_timer.setInterval(HEARTBEAT_INTERVAL_MS)
        self._hb_timer.timeout.connect(self._heartbeat)
        self._hb_timer.start()

    # ------------------------------------------------------------------
    # Public — read state
    # ------------------------------------------------------------------

    def state(self, mount_id: int) -> MountState_:
        return self._states[mount_id]

    def all_states(self) -> list[MountState_]:
        return list(self._states.values())

    # ------------------------------------------------------------------
    # Public — send commands
    # ------------------------------------------------------------------

    def send_jog(self, mount_id: int, pan: int, tilt: int,
                 slider: int, zoom: int,
                 pt_preset: int = 2, sz_preset: int = 2,
                 axis_mask: int = 0x0F) -> None:
        """Send a jog command.
        axis_mask 0x0F = all axes (default).
        axis_mask 0x03 = pan+tilt only — used in CV-tracking mode.
        """
        self._send(pkt_jog(mount_id, pan, tilt, slider, zoom,
                           pt_preset, sz_preset, axis_mask))

        # In look-at mode, any physical axis movement deselects the active
        # subject immediately so the UI doesn't wait for a Teensy round-trip.
        if pan != 0 or tilt != 0 or slider != 0:
            st = self._states.get(mount_id)
            if st and st.look_at_mode and st.look_at_status is not None:
                if st.look_at_status.subject_id != 0xFF:
                    st.look_at_status.subject_id = 0xFF
                    self.look_at_status_updated.emit(mount_id)

    def send_goto(self, mount_id: int, pan: int, tilt: int,
                  slider: int, zoom: int, speed_preset: int) -> None:
        self._send(pkt_goto(mount_id, pan, tilt, slider, zoom, speed_preset))

    def send_move_rel(self, mount_id: int, d_pan: int, d_tilt: int,
                      d_slider: int, d_zoom: int, speed_preset: int = 2) -> None:
        """Send a relative move — Teensy adds deltas to its current position."""
        self._send(pkt_move_rel(mount_id, d_pan, d_tilt, d_slider, d_zoom, speed_preset))

    def send_save_pos(self, mount_id: int, slot: int) -> None:
        self._send(pkt_save_pos(mount_id, slot))

    def send_set_speed_preset(self, mount_id: int, axis_group: AxisGroup,
                               preset: int, max_speed: int, accel: int) -> None:
        self._send(pkt_set_speed_preset(mount_id, axis_group, preset, max_speed, accel))

    def send_find_limits(self, mount_id: int, axis: Axis, stall_threshold: int = 80) -> None:
        self._send(pkt_find_limits(mount_id, axis, stall_threshold))

    def send_find_home(self, mount_id: int, axis: Axis, stall_threshold: int = 80) -> None:
        self._send(pkt_find_home(mount_id, axis, stall_threshold))

    # ---- Pairing management (hub owns the table; view / set / clear it) ----
    def request_mount_table(self) -> None:
        """Ask the hub to push its current pairing table (CMD_MOUNT_TABLE)."""
        self._send(pkt_get_mount_table())

    def send_pair_decide(self, cam: int, replace: bool, mac: bytes) -> None:
        """Resolve a conflict on cam 1-5: replace=True binds the new device to
        the slot (set); replace=False ignores the claim (keep current)."""
        self._send(pkt_pair_decide(cam, 1 if replace else 0, mac))

    def send_pair_forget(self, cam: int) -> None:
        """Clear (unbind) a camera's pairing on the hub."""
        self._send(pkt_pair_forget(cam))

    def mount_table(self) -> list[bytes]:
        """Last table the hub pushed (5 × 6-byte MAC; all-zero = unbound)."""
        return list(self._mount_table)

    @property
    def mount_route(self) -> list[int]:
        """How the hub currently reaches each mount: 0 = its own radio,
        N = relayed by satellite N.  The missing half of RSSI — that figure is
        measured wherever the frame arrived, so it says nothing about distance
        from the hub once satellites are in play."""
        return list(self._mount_route)

    def send_set_limits(self, mount_id: int, axis: Axis,
                        min_steps: int, max_steps: int) -> None:
        self._send(pkt_set_limits(mount_id, axis, min_steps, max_steps))

    def send_set_orientation(self, mount_id: int, pan_invert: bool,
                              slider_invert: bool, has_slider: bool = True,
                              zoom_invert: bool = False,
                              lanc_zoom: bool = False,
                              tilt_invert: bool = False,
                              look_at_mode: bool = False) -> None:
        self._send(pkt_set_orientation(mount_id, pan_invert, slider_invert,
                                       has_slider, zoom_invert, lanc_zoom,
                                       tilt_invert, look_at_mode))

    def send_e_stop(self, mount_id: int = MOUNT_BROADCAST) -> None:
        """E-STOP bypasses the send queue for minimum latency."""
        self._bridge.send_immediate(pkt_e_stop(mount_id))

    def send_get_status(self, mount_id: int) -> None:
        self._send(pkt_get_status(mount_id))

    def send_get_state(self, mount_id: int) -> None:
        self._send(pkt_get_state(mount_id))

    def send_store_pos(self, mount_id: int, slot: int) -> None:
        self._send(pkt_store_pos(mount_id, slot))

    def send_clear_pos(self, mount_id: int, slot: int) -> None:
        self._send(pkt_clear_pos(mount_id, slot))

    def send_goto_slot(self, mount_id: int, slot: int, pt_preset: int = 2,
                       sl_preset: int = 2, axis_mask: int = 0x0F) -> None:
        """Recall a stored position by slot index.
        pt_preset / sl_preset: speed presets for pan/tilt and slider/zoom respectively,
          so each axis group moves at its own configured speed rather than sharing one.
        axis_mask 0x0F = all axes (default).
        axis_mask 0x04 = slider only — CV-tracking mode, leaves pan/tilt under CV control.
        """
        self._send(pkt_goto_slot(mount_id, slot, pt_preset, sl_preset, axis_mask))

    def send_set_active_preset(self, mount_id: int, axis_group: AxisGroup,
                                preset: int) -> None:
        self._send(pkt_set_active_preset(mount_id, axis_group, preset))

    def send_get_config(self, mount_id: int) -> None:
        """Request speed presets and orientation from a mount."""
        self._send(pkt_get_config(mount_id))

    def send_cam_autofocus(self, mount_id: int) -> None:
        """Instantaneous autofocus on that mount's Blackmagic camera.

        Fire-and-forget: the mount ACKs receiving the command, but there is no
        acknowledgement from the CAMERA — the Blackmagic control protocol has
        none.  Whether the camera link is up at all is in CMD_HEALTH, logged as
        "BLE PAIRED", so a camera that is off does not look like a dead mount.
        """
        self._send(pkt_cam_autofocus(mount_id))

    def send_set_stall_threshold(self, mount_id: int, axis: Axis,
                                  threshold: int) -> None:
        """Set and persist a StallGuard threshold on the mount."""
        self._send(pkt_set_stall_threshold(mount_id, axis, threshold))

    def send_save_speeds(self, mount_id: int,
                         pt_presets: list[tuple[int, int]],
                         sl_presets: list[tuple[int, int]],
                         zm_preset:  tuple[int, int]) -> None:
        self._send(pkt_save_speeds(mount_id, pt_presets, sl_presets, zm_preset))

    # ------------------------------------------------------------------
    # Public — v2 look-at / subject commands
    # ------------------------------------------------------------------

    def send_get_subjects(self, mount_id: int) -> None:
        """Request the full subject list from a mount."""
        self._send(pkt_get_subjects(mount_id))

    def send_add_subject_start(self, mount_id: int, subject_id: int, name: str) -> None:
        """Begin a 2-point subject calibration (moves slider to home)."""
        self._send(pkt_add_subject_start(mount_id, subject_id, name))

    def send_add_subject_set_a(self, mount_id: int) -> None:
        """Record observation A (at slider home) and move to slider max."""
        self._send(pkt_add_subject_set_a(mount_id))

    def send_add_subject_set_b(self, mount_id: int) -> None:
        """Record observation B (at slider max) and solve subject 3D position."""
        self._send(pkt_add_subject_set_b(mount_id))

    def send_add_subject_abort(self, mount_id: int) -> None:
        """Abort an in-progress subject calibration."""
        self._send(pkt_add_subject_abort(mount_id))

    def send_delete_subject(self, mount_id: int, subject_id: int) -> None:
        """Delete a subject by ID (0-7) from the mount's EEPROM."""
        self._send(pkt_delete_subject(mount_id, subject_id))

    def send_set_ref(self, mount_id: int, subject_id: int = 0xFF) -> None:
        """Set the angular reference frame.
        subject_id 0xFF → manual reference (pan=0°, tilt=0° at current position).
        subject_id 0-7  → compute reference from geometry using known subject.
        """
        self._send(pkt_set_ref(mount_id, subject_id))

    def send_set_slider_move(self, mount_id: int,
                              start_mm: float, end_mm: float,
                              speed_preset: int = 2) -> None:
        """Define the slider traversal for the next look-at move (stored in EEPROM)."""
        self._send(pkt_set_slider_move(mount_id, start_mm, end_mm, speed_preset))

    def send_start_look_at_move(self, mount_id: int, subject_id: int,
                                 direction: int, speed_preset: int = 2) -> None:
        """Start a look-at move toward the min (direction=0) or max (direction=1) slider limit."""
        self._send(pkt_start_look_at_move(mount_id, subject_id, direction, speed_preset))

    def send_switch_subject(self, mount_id: int, subject_id: int) -> None:
        """Switch to a different subject mid look-at move."""
        self._send(pkt_switch_subject(mount_id, subject_id))

    # ------------------------------------------------------------------
    # Incoming packet handler  (called from bridge RX thread)
    # ------------------------------------------------------------------

    def _on_packet(self, pkt: Packet) -> None:
        # Guard against packets arriving after the Qt C++ object has been
        # destroyed during app shutdown (bridge RX thread outlives the QObject).
        try:
            self._states  # cheap attribute access — raises RuntimeError if deleted
        except RuntimeError:
            return

        # Hub-level pairing packets carry the hub sentinel (0xFE), so they must
        # be handled BEFORE the 1-5 mount guard below.
        if pkt.cmd == Cmd.MOUNT_TABLE:
            try:
                self._mount_table = decode_mount_table(pkt.payload)
                self.mount_table_updated.emit(list(self._mount_table))
            except Exception as e:
                log.error(f"MOUNT_TABLE decode failed: {e}")
            return
        if pkt.cmd == Cmd.MOUNT_ROUTE:
            try:
                route = decode_mount_route(pkt.payload)
                changed = (route != self._mount_route)
                self._mount_route = route
                if changed:
                    self.mount_route_updated.emit(list(route))
            except Exception as e:
                log.error(f"MOUNT_ROUTE decode failed: {e}")
            return
        if pkt.cmd == Cmd.PAIR_CONFLICT:
            try:
                self.pair_conflict.emit(decode_pair_conflict(pkt.payload))
            except Exception as e:
                log.error(f"PAIR_CONFLICT decode failed: {e}")
            return

        mid = pkt.mount_id
        if mid < 1 or mid > NUM_MOUNTS:
            return

        st = self._states[mid]

        if pkt.cmd == Cmd.STATUS:
            try:
                s = decode_status(pkt.payload)
                was_connected = st.connected
                st.connected   = True
                st.last_pong_ms = time.monotonic() * 1000
                st.state       = s.state
                st.flags       = s.flags
                st.has_slider  = s.has_slider
                st.look_at_mode = s.look_at_mode

                # REMOVED: Assignment of pan, tilt, slider, zoom 
                # as they are no longer in the decoded StatusPayload.

                # Extended STATUS fields (these are still present!)
                st.active_pt_preset   = s.active_pt_preset
                st.active_sl_preset   = s.active_sl_preset
                st.slot_occupied_mask = s.slot_occupied_mask
                st.slot_at_mask       = s.slot_at_mask
                st.target_slot        = s.target_slot
                # Only when this STATUS actually carried byte [9].  A 9-byte
                # STATUS says nothing about the look-at subject, so leave the
                # last known value alone rather than clobbering it with "none".
                if s.la_subject_present:
                    st.active_subject_id = s.active_la_subject  # 0-7 or 0xFF (none)

                if not was_connected:
                    self.mount_connected.emit(mid)
                    # Request full state so slot coordinates are populated via STATE_REPORT
                    self._send(pkt_get_state(mid))
                    # Request subject list for mounts in look-at mode so the UI
                    # shows which subject slots are already stored on the Teensy.
                    if st.has_slider and st.look_at_mode:
                        self._send(pkt_get_subjects(mid))
                self.mount_status_updated.emit(mid)
            except Exception as e:
                print(f"!!! CRITICAL ERROR: Failed to decode STATUS from {mid}: {e}")
                log.warning(f"Bad STATUS from mount {mid}: {e}")

        elif pkt.cmd == Cmd.LIMITS_FOUND:
            try:
                lf = decode_limits_found(pkt.payload)
                if lf.axis == Axis.SLIDER:
                    st.slider_min = lf.min_steps
                    st.slider_max = lf.max_steps
                elif lf.axis == Axis.ZOOM:
                    st.zoom_min = lf.min_steps
                    st.zoom_max = lf.max_steps
                self.limits_found.emit(mid, int(lf.axis), lf.min_steps, lf.max_steps)
            except Exception as e:
                log.warning(f"Bad LIMITS_FOUND from mount {mid}: {e}")

        elif pkt.cmd == Cmd.HOME_COMPLETE:
            try:
                axis = decode_home_complete(pkt.payload)
                self.home_complete.emit(mid, int(axis))
            except Exception as e:
                log.warning(f"Bad HOME_COMPLETE from mount {mid}: {e}")

        elif pkt.cmd == Cmd.STATE_REPORT:
            try:
                sr = decode_state_report(pkt.payload)
                # Lightweight 6-byte STATE_REPORT has slots=[None]*10 —
                # preserve existing slot coordinates rather than overwriting.
                if any(s is not None for s in sr.slots):
                    st.slots = list(sr.slots)
                st.slot_occupied_mask = sr.slot_occupied_mask
                st.slot_at_mask       = sr.slot_at_mask
                st.active_pt_preset   = sr.active_pt_preset
                st.active_sl_preset   = sr.active_sl_preset
                if sr.slider_min_steps != 0 or sr.slider_max_steps != 0:
                    st.slider_min = sr.slider_min_steps
                    st.slider_max = sr.slider_max_steps
                if sr.zoom_min_steps != 0 or sr.zoom_max_steps != 0:
                    st.zoom_min = sr.zoom_min_steps
                    st.zoom_max = sr.zoom_max_steps
                self.state_report_received.emit(mid)
            except Exception as e:
                log.warning(f"Bad STATE_REPORT from mount {mid}: {e}")

        elif pkt.cmd == Cmd.CONFIG_REPORT:
            try:
                cr = decode_config_report(pkt.payload)
                st.look_at_mode = cr.look_at_mode
                st.last_config_report = cr   # cache for config dialog pre-population
                self.config_report_received.emit(mid, cr)
            except Exception as e:
                log.warning(f"Bad CONFIG_REPORT from mount {mid}: {e}")

        # ── v2 look-at packets ──────────────────────────────────────────
        elif pkt.cmd == Cmd.SUBJECT_LIST:
            try:
                subjects = decode_subject_list(pkt.payload)
                st.subjects = subjects
                self.subject_list_received.emit(mid)
            except Exception as e:
                log.warning(f"Bad SUBJECT_LIST from mount {mid}: {e}")

        elif pkt.cmd == Cmd.LOOK_AT_STATUS:
            try:
                la = decode_look_at_status(pkt.payload)
                st.look_at_status = la
                st.active_subject_id = la.subject_id
                self.look_at_status_updated.emit(mid)
            except Exception as e:
                log.warning(f"Bad LOOK_AT_STATUS from mount {mid}: {e}")

        elif pkt.cmd == Cmd.REF_CONFIRMED:
            try:
                rc = decode_ref_confirmed(pkt.payload)
                self.ref_confirmed.emit(mid, rc.pan_ref_deg, rc.tilt_ref_deg)
            except Exception as e:
                log.warning(f"Bad REF_CONFIRMED from mount {mid}: {e}")

        elif pkt.cmd == Cmd.CALIB_PROMPT:
            try:
                prompt = decode_calib_prompt(pkt.payload)
                self.calib_prompt_received.emit(mid, int(prompt))
            except Exception as e:
                log.warning(f"Bad CALIB_PROMPT from mount {mid}: {e}")

        elif pkt.cmd == Cmd.LA_MOVE_DIR:
            # Hub-injected packet: direction byte 0=◀(min), 1=▶(max), 0xFF=stopped
            if pkt.payload:
                self.la_move_dir_received.emit(mid, pkt.payload[0])

        elif pkt.cmd == Cmd.POSITION:
            try:
                pos = decode_position(pkt.payload)
                st.position = pos
                self.position_updated.emit(mid, pos)
            except Exception as e:
                log.warning(f"Bad POSITION from mount {mid}: {e}")

        elif pkt.cmd == Cmd.PONG:
            st.connected    = True
            st.last_pong_ms = time.monotonic() * 1000
            self._send(pkt_get_status(mid)) 

        elif pkt.cmd == Cmd.NACK:
            try:
                n = decode_nack(pkt.payload)
                log.warning(f"NACK from mount {mid}: seq={n.nacked_seq} err={n.error}")
                self.nack_received.emit(mid, n.nacked_seq, int(n.error))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def _heartbeat(self) -> None:
        if not self._bridge.connected:
            return

        self._send(pkt_ping(MOUNT_BROADCAST))

        # Idle probe — keep the OUT pipe exercised so a wedge is detected while
        # the system sits unattended, not only when the user next acts.  Only
        # fires when no tracked command has gone out recently, so it adds nothing
        # during active operation.  GET_CONFIG is read-only and ack-tracked.
        if self._bridge.seconds_since_tracked_tx() >= IDLE_PROBE_INTERVAL_S:
            for mid, st in self._states.items():
                if st.connected:
                    self.send_get_config(mid)

        now_ms = time.monotonic() * 1000
        for mid, st in self._states.items():
            if st.connected and (now_ms - st.last_pong_ms) > HEARTBEAT_TIMEOUT_MS:
                st.connected = False
                self.mount_disconnected.emit(mid)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _send(self, data: bytes) -> None:
        if self._bridge.connected:
            self._bridge.send(data)
