#!/usr/bin/env python3
"""
mount_sim.py — hub + 5-mount simulator for developing the PC app with no hardware.

Speaks the real wire protocol (imports pc_app/comms/protocol.py — the same
implementation the golden test locks against the firmware) and behaves like
the hub's TCP server with five mounts behind it.

USAGE
    python3 tools/mount_sim.py                 # listen on 0.0.0.0:7777
    python3 tools/mount_sim.py --port 7778
    python3 tools/mount_sim.py --selftest      # automated smoke test, exit 0/1

Point the PC app at it:  Config → connection host 127.0.0.1 (TCP) → OK.
The web app cannot connect (it needs the hub's WebSocket), but every PC-app
feature works: jog, slots, presets, homing, config, look-at, health telemetry.

WHAT IS SIMULATED
    - 10-byte STATUS at 50 Hz per mount (state, flags, presets, slot masks,
      target slot, active look-at subject)
    - ACK for every command (NACK on bad input / forced faults / NO_REF)
    - jog with 500 ms dead-man (mirrors the Teensy watchdog), slot store /
      clear / goto with real travel time, AT-slot detection
    - homing (FIND_LIMITS / FIND_HOME → LIMITS_FOUND + HOME_COMPLETE)
    - GET_STATE → 6-byte STATE_REPORT, GET_CONFIG → 75-byte CONFIG_REPORT
    - look-at v2: subject calibration (CALIB_PROMPT sequence), SUBJECT_LIST,
      SET_REF → REF_CONFIRMED, START_LOOK_AT_MOVE with PRE_AIM → LOOK_AT_MOVE,
      20 Hz LOOK_AT_STATUS, hub-injected CMD_LA_MOVE_DIR, SWITCH_SUBJECT
    - CMD_HEALTH from every simulated node (bridge + teensy per mount, hub,
      display) every 10 s, HUB_DIAG at 1 Hz
    - hub recovery commands: CMD_HUB_REINIT_ESPNOW / CMD_HUB_RESTART

FAULT CONSOLE (stdin)
    kill N / revive N   mount N vanishes (no STATUS/ACK) / returns
    wedge / unwedge     swallow client→mount commands while STATUS keeps
                        flowing — the June-2026 hub TX-wedge signature; watch
                        the PC app detect, escalate, and recover
    nack N CODE         force next command to mount N to NACK
                        (codes: crc len cmd param busy noref)
    drop P              drop STATUS with probability P (0-1), e.g. drop 0.3
    conflict [N]        raise a pairing conflict on cam N (default 1): a new
                        device claims a bound slot — resolve with Replace/Ignore
                        from any surface (web app / PC app / hub display)
    table               print the simulated paired-mount table
    state               print the simulator state table
    help / quit
"""
from __future__ import annotations

import argparse
import math
import random
import socket
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pc_app"))
from comms.protocol import (  # noqa: E402
    Cmd, MountState, MountFlag, NackError, CalibPrompt, AxisGroup,
    PacketReader, build_packet, NUM_MOUNTS, MAX_SUBJECTS, SUBJECT_NAME_LEN,
    SUBJECT_RECORD_LEN, SUBJECT_LIST_PAYLOAD_LEN,
)

STATUS_HZ        = 50
LA_STATUS_HZ     = 20
JOG_WATCHDOG_S   = 0.5
SLIDER_MAX_STEPS = 100_000          # 1000 mm at 100 steps/mm
STEPS_PER_MM     = 100
STEPS_PER_DEG    = 100
ZOOM_MAX_STEPS   = 20_000
SLOT_AT_TOL      = 50               # steps
PT_MAX_DEG_S     = [6, 12, 20, 35]  # per preset 1-4
SL_MAX_MM_S      = [10, 25, 45, 80]

NACK_CODES = {"crc": NackError.BAD_CRC, "len": NackError.BAD_LENGTH,
              "cmd": NackError.UNKNOWN_CMD, "param": NackError.INVALID_PARAM,
              "busy": NackError.BUSY, "noref": NackError.NO_REF}


def now() -> float:
    return time.monotonic()


class SimMount:
    """One mount: bridge + teensy behaviour behind the simulated hub."""

    def __init__(self, mid: int):
        self.id          = mid
        self.alive       = True
        self.has_slider  = mid in (3, 6 - 3)          # mounts 3 — tweak freely
        self.look_at     = False
        self.state       = MountState.IDLE
        self.pos         = [0.0, 0.0, 0.0, 0.0]       # pan/tilt/slider/zoom steps
                                                       # (floats: slow jogs must
                                                       # accumulate sub-step motion)
        self.vel         = [0.0, 0.0, 0.0, 0.0]       # steps/s
        self.pt_preset   = 2
        self.sl_preset   = 2
        self.slots       = {}                          # slot → pos[4]
        self.target_slot = 0xFF
        self.goto_tgt    = None                        # pos[4]
        self.move_preset = None                        # GOTO/MOVE_REL travel preset
        self.jog_last    = 0.0
        self.limits_set  = True
        self.ref_set     = False
        self.subjects    = {}                          # id → (name, x, y, z) mm
        self.active_subj = 0xFF
        self.calib       = None                        # (subj_id, name, stage, t_next)
        self.la          = None                        # (direction, end_steps)
        self.force_nack: NackError | None = None
        self.la_end_flags = 0

    # ── masks / flags ────────────────────────────────────────────────────
    def occupied_mask(self) -> int:
        return sum(1 << s for s in self.slots)

    def at_mask(self) -> int:
        m = 0
        for s, p in self.slots.items():
            if all(abs(self.pos[a] - p[a]) <= SLOT_AT_TOL for a in range(4)):
                m |= 1 << s
        return m

    def flags(self) -> int:
        f = 0
        if self.has_slider:  f |= MountFlag.HAS_SLIDER
        if self.limits_set:  f |= MountFlag.LIMITS_SET
        if self.look_at:     f |= MountFlag.LOOK_AT_MODE
        if self.ref_set:     f |= MountFlag.REF_SET
        if self.la:          f |= MountFlag.LOOK_AT_ACTIVE
        if self.has_slider:
            if self.pos[2] <= 0:                f |= MountFlag.AT_MIN_LIMIT
            elif self.pos[2] >= SLIDER_MAX_STEPS: f |= MountFlag.AT_MAX_LIMIT
            else:                               f |= self.la_end_flags
        return f

    # ── physics tick ─────────────────────────────────────────────────────
    def tick(self, dt: float, emit):
        t = now()

        if self.state == MountState.JOGGING:
            if t - self.jog_last > JOG_WATCHDOG_S:
                self.vel = [0.0] * 4                   # Teensy dead-man mirror
                self.state = MountState.IDLE
            for a in range(4):
                self.pos[a] += self.vel[a] * dt

        elif self.state == MountState.MOVING_TO_POS and self.goto_tgt:
            done = True
            ptp = self.move_preset or self.pt_preset
            slp = self.move_preset or self.sl_preset
            spd = [PT_MAX_DEG_S[ptp - 1] * STEPS_PER_DEG] * 2 + \
                  [SL_MAX_MM_S[slp - 1] * STEPS_PER_MM, 4000]
            for a in range(4):
                d = self.goto_tgt[a] - self.pos[a]
                step = spd[a] * dt
                if abs(d) <= step: self.pos[a] = self.goto_tgt[a]
                else:
                    self.pos[a] += math.copysign(step, d); done = False
            if done:
                self.goto_tgt, self.target_slot = None, 0xFF
                self.move_preset = None
                self.state = MountState.IDLE

        elif self.state == MountState.FINDING_LIMITS:
            stage = self.calib          # reused as (axis, t_end) during homing
            if stage and t >= stage[1]:
                axis = stage[0]
                self.calib = None
                self.limits_set = True
                self.state = MountState.IDLE
                mx = SLIDER_MAX_STEPS if axis == 2 else ZOOM_MAX_STEPS
                emit(self.id, Cmd.LIMITS_FOUND, struct.pack(">Bii", axis, 0, mx))
                emit(self.id, Cmd.HOME_COMPLETE, bytes([axis]))

        elif self.state == MountState.CALIBRATING_SUBJECT and self.calib:
            sid, name, stage, t_next = self.calib
            if stage in ("to_a", "to_b") and t >= t_next:
                nxt = CalibPrompt.WAIT_SET_A if stage == "to_a" else CalibPrompt.WAIT_SET_B
                self.calib = (sid, name, "wait_a" if stage == "to_a" else "wait_b", 0)
                emit(self.id, Cmd.CALIB_PROMPT, bytes([nxt]))

        elif self.state in (MountState.LOOK_AT_PRE_AIM, MountState.LOOK_AT_MOVE):
            if self.state == MountState.LOOK_AT_PRE_AIM:
                if self.calib and t >= self.calib[1]:
                    self.calib = None
                    self.state = MountState.LOOK_AT_MOVE
            elif self.la:
                direction, end = self.la
                spd = SL_MAX_MM_S[self.sl_preset - 1] * STEPS_PER_MM
                d = end - self.pos[2]
                step = spd * dt
                if abs(d) <= step:
                    self.pos[2] = end
                    self.la = None
                    self.la_end_flags = (MountFlag.AT_MIN_LIMIT if direction == 0
                                         else MountFlag.AT_MAX_LIMIT)
                    self.state = MountState.IDLE
                    emit(self.id, Cmd.LOOK_AT_STATUS, self.la_status_payload())
                    emit(0, Cmd.LA_MOVE_DIR, bytes([0xFF]), hub_inject=self.id)
                else:
                    self.pos[2] += math.copysign(step, d)

        self.pos[2] = max(0, min(SLIDER_MAX_STEPS, self.pos[2]))
        self.pos[3] = max(0, min(ZOOM_MAX_STEPS, self.pos[3]))

    # ── look-at geometry (matches DESIGN_V2 axes) ────────────────────────
    def la_angles(self):
        if self.active_subj not in self.subjects:
            return 0.0, 0.0
        _, sx, sy, sz = self.subjects[self.active_subj]
        dx = sx - self.pos[2] / STEPS_PER_MM
        pan  = math.degrees(math.atan2(dx, sz))
        tilt = math.degrees(math.atan2(sy, math.hypot(dx, sz)))
        return pan, tilt

    def la_status_payload(self) -> bytes:
        pan, tilt = self.la_angles()
        return struct.pack(">fffBB", self.pos[2] / STEPS_PER_MM, pan, tilt,
                           self.active_subj & 0xFF, self.flags() & 0xFF)

    def subject_list_payload(self) -> bytes:
        out = bytearray()
        for i in range(MAX_SUBJECTS):
            if i in self.subjects:
                name, x, y, z = self.subjects[i]
                nb = name.encode()[:SUBJECT_NAME_LEN].ljust(SUBJECT_NAME_LEN, b"\0")
                out += bytes([1]) + nb + struct.pack(">fff", x, y, z)
            else:
                out += bytes(SUBJECT_RECORD_LEN)
        assert len(out) == SUBJECT_LIST_PAYLOAD_LEN
        return bytes(out)

    def status_payload(self) -> bytes:
        return struct.pack(">BBBBHHBB", int(self.state), self.flags(),
                           self.pt_preset, self.sl_preset,
                           self.occupied_mask(), self.at_mask(),
                           self.target_slot, self.active_subj & 0xFF)

    def position_payload(self) -> bytes:
        moving = 0
        if self.state == MountState.JOGGING:
            for a in range(4):
                if abs(self.vel[a]) > 1: moving |= 1 << a
        elif self.state == MountState.MOVING_TO_POS and self.goto_tgt:
            for a in range(4):
                if self.goto_tgt[a] != self.pos[a]: moving |= 1 << a
        elif self.state in (MountState.LOOK_AT_MOVE, MountState.LOOK_AT_PRE_AIM):
            moving |= 0b0111
        return struct.pack(">fffiB",
                           self.pos[0] / STEPS_PER_DEG,
                           self.pos[1] / STEPS_PER_DEG,
                           self.pos[2] / STEPS_PER_MM,
                           int(self.pos[3]), moving)


class HubSim:
    def __init__(self, port: int, verbose: bool = True):
        self.port     = port
        self.verbose  = verbose
        self.mounts   = {i: SimMount(i) for i in range(1, NUM_MOUNTS + 1)}
        self.clients: list[socket.socket] = []
        self.clk      = threading.Lock()
        self.seq      = 0
        self.wedged   = False
        self.drop_p   = 0.0
        self.rx_bytes = 0
        self.rx_pkts  = 0
        self.reset_reason = 1          # POWERON
        self.t0       = now()
        self.running  = True
        self._srv: socket.socket | None = None
        # give every mount slider+look-at so all UI paths are exercisable
        for m in self.mounts.values():
            m.has_slider = True
        # Paired-mount table (5 × 6-byte MAC; all-zero slot = unbound).  Seeded
        # bound for all 5, matching the alive mounts — the hub owns this table;
        # clients (web/PC/display) view and set/clear it over the protocol.
        self.mount_table = [bytes([0x02, 0, 0, 0, 0, i]) for i in range(1, NUM_MOUNTS + 1)]

    # ── output ───────────────────────────────────────────────────────────
    def log(self, msg: str):
        if self.verbose:
            print(f"[sim] {msg}", flush=True)

    def nseq(self) -> int:
        self.seq = (self.seq + 1) & 0xFFFF
        return self.seq

    def send_raw(self, data: bytes):
        with self.clk:
            for c in list(self.clients):
                try:
                    c.sendall(data)
                except OSError:
                    try: c.close()
                    except OSError: pass
                    self.clients.remove(c)

    def emit(self, mount_id: int, cmd: Cmd, payload: bytes = b"",
             hub_inject: int | None = None):
        mid = hub_inject if hub_inject is not None else mount_id
        self.send_raw(build_packet(mid, cmd, payload, seq=self.nseq()))

    def send_mount_table(self):
        # 30-byte payload = 5 × MAC(6); hub sentinel 0xFE, like the real hub
        self.emit(0xFE, Cmd.MOUNT_TABLE, b"".join(self.mount_table))

    # ── periodic broadcast ────────────────────────────────────────────────
    def ticker(self):
        last_status = last_la = last_diag = last_health = 0.0
        prev = now()
        while self.running:
            time.sleep(1.0 / (STATUS_HZ * 2))
            t = now(); dt = t - prev; prev = t
            for m in self.mounts.values():
                if m.alive:
                    m.tick(dt, self.emit)

            if t - last_status >= 1.0 / STATUS_HZ:
                last_status = t
                for m in self.mounts.values():
                    if m.alive and random.random() >= self.drop_p:
                        self.emit(m.id, Cmd.STATUS, m.status_payload())

            if t - last_la >= 1.0 / LA_STATUS_HZ:
                last_la = t
                for m in self.mounts.values():
                    if m.alive and m.state in (MountState.LOOK_AT_MOVE,
                                               MountState.LOOK_AT_PRE_AIM):
                        self.emit(m.id, Cmd.LOOK_AT_STATUS, m.la_status_payload())

            # Live positions — 5 Hz while a mount moves, 1 Hz at rest
            for m in self.mounts.values():
                if not m.alive:
                    continue
                iv = 0.2 if m.state != MountState.IDLE else 1.0
                if t - getattr(m, "_pos_t", 0.0) >= iv:
                    m._pos_t = t
                    self.emit(m.id, Cmd.POSITION, m.position_payload())

            if t - last_diag >= 1.0:
                last_diag = t
                self.emit(0xFE, Cmd.HUB_DIAG,
                          struct.pack(">IIBI", self.rx_bytes & 0xFFFFFFFF,
                                      self.rx_pkts & 0xFFFFFFFF,
                                      self.reset_reason, 0))

            if t - last_health >= 10.0:
                last_health = t
                up = int(t - self.t0)
                def hp(node, heap, mn, loopmax, txfail, rssi, n32):
                    return struct.pack(">BBIIIHHbBI", node, self.reset_reason,
                                       up, heap, mn, loopmax, txfail, rssi, 0, n32)
                self.emit(0xFE, Cmd.HEALTH, hp(0, 118_000, 102_000, 9, 0, 0, 0))
                self.emit(0xFD, Cmd.HEALTH, hp(3, 96_000, 71_000, 38, 0, 0, 0))
                for m in self.mounts.values():
                    if m.alive:
                        self.emit(m.id, Cmd.HEALTH,
                                  hp(1, 131_000, 117_000, 21, 0, -52 - m.id, 0))
                        self.emit(m.id, Cmd.HEALTH,
                                  hp(2, 289_000, 288_000, 2, 0, 0, 0))

    # ── command handling ──────────────────────────────────────────────────
    def handle(self, pkt):
        self.rx_pkts += 1
        cmd = pkt.cmd

        if cmd == Cmd.HUB_REINIT_ESPNOW:
            self.log("hub: CMD_HUB_REINIT_ESPNOW received "
                     + ("(deep wedge simulated — NOT cured)" if self.wedged else ""))
            return
        if cmd == Cmd.HUB_RESTART:
            self.log("hub: CMD_HUB_RESTART — simulating reboot (drops clients, cures wedge)")
            self.wedged = False
            self.rx_bytes = self.rx_pkts = 0
            self.reset_reason = 3      # SW restart
            with self.clk:
                for c in self.clients:
                    try: c.close()
                    except OSError: pass
                self.clients.clear()
            return

        # ── Pairing management (hub-scoped; works even while wedged) ──────────
        if cmd == Cmd.GET_MOUNT_TABLE:
            self.send_mount_table(); return
        if cmd == Cmd.PAIR_DECIDE and len(pkt.payload) >= 8:
            cam, decision, mac = pkt.payload[0], pkt.payload[1], bytes(pkt.payload[2:8])
            if 1 <= cam <= NUM_MOUNTS and decision == 1:
                self.mount_table[cam - 1] = mac          # REPLACE: set the binding
                self.log(f"pair: REPLACE cam{cam} -> {mac.hex(':')}")
            else:
                self.log(f"pair: IGNORE cam{cam}")
            self.emit(0xFE, Cmd.PAIR_CONFLICT, bytes(13))  # cam=0 → dismiss prompt everywhere
            self.send_mount_table()
            return
        if cmd == Cmd.PAIR_FORGET and len(pkt.payload) >= 1:
            cam = pkt.payload[0]
            if 1 <= cam <= NUM_MOUNTS:
                self.mount_table[cam - 1] = bytes(6)       # CLEAR: unbind the slot
                self.log(f"pair: FORGET cam{cam}")
            self.send_mount_table()
            return

        if self.wedged:
            return                      # hub→mount path dead; STATUS keeps flowing

        targets = ([pkt.mount_id] if 1 <= pkt.mount_id <= NUM_MOUNTS
                   else list(self.mounts))
        for mid in targets:
            m = self.mounts[mid]
            if not m.alive:
                continue
            self.dispatch(m, pkt)

    def ack(self, m: SimMount, pkt):
        self.emit(m.id, Cmd.ACK, struct.pack(">H", pkt.seq))

    def nack(self, m: SimMount, pkt, err: NackError):
        self.emit(m.id, Cmd.NACK, struct.pack(">HB", pkt.seq, err))

    def dispatch(self, m: SimMount, pkt):
        cmd, p = pkt.cmd, pkt.payload

        if m.force_nack is not None and cmd != Cmd.PING:
            err, m.force_nack = m.force_nack, None
            self.log(f"cam{m.id}: forced NACK({err.name}) for {cmd.name}")
            self.nack(m, pkt, err)
            return

        # A homing mount is busy — motion commands are refused like the real
        # Teensy refuses them (jog is silently ignored; it is never ACKed).
        if m.state == MountState.FINDING_LIMITS and cmd in (
                Cmd.JOG, Cmd.GOTO, Cmd.MOVE_REL, Cmd.GOTO_SLOT,
                Cmd.START_LOOK_AT_MOVE, Cmd.FIND_LIMITS, Cmd.FIND_HOME):
            if cmd != Cmd.JOG:
                self.nack(m, pkt, NackError.BUSY)
            return

        if cmd == Cmd.JOG and len(p) >= 10:
            pan, tilt, sl, zm, ptp, szp = struct.unpack(">hhhhBB", p[:10])
            m.pt_preset, m.sl_preset = ptp, szp
            if m.state in (MountState.LOOK_AT_MOVE, MountState.LOOK_AT_PRE_AIM):
                return                                  # look-at owns the axes
            m.vel = [pan / 1000 * PT_MAX_DEG_S[ptp-1] * STEPS_PER_DEG,
                     tilt / 1000 * PT_MAX_DEG_S[ptp-1] * STEPS_PER_DEG,
                     (sl / 1000 * SL_MAX_MM_S[szp-1] * STEPS_PER_MM) if m.has_slider else 0,
                     zm / 1000 * 2000]
            m.jog_last = now()
            m.state = MountState.JOGGING
            return                                      # jog is not ACKed (50 Hz)

        if cmd == Cmd.PING:
            self.emit(m.id, Cmd.PONG, p[:4]); return
        if cmd == Cmd.E_STOP:
            m.vel = [0.0]*4; m.goto_tgt = None; m.la = None
            m.move_preset = None
            m.target_slot = 0xFF; m.state = MountState.IDLE
            self.ack(m, pkt); return
        if cmd == Cmd.GET_STATUS:
            self.emit(m.id, Cmd.STATUS, m.status_payload()); return
        if cmd == Cmd.GET_POSITION:
            self.emit(m.id, Cmd.POSITION, m.position_payload()); return  # no ACK, like the Teensy
        if cmd == Cmd.GET_STATE:
            self.emit(m.id, Cmd.STATE_REPORT,
                      struct.pack(">HHBB", m.occupied_mask(), m.at_mask(),
                                  m.pt_preset, m.sl_preset))
            self.ack(m, pkt); return
        if cmd == Cmd.GET_CONFIG:
            # Report the ACTUAL slider state (was hardcoded 0x04 — which made a
            # toggled-off mount still claim a slider, re-enabling the PC app's
            # slider dial after it had greyed out).
            ori = (0x04 if m.has_slider else 0) | (0x40 if m.look_at else 0)
            cfg = bytes([ori])
            for s in ([(6,12),(12,24),(20,40),(35,70)] +
                      [(10,20),(25,50),(45,90),(80,160)] + [(50,100)]):
                cfg += struct.pack(">II", *s)
            cfg += bytes([60, 60])
            self.emit(m.id, Cmd.CONFIG_REPORT, cfg)
            self.ack(m, pkt); return
        if cmd in (Cmd.STORE_POS, Cmd.SAVE_POS) and len(p) >= 1:
            if p[0] > 9: self.nack(m, pkt, NackError.INVALID_PARAM); return
            m.slots[p[0]] = list(m.pos); self.ack(m, pkt); return
        if cmd == Cmd.CLEAR_POS and len(p) >= 1:
            m.slots.pop(p[0], None); self.ack(m, pkt); return
        if cmd == Cmd.GOTO_SLOT and len(p) >= 1:
            if p[0] not in m.slots: self.nack(m, pkt, NackError.INVALID_PARAM); return
            if len(p) >= 3: m.pt_preset, m.sl_preset = p[1], p[2]
            m.goto_tgt, m.target_slot = list(m.slots[p[0]]), p[0]
            m.move_preset = None          # slot recalls travel at active presets
            m.state = MountState.MOVING_TO_POS
            self.ack(m, pkt); return
        if cmd == Cmd.SET_ACTIVE_PRESET and len(p) >= 2:
            if p[0] == AxisGroup.PAN_TILT: m.pt_preset = p[1]
            else:                          m.sl_preset = p[1]
            self.ack(m, pkt); return
        if cmd == Cmd.SET_ORIENTATION and len(p) >= 1:
            m.has_slider = bool(p[0] & 0x04)
            m.look_at    = bool(p[0] & 0x40)
            self.ack(m, pkt); return
        if cmd in (Cmd.FIND_LIMITS, Cmd.FIND_HOME) and len(p) >= 1:
            m.state = MountState.FINDING_LIMITS
            m.calib = (p[0], now() + 3.0)
            self.ack(m, pkt); return
        if cmd in (Cmd.GOTO, Cmd.MOVE_REL) and len(p) >= 17:
            # 17 bytes: pan(i32) tilt(i32) slider(i32) zoom(i32) preset(i8).
            # GOTO is absolute; MOVE_REL adds the deltas to the current
            # position — the PC app's manual move/nudge buttons use this, so
            # the sim must genuinely travel (and drop off any stored slot's
            # at-position tolerance, turning its green border red).
            pan, tilt, sl, zm, preset = struct.unpack(">iiiib", p[:17])
            base = [0.0] * 4 if cmd == Cmd.GOTO else list(m.pos)
            tgt = [base[0] + pan, base[1] + tilt,
                   base[2] + (sl if m.has_slider else 0), base[3] + zm]
            tgt[2] = max(0.0, min(float(SLIDER_MAX_STEPS), tgt[2]))
            tgt[3] = max(0.0, min(float(ZOOM_MAX_STEPS),  tgt[3]))
            m.goto_tgt    = tgt
            m.move_preset = max(1, min(4, preset)) if preset else None
            m.target_slot = 0xFF          # not a slot recall — no target flash
            m.state       = MountState.MOVING_TO_POS
            self.ack(m, pkt); return

        if cmd in (Cmd.SET_SPEED_PRESET, Cmd.SAVE_SPEEDS, Cmd.SET_LIMITS,
                   Cmd.SET_STALL_THRESHOLD):
            self.ack(m, pkt); return

        # ── look-at v2 ────────────────────────────────────────────────────
        if cmd == Cmd.ADD_SUBJECT_START and len(p) >= 17:
            name = p[1:17].rstrip(b"\0").decode(errors="replace")
            m.calib = (p[0], name, "to_a", now() + 1.0)
            m.state = MountState.CALIBRATING_SUBJECT
            self.ack(m, pkt)
            self.emit(m.id, Cmd.CALIB_PROMPT, bytes([CalibPrompt.MOVING_TO_A]))
            return
        if cmd == Cmd.ADD_SUBJECT_SET_A and m.calib:
            sid, name, _, _ = m.calib
            m.calib = (sid, name, "to_b", now() + 1.0)
            self.ack(m, pkt)
            self.emit(m.id, Cmd.CALIB_PROMPT, bytes([CalibPrompt.MOVING_TO_B]))
            return
        if cmd == Cmd.ADD_SUBJECT_SET_B and m.calib:
            sid, name, _, _ = m.calib
            m.subjects[sid] = (name, 500.0 - 150.0 * sid, 200.0, 2000.0)
            m.calib = None
            m.state = MountState.IDLE
            self.ack(m, pkt)
            self.emit(m.id, Cmd.CALIB_PROMPT, bytes([CalibPrompt.SOLVED]))
            self.emit(m.id, Cmd.SUBJECT_LIST, m.subject_list_payload())
            return
        if cmd == Cmd.ADD_SUBJECT_ABORT:
            m.calib = None; m.state = MountState.IDLE; self.ack(m, pkt); return
        if cmd == Cmd.DELETE_SUBJECT and len(p) >= 1:
            m.subjects.pop(p[0], None)
            self.ack(m, pkt)
            self.emit(m.id, Cmd.SUBJECT_LIST, m.subject_list_payload())
            return
        if cmd == Cmd.GET_SUBJECTS:
            self.emit(m.id, Cmd.SUBJECT_LIST, m.subject_list_payload())
            self.ack(m, pkt); return
        if cmd == Cmd.SET_REF and len(p) >= 1:
            m.ref_set = True
            self.ack(m, pkt)
            pan, tilt = m.la_angles()
            self.emit(m.id, Cmd.REF_CONFIRMED, struct.pack(">ff", pan, tilt))
            return
        if cmd == Cmd.START_LOOK_AT_MOVE and len(p) >= 3:
            if not m.ref_set:
                self.nack(m, pkt, NackError.NO_REF); return
            subj, direction = p[0], p[1]
            if subj not in m.subjects:
                self.nack(m, pkt, NackError.INVALID_PARAM); return
            m.active_subj = subj
            m.sl_preset   = max(1, min(4, p[2]))
            m.la          = (direction, 0 if direction == 0 else SLIDER_MAX_STEPS)
            m.la_end_flags = 0
            m.calib       = (0, now() + 0.8)            # pre-aim timer
            m.state       = MountState.LOOK_AT_PRE_AIM
            self.ack(m, pkt)
            self.emit(0, Cmd.LA_MOVE_DIR, bytes([direction]), hub_inject=m.id)
            return
        if cmd == Cmd.SWITCH_SUBJECT and len(p) >= 1:
            if p[0] in m.subjects: m.active_subj = p[0]
            self.ack(m, pkt); return

        self.nack(m, pkt, NackError.UNKNOWN_CMD)

    # ── networking ────────────────────────────────────────────────────────
    def client_thread(self, conn: socket.socket, addr):
        self.log(f"client connected: {addr[0]}:{addr[1]}")
        reader = PacketReader()
        conn.settimeout(0.5)
        while self.running:
            try:
                data = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            self.rx_bytes += len(data)
            reader.feed(data)
            for pkt in reader.packets():
                self.handle(pkt)
        with self.clk:
            if conn in self.clients:
                self.clients.remove(conn)
        try: conn.close()
        except OSError: pass
        self.log(f"client disconnected: {addr[0]}:{addr[1]}")

    def serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(4)
        srv.settimeout(0.5)
        self._srv = srv
        self.log(f"listening on 0.0.0.0:{self.port} — point the PC app at "
                 f"127.0.0.1:{self.port} (TCP)")
        threading.Thread(target=self.ticker, daemon=True).start()
        while self.running:
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self.clk:
                self.clients.append(conn)
            threading.Thread(target=self.client_thread, args=(conn, addr),
                             daemon=True).start()

    # ── console ───────────────────────────────────────────────────────────
    def console(self):
        for line in sys.stdin:
            parts = line.split()
            if not parts: continue
            c = parts[0].lower()
            try:
                if c == "quit": self.running = False; break
                elif c == "kill":    self.mounts[int(parts[1])].alive = False
                elif c == "revive":  self.mounts[int(parts[1])].alive = True
                elif c == "wedge":   self.wedged = True
                elif c == "unwedge": self.wedged = False
                elif c == "drop":    self.drop_p = float(parts[1])
                elif c == "conflict":
                    cam = int(parts[1]) if len(parts) > 1 else 1
                    newmac = bytes([0x02, 0, 0, 0, 0xAA, cam])
                    oldmac = (self.mount_table[cam - 1]
                              if 1 <= cam <= NUM_MOUNTS else bytes(6))
                    self.emit(0xFE, Cmd.PAIR_CONFLICT,
                              bytes([cam]) + newmac + oldmac)
                elif c == "table":
                    for i, mac in enumerate(self.mount_table, 1):
                        print(f"  cam{i}: "
                              f"{mac.hex(':') if any(mac) else '- unpaired -'}")
                    continue
                elif c == "nack":
                    self.mounts[int(parts[1])].force_nack = NACK_CODES[parts[2]]
                elif c == "state":
                    for m in self.mounts.values():
                        print(f"  cam{m.id}: alive={m.alive} state={m.state.name} "
                              f"pos={m.pos} slots={sorted(m.slots)} "
                              f"subj={sorted(m.subjects)} ref={m.ref_set}")
                    print(f"  wedged={self.wedged} drop={self.drop_p} "
                          f"clients={len(self.clients)}")
                    continue
                elif c == "help":
                    print(__doc__.split("FAULT CONSOLE")[1]); continue
                else:
                    print("?  (help for commands)"); continue
                self.log(f"console: {' '.join(parts)}")
            except (KeyError, ValueError, IndexError) as e:
                print(f"bad command: {e}")


# ── selftest ────────────────────────────────────────────────────────────────

def selftest() -> int:
    from comms.protocol import (pkt_jog, pkt_store_pos, pkt_goto_slot,
                                pkt_set_ref, pkt_start_look_at_move, pkt_e_stop)
    sim = HubSim(port=0, verbose=False)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0)); port = srv.getsockname()[1]; srv.close()
    sim.port = port
    threading.Thread(target=sim.serve, daemon=True).start()
    time.sleep(0.3)

    s = socket.create_connection(("127.0.0.1", port), timeout=2)
    s.settimeout(0.05)
    reader = PacketReader()
    seen: dict[Cmd, list] = {}

    def pump(dur: float):
        end = time.time() + dur
        while time.time() < end:
            try:
                d = s.recv(65536)
            except socket.timeout:
                continue
            reader.feed(d)
            for pkt in reader.packets():
                seen.setdefault(pkt.cmd, []).append(pkt)

    def check(name, cond):
        print(f"  {'✓' if cond else '✗'} {name}")
        if not cond: raise AssertionError(name)

    print("[selftest]")
    pump(0.3)
    check("STATUS flowing from 5 mounts",
          len({p.mount_id for p in seen.get(Cmd.STATUS, [])}) == NUM_MOUNTS)

    pump(1.1)   # idle POSITION cadence is 1 Hz — allow one full interval
    check("POSITION flowing from 5 mounts",
          len({p.mount_id for p in seen.get(Cmd.POSITION, [])}) == NUM_MOUNTS)

    s.sendall(pkt_jog(2, 500, 0, 0, 0)); pump(0.2)
    st = [p for p in seen[Cmd.STATUS] if p.mount_id == 2][-1]
    check("JOG → mount 2 JOGGING", st.payload[0] == MountState.JOGGING)
    from comms.protocol import decode_position
    pos2 = [decode_position(p.payload) for p in seen[Cmd.POSITION] if p.mount_id == 2]
    check("POSITION shows pan moving during jog",
          any(pp.moving_mask & 1 for pp in pos2) and pos2[-1].pan_deg != 0.0)
    pump(0.7)
    st = [p for p in seen[Cmd.STATUS] if p.mount_id == 2][-1]
    check("jog dead-man fired after 0.5 s silence",
          st.payload[0] == MountState.IDLE)

    seen.clear(); s.sendall(pkt_store_pos(1, 4)); pump(0.3)
    check("STORE_POS ACKed", Cmd.ACK in seen)
    st = [p for p in seen[Cmd.STATUS] if p.mount_id == 1][-1]
    occupied = (st.payload[4] << 8) | st.payload[5]
    check("slot 5 occupied in STATUS", bool(occupied & (1 << 4)))
    at = (st.payload[6] << 8) | st.payload[7]
    check("slot 5 AT-position after store (green)", bool(at & (1 << 4)))

    # The reported bug: a manual move (MOVE_REL — the PC app's nudge buttons)
    # must actually travel, dropping the stored slot's at-position bit
    # (green border → red) exactly like a real mount.
    from comms.protocol import pkt_move_rel
    seen.clear(); s.sendall(pkt_move_rel(1, 800, 0, 0, 0)); pump(1.2)
    st = [p for p in seen[Cmd.STATUS] if p.mount_id == 1][-1]
    at = (st.payload[6] << 8) | st.payload[7]
    check("MOVE_REL travels: AT bit cleared (green → red)",
          not (at & (1 << 4)) and
          any(p.payload[0] == MountState.MOVING_TO_POS
              for p in seen[Cmd.STATUS] if p.mount_id == 1))
    seen.clear(); s.sendall(pkt_goto_slot(1, 4)); pump(1.5)
    st = [p for p in seen[Cmd.STATUS] if p.mount_id == 1][-1]
    at = (st.payload[6] << 8) | st.payload[7]
    check("recall returns: AT bit restored (red → green)", bool(at & (1 << 4)))

    # Jog away from the stored spot so the recall has real travel time
    s.sendall(pkt_jog(1, 800, 800, 0, 0)); pump(0.35)
    seen.clear(); s.sendall(pkt_goto_slot(1, 4)); pump(0.3)
    check("GOTO_SLOT travels (MOVING + target flashes)",
          any(p.payload[8] == 4 and p.payload[0] == MountState.MOVING_TO_POS
              for p in seen[Cmd.STATUS] if p.mount_id == 1))

    seen.clear(); s.sendall(pkt_start_look_at_move(3, 0, 1, 2)); pump(0.3)
    check("look-at without ref → NACK(NO_REF)",
          any(p.payload[2] == NackError.NO_REF for p in seen.get(Cmd.NACK, [])))

    sim.wedged = True
    seen.clear(); s.sendall(pkt_store_pos(1, 6)); pump(0.5)
    check("wedge: command swallowed (no ACK) while STATUS flows",
          Cmd.ACK not in seen and Cmd.STATUS in seen)
    sim.wedged = False

    seen.clear(); s.sendall(pkt_e_stop(0)); pump(0.4)
    check("broadcast E-STOP ACKed by all mounts",
          len({p.mount_id for p in seen.get(Cmd.ACK, [])}) == NUM_MOUNTS)

    # ── Pairing management: view / clear / set the hub's mount table ──────────
    seen.clear(); s.sendall(build_packet(0xFE, Cmd.GET_MOUNT_TABLE, b"")); pump(0.3)
    mt = seen.get(Cmd.MOUNT_TABLE, [])
    check("GET_MOUNT_TABLE → 30-byte MOUNT_TABLE, 5 slots bound",
          bool(mt) and len(mt[-1].payload) == 30 and
          all(any(mt[-1].payload[i*6:i*6+6]) for i in range(NUM_MOUNTS)))

    seen.clear(); s.sendall(build_packet(0xFE, Cmd.PAIR_FORGET, bytes([3]))); pump(0.3)
    mt = seen.get(Cmd.MOUNT_TABLE, [])
    check("PAIR_FORGET cam3 → slot 3 cleared in pushed table",
          bool(mt) and not any(mt[-1].payload[12:18]))

    newmac = bytes([0x02, 0, 0, 0, 0xAA, 3])
    seen.clear()
    s.sendall(build_packet(0xFE, Cmd.PAIR_DECIDE, bytes([3, 1]) + newmac)); pump(0.3)
    mt = seen.get(Cmd.MOUNT_TABLE, [])
    check("PAIR_DECIDE replace → slot 3 rebound to the new device",
          bool(mt) and mt[-1].payload[12:18] == newmac)

    sim.running = False
    print("[selftest] PASS")
    return 0


def main():
    ap = argparse.ArgumentParser(description="hub + 5-mount simulator")
    ap.add_argument("--port", type=int, default=7777)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(selftest())
    sim = HubSim(args.port)
    threading.Thread(target=sim.serve, daemon=True).start()
    try:
        sim.console()
    except KeyboardInterrupt:
        pass
    sim.running = False


if __name__ == "__main__":
    main()
