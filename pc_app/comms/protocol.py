"""
Packet protocol for camera mount controller.

Packet format:
  [0xAA][0x55][LEN:u8][MOUNT_ID:u8][SEQ_HI:u8][SEQ_LO:u8][CMD:u8][PAYLOAD...][CRC_HI:u8][CRC_LO:u8]

  LEN  = number of bytes from MOUNT_ID through end of PAYLOAD (does not include CRC).
  CRC  = CRC-16/CCITT-FALSE over bytes from LEN through end of PAYLOAD.

  MOUNT_ID: 0x00 = broadcast all mounts, 0x01-0x05 = individual mounts.
  SEQ:      rolling u16 sequence number; used to match ACK/NACK replies.
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PACKET_START_1 = 0xAA
PACKET_START_2 = 0x55
MOUNT_BROADCAST = 0x00
NUM_MOUNTS = 5
NUM_POSITIONS = 10

# STATUS target_slot values (mirror of shared/protocol.h).
# 0-9 name the position slot a GOTO_SLOT move is heading for.  On a LOOK-AT
# mount there are no position slots 9 and 10 — slots 0-7 are subjects and 8/9
# are the guide arrows — so 8/9 instead report a running look-at slider move,
# and mean that ONLY when the mount is in look-at mode.  Check the mode before
# reading them.
TARGET_SLOT_NONE   = 0xFF
TARGET_SLOT_LA_MIN = 8    # look-at slider move running toward min (left arrow)
TARGET_SLOT_LA_MAX = 9    # look-at slider move running toward max (right arrow)

# Minimum bytes in a valid packet: start(2) + len(1) + mount_id(1) + seq(2) + cmd(1) + crc(2) = 9
PACKET_MIN_SIZE = 9
PACKET_MAX_PAYLOAD = 256   # raised: CMD_SUBJECT_LIST needs 232 bytes

# Payload sizes for fixed-length large packets
STATE_REPORT_PAYLOAD_LEN = 182   # 10 slots × 16 bytes + 22 bytes metadata
SAVE_SPEEDS_PAYLOAD_LEN  = 72    # (4 PT + 4 SL + 1 ZM) × 8 bytes
NUM_SLOTS = 10

# v2 look-at subject constants
MAX_SUBJECTS           = 8
MAX_SLIDER_MOVES       = 8
SUBJECT_NAME_LEN       = 16
SUBJECT_RECORD_LEN     = 29   # valid(1) + name(16) + x_mm(4f) + y_mm(4f) + z_mm(4f)
SUBJECT_LIST_PAYLOAD_LEN = MAX_SUBJECTS * SUBJECT_RECORD_LEN  # 232


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class Cmd(IntEnum):
    # PC → Mount
    JOG               = 0x01
    GOTO              = 0x02
    SAVE_POS          = 0x03   # legacy alias; use STORE_POS
    SET_SPEED_PRESET  = 0x04
    SET_LIMITS        = 0x05
    FIND_LIMITS       = 0x06
    SET_ORIENTATION   = 0x07
    E_STOP            = 0x08
    GET_STATUS        = 0x09
    PING              = 0x0A
    GET_STATE         = 0x0B   # request full state dump (slots + presets + limits)
    STORE_POS         = 0x0C   # store current position in slot N (1 byte: slot 0-9)
    CLEAR_POS         = 0x0D   # clear slot N (1 byte: slot 0-9)
    SET_ACTIVE_PRESET = 0x0E   # change active preset (2 bytes: group, preset 1-4)
    SAVE_SPEEDS       = 0x0F   # write 9 speed presets to EEPROM (72 bytes)
    GOTO_SLOT         = 0x10   # recall stored position by slot index (2 bytes: slot 0-9, pt_preset 1-4)
    MOVE_REL          = 0x11   # move relative to current position — same payload as GOTO (17 bytes)
    GET_CONFIG        = 0x12   # request speed presets + orientation from mount (no payload)
    FIND_HOME             = 0x13   # home one axis to its min end stop (1 byte: axis)
    SET_STALL_THRESHOLD   = 0x14   # set + persist StallGuard threshold (2 bytes: axis, threshold)
    GET_POSITION          = 0x15   # request an immediate POSITION reply (no payload)

    # PC → Mount  (v2 — look-at tracking)
    ADD_SUBJECT_START  = 0x20   # begin subject calibration: subject_id(1) + name(16) = 17B
    ADD_SUBJECT_SET_A  = 0x21   # record point A at slider home (no payload)
    ADD_SUBJECT_SET_B  = 0x22   # record point B at slider max (no payload)
    ADD_SUBJECT_ABORT  = 0x23   # cancel calibration in progress (no payload)
    DELETE_SUBJECT     = 0x24   # delete subject: subject_id(1)
    SET_REF            = 0x25   # set pan/tilt session reference: subject_id(1)
    SET_SLIDER_MOVE    = 0x26   # store slider move: slot(1)+start_mm(4f)+end_mm(4f)+preset(1) = 10B
    START_LOOK_AT_MOVE = 0x27   # start tracking: slider_slot(1)+subject_id(1)+max_pt_deg_s(4f) = 6B
    SWITCH_SUBJECT     = 0x28   # switch tracking target mid-move: subject_id(1)
    GET_SUBJECTS       = 0x29   # request full subject list (no payload)

    # Mount → PC
    STATUS            = 0x80
    LIMITS_FOUND      = 0x81
    ACK               = 0x82
    NACK              = 0x83
    PONG              = 0x84
    STATE_REPORT      = 0x85   # full state: 10 slots + active presets + limits
    CONFIG_REPORT     = 0x86   # config: orientation flags + 9 speed presets (73 bytes)
    HOME_COMPLETE     = 0x87   # homing finished (1 byte: axis)

    # Mount → PC  (v2)
    SUBJECT_LIST      = 0x90   # all subjects: 8 × SUBJECT_RECORD_LEN bytes (232B)
    LOOK_AT_STATUS    = 0x91   # live telemetry: slider_mm(4f)+pan_deg(4f)+tilt_deg(4f)+subj_id(1)+flags(1) = 14B
    REF_CONFIRMED     = 0x92   # reference set echo: pan_deg(4f)+tilt_deg(4f) = 8B
    CALIB_PROMPT      = 0x93   # calibration step notification: sub_state(1)

    # Hub-injected (never sent by Teensy — generated by hub ESP32 for all clients)
    LA_MOVE_DIR       = 0x94   # look-at move direction: 0=min(◀), 1=max(▶), 0xFF=stopped
    HUB_DIAG          = 0x95   # hub→USB-PC only: usb_rx_bytes(u32)+usb_rx_pkts(u32) — USB wedge diagnostic
    HUB_REINIT_ESPNOW = 0x96   # PC→hub: full esp_now reinit — recovers the hub→mount ESP-NOW send wedge
    HUB_RESTART       = 0x97   # PC→hub: full esp_restart() — escalation when 0x96 doesn't clear the wedge
    HUB_EVENT         = 0x98   # hub→USB-PC only: kind(1)+mount(1)+rssi(1)+state(1)+flags(1)+uptime_s(u32)
    HEALTH            = 0x99   # node→PC log: uniform 24B health record (see HealthPayload)
    POSITION          = 0x9A   # mount→clients: live positions, 17B (see PositionPayload);
                               # 5 Hz while moving / 1 Hz at rest / instant on GET_POSITION

    # Pairing management — hub mount-table access for all clients (hub owns the
    # table in NVS; these give the PC/web app the same view/set/clear the 7"
    # display has).  Hub-consumed or hub-originated; never forwarded to mounts.
    GET_MOUNT_TABLE   = 0x9B   # client→hub, no payload: request a MOUNT_TABLE push
    MOUNT_TABLE       = 0x9C   # hub→clients, 30B: 5 × MAC(6); all-zero slot = unbound
    PAIR_CONFLICT     = 0x9D   # hub→clients, 13B: cam(1)+new_mac(6)+old_mac(6); cam=0 = dismiss
    PAIR_DECIDE       = 0x9E   # client→hub, 8B: cam(1)+decision(1: 1=replace, 0=ignore)+new_mac(6)
    PAIR_FORGET       = 0x9F   # client→hub, 1B: cam — clear (unbind) that slot
    MOUNT_ROUTE       = 0xA0   # hub→clients, 5B: per-cam 0 = direct, N = via satellite N
    SAT_HELLO         = 0xA3   # satellite→hub, 13B: its location name
    SAT_NAMES         = 0xA4   # hub→clients, 6×13B: slot → location name
    RESCAN_BASES      = 0xA5   # hub→mounts: a satellite returned, re-pick a base
    MOUNT_EVENT       = 0xA6   # mount→clients, 13B: why it restarted itself
    CAM_CONTROL        = 0xA1   # client→hub→mount: Blackmagic camera command, relayed verbatim
    CAM_STATUS         = 0xA2   # mount→clients: Blackmagic status, relayed verbatim


# CMD_HEALTH node_type values (payload byte [0])
HEALTH_NODE_SATELLITE = 4
HEALTH_NODE_NAMES = {0: "hub", 1: "bridge", 2: "teensy", 3: "display",
                     HEALTH_NODE_SATELLITE: "sat"}

# PayloadHealth.flags bits — spare bits in a byte every node already sends
# every 10 s, so no payload growth and no protocol version to think about.
HEALTH_FLAG_ANOMALY   = 0x01
HEALTH_FLAG_BLE_BUILD = 0x02   # mount firmware has camera support at all
HEALTH_FLAG_BLE_LINK  = 0x04   # ...and the camera is currently paired
HEALTH_FLAG_CAM_WR_ERR = 0x08  # a camera write failed since the last report
HEALTH_FLAG_CAM_SUBSCR = 0x10  # camera status notifications are subscribed
HEALTH_FLAG_CAM_RX     = 0x20  # at least one notification actually received
HEALTH_FLAG_CAM_UNPAIRED = 0x40  # no bond — pair on the mount (hold screen twice)
HEALTH_FLAG_CAM_CACHE_FULL = 0x80  # a camera parameter is being dropped, silently


class Axis(IntEnum):
    PAN    = 0
    TILT   = 1
    SLIDER = 2
    ZOOM   = 3


class AxisGroup(IntEnum):
    PAN_TILT    = 0
    SLIDER_ZOOM = 1   # slider presets (4 presets)
    ZOOM        = 2   # zoom has a single independent preset


class MountState(IntEnum):
    IDLE                = 0
    JOGGING             = 1
    MOVING_TO_POS       = 2
    FINDING_LIMITS      = 3
    ERROR               = 4
    LOOK_AT_MOVE        = 5   # v2: slider moving, pan/tilt tracking subject
    CALIBRATING_SUBJECT = 6   # v2: 2-point subject calibration in progress
    LOOK_AT_PRE_AIM     = 7   # v2: pre-aiming pan/tilt before slider starts
    # NOTE: keep in sync with MountState in firmware/shared/protocol.h — a
    # value missing here makes decode_status() raise and the PC app silently
    # drop every STATUS packet the mount sends while in that state.


class CalibPrompt(IntEnum):
    """Sub-state codes sent in Cmd.CALIB_PROMPT."""
    MOVING_TO_A = 0x01   # slider moving to home — wait
    WAIT_SET_A  = 0x02   # at home: aim camera then confirm
    MOVING_TO_B = 0x03   # slider moving to max — wait
    WAIT_SET_B  = 0x04   # at max: re-aim camera then confirm
    SOLVED      = 0x05   # 3D position solved and saved
    ERROR       = 0x06   # calibration failed (bad geometry)


class MountFlag(IntEnum):
    AT_MIN_LIMIT    = 0x01
    AT_MAX_LIMIT    = 0x02
    STALL_ERROR     = 0x04
    LIMITS_SET      = 0x08   # slider/zoom limits have been found
    HAS_SLIDER      = 0x10   # mount has a physical slider axis
    REF_SET         = 0x20   # v2: pan/tilt session reference established
    LOOK_AT_ACTIVE  = 0x40   # v2: look-at move currently running
    LOOK_AT_MODE    = 0x80   # v2: slider uses 3D triangulation mode


class NackError(IntEnum):
    BAD_CRC       = 0x01
    BAD_LENGTH    = 0x02
    UNKNOWN_CMD   = 0x03
    INVALID_PARAM = 0x04
    BUSY          = 0x05
    NO_REF        = 0x06   # v2: look-at refused — session reference not set


# ---------------------------------------------------------------------------
# CRC-16/CCITT-FALSE  (poly=0x1021, init=0xFFFF, no reflect)
# ---------------------------------------------------------------------------

def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
        crc &= 0xFFFF
    return crc


# ---------------------------------------------------------------------------
# Packet building
# ---------------------------------------------------------------------------

_seq_counter = 0

def _next_seq() -> int:
    global _seq_counter
    _seq_counter = (_seq_counter + 1) & 0xFFFF
    return _seq_counter


def build_packet(mount_id: int, cmd: Cmd, payload: bytes = b"",
                 seq: Optional[int] = None) -> bytes:
    """Encode a complete packet ready for transmission."""
    if seq is None:
        seq = _next_seq()

    # LEN covers: mount_id(1) + seq(2) + cmd(1) + payload
    length = 1 + 2 + 1 + len(payload)
    header = bytes([mount_id, (seq >> 8) & 0xFF, seq & 0xFF, cmd]) + payload
    crc_input = bytes([length]) + header
    crc = crc16(crc_input)

    return bytes([PACKET_START_1, PACKET_START_2, length]) + header + bytes([crc >> 8, crc & 0xFF])


# ---------------------------------------------------------------------------
# Packet parsing
# ---------------------------------------------------------------------------

@dataclass
class Packet:
    mount_id: int
    seq: int
    cmd: Cmd
    payload: bytes


class ParseError(Exception):
    pass


def parse_packet(data: bytes) -> Packet:
    """
    Parse and validate a complete packet.
    Raises ParseError on any validation failure.
    """
    if len(data) < PACKET_MIN_SIZE:
        raise ParseError(f"Too short: {len(data)} bytes")

    if data[0] != PACKET_START_1 or data[1] != PACKET_START_2:
        raise ParseError("Bad start bytes")

    length = data[2]
    expected_total = 3 + length + 2  # start(2) + len(1) + length bytes + crc(2)

    if len(data) < expected_total:
        raise ParseError(f"Incomplete packet: need {expected_total}, have {len(data)}")

    crc_input = data[2 : 3 + length]          # from LEN byte through end of payload
    received_crc = (data[3 + length] << 8) | data[3 + length + 1]
    computed_crc = crc16(crc_input)

    if received_crc != computed_crc:
        raise ParseError(f"CRC mismatch: got {received_crc:#06x}, expected {computed_crc:#06x}")

    # Decode header fields inside the length-prefixed region
    body = data[3 : 3 + length]               # mount_id + seq(2) + cmd + payload
    if len(body) < 4:
        raise ParseError("Body too short")

    mount_id = body[0]
    seq = (body[1] << 8) | body[2]
    try:
        cmd = Cmd(body[3])
    except ValueError:
        raise ParseError(f"Unknown command: {body[3]:#04x}")

    payload = bytes(body[4:])

    return Packet(mount_id=mount_id, seq=seq, cmd=cmd, payload=payload)

# ---------------------------------------------------------------------------
# Packet stream reader  (reassembles packets from a byte stream)
# ---------------------------------------------------------------------------

class PacketReader:
    """
    Feed raw bytes from the serial port; call packets() to get complete
    parsed Packet objects.  Handles partial reads and framing.
    """

    # Cap on retained inter-packet bytes, so a stream that never contains a
    # newline can't grow this without bound.
    _TEXT_MAX = 4096
    # Minimum consecutive printable characters to count as a real message.
    _TEXT_MIN_RUN = 12

    def __init__(self):
        self._buf = bytearray()
        self._text = bytearray()   # bytes seen between frames — see text_lines()

    def feed(self, data: bytes) -> None:
        self._buf.extend(data)

    def packets(self) -> list[Packet]:
        results = []
        while True:
            # Find start sequence
            idx = self._find_start()
            if idx == -1:
                # No start found — discard everything except a possible
                # trailing 0xAA that might be the first byte of the next packet.
                if self._buf and self._buf[-1] == PACKET_START_1:
                    self._keep_text(self._buf[:-1])
                    self._buf = bytearray([PACKET_START_1])
                else:
                    self._keep_text(self._buf)
                    self._buf.clear()
                break
            if idx > 0:
                self._keep_text(self._buf[:idx])
                del self._buf[:idx]

            # Need at least 3 bytes to read LEN
            if len(self._buf) < 3:
                break

            length = self._buf[2]
            expected_total = 3 + length + 2

            if len(self._buf) < expected_total:
                break  # wait for more data

            packet_bytes = bytes(self._buf[:expected_total])
            del self._buf[:expected_total]

            try:
                results.append(parse_packet(packet_bytes))
            except ParseError as e:
                log.debug(f"Parse error (skipping packet): {e} — raw: {packet_bytes.hex(' ')}")
                # Don't re-raise — just skip this packet and keep reading.

        return results

    def _find_start(self) -> int:
        """Return index of first 0xAA 0x55 pair, or -1 if none found."""
        buf = self._buf
        for i in range(len(buf) - 1):
            if buf[i] == PACKET_START_1 and buf[i + 1] == PACKET_START_2:
                return i
        return -1

    def _keep_text(self, chunk) -> None:
        self._text.extend(chunk)
        if len(self._text) > self._TEXT_MAX:
            del self._text[:-self._TEXT_MAX]

    def text_lines(self) -> list[str]:
        """Drain complete lines of plain text the device printed between frames.

        The hub shares one USB serial link between this binary protocol and its
        own Serial.printf() diagnostics, so those land in the gaps between
        packets. They used to be discarded silently, which made the hub's log
        unreadable in a terminal — the interesting lines are buried in binary.
        Pulling them out here puts them in the PC app's log instead.
        """
        out = []
        while True:
            nl = self._text.find(b"\n")
            if nl == -1:
                break
            raw = bytes(self._text[:nl])
            del self._text[:nl + 1]
            # A line often has binary debris stuck to it (a truncated frame that
            # never completed), so pull out the printable runs rather than
            # judging the line as a whole. Random binary almost never produces a
            # long run of consecutive printable ASCII, real messages always do.
            run = bytearray()
            for b in raw + b"\x00":
                if 32 <= b < 127 or b == 9:
                    run.append(b)
                else:
                    if len(run) >= self._TEXT_MIN_RUN:
                        out.append(run.decode("ascii", "replace").strip())
                    run.clear()
        return out


# ---------------------------------------------------------------------------
# Payload encoders  (PC → Mount)
# ---------------------------------------------------------------------------

def encode_jog(pan_vel: int, tilt_vel: int, slider_vel: int, zoom_vel: int,
               pt_preset: int = 2, sz_preset: int = 2,
               axis_mask: int = 0x0F) -> bytes:
    """
    Encode joystick-driven velocities (clamped to [-1000, 1000])
    plus speed preset indices and an optional axis mask.

    axis_mask bits: 0=pan, 1=tilt, 2=slider, 3=zoom.  0x0F = all axes (default).
    0x03 = pan+tilt only — used in CV-tracking mode so the Teensy calls
    jogPanTilt() and leaves an in-progress moveSliderTo() undisturbed.
    The extra byte is only appended when axis_mask != 0x0F so existing
    firmware that doesn't know about it is unaffected.
    """
    def clamp(v): return max(-1000, min(1000, int(v)))
    pt = max(1, min(4, pt_preset))
    sz = max(1, min(4, sz_preset))
    base = struct.pack(">hhhhBB", clamp(pan_vel), clamp(tilt_vel),
                       clamp(slider_vel), clamp(zoom_vel), pt, sz)
    if axis_mask != 0x0F:
        return base + struct.pack("B", axis_mask & 0xFF)
    return base


def encode_goto(pan: int, tilt: int, slider: int, zoom: int,
                speed_preset: int) -> bytes:
    return struct.pack(">iiiib", pan, tilt, slider, zoom, speed_preset)


def encode_move_rel(d_pan: int, d_tilt: int, d_slider: int, d_zoom: int,
                    speed_preset: int) -> bytes:
    """Relative move — Teensy adds these deltas to its current position."""
    return struct.pack(">iiiib", d_pan, d_tilt, d_slider, d_zoom, speed_preset)


def encode_save_pos(slot: int) -> bytes:
    return struct.pack(">B", slot)


def encode_set_speed_preset(axis_group: AxisGroup, preset: int,
                             max_speed: int, accel: int) -> bytes:
    return struct.pack(">BBii", int(axis_group), preset, max_speed, accel)


def encode_set_limits(axis: Axis, min_steps: int, max_steps: int) -> bytes:
    return struct.pack(">Bii", int(axis), min_steps, max_steps)


def encode_find_limits(axis: Axis, stall_threshold: int = 80) -> bytes:
    return struct.pack(">BB", int(axis), max(1, min(255, stall_threshold)))


def encode_find_home(axis: Axis, stall_threshold: int = 80) -> bytes:
    return struct.pack(">BB", int(axis), max(1, min(255, stall_threshold)))


def encode_set_orientation(pan_invert: bool, slider_invert: bool,
                           has_slider: bool = True,
                           zoom_invert: bool = False,
                           lanc_zoom: bool = False,
                           tilt_invert: bool = False,
                           look_at_mode: bool = False) -> bytes:
    flags = ((0x01 if pan_invert    else 0) |
             (0x02 if slider_invert  else 0) |
             (0x04 if has_slider     else 0) |
             (0x08 if zoom_invert    else 0) |
             (0x10 if lanc_zoom      else 0) |
             (0x20 if tilt_invert    else 0) |
             (0x40 if look_at_mode   else 0))
    return struct.pack(">B", flags)


def encode_ping(timestamp_ms: int) -> bytes:
    return struct.pack(">I", timestamp_ms & 0xFFFFFFFF)


# ---------------------------------------------------------------------------
# Payload decoders  (Mount → PC)
# ---------------------------------------------------------------------------

@dataclass
class StatusPayload:
    state: MountState
    flags: int
    # Extended fields (25-byte STATUS)
    active_pt_preset:   int = 2    # 1-4
    active_sl_preset:   int = 2    # 1-4
    slot_occupied_mask:   int = 0    # bits 0-9: slot N has stored position
    slot_at_mask:         int = 0    # bits 0-9: mount is AT slot N
    target_slot:          int = 0xFF # slot currently being moved to (0xFF = none)
    active_la_subject:    int = 0xFF # active look-at subject (0-7, 0xFF = none)
    # False when the sender used the 9-byte STATUS, which has no byte [9] and so
    # carries no look-at subject at all.  ABSENT IS NOT "NONE": the mount bridge's
    # send_status_heartbeat() hand-rolls a 9-byte STATUS while the shared
    # build_status() sends 10, so both lengths arrive from one mount.  Reading the
    # missing byte as 0xFF wiped a known subject on every short packet and made
    # the stored-location border flicker red/green several times a second.
    la_subject_present:  bool = False

    @property
    def at_min_limit(self) -> bool:
        return bool(self.flags & MountFlag.AT_MIN_LIMIT)

    @property
    def at_max_limit(self) -> bool:
        return bool(self.flags & MountFlag.AT_MAX_LIMIT)

    @property
    def stall_error(self) -> bool:
        return bool(self.flags & MountFlag.STALL_ERROR)

    @property
    def limits_set(self) -> bool:
        return bool(self.flags & MountFlag.LIMITS_SET)

    @property
    def has_slider(self) -> bool:
        return bool(self.flags & MountFlag.HAS_SLIDER)

    @property
    def look_at_mode(self) -> bool:
        return bool(self.flags & MountFlag.LOOK_AT_MODE)


@dataclass
class StateReportPayload:
    slots: list
    slot_occupied_mask: int
    slot_at_mask: int
    active_pt_preset: int
    active_sl_preset: int
    slider_min_steps: int
    slider_max_steps: int
    zoom_min_steps: int
    zoom_max_steps: int


@dataclass
class LimitsFoundPayload:
    axis: Axis
    min_steps: int
    max_steps: int


@dataclass
class AckPayload:
    acked_seq: int


@dataclass
class NackPayload:
    nacked_seq: int
    error: NackError


def decode_status(payload: bytes) -> StatusPayload:
    if len(payload) < 9:
        raise ParseError(f"STATUS payload too short: {len(payload)} bytes")

    # ">BBBBHHB" = state, flags, pt_preset, sl_preset, occupied(16), at(16), target
    fields = struct.unpack(">BBBBHHB", payload[:9])

    # Byte [9] (added in v2): active look-at subject (0-7, 0xFF = none).
    # Track presence separately — a short STATUS means "not reported", which
    # callers must not confuse with "reported as none".
    has_la_subject    = len(payload) >= 10
    active_la_subject = payload[9] if has_la_subject else 0xFF

    return StatusPayload(
        state              = MountState(fields[0]),
        flags              = fields[1],
        active_pt_preset   = fields[2],
        active_sl_preset   = fields[3],
        slot_occupied_mask = fields[4],
        slot_at_mask       = fields[5],
        target_slot        = fields[6],
        active_la_subject  = active_la_subject,
        la_subject_present = has_la_subject,
    )


def decode_state_report(payload: bytes) -> StateReportPayload:
    """Decode CMD_STATE_REPORT.

    Accepts two payload sizes:
      • 6 bytes  — lightweight format sent by current Teensy firmware:
                   occupied(H) | slot_at(H) | pt_preset(B) | sl_preset(B)
                   Slot position data and limits are not transmitted; the PC
                   app retains whatever values it already holds for this mount.
      • 182 bytes — full format: 10 × (pan,tilt,slider,zoom) int32 +
                   masks + presets + limits.
    """
    if len(payload) < 6:
        raise ParseError(f"STATE_REPORT payload too short: {len(payload)}")

    if len(payload) < STATE_REPORT_PAYLOAD_LEN:
        # Lightweight 6-byte format from Teensy firmware.
        occupied, at_mask = struct.unpack(">HH", payload[0:4])
        pt_preset = payload[4]
        sl_preset = payload[5]
        return StateReportPayload(
            slots=[None] * NUM_SLOTS,   # positions not transmitted
            slot_occupied_mask=occupied,
            slot_at_mask=at_mask,
            active_pt_preset=pt_preset,
            active_sl_preset=sl_preset,
            slider_min_steps=0,
            slider_max_steps=0,
            zoom_min_steps=0,
            zoom_max_steps=0,
        )

    # Full 182-byte format.
    slots = []
    for i in range(NUM_SLOTS):
        off = i * 16
        pan, tilt, slider, zoom = struct.unpack(">iiii", payload[off:off + 16])
        slots.append((pan, tilt, slider, zoom))
    occupied, at_mask = struct.unpack(">HH", payload[160:164])
    pt_preset = payload[164]
    sl_preset = payload[165]
    sl_min, sl_max, zm_min, zm_max = struct.unpack(">iiii", payload[166:182])
    return StateReportPayload(
        slots=slots,
        slot_occupied_mask=occupied,
        slot_at_mask=at_mask,
        active_pt_preset=pt_preset,
        active_sl_preset=sl_preset,
        slider_min_steps=sl_min,
        slider_max_steps=sl_max,
        zoom_min_steps=zm_min,
        zoom_max_steps=zm_max,
    )


@dataclass
class ConfigReportPayload:
    """Decoded CMD_CONFIG_REPORT (75-byte payload)."""
    pan_invert:    bool
    tilt_invert:   bool
    slider_invert: bool
    has_slider:    bool
    zoom_invert:   bool
    lanc_zoom:     bool
    # Speed presets in physical units (deg/s or mm/s, deg/s² or mm/s²)
    pt_presets: list  # list of 4 (max_speed, accel) tuples, index 0 = preset 1
    sl_presets: list  # list of 4 (max_speed, accel) tuples
    zm_preset:  tuple # (max_speed, accel)
    # StallGuard thresholds stored on the mount (bytes 73-74; 0 = unknown/old firmware)
    stall_threshold_slider: int = 0
    stall_threshold_zoom:   int = 0
    look_at_mode:           bool = False


def decode_config_report(payload: bytes) -> ConfigReportPayload:
    """
    Decode CMD_CONFIG_REPORT (75-byte payload, 73-byte accepted for old firmware).
    Layout:
      [0]       orientation flags (bit0=pan_inv, bit1=slider_inv, bit2=has_slider, bit3=zoom_inv, bit4=lanc_zoom, bit5=tilt_inv)
      [1..32]   4 × PT preset: uint32 max_speed + uint32 accel (presets 1-4)
      [33..64]  4 × SL preset: uint32 max_speed + uint32 accel
      [65..72]  1 × ZM preset: uint32 max_speed + uint32 accel
      [73]      stall_threshold[AXIS_SLIDER]
      [74]      stall_threshold[AXIS_ZOOM]
    """
    if len(payload) < 73:
        raise ParseError(f"CONFIG_REPORT payload too short: {len(payload)}")
    ori = payload[0]
    pan_invert    = bool(ori & 0x01)
    slider_invert = bool(ori & 0x02)
    has_slider    = bool(ori & 0x04)
    zoom_invert   = bool(ori & 0x08)
    lanc_zoom     = bool(ori & 0x10)
    tilt_invert   = bool(ori & 0x20)
    look_at_mode  = bool(ori & 0x40)
    pt_presets = []
    for p in range(4):
        off = 1 + p * 8
        spd, acc = struct.unpack(">II", payload[off:off + 8])
        pt_presets.append((spd, acc))
    sl_presets = []
    for p in range(4):
        off = 33 + p * 8
        spd, acc = struct.unpack(">II", payload[off:off + 8])
        sl_presets.append((spd, acc))
    zm_spd, zm_acc = struct.unpack(">II", payload[65:73])
    sg_slider = payload[73] if len(payload) >= 75 else 0
    sg_zoom   = payload[74] if len(payload) >= 75 else 0
    return ConfigReportPayload(
        pan_invert=pan_invert,
        tilt_invert=tilt_invert,
        slider_invert=slider_invert,
        has_slider=has_slider,
        zoom_invert=zoom_invert,
        lanc_zoom=lanc_zoom,
        look_at_mode=look_at_mode,
        pt_presets=pt_presets,
        sl_presets=sl_presets,
        zm_preset=(zm_spd, zm_acc),
        stall_threshold_slider=sg_slider,
        stall_threshold_zoom=sg_zoom,
    )


def decode_limits_found(payload: bytes) -> LimitsFoundPayload:
    axis_raw, min_steps, max_steps = struct.unpack(">Bii", payload[:9])
    return LimitsFoundPayload(axis=Axis(axis_raw), min_steps=min_steps, max_steps=max_steps)


def decode_home_complete(payload: bytes) -> Axis:
    """Decode CMD_HOME_COMPLETE — returns the axis that finished homing."""
    return Axis(payload[0])


@dataclass
class HealthPayload:
    """Decoded CMD_HEALTH (24-byte payload) — uniform node health record."""
    node_type:     int    # 0=hub 1=bridge 2=teensy 3=display 4=sat (HEALTH_NODE_NAMES)
    reset_reason:  int
    uptime_s:      int
    free_heap:     int
    min_free_heap: int
    loop_max_ms:   int    # worst loop/task iteration since last report
    tx_fail:       int    # cumulative link send failures (wraps)
    rssi:          int
    flags:         int    # HEALTH_FLAG_* bits
    node_u32:      int    # node-specific counter (hub=ghost drops, bridge=reinits)

    @property
    def anomaly(self) -> bool:
        return bool(self.flags & 0x01)

    @property
    def node_name(self) -> str:
        return HEALTH_NODE_NAMES.get(self.node_type, f"type{self.node_type}")


def decode_health(payload: bytes) -> HealthPayload:
    """Decode CMD_HEALTH (24-byte payload)."""
    if len(payload) < 24:
        raise ParseError(f"HEALTH payload too short: {len(payload)}")
    (node_type, reset_reason, uptime_s, free_heap, min_free,
     loop_max_ms, tx_fail, rssi, flags, node_u32) = struct.unpack(
        ">BBIIIHHbBI", payload[:24])
    return HealthPayload(
        node_type=node_type, reset_reason=reset_reason, uptime_s=uptime_s,
        free_heap=free_heap, min_free_heap=min_free, loop_max_ms=loop_max_ms,
        tx_fail=tx_fail, rssi=rssi, flags=flags, node_u32=node_u32)


@dataclass
class PositionPayload:
    """Decoded CMD_POSITION (17-byte payload) — live positions, physical units."""
    pan_deg:     float
    tilt_deg:    float
    slider_mm:   float   # 0.0 when the mount has no slider
    zoom_steps:  int     # raw steps; meaningless for LANC zoom
    moving_mask: int     # bit per axis (0=pan 1=tilt 2=slider 3=zoom)

    def axis_moving(self, axis: "Axis") -> bool:
        return bool(self.moving_mask & (1 << int(axis)))

    @property
    def any_moving(self) -> bool:
        return self.moving_mask != 0


def decode_position(payload: bytes) -> PositionPayload:
    """Decode CMD_POSITION (17-byte payload)."""
    if len(payload) < 17:
        raise ParseError(f"POSITION payload too short: {len(payload)}")
    pan, tilt, slider, zoom, mask = struct.unpack(">fffiB", payload[:17])
    return PositionPayload(pan_deg=pan, tilt_deg=tilt, slider_mm=slider,
                           zoom_steps=zoom, moving_mask=mask)


def pkt_get_position(mount_id: int) -> bytes:
    """Request an immediate CMD_POSITION reply from a mount."""
    return build_packet(mount_id, Cmd.GET_POSITION)


def decode_ack(payload: bytes) -> AckPayload:
    seq, = struct.unpack(">H", payload[:2])
    return AckPayload(acked_seq=seq)


def decode_nack(payload: bytes) -> NackPayload:
    seq, error_raw = struct.unpack(">HB", payload[:3])
    return NackPayload(nacked_seq=seq, error=NackError(error_raw))


def decode_pong(payload: bytes) -> int:
    ts, = struct.unpack(">I", payload[:4])
    return ts


# ---------------------------------------------------------------------------
# Convenience packet constructors
# ---------------------------------------------------------------------------

def pkt_jog(mount_id: int, pan_vel: int, tilt_vel: int,
            slider_vel: int, zoom_vel: int,
            pt_preset: int = 2, sz_preset: int = 2,
            axis_mask: int = 0x0F) -> bytes:
    """Sends a relative velocity command (joystick movement)."""
    return build_packet(mount_id, Cmd.JOG,
                        encode_jog(pan_vel, tilt_vel, slider_vel, zoom_vel,
                                   pt_preset, sz_preset, axis_mask))

def pkt_goto(mount_id: int, pan: int, tilt: int, slider: int, zoom: int,
             speed_preset: int) -> bytes:
    return build_packet(mount_id, Cmd.GOTO,
                        encode_goto(pan, tilt, slider, zoom, speed_preset))


def pkt_move_rel(mount_id: int, d_pan: int, d_tilt: int, d_slider: int, d_zoom: int,
                 speed_preset: int = 2) -> bytes:
    return build_packet(mount_id, Cmd.MOVE_REL,
                        encode_move_rel(d_pan, d_tilt, d_slider, d_zoom, speed_preset))

def pkt_save_pos(mount_id: int, slot: int) -> bytes:
    return build_packet(mount_id, Cmd.SAVE_POS, encode_save_pos(slot))

def pkt_set_speed_preset(mount_id: int, axis_group: AxisGroup, preset: int,
                          max_speed: int, accel: int) -> bytes:
    return build_packet(mount_id, Cmd.SET_SPEED_PRESET,
                        encode_set_speed_preset(axis_group, preset, max_speed, accel))

def pkt_set_limits(mount_id: int, axis: Axis, min_steps: int, max_steps: int) -> bytes:
    return build_packet(mount_id, Cmd.SET_LIMITS, encode_set_limits(axis, min_steps, max_steps))

def pkt_find_limits(mount_id: int, axis: Axis, stall_threshold: int = 80) -> bytes:
    return build_packet(mount_id, Cmd.FIND_LIMITS, encode_find_limits(axis, stall_threshold))

def pkt_find_home(mount_id: int, axis: Axis, stall_threshold: int = 80) -> bytes:
    return build_packet(mount_id, Cmd.FIND_HOME, encode_find_home(axis, stall_threshold))

def pkt_set_stall_threshold(mount_id: int, axis: Axis, threshold: int) -> bytes:
    return build_packet(mount_id, Cmd.SET_STALL_THRESHOLD,
                        struct.pack(">BB", int(axis), max(1, min(255, threshold))))

def pkt_set_orientation(mount_id: int, pan_invert: bool, slider_invert: bool,
                        has_slider: bool = True,
                        zoom_invert: bool = False,
                        lanc_zoom: bool = False,
                        tilt_invert: bool = False,
                        look_at_mode: bool = False) -> bytes:
    return build_packet(mount_id, Cmd.SET_ORIENTATION,
                        encode_set_orientation(pan_invert, slider_invert,
                                               has_slider, zoom_invert, lanc_zoom,
                                               tilt_invert, look_at_mode))

def pkt_e_stop(mount_id: int = MOUNT_BROADCAST) -> bytes:
    return build_packet(mount_id, Cmd.E_STOP)

def pkt_get_status(mount_id: int) -> bytes:
    return build_packet(mount_id, Cmd.GET_STATUS)

def pkt_ping(mount_id: int = MOUNT_BROADCAST) -> bytes:
    import time
    return build_packet(mount_id, Cmd.PING, encode_ping(int(time.monotonic() * 1000)))


def encode_store_pos(slot: int) -> bytes:
    return struct.pack(">B", slot & 0xFF)


def encode_clear_pos(slot: int) -> bytes:
    return struct.pack(">B", slot & 0xFF)


def encode_set_active_preset(axis_group: AxisGroup, preset: int) -> bytes:
    return struct.pack(">BB", int(axis_group), max(1, min(4, preset)))


def encode_save_speeds(pt_presets: list[tuple[int, int]],
                       sl_presets: list[tuple[int, int]],
                       zm_preset:  tuple[int, int]) -> bytes:
    """
    Build 72-byte CMD_SAVE_SPEEDS payload.

    pt_presets: list of 4 (max_speed, accel) tuples for presets 1-4
    sl_presets: list of 4 (max_speed, accel) tuples for presets 1-4
    zm_preset:  single (max_speed, accel) tuple
    """
    out = b""
    for spd, acc in pt_presets[:4]:
        out += struct.pack(">II", spd, acc)
    for spd, acc in sl_presets[:4]:
        out += struct.pack(">II", spd, acc)
    spd, acc = zm_preset
    out += struct.pack(">II", spd, acc)
    return out


def encode_goto_slot(slot: int, pt_preset: int = 2, sl_preset: int = 2,
                     axis_mask: int = 0x0F) -> bytes:
    """axis_mask 0x0F = all axes (default).  0x04 = slider only (CV-tracking mode).
    pt_preset and sl_preset are sent separately so each axis group respects its
    own active speed — e.g. slider on speed 2 won't be driven at pan/tilt speed 4."""
    base = struct.pack(">BBB", slot & 0xFF,
                       max(1, min(4, pt_preset)),
                       max(1, min(4, sl_preset)))
    if axis_mask != 0x0F:
        return base + struct.pack("B", axis_mask & 0xFF)
    return base


def pkt_goto_slot(mount_id: int, slot: int, pt_preset: int = 2, sl_preset: int = 2,
                  axis_mask: int = 0x0F) -> bytes:
    return build_packet(mount_id, Cmd.GOTO_SLOT,
                        encode_goto_slot(slot, pt_preset, sl_preset, axis_mask))


def pkt_get_state(mount_id: int) -> bytes:
    return build_packet(mount_id, Cmd.GET_STATE)

def pkt_store_pos(mount_id: int, slot: int) -> bytes:
    return build_packet(mount_id, Cmd.STORE_POS, encode_store_pos(slot))

def pkt_clear_pos(mount_id: int, slot: int) -> bytes:
    return build_packet(mount_id, Cmd.CLEAR_POS, encode_clear_pos(slot))

def pkt_set_active_preset(mount_id: int, axis_group: AxisGroup, preset: int) -> bytes:
    return build_packet(mount_id, Cmd.SET_ACTIVE_PRESET,
                        encode_set_active_preset(axis_group, preset))

def pkt_get_config(mount_id: int) -> bytes:
    """Request configuration (speed presets + orientation) from a mount."""
    return build_packet(mount_id, Cmd.GET_CONFIG)


# ── Blackmagic camera control, relayed by the mount over BLE ────────────────
# Commands are built HERE, not on the mount.  The mount writes whatever arrives
# straight to the camera's control characteristic without parsing it, so adding
# a camera function is a change to this file alone — no firmware, nothing to
# keep in sync across three codebases.
#
# Wire format is Blackmagic's own, from the camera manual's developer section:
#
#   [0] destination   255 = broadcast, i.e. the camera on that mount
#   [1] length        bytes of command data after this 4-byte header
#   [2] command id    0 = change configuration
#   [3] reserved
#   [4] category      0 = lens
#   [5] parameter
#   [6] data type
#   [7] operation     0 = assign
#   [8+] data, padded to a 4-byte boundary
def bmd_command(category: int, parameter: int, data_type: int = 0,
                operation: int = 0, data: bytes = b"") -> bytes:
    body = bytes([category, parameter, data_type, operation]) + data
    while len(body) % 4:
        body += b"\x00"
    return bytes([0xFF, len(body), 0x00, 0x00]) + body


def pkt_cam_autofocus(mount_id: int) -> bytes:
    """Instantaneous autofocus — lens category, parameter 1.

    Byte-for-byte what schoolpost/BlueMagic32 sends and is known to work on a
    Pocket Cinema Camera 4K:  FF 04 00 00 00 01 01 00 00 00 00 00
    """
    cmd = bmd_command(category=0, parameter=1, data_type=1) + b"\x00\x00\x00\x00"
    return build_packet(mount_id, Cmd.CAM_CONTROL, cmd)


def pkt_cam_iso(mount_id: int, iso: int) -> bytes:
    """Sensor ISO — video category, parameter 14, int32.

    Blackmagic calls this ISO and the camera displays it as ISO; the operator
    calls it gain.  The wire value is the ISO number itself (400, 1250, ...).
    """
    data = int(iso).to_bytes(4, "little", signed=True)
    return build_packet(mount_id, Cmd.CAM_CONTROL,
                        bmd_command(category=1, parameter=14, data_type=3, data=data))


def pkt_cam_white_balance(mount_id: int, kelvin: int, tint: int = 0) -> bytes:
    """Manual white balance — video category, parameter 2, two int16s.

    Tint travels with the temperature in the same command, so it has to be sent
    even when only the temperature is changing; passing the camera's last
    reported tint keeps it where the operator left it.
    """
    data = (int(kelvin).to_bytes(2, "little", signed=True)
            + int(tint).to_bytes(2, "little", signed=True))
    return build_packet(mount_id, Cmd.CAM_CONTROL,
                        bmd_command(category=1, parameter=2, data_type=2, data=data))


# ── Decoding what the camera reports ────────────────────────────────────────
# CMD_CAM_STATUS carries a Blackmagic status message verbatim, in the same
# framing as a command:
#
#   [4] category  [5] parameter  [6] type  [7] operation  [8+] data
#
# Returns {"iso": n} / {"white_balance": k, "tint": t} / {} for anything not
# understood — an unknown parameter is normal traffic, not an error, because
# the camera reports everything it feels like reporting.
def decode_cam_status(payload: bytes) -> dict:
    if len(payload) < 8:
        return {}
    category, parameter = payload[4], payload[5]
    data = payload[8:]
    if category == 1 and parameter == 14 and len(data) >= 2:      # ISO
        return {"iso": int.from_bytes(data[:4].ljust(4, b"\x00"), "little", signed=True)}
    if category == 1 and parameter == 2 and len(data) >= 4:       # white balance
        return {"white_balance": int.from_bytes(data[0:2], "little", signed=True),
                "tint":          int.from_bytes(data[2:4], "little", signed=True)}
    return {}


def pkt_save_speeds(mount_id: int,
                    pt_presets: list[tuple[int, int]],
                    sl_presets: list[tuple[int, int]],
                    zm_preset:  tuple[int, int]) -> bytes:
    return build_packet(mount_id, Cmd.SAVE_SPEEDS,
                        encode_save_speeds(pt_presets, sl_presets, zm_preset))


# ---------------------------------------------------------------------------
# v2 — Payload dataclasses  (decoded Mount → PC packets)
# ---------------------------------------------------------------------------

@dataclass
class SubjectRecord:
    """One entry from CMD_SUBJECT_LIST."""
    subject_id:  int          # 0–7 (position in array)
    valid:       bool
    name:        str          # up to 16 chars
    x_mm:        float = 0.0
    y_mm:        float = 0.0
    z_mm:        float = 0.0


@dataclass
class LookAtStatusPayload:
    """Decoded CMD_LOOK_AT_STATUS (14-byte payload)."""
    slider_pos_mm: float
    pan_deg:       float
    tilt_deg:      float
    subject_id:    int
    flags:         int   # bitmask — FLAG_REF_SET, FLAG_LOOK_AT_ACTIVE

    @property
    def ref_set(self) -> bool:
        return bool(self.flags & MountFlag.REF_SET)

    @property
    def look_at_active(self) -> bool:
        return bool(self.flags & MountFlag.LOOK_AT_ACTIVE)


@dataclass
class RefConfirmedPayload:
    """Decoded CMD_REF_CONFIRMED (8-byte payload)."""
    pan_deg:  float
    tilt_deg: float


# ---------------------------------------------------------------------------
# v2 — Encoders  (PC → Mount)
# ---------------------------------------------------------------------------

def encode_add_subject_start(subject_id: int, name: str) -> bytes:
    """17 bytes: subject_id(1) + name(16, null-padded)."""
    name_bytes = name.encode("utf-8")[:SUBJECT_NAME_LEN]
    name_bytes = name_bytes.ljust(SUBJECT_NAME_LEN, b"\x00")
    return struct.pack(">B", subject_id & 0x07) + name_bytes


def encode_delete_subject(subject_id: int) -> bytes:
    return struct.pack(">B", subject_id & 0x07)


def encode_set_ref(subject_id: int) -> bytes:
    # 0xFF = manual ref (sentinel) — must not be masked to 0x07
    val = 0xFF if subject_id == 0xFF else (subject_id & 0x07)
    return struct.pack(">B", val)


def encode_set_slider_move(slot: int, start_mm: float, end_mm: float,
                           speed_preset: int) -> bytes:
    """10 bytes: slot(1) + start_mm(4f BE) + end_mm(4f BE) + preset(1)."""
    return struct.pack(">BffB",
                       slot & 0x07,
                       start_mm,
                       end_mm,
                       max(1, min(4, speed_preset)))


def encode_switch_subject(subject_id: int) -> bytes:
    return struct.pack(">B", subject_id & 0x07)


# ---------------------------------------------------------------------------
# v2 — Decoders  (Mount → PC)
# ---------------------------------------------------------------------------

def decode_subject_list(payload: bytes) -> list[SubjectRecord]:
    """
    Decode CMD_SUBJECT_LIST (232-byte payload).
    Returns a list of MAX_SUBJECTS SubjectRecord objects.
    """
    if len(payload) < SUBJECT_LIST_PAYLOAD_LEN:
        raise ParseError(f"SUBJECT_LIST payload too short: {len(payload)} bytes, need {SUBJECT_LIST_PAYLOAD_LEN}")
    records = []
    for i in range(MAX_SUBJECTS):
        off = i * SUBJECT_RECORD_LEN
        valid = bool(payload[off])
        name_bytes = payload[off + 1: off + 1 + SUBJECT_NAME_LEN]
        name = name_bytes.rstrip(b"\x00").decode("utf-8", errors="replace")
        x_mm, y_mm, z_mm = struct.unpack(">fff", payload[off + 17: off + 29])
        records.append(SubjectRecord(
            subject_id=i,
            valid=valid,
            name=name,
            x_mm=x_mm,
            y_mm=y_mm,
            z_mm=z_mm,
        ))
    return records


def decode_look_at_status(payload: bytes) -> LookAtStatusPayload:
    """Decode CMD_LOOK_AT_STATUS (14-byte payload)."""
    if len(payload) < 14:
        raise ParseError(f"LOOK_AT_STATUS payload too short: {len(payload)}")
    slider_mm, pan_deg, tilt_deg = struct.unpack(">fff", payload[0:12])
    subject_id = payload[12]
    flags = payload[13]
    return LookAtStatusPayload(
        slider_pos_mm=slider_mm,
        pan_deg=pan_deg,
        tilt_deg=tilt_deg,
        subject_id=subject_id,
        flags=flags,
    )


def decode_ref_confirmed(payload: bytes) -> RefConfirmedPayload:
    """Decode CMD_REF_CONFIRMED (8-byte payload)."""
    if len(payload) < 8:
        raise ParseError(f"REF_CONFIRMED payload too short: {len(payload)}")
    pan_deg, tilt_deg = struct.unpack(">ff", payload[0:8])
    return RefConfirmedPayload(pan_deg=pan_deg, tilt_deg=tilt_deg)


def decode_calib_prompt(payload: bytes) -> CalibPrompt:
    """Decode CMD_CALIB_PROMPT (1-byte payload)."""
    if len(payload) < 1:
        raise ParseError("CALIB_PROMPT payload empty")
    return CalibPrompt(payload[0])


# ---------------------------------------------------------------------------
# v2 — Convenience packet constructors
# ---------------------------------------------------------------------------

def pkt_add_subject_start(mount_id: int, subject_id: int, name: str) -> bytes:
    """Begin 2-point subject calibration for slot subject_id."""
    return build_packet(mount_id, Cmd.ADD_SUBJECT_START,
                        encode_add_subject_start(subject_id, name))


def pkt_add_subject_set_a(mount_id: int) -> bytes:
    """Record observation A (slider at home, pan/tilt at current position)."""
    return build_packet(mount_id, Cmd.ADD_SUBJECT_SET_A)


def pkt_add_subject_set_b(mount_id: int) -> bytes:
    """Record observation B (slider at max, pan/tilt at current position)."""
    return build_packet(mount_id, Cmd.ADD_SUBJECT_SET_B)


def pkt_add_subject_abort(mount_id: int) -> bytes:
    """Abort an in-progress subject calibration."""
    return build_packet(mount_id, Cmd.ADD_SUBJECT_ABORT)


def pkt_delete_subject(mount_id: int, subject_id: int) -> bytes:
    return build_packet(mount_id, Cmd.DELETE_SUBJECT,
                        encode_delete_subject(subject_id))


def pkt_set_ref(mount_id: int, subject_id: int) -> bytes:
    """Set pan/tilt session reference by aiming at a known subject."""
    return build_packet(mount_id, Cmd.SET_REF, encode_set_ref(subject_id))


def pkt_set_slider_move(mount_id: int, slot: int, start_mm: float,
                         end_mm: float, speed_preset: int) -> bytes:
    return build_packet(mount_id, Cmd.SET_SLIDER_MOVE,
                        encode_set_slider_move(slot, start_mm, end_mm, speed_preset))


def pkt_start_look_at_move(mount_id: int, subject_id: int,
                            direction: int, speed_preset: int = 2) -> bytes:
    """3 bytes: subject_id(1) + direction(1) + speed_preset(1).
    direction: 0 = go to min/left limit, 1 = go to max/right limit.
    speed_preset: 1–4."""
    payload = bytes([subject_id & 0x07, direction & 0x01, max(1, min(4, speed_preset))])
    return build_packet(mount_id, Cmd.START_LOOK_AT_MOVE, payload)


def pkt_switch_subject(mount_id: int, subject_id: int) -> bytes:
    """Switch tracking target while a look-at move is running."""
    return build_packet(mount_id, Cmd.SWITCH_SUBJECT,
                        encode_switch_subject(subject_id))


def pkt_get_subjects(mount_id: int) -> bytes:
    """Request the full subject list from the mount."""
    return build_packet(mount_id, Cmd.GET_SUBJECTS)


# ---------------------------------------------------------------------------
# Pairing management — the hub owns the mount table; clients view/set/clear it.
# Hub-scoped, so these carry the hub sentinel mount_id (like HUB_RESTART).
# ---------------------------------------------------------------------------
HUB_SENTINEL              = 0xFE
MOUNT_TABLE_PAYLOAD_LEN   = 30   # 5 × MAC(6)
PAIR_CONFLICT_PAYLOAD_LEN = 13   # cam(1) + new_mac(6) + old_mac(6)
MOUNT_ROUTE_PAYLOAD_LEN   = 5    # one byte per cam
SAT_NAME_LEN              = 13   # 12 characters + NUL, as in the AP SSID
SAT_SLOTS                 = 6
SAT_NAMES_PAYLOAD_LEN     = SAT_SLOTS * SAT_NAME_LEN
MOUNT_EVENT_PAYLOAD_LEN   = 13
# Satellite health arrives addressed SAT_ADDR_BASE + slot (1-based): the
# satellite cannot know its own slot, so the hub stamps it on the way past.
SAT_ADDR_BASE             = 0xF0
MOUNT_EVENT_ISOLATED      = 1
CAM_CONTROL_MAX_LEN     = 40   # longest BMD command we relay


def pkt_get_mount_table() -> bytes:
    """Ask the hub to push its current CMD_MOUNT_TABLE."""
    return build_packet(HUB_SENTINEL, Cmd.GET_MOUNT_TABLE)


def pkt_pair_decide(cam: int, decision: int, mac: bytes) -> bytes:
    """Resolve a pairing conflict on cam 1-5.  decision: 1 = replace (bind the
    new device to this slot), 0 = ignore (keep the current one)."""
    payload = bytes([cam & 0xFF, decision & 0x01]) + bytes(mac[:6]).ljust(6, b"\x00")
    return build_packet(HUB_SENTINEL, Cmd.PAIR_DECIDE, payload)


def pkt_pair_forget(cam: int) -> bytes:
    """Clear (unbind) the pairing for cam 1-5."""
    return build_packet(HUB_SENTINEL, Cmd.PAIR_FORGET, bytes([cam & 0xFF]))


def decode_mount_table(payload: bytes) -> list[bytes]:
    """CMD_MOUNT_TABLE → 5 × 6-byte MAC; an all-zero entry means unbound."""
    if len(payload) < MOUNT_TABLE_PAYLOAD_LEN:
        raise ParseError(f"MOUNT_TABLE payload too short: {len(payload)}")
    return [bytes(payload[i * 6:i * 6 + 6]) for i in range(NUM_MOUNTS)]


def decode_mount_route(payload: bytes) -> list[int]:
    """CMD_MOUNT_ROUTE → 5 ints: 0 = the hub reaches that cam on its own radio,
    N = relayed by satellite N.

    Worth surfacing because it is the missing half of RSSI.  Signal strength is
    measured wherever the frame actually arrived, so a mount reads -40 when a
    satellite is beside it and -85 when that satellite drops and it falls back
    to the hub — the same mount, unmoved, with no other indication why."""
    if len(payload) < MOUNT_ROUTE_PAYLOAD_LEN:
        raise ParseError(f"MOUNT_ROUTE payload too short: {len(payload)}")
    return [int(payload[i]) for i in range(NUM_MOUNTS)]


def decode_sat_names(payload: bytes) -> dict[int, str]:
    """CMD_SAT_NAMES → {slot number (1-based, as CMD_MOUNT_ROUTE reports): name}.

    Only slots that actually gave a name appear.  An empty string means the slot
    is unoccupied, or holds a satellite too old to introduce itself; both want
    the caller to fall back to the bare slot number rather than invent a label.
    """
    if len(payload) < SAT_NAMES_PAYLOAD_LEN:
        raise ParseError(f"SAT_NAMES payload too short: {len(payload)}")
    out: dict[int, str] = {}
    for i in range(SAT_SLOTS):
        raw = bytes(payload[i * SAT_NAME_LEN:(i + 1) * SAT_NAME_LEN])
        name = raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
        if name:
            out[i + 1] = name
    return out


@dataclass
class PairConflictPayload:
    cam: int        # camera 1-5, or 0 = dismiss the prompt
    new_mac: bytes  # the device now claiming the slot
    old_mac: bytes  # the device currently bound to it


def decode_pair_conflict(payload: bytes) -> PairConflictPayload:
    """CMD_PAIR_CONFLICT → (cam, new_mac, old_mac); cam 0 dismisses."""
    if len(payload) < PAIR_CONFLICT_PAYLOAD_LEN:
        raise ParseError(f"PAIR_CONFLICT payload too short: {len(payload)}")
    return PairConflictPayload(cam=payload[0],
                               new_mac=bytes(payload[1:7]),
                               old_mac=bytes(payload[7:13]))
