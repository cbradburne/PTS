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
    decode_ack, decode_nack, decode_pong, NackError,
    decode_state_report, decode_config_report,
    StatusPayload, LimitsFoundPayload, StateReportPayload, ConfigReportPayload,
    pkt_jog, pkt_goto, pkt_save_pos, pkt_set_speed_preset,
    pkt_set_limits, pkt_find_limits, pkt_find_home, pkt_set_orientation,
    pkt_e_stop, pkt_get_status, pkt_ping,
    pkt_get_state, pkt_store_pos, pkt_clear_pos,
    pkt_set_active_preset, pkt_save_speeds, pkt_goto_slot, pkt_move_rel,
    pkt_get_config, pkt_set_stall_threshold, pkt_cam_autofocus, pkt_get_position,
    pkt_cam_iso, pkt_cam_white_balance, decode_cam_status,
    pkt_cam_lift, pkt_cam_gamma, pkt_cam_gain, pkt_cam_offset,
    pkt_cam_contrast, pkt_cam_luma_mix, pkt_cam_hue_sat, pkt_cam_cc_reset,
    pkt_cam_focus, pkt_cam_iris, pkt_cam_auto_iris, pkt_cam_zoom_norm,
    pkt_cam_zoom_speed, pkt_cam_shutter_speed, pkt_cam_transport, pkt_cam_tally, pkt_cam_tally_front, pkt_cam_tally_rear,
    describe_cam_param, TRANSPORT_RECORD, TRANSPORT_PREVIEW, CAM_MEASUREMENTS,
    pkt_cam_shutter_angle, pkt_cam_nd, pkt_cam_auto_wb, pkt_cam_restore_auto_wb,
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
    decode_mount_table, decode_mount_route, decode_sat_names,
    MOUNT_EVENT_PAYLOAD_LEN, MOUNT_EVENT_ISOLATED, MOUNT_EVENT_TX_WEDGE,
    MOUNT_EVENT_TX_WEDGE_REBOOT,
    RF_REPORT_PAYLOAD_LEN,
    decode_pair_conflict, PairConflictPayload,
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

# TEMPORARY — look-at motion diagnostic.
#
# Switching subject mid-move reads as abrupt, and two attempts to fix it were
# reasoned from the firmware rather than measured: the setpoint blend, then the
# slew-cap timing.  Both were sound about what the code does and neither changed
# what the operator sees, which is the point at which guessing stops being
# useful.
#
# There is no position data anywhere in the system — CMD_POSITION is answered on
# request and nothing asks (see docs/link_traffic.md).  This asks, at 5 Hz, ONLY
# while a mount is actually in a look-at state, and logs pan/tilt with the
# angular velocity between samples.  Velocity is the thing being complained
# about; position alone would need reading off with a ruler.
#
# Cost: 5 requests + 5 replies per second, for the few seconds a switch lasts,
# on a link that is otherwise one packet per 5 s at rest.  Bounded, and only
# while the thing under study is happening.
#
# REMOVE once the profile has been read.  Set False to silence without deleting.
LOOK_AT_DIAGNOSTIC     = True
LOOK_AT_POLL_MS        = 200

# Camera-parameter logging: how many changes one parameter may report in a
# minute before it is silenced as a runaway.  Generous, because a setting
# being actively adjusted legitimately changes often and that is exactly when
# the log is worth having; the battery, which prompted the limit, is
# suppressed outright by CAM_MEASUREMENTS instead.
_CAM_CHANGE_BUDGET   = 40
_CAM_CHANGE_WINDOW_S = 60.0


# esp_now_send() refusals, named.  ESP_ERR_ESPNOW_BASE is ESP_ERR_WIFI_BASE
# (0x3000) + 100, and these are the codes the mount can actually hit; anything
# else prints as hex rather than being guessed at.  Named here because the whole
# point of the exercise is a log line an operator can read at the rig — a bare
# 0x3067 is the same dead end as no number at all.
_ESPNOW_ERRS = {
    0x3065: "ESP-NOW not initialised",
    0x3066: "invalid argument",
    0x3067: "OUT OF MEMORY — the stack's TX queue was full",
    0x3068: "peer list full",
    0x3069: "peer not found — the base it was sending to had gone",
    0x306A: "internal error",
    0x306B: "peer already exists",
    0x306C: "wifi interface error",
    0x306D: "wrong channel",
}


def _espnow_err_text(refused: int, err: int) -> str:
    """Describe the synchronous send refusals leading up to a restart.

    These are the sends esp_now_send() rejected outright rather than queued.
    They never reach the send callback, so they move neither txfail nor the
    last-good-TX clock: the mount goes quiet with every counter frozen, which
    is precisely how mount 5 looked on 2026-08-11 — txfail pinned at 70 for the
    whole three-minute outage.  A zero here is therefore a real answer, not a
    missing one: it rules the refusal path out and points back at the radio.
    """
    if not refused:
        return "the stack accepted every send (0 refused) — TX stopped below that"
    name = _ESPNOW_ERRS.get(err, f"unknown error 0x{err:04X}")
    return f"{refused} sends REFUSED by the stack, last: {name}"


@dataclass
class MountState_:
    """Live state for one mount."""
    mount_id:    int
    connected:   bool = False
    last_pong_ms: float = 0.0
    # Heard-but-not-obeying.  Presence and RESPONSIVENESS are different things,
    # and conflating them cost a whole session: "any packet proves it is alive"
    # fixed cameras loading greyed out, and in doing so pinned mount 4 to
    # "connected" while 79% of its commands went unacknowledged — its health
    # packets still trickling through were enough to keep it looking perfect.
    # The rule it replaced flapped the mount on and off every few seconds, and
    # that flapping was HONEST.  A mount that shows offline is annoying; one
    # that shows connected and ignores four commands in five is something you
    # find out about mid-shot.
    unacked:      int = 0
    unresponsive: bool = False

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
    # Blackmagic camera, as REPORTED by the camera — never what we last sent.
    # None until the camera says so, which is what lets the UI grey a control
    # rather than invent a starting value and drift from the real one.
    # Recording, as the camera reports it (media category, transport mode).
    # None until it has said so — greying a Record button is honest, and a
    # tally that guesses is worse than no tally.
    cam_recording: Optional[bool] = None
    cam_iso:  Optional[int] = None
    cam_wb:   Optional[int] = None
    cam_tint: Optional[int] = None
    # Everything else the camera reports, by name, for the advanced panel.  A
    # dict rather than named fields because the camera volunteers whatever it
    # feels like and the set grows: a parameter this app does not yet drive
    # still arrives, and is still worth having when someone comes looking.
    cam_adv: dict = dc_field(default_factory=dict)
    # What this app last SENT the camera, by the same names.  Needed because the
    # camera reports only a handful of the parameters the advanced panel drives
    # — iris, ISO and white balance come back; shutter, ND and the whole of
    # colour correction never do.  Without this the panel has no way to show a
    # setting it made itself once its dialog has been closed, and every one of
    # those controls reads neutral on reopen however the camera is actually set.
    # The more RECENT of the two accounts wins; see cam_known().
    cam_sent: dict = dc_field(default_factory=dict)
    # When each parameter was last sent, and last heard from the camera.  Needed
    # because "the camera always wins" is wrong once the camera has gone quiet:
    # a report from an hour ago would otherwise override a setting made a second
    # ago, for ever, and the control would spring back every time the panel
    # refreshed.  A camera that has genuinely changed since reports again, and
    # then it is the newer of the two and wins on its own merit.
    cam_sent_at:  dict = dc_field(default_factory=dict)
    cam_heard_at: dict = dc_field(default_factory=dict)

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
    cam_status_received    = pyqtSignal(int)           # mount_id — camera settings updated
    # Fired only on a CHANGE of transport mode, so anything downstream — the
    # Record button, the OSC tally — reacts to the camera starting or stopping,
    # not to it repeating itself every few seconds.
    cam_recording_changed  = pyqtSignal(int, bool)     # mount_id, recording
    look_at_status_updated = pyqtSignal(int)           # mount_id — LookAtStatusPayload updated
    calib_prompt_received  = pyqtSignal(int, int)      # mount_id, CalibPrompt value
    ref_confirmed          = pyqtSignal(int, float, float)  # mount_id, pan_deg, tilt_deg
    la_move_dir_received   = pyqtSignal(int, int)      # mount_id, direction (0=◀, 1=▶, 0xFF=stopped)
    position_updated       = pyqtSignal(int, object)   # mount_id, PositionPayload

    # Pairing management (hub-owned mount table; nothing stored locally)
    mount_table_updated    = pyqtSignal(list)          # [5 × 6-byte MAC]; all-zero = unbound
    mount_route_updated    = pyqtSignal(list)          # [5 × int]; 0 = direct, N = via satellite N
    sat_names_updated      = pyqtSignal(dict)          # {slot: name} for slots that gave one
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
        self._route_logged = False
        self._asked_for_table = False
        self._sat_names: dict[int, str] = {}
        # (mount, category, parameter) -> the value last logged for it.
        self._cam_params_seen: dict[tuple[int, int, int], object] = {}
        # key -> (window start, changes this window, gone quiet)
        self._cam_param_rate: dict[tuple[int, int, int], tuple] = {}

        bridge.on_packet(self._on_packet)
        self.destroyed.connect(lambda: bridge.off_packet(self._on_packet))

        # Heartbeat timer
        self._hb_timer = QTimer(self)
        self._hb_timer.setInterval(HEARTBEAT_INTERVAL_MS)
        self._hb_timer.timeout.connect(self._heartbeat)
        self._hb_timer.start()

        # TEMPORARY — see LOOK_AT_DIAGNOSTIC.  Separate from the heartbeat
        # because 1 Hz cannot resolve a 1.6 s move, which is the whole reason
        # the previous position logging could not answer this.
        if LOOK_AT_DIAGNOSTIC:
            self._la_poll_timer = QTimer(self)
            self._la_poll_timer.setInterval(LOOK_AT_POLL_MS)
            self._la_poll_timer.timeout.connect(self._poll_look_at_positions)
            self._la_poll_timer.start()
        self._la_last: dict[int, tuple] = {}   # mount -> (t, pan, tilt)
        self._la_prev_v: dict[int, tuple] = {}  # mount -> (v_pan, v_tilt)
        self._la_series: dict[int, list] = {}   # mount -> [(v_pan, v_tilt), ...]
        self._la_moving: dict[int, bool] = {}
        self._la_quiet:  dict[int, int]  = {}

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

    def _log_route(self) -> None:
        """Say which base every mount is on, using the best labels we have yet.

        Which base a mount is on had only ever existed in the UI, which left
        comms.log unable to tell a mount whose signal is DEGRADING from one
        that is ALTERNATING between two bases — identical in the rssi column,
        and opposite in what they need doing about them.
        """
        self._route_logged = True
        log.info("MOUNT ROUTE: %s", ", ".join(
            "cam%d %s" % (i + 1, "direct" if s == 0 else "via " + self.sat_label(s))
            for i, s in enumerate(self._mount_route)))

    def sat_label(self, slot: int) -> str:
        """How to refer to satellite `slot` (1-based, as mount_route reports).

        The name if the satellite gave one, otherwise "SAT N".  The fallback is
        not cosmetic: a satellite running firmware from before names existed
        never introduces itself, and a blank label would read as a bug in the
        route rather than an out-of-date box.
        """
        return self._sat_names.get(slot) or f"SAT {slot}"

    def send_set_limits(self, mount_id: int, axis: Axis,
                        min_steps: int, max_steps: int) -> None:
        self._send(pkt_set_limits(mount_id, axis, min_steps, max_steps))

    def send_set_orientation(self, mount_id: int, pan_invert: bool,
                              slider_invert: bool, has_slider: bool = True,
                              zoom_invert: bool = False,
                              lanc_zoom: bool = False,
                              tilt_invert: bool = False,
                              look_at_mode: bool = False,
                              slider_tilt_deg: float = 0.0) -> None:
        self._send(pkt_set_orientation(mount_id, pan_invert, slider_invert,
                                       has_slider, zoom_invert, lanc_zoom,
                                       tilt_invert, look_at_mode,
                                       slider_tilt_deg))

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

    # Consecutive unacknowledged ack-tracked commands before a mount is called
    # unresponsive.  Three, against a ~4 s idle probe, means ~12 s of being told
    # things and doing none of them — long enough not to trip on one lost frame,
    # short enough to notice before an operator reaches for the joystick.
    UNACKED_LIMIT = 3

    @staticmethod
    def _subject_key(s):
        """Identity of a solved subject: its name and where it solved to."""
        return (s.name, round(s.x_mm), round(s.y_mm), round(s.z_mm))

    def _log_solved_subjects(self, mid: int, prev: list, now: list) -> None:
        """Say where a subject solved, when that changes.

        The mount's own account of a solve goes to its USB serial, which nothing
        reads at the rig, so a calibration that landed 5 m in front and one that
        landed behind the rail looked identical from here.  SUBJECT_LIST is
        polled every few seconds, hence "on change" rather than on arrival.

        `prev` may still be the initial [None] * MAX_SUBJECTS — a mount that has
        not sent a list yet — so entries are checked before use rather than
        assumed to be records.
        """
        try:
            was = {self._subject_key(s) for s in (prev or []) if s and s.valid}
            for s in now:
                if s and s.valid and self._subject_key(s) not in was:
                    log.info(f"SUBJECT cam{mid}: '{s.name}' solved at "
                             f"x={s.x_mm:.0f} y={s.y_mm:.0f} z={s.z_mm:.0f} mm "
                             f"(z is distance in front of the rail)")
        except Exception as e:
            # Never let a log line cost the caller anything.  Reported as this
            # app's fault, which is what it would be.
            log.warning("SUBJECT logging failed for mount %d: %s", mid, e)

    def _set_mount_online(self, mount_id: int, *,
                          connected: bool | None = None,
                          unresponsive: bool | None = None) -> bool:
        """Update presence, emitting only when the EFFECTIVE state changes.

        A mount is online to the operator when it is both heard (connected) and
        acting on what it is told (not unresponsive).  Those two flags were
        written at six separate sites, each deciding for itself whether to
        signal — and one of them set connected = True while skipping the signal
        whenever unresponsive happened to be set.  From that point the mount was
        online internally and greyed on screen, and neither "if not st.connected"
        nor "if not was_connected" could ever fire again to correct it, because
        both had just been satisfied.  A genuinely active mount stayed greyed
        for the rest of the session — a different one each launch, depending on
        which mount's first packet landed inside that window.

        Note `connected` still means HEARD.  The idle probe deliberately keeps
        polling a heard-but-unresponsive mount, because an ACK is the only thing
        that clears unresponsive.

        Returns True if the effective state changed.
        """
        st = self._states.get(mount_id)
        if st is None:
            return False
        before = st.connected and not st.unresponsive
        if connected is not None:
            st.connected = connected
        if unresponsive is not None:
            st.unresponsive = unresponsive
        after = st.connected and not st.unresponsive
        if after == before:
            return False
        (self.mount_connected if after else self.mount_disconnected).emit(mount_id)
        return True

    def resync_presence(self) -> None:
        """Re-announce every mount's presence as it currently stands.

        Presence reaches the UI as transitions, and the UI subscribes to those
        signals AFTER the bridge is already connected — MainWindow.__init__
        connects the bridge around 230 lines before it connects these signals.
        A mount whose first packet lands inside that window has its transition
        emitted into a void, and since presence only changes on a transition,
        nothing afterwards corrects it: the row stays greyed for the whole
        session with the mount plainly working and its data arriving.

        This makes the UI's view a function of the current state rather than of
        an event it had to be present to hear.  Call it once the signals are
        wired, and on reconnect.
        """
        for mid, st in self._states.items():
            online = st.connected and not st.unresponsive
            (self.mount_connected if online else self.mount_disconnected).emit(mid)

    def _note_tracked_send(self, mount_id: int) -> None:
        st = self._states.get(mount_id)
        if not st or not st.connected:
            return
        st.unacked += 1
        if st.unacked >= self.UNACKED_LIMIT and not st.unresponsive:
            log.warning("Mount %d is HEARD BUT NOT RESPONDING — %d commands "
                        "unacknowledged. It will keep reporting health, so it "
                        "looks connected; it is not accepting commands.",
                        mount_id, st.unacked)
            self._set_mount_online(mount_id, unresponsive=True)

    def send_get_config(self, mount_id: int) -> None:
        """Request speed presets and orientation from a mount."""
        self._send(pkt_get_config(mount_id))
        self._note_tracked_send(mount_id)

    def send_cam_autofocus(self, mount_id: int) -> None:
        """Instantaneous autofocus on that mount's Blackmagic camera.

        Fire-and-forget: the mount ACKs receiving the command, but there is no
        acknowledgement from the CAMERA — the Blackmagic control protocol has
        none.  Whether the camera link is up at all is in CMD_HEALTH, logged as
        "BLE PAIRED", so a camera that is off does not look like a dead mount.
        """
        self._send(pkt_cam_autofocus(mount_id))

    def _log_cam_param(self, mount_id: int, raw: bytes, upd: dict) -> None:
        """Name every distinct camera parameter this mount reports, once each.

        Gain and white balance have now gone missing twice, and both times the
        question "is the camera even reporting them?" was answered by reasoning
        rather than by looking — wrongly, both times.  The frames are already
        arriving here, so the answer costs one dict and one log line per
        parameter, and lands in comms.log where it can actually be read: the
        mount's serial port is inside the enclosure on a rig.

        Logged when the VALUE changes, not once per parameter.  It was once per
        parameter, and that turned this instrument into a third way of being
        misled: asked "did the camera report ISO while gain was being changed?"
        the log said nothing, so ISO was diagnosed twice as an app-side fault
        when the camera had in fact been reporting every step.  A parameter
        re-reported unchanged still stays quiet, which is all the flood control
        the 5 s replay needed.
        """
        if len(raw) < 6:
            return
        key = (mount_id, raw[4], raw[5])
        value = describe_cam_param(raw)
        first = key not in self._cam_params_seen
        if not first and self._cam_params_seen[key] == value:
            return
        self._cam_params_seen[key] = value
        if not first and (raw[4], raw[5]) in CAM_MEASUREMENTS:
            return          # recorded once when first seen; its drift is noise

        # Logging on change is right for a setting and useless for a MEASUREMENT.
        # The battery voltage ticks by a millivolt every few seconds, and on its
        # own it was 215 of 328 lines — two thirds of the log, burying the ISO
        # and aperture changes it exists to show.  A parameter is allowed a few
        # changes a minute; past that it says so once and goes quiet until the
        # minute rolls, so a chatty measurement cannot drown a real event.
        now = time.monotonic()
        start, count, quiet = self._cam_param_rate.get(key, (now, 0, False))
        if now - start >= _CAM_CHANGE_WINDOW_S:
            start, count, quiet = now, 0, False
        count += 1
        if count > _CAM_CHANGE_BUDGET:
            self._cam_param_rate[key] = (start, count, True)
            if not quiet:
                log.info("CAM PARAM cam%d category=%d parameter=%d — changing "
                         "continuously (%s); further changes quiet for %ds",
                         mount_id, raw[4], raw[5], value, int(_CAM_CHANGE_WINDOW_S))
            return
        self._cam_param_rate[key] = (start, count, quiet)
        log.info("CAM PARAM cam%d category=%d parameter=%d len=%d — %s%s",
                 mount_id, raw[4], raw[5], len(raw), describe_cam_param(raw),
                 "" if first else "   (changed)")

    def send_cam_iso(self, mount_id: int, iso: int) -> None:
        self._note_cam(mount_id, "iso", int(iso))
        self._send(pkt_cam_iso(mount_id, iso))

    def send_cam_white_balance(self, mount_id: int, kelvin: int,
                               tint: int | None = None) -> None:
        """Temperature, and tint if the caller has one.

        Tint is carried in the same command as the temperature, so it has to be
        sent either way. Callers with no tint control (the everyday dialog) omit
        it and the camera's last reported tint is resent unchanged rather than
        zeroed; the Advanced panel owns a tint box and passes it explicitly.
        """
        if tint is None:
            st = self._states.get(mount_id)
            tint = st.cam_tint if st and st.cam_tint is not None else 0
        self._note_cam(mount_id, "white_balance", int(kelvin))
        self._note_cam(mount_id, "tint", int(tint))
        self._send(pkt_cam_white_balance(mount_id, kelvin, int(tint)))

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

    # ── Blackmagic camera control, full surface ──────────────────────────
    # Fire-and-forget, every one of them.  The protocol has no acknowledgement,
    # so there is nothing to await and nothing to retry against — see the note
    # at the top of camera_advanced_dialog.py.
    # Axis bits in PositionPayload.moving_mask.
    _AX_PAN, _AX_TILT, _AX_SLIDER, _AX_ZOOM = 1, 2, 4, 8

    def _note_cam(self, m: int, key: str, value) -> None:
        """Remember a value we sent, under the name the camera would report it by.

        Same names as decode_cam_status produces, so cam_known() can merge the
        two dicts and let anything the camera actually said win.
        """
        st = self._states.get(m)
        if st is not None:
            st.cam_sent[key] = value
            st.cam_sent_at[key] = time.monotonic()

    def send_cam_lift(self, m, r, g, b, y):
        self._note_cam(m, "lift", (r, g, b, y));    self._send(pkt_cam_lift(m, r, g, b, y))

    def send_cam_gamma(self, m, r, g, b, y):
        self._note_cam(m, "gamma", (r, g, b, y));   self._send(pkt_cam_gamma(m, r, g, b, y))

    def send_cam_gain_cc(self, m, r, g, b, y):
        self._note_cam(m, "gain_cc", (r, g, b, y)); self._send(pkt_cam_gain(m, r, g, b, y))

    def send_cam_offset(self, m, r, g, b, y):
        self._note_cam(m, "offset", (r, g, b, y));  self._send(pkt_cam_offset(m, r, g, b, y))

    def send_cam_contrast(self, m, pivot, adj):
        self._note_cam(m, "contrast", (pivot, adj)); self._send(pkt_cam_contrast(m, pivot, adj))

    def send_cam_luma_mix(self, m, mix):
        self._note_cam(m, "luma_mix", mix);         self._send(pkt_cam_luma_mix(m, mix))

    def send_cam_hue_sat(self, m, hue, sat):
        self._note_cam(m, "hue_sat", (hue, sat));   self._send(pkt_cam_hue_sat(m, hue, sat))

    def send_cam_focus(self, m, pos):
        self._note_cam(m, "focus", pos);            self._send(pkt_cam_focus(m, pos))

    def send_cam_iris(self, m, norm):
        self._note_cam(m, "iris", norm);            self._send(pkt_cam_iris(m, norm))

    def send_cam_zoom_norm(self, m, norm):
        self._note_cam(m, "zoom", norm);            self._send(pkt_cam_zoom_norm(m, norm))

    def send_cam_shutter_speed(self, m, den):
        self._note_cam(m, "shutter_speed", den);    self._send(pkt_cam_shutter_speed(m, den))

    def send_cam_shutter_angle(self, m, deg):
        self._note_cam(m, "shutter_angle", deg);    self._send(pkt_cam_shutter_angle(m, deg))

    def send_cam_nd(self, m, stop):
        self._note_cam(m, "nd", stop);              self._send(pkt_cam_nd(m, stop))

    def send_cam_cc_reset(self, m):
        # The camera returns every correction parameter to its neutral, so the
        # remembered values have to go too, or the panel would show the old
        # grade next time it opened.
        st = self._states.get(m)
        if st is not None:
            for k in ("lift", "gamma", "gain_cc", "offset", "contrast",
                      "luma_mix", "hue_sat"):
                st.cam_sent.pop(k, None)
                st.cam_adv.pop(k, None)
        self._send(pkt_cam_cc_reset(m))

    def send_cam_zoom_speed(self, m, spd):      self._send(pkt_cam_zoom_speed(m, spd))
    def send_cam_auto_iris(self, m):            self._send(pkt_cam_auto_iris(m))
    def send_cam_record(self, m, on: bool) -> None:
        """Start or stop recording.  Nothing is assumed about the result:
        cam_recording only moves when the camera reports the transport mode,
        so a Record button that lights up is a camera that IS rolling, not one
        that was asked to."""
        self._send(pkt_cam_transport(m, TRANSPORT_RECORD if on else TRANSPORT_PREVIEW))

    def send_cam_tally(self, m, brightness: float, lamp: str = "both") -> None:
        """Tally lamp brightness, 0.0 off to 1.0 full.

        BRIGHTNESS, not a program/preview colour: the red/green state on a
        Blackmagic camera arrives over SDI and this rig has no SDI path to
        it.  Turning the lamp up and down is what the Bluetooth link offers.
        """
        fn = {"front": pkt_cam_tally_front,
              "rear":  pkt_cam_tally_rear}.get(lamp, pkt_cam_tally)
        self._note_cam(m, f"tally_{lamp}", float(brightness))
        self._send(fn(m, brightness))

    def send_cam_auto_wb(self, m):              self._send(pkt_cam_auto_wb(m))
    def send_cam_restore_auto_wb(self, m):      self._send(pkt_cam_restore_auto_wb(m))

    def cam_known(self, mount_id: int) -> dict:
        """Everything known about this camera's settings, most recent account wins.

        Per parameter, whichever is newer: what the camera last reported, or
        what this app last sent.  Not "the camera always wins" — the camera
        reports only a few of these parameters and then falls silent, so an old
        report would override a new setting for ever and the control would
        spring back to it every time the panel refreshed.  A camera that really
        has moved since reports again, and wins by being newer.
        """
        st = self._states.get(mount_id)
        if st is None:
            return {}
        heard = dict(st.cam_adv)
        # These three live in named fields rather than cam_adv, because the
        # everyday dialog reads them; fold them in so a caller has one dict.
        if st.cam_wb is not None:
            heard["white_balance"] = st.cam_wb
        if st.cam_tint is not None:
            heard["tint"] = st.cam_tint
        if st.cam_iso is not None:
            heard["iso"] = st.cam_iso

        known = dict(st.cam_sent)
        for k, v in heard.items():
            if k not in known or st.cam_heard_at.get(k, 0.0) >= st.cam_sent_at.get(k, 0.0):
                known[k] = v
        return known

    def cam_ble_state(self, mount_id: int):
        """None = no camera support, "unpaired", False = link down, True = ready.

        Same source the ordinary camera dialog reads, so the two cannot disagree
        about whether a camera is there — which they would if this asked a
        different question of a different object.
        """
        b = getattr(self, "_bridge", None)
        return b.cam_ble_link(mount_id) if b is not None else None

    def send_start_look_at_move(self, mount_id: int, subject_id: int,
                                 direction: int, speed_preset: int = 2,
                                 repeat: bool = False) -> None:
        """Start a look-at move toward the min (direction=0) or max (direction=1) limit.

        repeat=True hands the whole run to the mount: it flips direction and
        starts the next leg itself when one ends.  This app then does nothing
        further — see _on_look_at_status_from_mount, which used to drive it.
        """
        self._send(pkt_start_look_at_move(mount_id, subject_id, direction,
                                          speed_preset, repeat))

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
                first   = not self._route_logged
                self._mount_route = route
                if changed:
                    self.mount_route_updated.emit(list(route))
                # Which base each mount is on has only ever existed in the UI.
                # That left comms.log unable to tell a mount whose signal is
                # DEGRADING from one that is ALTERNATING between two bases —
                # they look identical in the rssi column, and mount 5 swinging
                # 13 dB between consecutive samples on 2026-08-11 could have
                # been either.  Logged on change, plus once at startup so a log
                # that begins mid-session still says where everything is.
                if changed or first:
                    self._log_route()
            except Exception as e:
                log.error(f"MOUNT_ROUTE decode failed: {e}")
            return
        if pkt.cmd == Cmd.SAT_NAMES:
            try:
                names = decode_sat_names(pkt.payload)
                if names != self._sat_names:
                    self._sat_names = names
                    self.sat_names_updated.emit(dict(names))
                    # The route almost always arrives BEFORE the names — the hub
                    # sends both in answer to one request, routes first — so the
                    # first route line reads "via SAT 1" for a satellite that is
                    # perfectly well named a moment later.  Say it again now
                    # that it can be said properly; "via Foyer" is the whole
                    # point of the names existing.
                    if self._route_logged and any(self._mount_route):
                        self._log_route()
            except Exception as e:
                log.error(f"SAT_NAMES decode failed: {e}")
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

        # ANY packet from a mount proves it is alive.  Until now only STATUS and
        # PONG set this, so a mount could sit ACKing GET_CONFIG every four
        # seconds — its speeds populating the UI perfectly — while the app still
        # showed it greyed out as disconnected.  That is what the dimmed camera
        # rows were: the app had the data and was ignoring the evidence.
        #
        # It also could not recover on its own, because the idle probe only
        # polls mounts already believed connected, so a mount that missed the
        # one PONG at startup was skipped by the very thing that would have
        # noticed it.  Restarting the app was the only cure.
        #
        # Deliberately before the per-command handling, so it covers ACK, NACK,
        # POSITION, CAM_STATUS and anything added later.
        # ...but "alive" is not "usable".  A mount that is heard and does not
        # ACK stays greyed until an ACK arrives, or its own health packets would
        # walk it straight back to looking connected — which is exactly how 79%
        # command loss displayed as a perfectly healthy mount 4.
        if not st.connected:
            self._set_mount_online(mid, connected=True)
            self._send(pkt_get_state(mid))     # as the STATUS path does
        st.last_pong_ms = time.monotonic() * 1000

        if pkt.cmd == Cmd.MOUNT_EVENT and len(pkt.payload) >= MOUNT_EVENT_PAYLOAD_LEN:
            # A mount explaining, after the fact, why it restarted itself.  It
            # cannot say so at the time — while isolated it cannot transmit at
            # all, and the restart clears the counters — so this is carried
            # across the reboot in RTC memory.  WARNING level: a mount that had
            # to rescue itself is worth noticing even when it came back.
            b = bytes(pkt.payload)
            kind = b[0]
            txf  = (b[1] << 8) | b[2]
            rei  = (b[3] << 8) | b[4]
            rx_s = (b[5] << 8) | b[6]
            tx_s = (b[7] << 8) | b[8]
            ref  = (b[9] << 8) | b[10]
            err  = (b[11] << 8) | b[12]
            wifi = b[13]
            if kind == MOUNT_EVENT_TX_WEDGE_REBOOT:
                # The escalation, and the only remedy with evidence behind it:
                # 99 WiFi-level restarts on one mount changed nothing, and one
                # reboot cleared the same fault for hours at identical signal.
                log.warning(
                    "MOUNT EVENT cam%d REBOOTED to clear a TX wedge — the "
                    "WiFi-level restart did not hold (%d tried since boot) | "
                    "%d sends failed, %d stack reinits | %s",
                    mid, wifi, txf, rei, _espnow_err_text(ref, err))
                return
            if kind == MOUNT_EVENT_TX_WEDGE:
                # Not a restart: the mount recovered itself without rebooting.
                # Worth a WARNING all the same — this is the failure that used
                # to persist silently for an hour because the mount was only
                # half-dead and the isolation rule needed it fully dead.
                log.warning(
                    "MOUNT EVENT cam%d RECOVERED A ONE-WAY TX WEDGE with a "
                    "WiFi-level restart (%d since boot) | it was still "
                    "receiving normally, so the isolation restart could never "
                    "have fired | %d sends failed over %ds, %d stack reinits "
                    "| %s",
                    mid, wifi, txf, tx_s, rei, _espnow_err_text(ref, err))
                return
            what = ("isolated — no RX and no TX" if kind == MOUNT_EVENT_ISOLATED
                    else f"kind {kind}")
            # RX silence is pinned to the mount's 2-minute restart threshold by
            # construction, so it is the GAP that means something: TX dying
            # first, then RX following, is a stack going down in stages rather
            # than a mount driving out of range.
            gap = tx_s - rx_s
            when = (f", TX died {gap}s before RX" if gap > 0 else "")
            log.warning("MOUNT EVENT cam%d RESTARTED ITSELF: %s | at the time: "
                        "txfail %d, %d stack reinits, RX silent %ds, TX silent %ds%s"
                        " | %s",
                        mid, what, txf, rei, rx_s, tx_s, when,
                        _espnow_err_text(ref, err))
            return

        if pkt.cmd == Cmd.RF_REPORT and len(pkt.payload) >= RF_REPORT_PAYLOAD_LEN:
            b = bytes(pkt.payload)
            sig = [int.from_bytes(b[i:i+1], "big", signed=True) for i in range(6)]
            rmin, rmean, rmax, nmin, nmean, nmax = sig
            n    = (b[6] << 8) | b[7]
            drop = (b[8] << 8) | b[9]  if len(b) >= 10 else 0
            txa  = (b[10] << 8) | b[11] if len(b) >= 14 else 0
            txf  = (b[12] << 8) | b[13] if len(b) >= 14 else 0
            if drop:
                # Heard, MAC-acknowledged, then thrown away because the
                # application queue was full.  The sender sees a successful send
                # and the command never happens — both ends reporting success
                # for something that did not occur.
                log.warning("RF cam%d: receive queue FULL — %d frames dropped "
                            "after being received (a dropped command is never "
                            "acknowledged, so it reads as a mount ignoring it)",
                            mid, drop)
            if not n:
                log.warning("RF cam%d: heard NOTHING in the last window", mid)
                return
            # SNR is the number that decides it.  A link fails on signal-to-
            # noise, not signal: -59 dBm on a -95 dBm floor has 36 dB of margin
            # and the same -59 on a -65 dBm floor has six.  Those are identical
            # in the rssi column and want opposite remedies.
            snr = rmean - nmean
            worst = rmin - nmax          # the moment the frames actually died
            # The floor is AGC-relative, not absolute: on 2026-08-11 mount 4
            # reported -99 while hearing -79, and mounts 1 and 5 reported -90
            # while hearing -48 and -41.  A receiver at high gain reports a
            # different floor from one turned down.  So this reads as "high in
            # any gain state", and the number to watch is a mount's own floor
            # MOVING, not one mount against another.
            verdict = ("interference — the noise floor is up, not the signal down"
                       if nmax > -75 else
                       "quiet band — the noise floor is where it should be")
            # 10 dB, not 20.  20 was a guess and it cried wolf immediately:
            # mount 4 sat at 18-19 dB for an hour, warning on every line, while
            # failing 4 sends in 160 s.  802.11b at 1 Mbps needs roughly 4-10 dB,
            # so below 10 is genuinely thin and above it is not worth a colour.
            fn = log.warning if worst < 10 else log.info
            # TX as a RATE.  Only failures were counted before, which cannot be
            # read on a mount that transmits more than its neighbours — and the
            # one with a camera does, by roughly three times.  A percentage
            # compares across mounts; a raw count only compares across time on
            # the same mount.
            tx = ""
            if txa:
                tx = "  | TX %d sent, %d did not go out (%.2f%%)" % (
                    txa, txf, 100.0 * txf / txa)
            fn("RF cam%d: rssi %d/%d/%d  noise %d/%d/%d  (min/mean/max, dBm) "
               "| SNR %d dB, worst %d dB over %d frames | %s%s",
               mid, rmin, rmean, rmax, nmin, nmean, nmax, snr, worst, n, verdict, tx)
            return

        if pkt.cmd == Cmd.STATUS:
            try:
                s = decode_status(pkt.payload)
                was_online = st.connected and not st.unresponsive
                self._set_mount_online(mid, connected=True)
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

                if not was_online:
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

        elif pkt.cmd == Cmd.CAM_STATUS:

            # The camera reports whatever it likes, whenever it likes — including

            # changes made on the camera body.  That is the point: the UI shows the

            # camera's truth, not an echo of our own commands.

            raw = bytes(pkt.payload)

            upd = decode_cam_status(raw)

            self._log_cam_param(pkt.mount_id, raw, upd)

            if upd:

                st = self._states.get(pkt.mount_id)

                if st:

                    if "recording" in upd:
                        was = st.cam_recording
                        st.cam_recording = upd["recording"]
                        if was != st.cam_recording:
                            log.info("CAM%d %s", pkt.mount_id,
                                     "RECORDING" if st.cam_recording else "stopped recording")
                            self.cam_recording_changed.emit(pkt.mount_id,
                                                           bool(st.cam_recording))
                    if "iso" in upd:           st.cam_iso  = upd["iso"]

                    if "white_balance" in upd: st.cam_wb   = upd["white_balance"]

                    if "tint" in upd:          st.cam_tint = upd["tint"]

                    # Everything else the camera volunteers, kept by name for
                    # the advanced panel.  Stored even when nothing is currently
                    # displaying it: the camera reports what it likes, and a
                    # parameter nobody reads today is still the only evidence
                    # that a command landed, because the protocol acknowledges
                    # nothing.

                    for k, v in upd.items():

                        if k not in ("iso", "white_balance", "tint"):

                            st.cam_adv[k] = v

                    # When the camera said it, for every key including the three
                    # above.  cam_known() compares this against cam_sent_at to
                    # decide which is the more recent account of a parameter.

                    now = time.monotonic()

                    for k in upd:

                        st.cam_heard_at[k] = now

                    self.cam_status_received.emit(pkt.mount_id)


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
                # Report the rail geometry when it changes.  A flash bumps
                # EEPROM_MAGIC and silently resets the tilt to 0, which leaves
                # pan tracking fine and tilt almost flat — a failure that looks
                # like bad tracking rather than lost config.  GET_CONFIG is
                # polled every ~2 s, so log only on change.
                prev_cr = st.last_config_report
                if (prev_cr is None
                        or abs(prev_cr.slider_tilt_deg - cr.slider_tilt_deg) > 0.05):
                    note = ("level — correct for a horizontal rail; if this rail "
                            "is inclined, a flash has reset it"
                            if abs(cr.slider_tilt_deg) < 0.05 else "inclined")
                    log.info(f"RAIL cam{mid}: slider tilt {cr.slider_tilt_deg:+.1f}° ({note})")
                st.look_at_mode = cr.look_at_mode
                st.last_config_report = cr   # cache for config dialog pre-population
                self.config_report_received.emit(mid, cr)
            except Exception as e:
                log.warning(f"Bad CONFIG_REPORT from mount {mid}: {e}")

        # ── v2 look-at packets ──────────────────────────────────────────
        elif pkt.cmd == Cmd.SUBJECT_LIST:
            try:
                subjects = decode_subject_list(pkt.payload)
            except Exception as e:
                log.warning(f"Bad SUBJECT_LIST from mount {mid}: {e}")
            else:
                # Store and publish FIRST.  Logging is a nicety; the subject
                # list is what the UI draws its borders from, and an exception
                # raised while logging must not cost the app the packet.  It
                # did: a crash here skipped both lines below, so st.subjects
                # kept its initial [None] * 8, which then crashed the next one
                # the same way — a permanent loop that also blamed the mount
                # for a fault in this file.
                prev = st.subjects
                st.subjects = subjects
                self.subject_list_received.emit(mid)
                self._log_solved_subjects(mid, prev, subjects)

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
                # Fields are pan_deg/tilt_deg — this read pan_ref_deg and threw
                # every single time, since the initial commit.  The signal's own
                # comment names them correctly; only these two reads were wrong.
                log.info("REF CONFIRMED cam%d: pan=%.2fdeg tilt=%.2fdeg",
                         mid, rc.pan_deg, rc.tilt_deg)
                self.ref_confirmed.emit(mid, rc.pan_deg, rc.tilt_deg)
            except Exception as e:
                log.warning(f"Bad REF_CONFIRMED from mount {mid}: {e}")

        elif pkt.cmd == Cmd.CALIB_PROMPT:
            try:
                prompt = decode_calib_prompt(pkt.payload)
                # Subject calibration is a handshake — the mount prompts, the
                # operator acts, the app answers — and only the app's half was
                # ever recorded.  A setup that stalls therefore shows SET_B with
                # no SET_A before it and nothing to say why, which is exactly
                # what the 2026-08-19 attempt left behind.
                log.info("CALIB PROMPT cam%d: %s", mid,
                         getattr(prompt, "name", None) or f"value {int(prompt)}")
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
                prev = st.position
                st.position = pos
                self.position_updated.emit(mid, pos)
                self._log_look_at_sample(mid, st, pos)
            except Exception as e:
                log.warning(f"Bad POSITION from mount {mid}: {e}")

        elif pkt.cmd == Cmd.ACK:
            # Proof the mount is not just audible but ACTING on what it is told.
            st.unacked = 0
            if st.unresponsive:
                log.warning("Mount %d is responding again", mid)
                self._set_mount_online(mid, unresponsive=False)

        elif pkt.cmd == Cmd.PONG:
            self._set_mount_online(mid, connected=True)
            st.last_pong_ms = time.monotonic() * 1000
            # No GET_STATUS here.  This turned one heartbeat into two downlink
            # frames per mount — ping out, pong back, status request out — and
            # measuring the relay showed the two of them were 95% of everything
            # it carried: PING x60 and GET_STATUS x60 per 10 s against 126 frames
            # total.  Half of that was the app asking a question it had just
            # provoked itself into asking.
            #
            # It is also now redundant: the mount sends STATUS when state
            # CHANGES, plus a 5 s refresh, so polling on every pong asks for
            # something already volunteered.  A pong proves the mount is alive,
            # which is all a pong was ever for.

        elif pkt.cmd == Cmd.NACK:
            try:
                n = decode_nack(pkt.payload)
                # Name the refusal.  NO_REF in particular has an action attached
                # to it, and "err=6" does not carry that to whoever is at the
                # rig wondering why a calibration will not start.
                if int(n.error) == int(NackError.NO_REF):
                    log.warning("NACK from mount %d: no session reference — "
                                "run Set Ref (0/0) before calibrating a subject "
                                "or starting a look-at move. The reference is "
                                "cleared by a reboot or a firmware flash.", mid)
                else:
                    log.warning(f"NACK from mount {mid}: seq={n.nacked_seq} err={n.error}")
                self.nack_received.emit(mid, n.nacked_seq, int(n.error))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    # TEMPORARY — see LOOK_AT_DIAGNOSTIC.
    _LOOK_AT_STATES = (MountState.LOOK_AT_MOVE, MountState.LOOK_AT_PRE_AIM)

    def _log_look_at_sample(self, mid: int, st, pos) -> None:
        """TEMPORARY — see LOOK_AT_DIAGNOSTIC.  One line per sample, with the
        angular velocity since the last one and the CHANGE in that velocity.

        Velocity rather than position because abruptness IS velocity: a
        position series would have to be differenced by hand to say anything
        about it, and the question is where the rate changes, not where the
        camera is.

        And the change-in-velocity column because "one speed, then the next" is
        a statement about that column specifically.  Last time the shape had to
        be read off a screen of position lines by eye, which is how two rounds
        of reasoning got spent on a cap that turns out not to bind during the
        ease out at all.  A step should be a number in the log, not an
        impression.
        """
        if not LOOK_AT_DIAGNOSTIC or st.state not in self._LOOK_AT_STATES:
            return
        now = time.monotonic()
        last = self._la_last.get(mid)
        if last is None:
            self._la_last[mid] = (now, pos.pan_deg, pos.tilt_deg)
            log.warning("LA POS cam%d: tracking — pan %+.2f tilt %+.2f "
                        "(velocity from the next sample)",
                        mid, pos.pan_deg, pos.tilt_deg)
            return
        dt = now - last[0]
        if dt <= 0.0:
            return

        # Replies arrive bunched when the link stutters: a delayed one and the
        # one behind it land together, so the second is TIMED over a fraction
        # of the poll interval while CARRYING a whole interval of movement.
        # That reads as a spike to double speed for exactly one sample.
        #
        # In the 2026-08-26 log every switch had one — +88, -92, +66 against
        # plateaus of 45 — and every one of them had dt 0.10 against a 0.21
        # nominal. None was real motion, and a fake 45 deg/s step is precisely
        # the shape being hunted, so it cannot be left in.
        #
        # Hold the anchor rather than logging it: the next sample then measures
        # across the whole span and comes out right.
        if dt < (LOOK_AT_POLL_MS / 1000.0) * 0.6:
            return
        self._la_last[mid] = (now, pos.pan_deg, pos.tilt_deg)
        v_pan  = (pos.pan_deg  - last[1]) / dt
        v_tilt = (pos.tilt_deg - last[2]) / dt

        # Change since the previous sample, per axis.  This is the ease.
        prev_v = self._la_prev_v.get(mid)
        self._la_prev_v[mid] = (v_pan, v_tilt)
        if prev_v is None:
            d_pan = d_tilt = 0.0
        else:
            d_pan, d_tilt = v_pan - prev_v[0], v_tilt - prev_v[1]

        log.warning("LA POS cam%d %+.2fs  pan %+7.2f (%+7.1f deg/s, d%+6.1f)  "
                    "tilt %+7.2f (%+7.1f deg/s, d%+6.1f)",
                    mid, dt, pos.pan_deg, v_pan, d_pan,
                    pos.tilt_deg, v_tilt, d_tilt)

        self._la_series.setdefault(mid, []).append((v_pan, v_tilt))
        self._la_check_settled(mid, v_pan, v_tilt)

    # Speeds that count as "moving" and as "stopped", for deciding when one
    # switch has finished.  A subject switch does not change the mount's STATE
    # — it stays in LOOK_AT_MOVE throughout — so the end of the move has to be
    # detected from the motion itself.
    #
    # These were 2.0 and 0.5 and no summary ever printed.  The assumption was
    # that a move ends with the camera stationary; on this rig it does not.
    # The mount goes on tracking the subject through the slider move at 1–5
    # deg/s indefinitely, so the speed never fell below 0.5 and the summary sat
    # waiting for a stillness that was never coming.
    #
    # Set from the 2026-08-26 log: baseline tracking 1–5 deg/s, switches 45.
    # There is a wide gap between those and nothing lives in it.
    _LA_MOVING_DPS  = 15.0
    _LA_STOPPED_DPS = 5.0

    def _la_check_settled(self, mid: int, v_pan: float, v_tilt: float) -> None:
        """TEMPORARY — see LOOK_AT_DIAGNOSTIC.  Print the whole move on one
        line once it has stopped.

        The rectangle was only obvious as a series; the same will be true of
        whatever shape the ease out really has.  One line that can be pasted
        back beats a screenful that has to be scrolled and described.
        """
        speed = max(abs(v_pan), abs(v_tilt))
        if speed > self._LA_MOVING_DPS:
            self._la_moving[mid] = True
            self._la_quiet[mid]  = 0
            return
        if not self._la_moving.get(mid):
            # Never got going — drifting tracking, not a switch.  Don't let
            # idle samples accumulate into a meaningless summary.
            self._la_series[mid] = self._la_series.get(mid, [])[-1:]
            return
        if speed > self._LA_STOPPED_DPS:
            return
        self._la_quiet[mid] = self._la_quiet.get(mid, 0) + 1
        if self._la_quiet[mid] < 2:
            return

        series = self._la_series.get(mid, [])
        self._la_moving[mid] = False
        self._la_quiet[mid]  = 0
        self._la_series[mid] = []
        if len(series) < 3:
            return

        pans  = [v for v, _ in series]
        tilts = [v for _, v in series]
        steps = [abs(pans[i] - pans[i - 1]) for i in range(1, len(pans))]
        worst = max(steps)
        where = steps.index(worst) + 1
        log.warning("LA MOVE cam%d pan:  %s", mid,
                    " ".join(f"{v:+.1f}" for v in pans))
        log.warning("LA MOVE cam%d tilt: %s", mid,
                    " ".join(f"{v:+.1f}" for v in tilts))
        log.warning("LA MOVE cam%d %d samples, peak %.1f deg/s, biggest pan "
                    "step %.1f deg/s at sample %d of %d (%s)",
                    mid, len(series), max(abs(v) for v in pans), worst,
                    where, len(pans),
                    "during the ease out" if where > len(pans) * 0.6
                    else "during the ease in")

    def _poll_look_at_positions(self) -> None:
        """Ask for a position, 5 Hz, only from a mount that is tracking.

        The mount answers one CMD_POSITION per request — the unsolicited stream
        was removed — so the sample rate is exactly the request rate, and the
        traffic stops the moment the look-at move does.
        """
        if not LOOK_AT_DIAGNOSTIC:
            return
        for mid, st in self._states.items():
            if not st.connected:
                continue
            if st.state in self._LOOK_AT_STATES:
                self._send(pkt_get_position(mid))
            elif mid in self._la_last:
                # Move over — drop the anchor so the next one starts clean
                # rather than reporting a velocity across the gap between them.
                del self._la_last[mid]
                self._la_prev_v.pop(mid, None)
                self._la_series.pop(mid, None)
                self._la_moving.pop(mid, None)
                self._la_quiet.pop(mid, None)

    def _heartbeat(self) -> None:
        if not self._bridge.connected:
            # Ask again on the next connection, and say the route again with
            # it: a reconnect is exactly when the layout may have changed
            # underneath us, and repeating it costs one line.
            self._asked_for_table = False
            self._route_logged    = False
            return

        # Ask once per connection for the hub's pairing table.  The hub answers
        # with the table, the ROUTES and the satellite names together, and until
        # now nothing requested any of it except the pairing dialog — so a
        # session where nobody opened that dialog never learned which base each
        # mount was on, and the route logging added for exactly that question
        # sat waiting for a packet that was never going to arrive.
        if not self._asked_for_table:
            self._asked_for_table = True
            self.request_mount_table()

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
                # unresponsive is cleared too: it describes a mount that is
                # heard and not acting.  Leaving it set on one that has gone
                # silent means the next packet re-enters as "heard but still
                # unresponsive" and cannot come back online until an ACK, which
                # is exactly how a live mount used to stay greyed.
                self._set_mount_online(mid, connected=False, unresponsive=False)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _send(self, data: bytes) -> None:
        if self._bridge.connected:
            self._bridge.send(data)
