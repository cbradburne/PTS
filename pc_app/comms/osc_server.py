"""
osc_server.py — OSC control surface for Bitfocus Companion / QLab.

A small UDP OSC server inside the PC app: show-control surfaces on the LAN
(Companion "Generic: OSC" module, QLab network cues, TouchOSC, …) can drive
every mount through the already-running PC app, which relays to the hub over
its existing USB/TCP link.  No third-party OSC library — the subset of OSC
1.0 we need (messages, bundles, i/f/s/T/F arguments) is ~80 lines to parse.

ADDRESS SPACE  (cam N = 1-5; slots are 1-based here, matching every UI)
    /pts/estop                          E-STOP every mount
    /pts/cam/N/estop                    E-STOP one mount
    /pts/cam/N/goto     <slot 1-10>     recall stored position (active presets)
    /pts/cam/N/store    <slot 1-10>     store current position
    /pts/cam/N/clear    <slot 1-10>     clear a stored position
    /pts/cam/N/jog      <pan> <tilt> <slider> <zoom>    velocities -1000..1000;
                                        the server re-streams at 20 Hz until a
                                        zero jog / jog/stop / TTL expiry
    /pts/cam/N/jog/stop                 stop jogging immediately
    /pts/cam/N/speed/pt <1-4>           active pan/tilt speed preset
    /pts/cam/N/speed/sl <1-4>           active slider speed preset
    /pts/cam/N/subject  <0-7>           select look-at subject (switches live
                                        if a look-at move is running)
    /pts/cam/N/lookat   <0|1>           look-at move to min (0/◀) or max (1/▶)
                                        using the selected subject + active
                                        slider preset

COMPANION: add a "Generic: OSC" connection → target = this PC's IP, port
below.  Button press/release action pairs give hold-to-jog.  See
docs/companion.md for ready-made button recipes.

SAFETY: jogs started over OSC are re-streamed by this server (the Teensy's
500 ms dead-man needs a continuous stream).  A TTL (default 15 s) bounds the
damage of a lost UDP release message — for longer moves use goto/lookat.
"""
from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from typing import Callable, Optional

from .protocol import Axis, AxisGroup, MountState, NUM_MOUNTS

log = logging.getLogger(__name__)

JOG_STREAM_HZ   = 20
JOG_TTL_S       = 15.0     # max hold without a refresh — lost-release safety

# Fraction of the requested zoom velocity actually sent for OSC jogs.
# Companion/Stream Deck buttons have no travel — they send full scale or zero —
# so zoom always arrived at 100%, which is too fast for a lens on a live shot.
OSC_ZOOM_SCALE = 0.60
DEFAULT_PORT    = 9700


# ---------------------------------------------------------------------------
# Minimal OSC 1.0 parsing (messages + bundles; i, f, s, T, F arguments)
# ---------------------------------------------------------------------------

def _pad4(n: int) -> int:
    return (n + 3) & ~3


def _read_string(data: bytes, ofs: int) -> tuple[str, int]:
    end = data.index(b"\0", ofs)
    return data[ofs:end].decode("ascii", errors="replace"), _pad4(end + 1)


def parse_osc(data: bytes) -> list[tuple[str, list]]:
    """Return a list of (address, args) — bundles are flattened."""
    out: list[tuple[str, list]] = []
    if not data:
        return out
    if data.startswith(b"#bundle\0"):
        ofs = 16                      # "#bundle\0" + 8-byte timetag (ignored)
        while ofs + 4 <= len(data):
            (size,) = struct.unpack(">i", data[ofs:ofs + 4])
            ofs += 4
            out.extend(parse_osc(data[ofs:ofs + size]))
            ofs += _pad4(size)
        return out

    addr, ofs = _read_string(data, 0)
    if not addr.startswith("/"):
        return out
    args: list = []
    if ofs < len(data) and data[ofs:ofs + 1] == b",":
        tags, ofs = _read_string(data, ofs)
        for t in tags[1:]:
            if t == "i":
                (v,) = struct.unpack(">i", data[ofs:ofs + 4]); ofs += 4
                args.append(v)
            elif t == "f":
                (v,) = struct.unpack(">f", data[ofs:ofs + 4]); ofs += 4
                args.append(v)
            elif t == "s":
                v, ofs = _read_string(data, ofs)
                args.append(v)
            elif t == "T":
                args.append(True)
            elif t == "F":
                args.append(False)
            else:
                break                  # unsupported tag — stop arg parsing
    out.append((addr, args))
    return out


def _as_int(args: list, idx: int, default: Optional[int] = None) -> Optional[int]:
    if idx >= len(args):
        return default
    v = args[idx]
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return int(round(v))
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class OscServer:
    """UDP OSC listener that drives a MountManager (duck-typed: any object
    with the manager's send_* methods and .state(mid) works — the end-to-end
    test runs the real manager against the mount simulator)."""

    def __init__(self, manager, port: int = DEFAULT_PORT,
                 host: str = "0.0.0.0"):
        self._mgr     = manager
        self._port    = port
        self._host    = host
        self._sock: Optional[socket.socket] = None
        self._running = False
        self._threads: list[threading.Thread] = []
        # Per-cam jog stream state: mid → (pan, tilt, slider, zoom, deadline)
        self._jogs: dict[int, tuple[int, int, int, int, float]] = {}
        self._jog_lock = threading.Lock()
        # Per-cam look-at subject selected over OSC (None = follow mount)
        self._subject_sel: dict[int, int] = {}

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> bool:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self._host, self._port))
            self._sock.settimeout(0.5)
        except OSError as e:
            log.error("OSC server failed to bind %s:%d: %s",
                      self._host, self._port, e)
            return False
        self._running = True
        for fn, name in ((self._rx_loop, "osc-rx"), (self._jog_loop, "osc-jog")):
            th = threading.Thread(target=fn, daemon=True, name=name)
            th.start()
            self._threads.append(th)
        log.info("OSC control listening on %s:%d (Companion/QLab)",
                 self._host, self._port)
        return True

    def stop(self) -> None:
        self._running = False
        if self._sock:
            try: self._sock.close()
            except OSError: pass

    # ── receive ──────────────────────────────────────────────────────────
    def _rx_loop(self) -> None:
        while self._running:
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            for address, args in parse_osc(data):
                try:
                    self._dispatch(address, args)
                except Exception as e:            # never kill the listener
                    log.warning("OSC %s %s failed: %s", address, args, e)

    # ── jog re-streaming (Teensy dead-man needs 20 Hz) ───────────────────
    def _jog_loop(self) -> None:
        while self._running:
            time.sleep(1.0 / JOG_STREAM_HZ)
            now = time.monotonic()
            with self._jog_lock:
                jogs = dict(self._jogs)
            for mid, (pan, tilt, sl, zm, deadline) in jogs.items():
                st = self._mgr.state(mid)
                if now >= deadline:
                    log.warning("OSC jog TTL expired on cam %d — stopping "
                                "(lost release?)", mid)
                    self._stop_jog(mid)
                    continue
                self._mgr.send_jog(mid, pan, tilt, sl, zm,
                                   st.active_pt_preset, st.active_sl_preset)

    def _stop_jog(self, mid: int) -> None:
        with self._jog_lock:
            self._jogs.pop(mid, None)
        st = self._mgr.state(mid)
        self._mgr.send_jog(mid, 0, 0, 0, 0,
                           st.active_pt_preset, st.active_sl_preset)

    # ── dispatch ─────────────────────────────────────────────────────────
    def _dispatch(self, address: str, args: list) -> None:
        parts = [p for p in address.split("/") if p]
        if not parts or parts[0] != "pts":
            return

        if parts[1:] == ["estop"]:
            log.info("OSC: E-STOP ALL")
            with self._jog_lock:
                self._jogs.clear()
            self._mgr.send_e_stop()
            return

        if len(parts) < 4 or parts[1] != "cam":
            log.debug("OSC: unknown address %s", address)
            return
        try:
            mid = int(parts[2])
        except ValueError:
            return
        if not 1 <= mid <= NUM_MOUNTS:
            return
        verb = parts[3]
        st = self._mgr.state(mid)

        if verb == "estop":
            self._stop_jog(mid)
            self._mgr.send_e_stop(mid)

        elif verb == "goto":
            slot = _as_int(args, 0)
            if slot is not None and 1 <= slot <= 10:
                self._mgr.send_goto_slot(mid, slot - 1,
                                         st.active_pt_preset,
                                         st.active_sl_preset)

        elif verb == "store":
            slot = _as_int(args, 0)
            if slot is not None and 1 <= slot <= 10:
                self._mgr.send_store_pos(mid, slot - 1)

        elif verb == "clear":
            slot = _as_int(args, 0)
            if slot is not None and 1 <= slot <= 10:
                self._mgr.send_clear_pos(mid, slot - 1)

        elif verb == "jog":
            if len(parts) >= 5 and parts[4] == "stop":
                self._stop_jog(mid)
                return
            vals = [max(-1000, min(1000, _as_int(args, i, 0) or 0))
                    for i in range(4)]
            # Zoom only.  A Stream Deck button is all-or-nothing — it sends full
            # scale or nothing — so the zoom axis arrived at 100% every time,
            # which is faster than anyone wants a lens to move on a shot.  Pan,
            # tilt and slider come from a stick that can be feathered, so they
            # are left alone.
            #
            # Scaled here rather than on the mount so it applies to OSC only:
            # the joystick and the web app still reach full speed.
            if vals[3]:
                z = int(vals[3] * OSC_ZOOM_SCALE)
                # Never round a deliberate nudge down to nothing — an all-zero
                # jog is how a stop is expressed, and a tiny zoom request must
                # not become one.
                vals[3] = z if z else (1 if vals[3] > 0 else -1)
            if any(vals):
                with self._jog_lock:
                    self._jogs[mid] = (*vals,
                                       time.monotonic() + JOG_TTL_S)
                self._mgr.send_jog(mid, *vals,
                                   st.active_pt_preset, st.active_sl_preset)
            else:
                self._stop_jog(mid)

        elif verb == "speed" and len(parts) >= 5:
            p = _as_int(args, 0)
            if p is not None and 1 <= p <= 4:
                if parts[4] == "pt":
                    self._mgr.send_set_active_preset(mid, AxisGroup.PAN_TILT, p)
                elif parts[4] == "sl":
                    self._mgr.send_set_active_preset(mid, AxisGroup.SLIDER_ZOOM, p)

        elif verb == "subject":
            subj = _as_int(args, 0)
            if subj is not None and 0 <= subj <= 7:
                self._subject_sel[mid] = subj
                # Live-switch if a look-at move is currently running
                if st.state == MountState.LOOK_AT_MOVE or (
                        st.look_at_status is not None
                        and getattr(st.look_at_status, "look_at_active", False)):
                    self._mgr.send_switch_subject(mid, subj)

        elif verb == "lookat":
            direction = _as_int(args, 0)
            if direction not in (0, 1):
                return
            subj = self._subject_sel.get(mid)
            if subj is None:
                subj = st.active_subject_id if st.active_subject_id <= 7 else 0
            self._mgr.send_start_look_at_move(mid, subj, direction,
                                              st.active_sl_preset)

        else:
            log.debug("OSC: unknown verb %s in %s", verb, address)
