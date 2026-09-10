"""
Bridge — connection to the ESP32 hub.

Supports two transports:
  - Serial  : USB serial port to a directly-connected ESP32 (dev/bench use)
  - TCP     : WiFi TCP socket to the ESP32 hub AP (production use)

The public API is identical regardless of transport:

    bridge = Bridge()
    bridge.on_packet(my_callback)

    bridge.connect("COM5")                    # serial
    bridge.connect_tcp("192.168.4.1", 7777)   # TCP (hub AP default)

    bridge.send(packet_bytes)         # queued, non-blocking
    bridge.send_immediate(bytes)      # direct write, bypasses queue (E-STOP)
    bridge.disconnect()
    bridge.connected                  # bool property
"""
from __future__ import annotations

import socket
import threading
import queue
import time
import logging
from typing import Callable, Optional

import serial
import serial.tools.list_ports

from .protocol import (PacketReader, Packet, Cmd, decode_health, ParseError,
                       build_packet, HEALTH_FLAG_BLE_BUILD, HEALTH_FLAG_BLE_LINK,
                       HEALTH_FLAG_CAM_WR_ERR,
                       HEALTH_FLAG_CAM_SUBSCR,
                       HEALTH_FLAG_CAM_RX,
                       HEALTH_FLAG_CAM_UNPAIRED, HEALTH_FLAG_CAM_CACHE_FULL,
                       HEALTH_NODE_SATELLITE, SAT_ADDR_BASE, decode_sat_names,
                       decode_mount_route, NUM_MOUNTS,
                       SAT_DOWNLINK_PAYLOAD_LEN, SAT_DOWNLINK_MIN_LEN,
                       MOUNT_OUTAGE_PAYLOAD_LEN,
                       decode_mount_outage)

log = logging.getLogger(__name__)

PacketCallback = Callable[[Packet], None]

HUB_DEFAULT_HOST = "192.168.4.1"
HUB_DEFAULT_PORT = 7777

# ── Diagnostic command logging ─────────────────────────────────────────────
# High-rate periodic traffic is suppressed from the per-command TX/ACK log so
# the interesting user-initiated commands (speed presets, gotos, stores,
# look-at, etc.) stand out.  These are the only ones tracked for ACK round-trip.
_TX_QUIET_CMDS = frozenset({
    int(Cmd.JOG), int(Cmd.GET_STATUS), int(Cmd.PING), int(Cmd.GET_STATE),
    int(Cmd.GET_POSITION),   # replied with CMD_POSITION, never ACKed
    # Hub-scoped, answered with a MOUNT_TABLE push rather than an ACK.  Tracked,
    # it reported "CMD hub 0/1 acknowledged (0.000%)" at WARNING once per
    # connection — a warning that is always wrong, which is how a log teaches
    # people to skim past warnings.
    int(Cmd.GET_MOUNT_TABLE),
    # Hub-control, fire-and-forget — the hub does not ACK these, so they must NOT
    # be ACK-tracked or they would themselves look like wedged commands.
    int(Cmd.HUB_REINIT_ESPNOW), int(Cmd.HUB_RESTART),
})

# Packet byte offsets (see protocol.py packet format)
_PKT_OFF_MOUNT = 3
_PKT_OFF_SEQ_HI = 4
_PKT_OFF_SEQ_LO = 5
_PKT_OFF_CMD = 6


def _cmd_name(cmd_val: int) -> str:
    try:
        return Cmd(cmd_val).name
    except ValueError:
        return f"0x{cmd_val:02X}"


# ---------------------------------------------------------------------------
# Transport abstractions
# ---------------------------------------------------------------------------

class _SerialTransport:
    """Wraps a pyserial Serial object with a uniform read/write interface."""

    BAUD = 921600

    def __init__(self, port: str, read_timeout: float = 0.1):
        # NOTE: pyserial asserts DTR on open, which the ESP32-S3's native USB
        # treats as "host connected" — the hub needs it to send (with
        # setTxTimeoutMs(0) it drops TX when DTR is low).  Asserting DTR also
        # resets the hub on open (reset reason USB), which is the cause of the
        # reconnect→reboot cascade — but that has to be fixed HUB-side (disable
        # the USB-CDC auto-reset), not by dropping DTR here, or the hub goes mute.
        self._s = serial.Serial(port, baudrate=self.BAUD, timeout=read_timeout)
        self._alive = True

    def read(self, n: int) -> bytes:
        try:
            return self._s.read(n)
        except (serial.SerialException, OSError):
            # Port handle died underneath us — USB re-enumeration (hub
            # reboot / watchdog reset) or cable glitch.  Windows surfaces
            # this as "ClearCommError failed (PermissionError(13, ...))".
            # pyserial leaves is_open True after this, so we must track
            # liveness ourselves or the RX loop never reconnects.
            self._alive = False
            return b""

    def write(self, data: bytes) -> None:
        try:
            self._s.write(data)
            self._s.flush()
        except (serial.SerialException, OSError):
            self._alive = False
            raise   # callers log the TX failure; RX loop sees alive=False and reconnects

    def mark_dead(self) -> None:
        """Flag the transport as dead WITHOUT closing the OS handle.  Lets a
        thread other than the RX loop (e.g. the health monitor) request a
        reconnect without racing the RX loop's close() — pyserial's Windows
        close() is not safe to call from two threads at once."""
        self._alive = False

    def close(self) -> None:
        self._alive = False
        try:
            if self._s.is_open:
                self._s.close()
        except (serial.SerialException, OSError, AttributeError, TypeError):
            # Closing an already-dead handle can raise on Windows.  pyserial's
            # serialwin32 close() additionally throws AttributeError/TypeError
            # if its overlapped structures were already torn down (e.g. a
            # double close) — swallow those so a close never kills the caller.
            pass

    @property
    def alive(self) -> bool:
        return self._alive and self._s.is_open


class _TcpTransport:
    """Wraps a TCP socket with a uniform read/write interface."""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._alive = True

    def read(self, n: int) -> bytes:
        try:
            data = self._sock.recv(n)
            if not data:
                # Remote closed the connection
                self._alive = False
            return data
        except socket.timeout:
            return b""
        except OSError:
            self._alive = False
            return b""

    def write(self, data: bytes) -> None:
        try:
            self._sock.sendall(data)
        except OSError:
            self._alive = False

    def mark_dead(self) -> None:
        """See _SerialTransport.mark_dead — flag dead without closing the socket
        so only the RX loop ever calls close()."""
        self._alive = False

    def close(self) -> None:
        self._alive = False
        try:
            self._sock.close()
        except OSError:
            pass

    @property
    def alive(self) -> bool:
        return self._alive


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

class Bridge:

    READ_TIMEOUT     = 0.1   # seconds — applies to both serial and TCP reads
    RECONNECT_DELAY  = 1.0   # seconds before an auto-reconnect attempt (enough
                             # for the OS to release the serial handle); the
                             # reconnect retries on failure, so this can be short.

    # The monitor thread ticks at this rate to check for a wedged command link.
    # Detection latency is roughly WEDGE_DETECT_S, independent of the (slower)
    # health-log cadence below.
    MONITOR_TICK_S = 1.0
    # A tracked command to a mount that has ACKed before, left unacked this long,
    # means the PC→hub command path is wedged.  Steady-state ACKs are <0.3 s, so
    # a few seconds is a large margin; the post-connect grace below covers the
    # slower cold-start ACKs (~1–3 s) seen right after a (re)connect.
    WEDGE_DETECT_S = 3.0
    # Command ledger.  A command unanswered for this long is counted lost — well
    # past any ordinary round trip (steady state is 47-80 ms) and past the late
    # answers that the wedge dict throws away.
    CMD_DEADLINE_S = 10.0
    # Acknowledged, but slowly enough that an operator would have noticed. A
    # separate count from lost, because "it worked eventually" and "it never
    # happened" feel identical at the time and want different fixes.
    CMD_SLOW_MS    = 750.0
    CMD_REPORT_S   = 300.0
    # A mount that has ACKED something within this window is NOT wedged, however
    # many other commands are outstanding.  The discriminator has to be the ACK,
    # not merely a packet arriving:
    #
    #   ACKing            → healthy.  A single lost ACK amid a steady stream is
    #                       packet loss, not a wedge; escalating it restarts the
    #                       hub and takes every mount down to cure nothing.
    #   sending, no ACKs  → hub→mount SEND wedge.  The mount is alive and its
    #                       health/STATUS keep arriving, but nothing we send
    #                       reaches it.  This is the real, dominant fault, and a
    #                       hub restart is the only confirmed cure — so this MUST
    #                       escalate.  Gating on "have we heard from it" instead
    #                       hid a rig-wide 4 h command outage on 2026-07-30 with
    #                       every mount at 0.4% ACK and not one WEDGE logged.
    #   fully silent      → mount-side fault, or gone.
    #
    # Healthy ACK cadence is one every ~3 s (the GET_CONFIG poll), so 15 s is a
    # wide margin over normal loss yet still catches a send wedge promptly.
    MOUNT_ACK_STALE_S = 15.0
    # A mount heard from within this window is PRESENT.  Beyond it, it is off or
    # gone, and nothing the hub does can help — restarting it would drop every
    # other mount to cure a camera someone has unplugged.  A live mount sends
    # STATUS at 10 Hz and health every 10 s, so 15 s of total silence is
    # unambiguous.  Without this the ladder restarted the hub whenever the only
    # deployed mount was switched off, which on a rig where mounts go out per
    # event is an ordinary end-of-day action.
    MOUNT_SILENT_S = 15.0
    # Don't run wedge detection for this long after a (re)connect — the first
    # command on a cold pipe can legitimately take 1–3 s to ACK.
    POST_CONNECT_GRACE_S = 6.0
    # Minimum gap between forced reconnects, so a reconnect that doesn't fix it
    # retries at a sane cadence instead of thrashing.
    RECONNECT_COOLDOWN_S = 8.0
    # On a serial wedge, first ask the hub to reinit its ESP-NOW (cheap, brief).
    HUB_REINIT_MIN_INTERVAL_S = 30.0
    # If the wedge persists this long, the reinit didn't clear it — escalate to a
    # full hub esp_restart() (the only confirmed cure; the wedge sits below the
    # ESP-NOW layer).  A restart drops all clients ~5s, so only as a last resort.
    HUB_RESTART_AFTER_S       = 30.0
    HUB_RESTART_MIN_INTERVAL_S = 90.0   # the hub takes several seconds to come back

    # Diagnostic: how often the health-monitor thread logs a status snapshot.
    # Set to 0 to disable.  Lower it (e.g. 5) when actively reproducing a stall.
    HEALTH_LOG_INTERVAL = 30.0   # seconds

    def __init__(self):
        self._transport: Optional[_SerialTransport | _TcpTransport] = None
        self._send_queue: queue.Queue[bytes] = queue.Queue()
        self._callbacks: list[PacketCallback] = []
        self._reconnect_cbs: list[Callable] = []
        self._reader = PacketReader()
        self._running = False
        self._rx_thread: Optional[threading.Thread] = None
        self._tx_thread: Optional[threading.Thread] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._connected = False
        # Stored so the RX thread can auto-reconnect; cleared on explicit disconnect.
        self._reconnect_params: Optional[dict] = None

        # ── Diagnostic counters / timestamps (see _monitor_loop) ───────────
        # These let the health monitor distinguish "RX alive, TX dead" (the
        # reported "looks active but speed dials do nothing" stall) from a
        # full transport loss.  Plain ints — incremented from RX/TX threads;
        # GIL-atomic enough for diagnostics, no locking needed.
        self._rx_pkt_count    = 0       # packets successfully parsed + dispatched
        self._tx_queued_count = 0       # packets handed to send()
        self._tx_written_count = 0      # packets actually written to the wire
        self._last_rx_t: Optional[float] = None   # monotonic ts of last RX bytes
        self._last_tx_t: Optional[float] = None   # monotonic ts of last TX write

        # ── ACK round-trip tracking (the key stall discriminator) ──────────
        # Maps the seq of each interesting (non-periodic) command we send to
        # (sent_monotonic_ts, cmd_name).  When the matching ACK/NACK comes
        # back we log the round-trip and remove it.  If a command is written
        # to the wire but never ACKed, the hub never processed it — i.e. the
        # PC→hub link is wedged even though STATUS keeps flowing the other way.
        self._diag_lock = threading.Lock()
        # seq → (sent_monotonic_ts, cmd_name, mount_id)
        self._pending_acks: dict[int, tuple[float, str, int]] = {}
        # Command ledger — a RECORD, deliberately separate from _pending_acks.
        # That dict exists for wedge detection and prunes hard: when an ACK
        # arrives it deletes every earlier entry for the same mount, and an ACK
        # whose entry has gone is dropped without even being logged.  Sensible
        # for deciding "is this mount wedged right now"; useless as a history,
        # and reading it as one produced a 19% loss figure for a mount that had
        # simply been answering a little late.
        #
        # This answers the question that actually matters: of the commands sent
        # to a mount, how many did it acknowledge, and how quickly.  Nothing
        # prunes it but the deadline below.
        self._cmd_ledger: dict[int, tuple[float, int]] = {}   # seq -> (sent_t, mount)
        self._cmd_stat: dict[int, dict] = {}
        self._cmd_report_t = 0.0
        # Mounts known to be PRESENT (so an unacked command to them is a real
        # wedge, not just an absent mount we shouldn't reconnect-loop on).  A
        # mount counts as present if it has ACKed a command OR sent us any packet
        # (STATUS) — the latter is essential: a link that is wedged from the
        # moment we connect never lets the mount ACK, so without the STATUS
        # signal the detector would never fire and recovery would stay disabled
        # exactly when it's needed.  Phantom (non-existent) mounts send nothing,
        # so they still never appear here.
        self._acked_mounts: set[int] = set()
        self._rx_mounts: set[int] = set()   # mounts we've received any packet from
        # Wedge detection needs BOTH of these, because three situations look
        # the same from a missing ACK alone:
        #   ACKing                  -> healthy
        #   sending, but not ACKing -> hub→mount SEND wedge, must escalate
        #   not sending at all      -> the mount is off or gone; the hub is fine
        # _mount_last_ack answers the first, _mount_last_rx the third.  Keeping
        # only the ACK timestamp made a switched-off mount indistinguishable
        # from a wedge, and restarted the hub every time one was powered down.
        self._mount_last_ack: dict[int, float] = {}
        self._mount_last_rx:  dict[int, float] = {}
        self._tx_cmd_sent  = 0          # interesting commands written
        self._tx_cmd_acked = 0          # interesting commands ACKed/NACKed back
        self._last_forced_reconnect: float = 0.0
        self._last_hub_reinit_t: float = 0.0    # paces CMD_HUB_REINIT_ESPNOW sends
        self._last_hub_restart_t: float = 0.0   # paces CMD_HUB_RESTART escalation
        # last uptime seen per node name — a decrease means it restarted
        self._node_uptime: dict[str, int] = {}
        # mount_id -> BLE camera link state, from CMD_HEALTH; absent = no camera build
        self._cam_ble: dict[int, bool] = {}
        # Satellite slot → name, so a satellite's health line can be headed
        # "Foyer" rather than "SAT 1".  Empty until the hub sends SAT_NAMES.
        self._sat_names: dict[int, str] = {}
        # Which base each mount is on: 0 = this hub's radio, >0 = satellite slot.
        # All-zero until the hub sends MOUNT_ROUTE, which reads as "direct" and
        # so keeps the pre-existing verdict rather than suppressing it.
        self._mount_route: list[int] = [0] * NUM_MOUNTS
        # Last cumulative downlink ledger per satellite, so each line can be
        # reported as a delta rather than an ever-growing total.
        self._sat_dn_prev: dict[str, tuple] = {}
        # Same thing across restarts of THIS app: {node: {uptime_s, reset, at}}.
        self._node_state_prev: dict = self._node_state_load()
        self._node_state_cur:  dict = {}
        self._node_state_written: float = 0.0
        # Monotonic ts of the last (re)connect — starts the post-connect grace.
        self._last_connect_t: float = 0.0
        # Monotonic ts of the last ACK-tracked command written.  Lets the idle
        # probe (mount_manager) decide when to inject a probe so the OUT pipe is
        # exercised even when the user isn't operating the system.
        self._last_tracked_tx_t: float = 0.0

        # ── USB wedge host-vs-hub diagnostic (Cmd.HUB_DIAG from the hub) ────
        # The hub reports how many bytes/packets it has read from the PC.  During
        # a wedge: if these keep climbing while our commands go unacked, the stall
        # is hub-side; if they freeze while we're still sending, our bytes never
        # arrive — a host-side (Windows USB-CDC) OUT-pipe halt.
        self._hub_rx_bytes = 0
        self._hub_rx_pkts  = 0
        self._hub_diag_rx_t: Optional[float] = None         # last diag received
        self._hub_rx_last_advance_t: Optional[float] = None  # last time rx_bytes grew
        # Cumulative ghost STATUS frames the hub has dropped (rssi==0) — the
        # phantom-camera guard.  Reported in HUB_DIAG; surfaced in HEALTH.
        self._hub_ghost_drops = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_packet(self, cb: PacketCallback) -> None:
        """Register a callback for received packets. Called from RX thread."""
        self._callbacks.append(cb)

    def off_packet(self, cb: PacketCallback) -> None:
        """Deregister a previously registered packet callback."""
        try:
            self._callbacks.remove(cb)
        except ValueError:
            pass

    def on_reconnect(self, cb: Callable) -> None:
        """Register a callback invoked after a successful auto-reconnect."""
        self._reconnect_cbs.append(cb)

    def connect(self, port_name: str) -> bool:
        """Open a USB serial connection (bench / development use)."""
        self._reconnect_params = {'type': 'serial', 'port': port_name}
        try:
            transport = _SerialTransport(port_name, self.READ_TIMEOUT)
        except serial.SerialException as e:
            log.error(f"Serial connect failed ({port_name}): {e}")
            return False
        log.info(f"Connected via serial: {port_name}")
        self._start(transport)
        return True

    def connect_tcp(self, host: str = HUB_DEFAULT_HOST,
                    port: int = HUB_DEFAULT_PORT) -> bool:
        """Open a TCP connection to the ESP32 hub AP (production use)."""
        self._reconnect_params = {'type': 'tcp', 'host': host, 'port': port}
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            sock.connect((host, port))
            sock.settimeout(self.READ_TIMEOUT)
        except OSError as e:
            log.error(f"TCP connect failed ({host}:{port}): {e}")
            return False
        log.info(f"Connected via TCP: {host}:{port}")
        self._start(_TcpTransport(sock))
        return True

    def disconnect(self) -> None:
        """Stop background threads and close the transport."""
        self._reconnect_params = None   # suppress auto-reconnect on intentional close
        self._running = False
        self._connected = False
        if self._transport:
            self._transport.close()
            self._transport = None

    @property
    def connected(self) -> bool:
        return self._connected and self._transport is not None and self._transport.alive

    def send(self, packet_bytes: bytes) -> None:
        """Queue a packet for transmission. Non-blocking."""
        self._tx_queued_count += 1
        self._send_queue.put(packet_bytes)

    def send_immediate(self, packet_bytes: bytes) -> None:
        """Write directly to the transport, bypassing the queue. Use for E-STOP only."""
        with self._lock:
            if self._transport and self._transport.alive:
                try:
                    self._transport.write(packet_bytes)
                except Exception as e:
                    log.error(f"E-STOP send failed: {e}")
                    self._connected = False

    @staticmethod
    def list_ports() -> list[str]:
        """Return available serial port names."""
        return [p.device for p in serial.tools.list_ports.comports()]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _start(self, transport: _SerialTransport | _TcpTransport) -> None:
        """Attach a transport and start background threads."""
        if self._running:
            self.disconnect()
        self._transport = transport
        self._connected = True
        self._running = True
        now = time.monotonic()
        self._last_connect_t    = now   # start the post-connect grace
        self._last_tracked_tx_t = now
        self._reader = PacketReader()   # fresh reader per connection
        self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True,
                                           name="bridge-rx")
        self._tx_thread = threading.Thread(target=self._tx_loop, daemon=True,
                                           name="bridge-tx")
        self._rx_thread.start()
        self._tx_thread.start()
        # The monitor thread drives wedge detection + recovery, not just the
        # health log, so it must always run regardless of HEALTH_LOG_INTERVAL.
        self._monitor_thread = threading.Thread(target=self._monitor_loop,
                                                daemon=True, name="bridge-mon")
        self._monitor_thread.start()

    def _rx_loop(self) -> None:
        # Outer guard: a daemon thread that dies silently is exactly how the
        # "looks connected but unresponsive" stall hides.  Log loudly on exit.
        try:
            while self._running:
                if not self._transport or not self._transport.alive:
                    self._connected = False
                    if self._transport:
                        # Release the dead handle so the OS frees the port before
                        # we try to reopen it.  Guard it: a close() that raises
                        # must never escape and kill this thread, or auto-reconnect
                        # (which only runs here) would be lost permanently.
                        try:
                            self._transport.close()
                        except Exception as e:
                            log.warning(f"Transport close during reconnect raised: {e}")
                    if self._running and self._reconnect_params:
                        log.warning("Transport lost — reconnecting in %.1fs",
                                    self.RECONNECT_DELAY)
                        time.sleep(self.RECONNECT_DELAY)
                        self._attempt_reconnect()
                    else:
                        time.sleep(0.1)
                    continue
                try:
                    data = self._transport.read(256)
                    if data:
                        self._last_rx_t = time.monotonic()
                        self._reader.feed(data)
                        for pkt in self._reader.packets():
                            self._rx_pkt_count += 1
                            self._dispatch(pkt)
                        # The hub's own Serial.printf() diagnostics share this
                        # link with the binary protocol, so surface them here —
                        # in a raw terminal they're unreadable amongst the frames.
                        for line in self._reader.text_lines():
                            log.info(f"HUB SERIAL: {line}")
                except Exception as e:
                    log.error(f"RX loop error: {e}")
                    time.sleep(0.1)   # never tight-spin if an error persists
        except Exception:
            log.exception("RX thread died with an unhandled exception")
        finally:
            log.warning("RX thread EXITING (running=%s) — no more packets will "
                        "be received until reconnect", self._running)

    def _attempt_reconnect(self) -> None:
        """Called from _rx_loop when the transport has died.  Tries to restore
        the connection using the parameters saved at connect time."""
        params = self._reconnect_params
        if not params:
            return
        log.info(f"Auto-reconnect: {params}")
        try:
            if params['type'] == 'tcp':
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5.0)
                sock.connect((params['host'], params['port']))
                sock.settimeout(self.READ_TIMEOUT)
                new_transport: _SerialTransport | _TcpTransport = _TcpTransport(sock)
            else:
                new_transport = _SerialTransport(params['port'], self.READ_TIMEOUT)

            now = time.monotonic()
            with self._lock:
                self._transport = new_transport
                self._connected = True
                self._reader    = PacketReader()
            # Commands in flight before the reconnect are void (the transport — and
            # possibly the hub, if this followed a CMD_HUB_RESTART — was reset), and
            # will never be ACKed.  Drop them so the wedge detector starts clean and
            # doesn't immediately re-escalate on stale entries.
            with self._diag_lock:
                self._pending_acks.clear()
            # Restart the post-connect grace and probe clock on the fresh pipe.
            self._last_connect_t    = now
            self._last_tracked_tx_t = now

            log.info("Auto-reconnect successful")
            for cb in self._reconnect_cbs:
                try:
                    cb()
                except Exception as e:
                    log.error(f"Reconnect callback error: {e}")

        except Exception as e:
            log.warning(f"Auto-reconnect failed: {e}")

    def _tx_loop(self) -> None:
        # Outer guard — see _rx_loop.  A silently-dead TX thread is the prime
        # suspect for "speed dials do nothing but the camera still looks live".
        try:
            while self._running:
                try:
                    data = self._send_queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                with self._lock:
                    if self._transport and self._transport.alive:
                        try:
                            self._transport.write(data)
                            self._tx_written_count += 1
                            self._last_tx_t = time.monotonic()
                            self._note_tx_command(data)
                        except Exception as e:
                            log.warning(f"TX error: {e}")
                            self._connected = False
                    else:
                        # Transport gone but a command was queued — note it so
                        # the health line's queued/written gap is explained.
                        log.warning("TX dropped a packet — transport not alive")
        except Exception:
            log.exception("TX thread died with an unhandled exception")
        finally:
            log.warning("TX thread EXITING (running=%s) — queued commands will "
                        "NOT be sent until reconnect", self._running)

    def _dispatch(self, pkt: Packet) -> None:
        # Any packet from a real mount (1-5) proves it's present — used by the
        # wedge detector so a from-the-start wedge (mount never ACKs) is still
        # detected.  The hub sentinel (0xFE) and broadcast (0) are excluded.
        if 1 <= pkt.mount_id <= 5:
            self._rx_mounts.add(pkt.mount_id)
            self._mount_last_rx[pkt.mount_id] = time.monotonic()
        self._note_rx_ack(pkt)
        self._note_hub_diag(pkt)
        self._note_hub_event(pkt)
        self._note_sat_names(pkt)
        self._note_mount_route(pkt)
        self._note_sat_downlink(pkt)
        self._note_mount_outage(pkt)
        self._note_node_health(pkt)
        for cb in self._callbacks:
            try:
                cb(pkt)
            except Exception as e:
                log.error(f"Packet callback error: {e}")

    # Last summary logged per mount, so the 60 s resend does not repeat an
    # unchanged line into the log forever.  A mount that is DOWN is exempt: its
    # running total climbs, and that is worth a line each time.
    _outage_last: dict[int, tuple] = {}
    _outage_down: set[int] = set()
    _outage_seen = False

    # Same idea for the hub's USB port: report the state when it CHANGES, not
    # thirty times an hour. None means nothing has been said yet, so the first
    # report states whatever it finds.
    _hub_usb_state: str | None = None

    def _note_mount_outage(self, pkt: Packet) -> None:
        """How long each mount has been uncontrollable, since the hub booted.

        The one number an operator asks for, and the one nothing else here could
        answer.  Uptimes and reinit counts say an interruption happened; this
        says for how long.

        Silent about a mount that has never been out — the interesting line is
        the one with a number in it, and five "0 outages" lines a minute would
        bury it.
        """
        if pkt.cmd != Cmd.MOUNT_OUTAGE or len(pkt.payload) < MOUNT_OUTAGE_PAYLOAD_LEN:
            return
        try:
            stats = decode_mount_outage(pkt.payload)
        except ParseError as e:
            log.warning("MOUNT OUTAGE payload rejected: %s", e)
            return
        # Say once that the chain is alive.  Everything below is silent while
        # every mount is behaving, which is right — but a silent instrument and
        # a dead one read identically in a log, and the whole point of this one
        # is that its silence means something.  One line on first receipt makes
        # the difference legible: after it, no news IS good news.
        if not self._outage_seen:
            self._outage_seen = True
            # Whatever is already down on the first packet is the STATE OF THE
            # WORLD, not an event.  On this rig four of the five mounts are off
            # most of the time, so warning about each of them would put four
            # warnings in the log before the operator had done anything, every
            # single launch — which is the same burial as before, just once a
            # session instead of once a minute.
            #
            # So the baseline is latched silently and named in one line. A
            # warning from here means a mount went away WHILE WE WERE WATCHING,
            # which is the only version of this worth interrupting for.
            already = sorted(m for m, s in stats.items() if s["now"])
            self._outage_down.update(already)
            log.info("MOUNT OUTAGE accounting live — hub is reporting; from here "
                     "silence means no mount has been out, not a dead counter%s",
                     ("; not present right now: "
                      + ", ".join(f"cam{m}" for m in already)) if already else "")
        for mid, st in sorted(stats.items()):
            # ONCE per outage, on the edge.
            #
            # The hub re-sends these totals every minute, and while a mount is
            # down its running gap grows, so every packet carried a different
            # total and every one was logged.  A mount switched off deliberately
            # produced 461 of the 462 warnings in a 7.7 hour log and buried the
            # one that meant something.  A warning that fires sixty times an
            # hour for an unchanged condition is not a warning, it is weather.
            #
            # So: warn on the way down, say nothing while it stays down, and
            # note the recovery on the way back up — which re-arms the warning
            # for the next one.
            down_now = bool(st["now"])
            was_down = mid in self._outage_down
            if down_now:
                if was_down:
                    continue
                self._outage_down.add(mid)
            elif was_down:
                self._outage_down.discard(mid)

            key = (st["count"], st["total_s"], st["min_s"], st["max_s"], st["now"])
            if not down_now and not was_down and self._outage_last.get(mid) == key:
                continue
            self._outage_last[mid] = key
            total = st["total_s"]
            human = (f"{total // 60}m {total % 60}s" if total >= 60 else f"{total}s")
            line = ("MOUNT OUTAGE cam%d: uncontrollable %s total over %d event(s)"
                    % (mid, human, st["count"]))
            if st["count"]:
                line += ", min %ds, max %ds" % (st["min_s"], st["max_s"])
            if down_now:
                line += "  — DOWN AS OF NOW (silent until it returns)"
            elif was_down:
                line += "  — BACK"
            (log.warning if down_now else log.info)(line)

    def _note_sat_downlink(self, pkt: Packet) -> None:
        """What the relay was asked to do, against what its radio took.

        Refusals were the only thing counted, and a refusal count on its own
        cannot be read.  The Foyer satellite restarted 36 times in 17 hours with
        ~1100 refusals before each — which is a stopped radio if only ~1200 were
        offered, and a flooded relay if a million were.  Differencing two of
        these lines answers it directly, so the traffic question stops being an
        argument and becomes a subtraction.
        """
        if pkt.cmd != Cmd.SAT_DOWNLINK or len(pkt.payload) < SAT_DOWNLINK_MIN_LEN:
            return
        b = bytes(pkt.payload)
        offered, attempts, sent, refused = (
            int.from_bytes(b[i:i+4], "big") for i in (0, 4, 8, 12))
        # The tail is optional.  A satellite from before the send-callback
        # counters sends 25 bytes, and everything above it still reads.
        cb = leak = stall = None
        held = 0
        if len(b) >= 33:
            cb    = int.from_bytes(b[25:29], "big")
            leak  = (b[29] << 8) | b[30]
            stall = (b[31] << 8) | b[32]
        if len(b) >= SAT_DOWNLINK_PAYLOAD_LEN:
            held  = (b[33] << 8) | b[34]
        slot = pkt.mount_id - SAT_ADDR_BASE
        who  = self._sat_names.get(slot) or f"SAT {slot}"
        prev = self._sat_dn_prev.get(who)
        self._sat_dn_prev[who] = (offered, attempts, sent, refused, cb)
        if prev is None:
            return                        # nothing to difference against yet
        d_off, d_att, d_sent, d_ref = (a - b_ for a, b_ in
                                       zip((offered, attempts, sent, refused), prev))
        if d_off < 0 or d_att < 0:        # the satellite rebooted; counters reset
            return
        # A refusal burst with attempts far above offered is ONE frame being
        # retried, not many frames arriving — the pump holds the head frame on
        # NO_MEM and tries it again next pass.  That distinction is the whole
        # point of counting both.
        # What the traffic actually IS.  Windowed, straight from the satellite,
        # so it cannot be argued with from the hub's source — which was tried
        # twice and was wrong twice.
        tops = []
        for i in range(3):
            off = 16 + i*3
            if off + 3 > len(b):
                break
            c = b[off]
            n = (b[off+1] << 8) | b[off+2]
            if n:
                tops.append(f"{_cmd_name(c)}×{n}")
        breakdown = ("  [" + ", ".join(tops) + "]") if tops else ""
        note = ""
        if d_ref:
            note = (" | REFUSING — %d retries per frame offered"
                    % (d_att // max(1, d_off)) if d_off else " | REFUSING")
        # Why it is refusing, which the refusal count alone cannot say.  A radio
        # that is merely busy still returns its buffers; a radio whose callback
        # has stopped never does, and every send after the pool empties is
        # refused with NO_MEM.  Foyer refused 600 of 641 send calls on
        # 2026-09-09 and the log could only report the refusals.
        d_cb = None
        if cb is not None and len(prev) >= 5 and prev[4] is not None:
            d_cb = cb - prev[4]
            if d_cb < 0:                  # rebooted, or the stack was rebuilt
                d_cb = None
        tx = ""
        if leak:
            tx += " | TX BUFFERS NOT RETURNED %d%s" % (leak,
                                                       "+" if leak >= 0xFFFF else "")
        if stall:
            tx += " | no send callback for %.1fs" % (stall / 1000.0)
        if d_cb is not None and d_sent and not d_cb:
            tx += (" | %d sends accepted and NOT ONE callback — the pool is emptying"
                   % d_sent)
        # Not a fault, and deliberately not a warning: it is the burst bound
        # doing its job.  It is here because a satellite that stops wedging
        # while this reads 0 did not stop because of the bound.
        quiet = ""
        if held:
            quiet = " | sends held back %d%s" % (held, "+" if held >= 0xFFFF else "")
        fn = log.warning if (d_ref or tx) else log.info
        tx += quiet
        fn("SAT DOWNLINK %-12s offered %d, sent %d, refused %d (%d send calls)%s%s%s",
           who, d_off, d_sent, d_ref, d_att, note, tx, breakdown)

    @staticmethod
    def _wedge_who(mount: int) -> str:
        """Who the hub is escalating on — a mount, or its own radio.

        mount 0 means the send callback stopped firing, which is hub-wide: no
        mount can be acknowledging while it is happening, and none is implicated.
        Older hubs cannot say this — they sent worst_i + 1 whatever the cause —
        so a "mount 1" here from one of those may still be the hub.  On
        2026-09-09 the callback stalled twice and both were logged as mount 1.
        """
        if mount == 0:
            return "the SEND CALLBACK (hub-wide — txfail cannot see this)"
        return "mount %d" % mount

    def _note_mount_route(self, pkt: Packet) -> None:
        """Track which base each mount is on, for _hub_tx_proven_ok().

        MountManager already decodes this for the UI, but the wedge verdict is
        made down here and was reading every mount's ACK as equal evidence.
        They are not: a satellite-relayed mount answers over Ethernet, so its
        ACK says nothing whatever about this hub's radio.
        """
        if pkt.cmd != Cmd.MOUNT_ROUTE:
            return
        try:
            self._mount_route = decode_mount_route(pkt.payload)
        except Exception as e:                        # pragma: no cover
            log.warning("MOUNT_ROUTE decode failed in bridge: %s", e)

    def _note_sat_names(self, pkt: Packet) -> None:
        """Learn satellite names here rather than borrowing MountManager's copy.

        The names arrive on this packet stream anyway, and the health line needs
        them at the moment it is formatted — reaching across to the UI layer for
        a label would couple logging to a component that may not exist yet when
        the first health packet lands.
        """
        if pkt.cmd != Cmd.SAT_NAMES:
            return
        try:
            self._sat_names = decode_sat_names(pkt.payload)
        except Exception as e:
            log.warning("SAT_NAMES decode failed in bridge: %s", e)

    # A node up X seconds when we last looked must be up at least X + elapsed
    # now.  The slack absorbs clock skew and the couple of seconds between a
    # health packet being built on the node and timestamped here; it is far
    # smaller than any real reboot, which resets uptime to zero.
    NODE_UPTIME_TOLERANCE_S = 120.0
    NODE_STATE_WRITE_EVERY_S = 30.0

    @staticmethod
    def _node_state_path():
        from pathlib import Path
        return Path.home() / "Documents" / "PTS" / "node_state.json"

    def _node_state_load(self) -> dict:
        """Last-seen uptime per node from previous runs.  Best effort: a missing
        or corrupt file simply means the first report after start is not
        checked, which is the behaviour we had before."""
        try:
            import json
            with open(self._node_state_path(), "r") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _node_state_save(self, who: str, uptime_s: int, reset_reason: int) -> None:
        """Record and periodically persist.  Throttled because health arrives
        every few seconds per node and this is a convenience, not a ledger —
        losing the last few seconds of it costs nothing."""
        self._node_state_cur[who] = {"uptime_s": int(uptime_s),
                                     "reset": int(reset_reason),
                                     "at": time.time()}
        now = time.monotonic()
        if now - self._node_state_written < self.NODE_STATE_WRITE_EVERY_S:
            return
        self._node_state_written = now
        try:
            import json
            path = self._node_state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w") as fh:
                json.dump(self._node_state_cur, fh)
            tmp.replace(path)      # atomic — never leave a half-written file
        except Exception as e:
            log.debug("node_state save skipped: %s", e)

    # Satellite loop sections, matching the SSEC_* enum in esp32_satellite.ino.
    # A satellite reporting a slow pass says which of these it was inside; the
    # order here IS the wire encoding, so it must not be reordered.
    _SAT_SECTION_NAMES = {
        0: "top",      1: "uplink",   2: "espnow_up", 3: "espnow_dn",
        4: "peers",    5: "dnpump",   6: "clink",     7: "ws_up",
        8: "ws_dn",    9: "ws_flush", 10: "reports",
    }
    # Below this a "worst section" is noise — see where it is used.
    _SAT_LOOP_NOTE_MS = 100

    # esp_reset_reason() codes (ESP-IDF) → name, for the hub reboot log.
    _RESET_REASON_NAMES = {
        0: "UNKNOWN", 1: "POWERON", 2: "EXT", 3: "SW(esp_restart)",
        4: "PANIC(crash)", 5: "INT_WDT", 6: "TASK_WDT", 7: "WDT(other)",
        8: "DEEPSLEEP", 9: "BROWNOUT(power)", 10: "SDIO", 11: "USB",
        12: "JTAG", 13: "EFUSE", 14: "PWR_GLITCH", 15: "CPU_LOCKUP",
    }

    def _note_hub_diag(self, pkt: Packet) -> None:
        """Record the hub's USB-RX counters (Cmd.HUB_DIAG).  Used to split a
        wedge into host-side (bytes never reach the hub) vs hub-side (bytes
        arrive but aren't forwarded).  Also detects hub reboots: the counter is
        monotonic on the hub, so any drop means it reset — and the payload's
        reset-reason byte then tells us why (brownout / panic / watchdog…)."""
        if pkt.cmd != Cmd.HUB_DIAG or len(pkt.payload) < 8:
            return
        rx_bytes = int.from_bytes(pkt.payload[0:4], "big")
        rx_pkts  = int.from_bytes(pkt.payload[4:8], "big")
        reason   = pkt.payload[8] if len(pkt.payload) >= 9 else None
        now = time.monotonic()
        # A drop in the hub's monotonic byte counter means the hub rebooted.
        if self._hub_rx_bytes > 0 and rx_bytes < self._hub_rx_bytes:
            rname = (self._RESET_REASON_NAMES.get(reason, f"code {reason}")
                     if reason is not None else "unknown (pre-diag firmware)")
            log.warning("HUB REBOOTED — rx counter reset %d → %d | reset reason: %s",
                        self._hub_rx_bytes, rx_bytes, rname)
        if rx_bytes > self._hub_rx_bytes:
            self._hub_rx_last_advance_t = now
        self._hub_rx_bytes  = rx_bytes
        self._hub_rx_pkts   = rx_pkts
        self._hub_diag_rx_t = now
        # Optional trailing ghost-drop counter (firmware ≥ the phantom-cam guard).
        if len(pkt.payload) >= 13:
            self._hub_ghost_drops = int.from_bytes(pkt.payload[9:13], "big")

    # Mount state names for the hub-event log (mirrors MountState).
    _STATE_NAMES = {
        0: "IDLE", 1: "JOGGING", 2: "MOVING", 3: "FINDING_LIMITS",
        4: "ERROR", 5: "LOOK_AT_MOVE", 6: "CALIBRATING", 7: "LOOK_AT_PRE_AIM",
    }

    def _note_node_health(self, pkt: Packet) -> None:
        """Log a CMD_HEALTH record (uniform node telemetry).

        Senders: mount_id 1-5 = that mount (payload says bridge vs teensy),
        0xFE = hub, 0xFD = hub display.  Anomaly-flagged records (first boot
        report, low heap, loop stall, TX-fail jump) log as WARNING; routine
        10 s reports log as INFO.  Grep 'NODE HEALTH' to trend any node —
        a falling min-heap or climbing txfail hours before symptoms is the
        early warning this exists for.
        """
        if pkt.cmd != Cmd.HEALTH:
            return
        try:
            h = decode_health(pkt.payload)
        except ParseError as e:
            log.warning("NODE HEALTH undecodable from mount_id=%d: %s",
                        pkt.mount_id, e)
            return
        if pkt.mount_id == 0xFE:
            who = "hub"
        elif pkt.mount_id == 0xFD:
            who = "display"
        elif h.node_type == HEALTH_NODE_SATELLITE:
            # Named, not numbered, for the same reason the route line is: the
            # slot is TCP accept order and means nothing to someone standing in
            # the building, whereas "Foyer" is the box they can go and look at.
            slot = pkt.mount_id - SAT_ADDR_BASE
            who  = self._sat_names.get(slot) or f"SAT {slot}"
        else:
            who = f"cam{pkt.mount_id}/{h.node_name}"
        # BLE camera state, on builds that have it.  A mount on a rig has no
        # readable serial port, so this is the only place its BLE link is
        # visible — and it sits next to txfail, which is exactly what it has to
        # be compared against.
        ble = ""
        if h.flags & HEALTH_FLAG_BLE_BUILD:
            linked = bool(h.flags & HEALTH_FLAG_BLE_LINK)
            unpaired = bool(h.flags & HEALTH_FLAG_CAM_UNPAIRED)
            # No bond: say NOTHING.  This is the normal state for every mount
            # without a camera of its own, and on a five-mount rig with one
            # camera that was four nodes each repeating a non-event every ten
            # seconds — 486 lines in one 93-minute log. A health line should
            # carry what changed or what is wrong, and this is neither.
            if unpaired:
                ble = ""
            else:
                ble = " | BLE PAIRED" if linked else " | BLE down"
            if linked:
                if not h.flags & HEALTH_FLAG_CAM_SUBSCR:
                    ble += " (NOT subscribed — no gain/WB)"
                elif h.flags & HEALTH_FLAG_CAM_RX:
                    ble += " (subscribed, camera reporting)"
                else:
                    # Subscribed but silent: the CCCD write succeeded and
                    # nothing has ever arrived.  Different fault from a relay
                    # that drops what it receives, and they look the same
                    # without this.
                    ble += " (subscribed but camera has never reported)"
            if h.flags & HEALTH_FLAG_CAM_WR_ERR:
                ble += " | CAMERA WRITE FAILED"
            if h.flags & HEALTH_FLAG_CAM_CACHE_FULL:
                # Silent data loss otherwise: some camera value simply never
                # arrives, and the only symptom is a dash where a number
                # belongs — which has already been misdiagnosed twice.
                ble += " | CAM CACHE FULL — a parameter is being dropped"
            # Kept so the camera-control dialog can grey a button rather than
            # firing into a link that is not there.  A plain dict read by the
            # UI on a timer: health arrives every 10 s, so a signal would add
            # plumbing for an update rate nothing can perceive.
            if 1 <= pkt.mount_id <= 5:
                self._cam_ble[pkt.mount_id] = "unpaired" if unpaired else linked
        # A bridge packs two counters into the node-specific u32: refusals in the
        # high half, ESP-NOW reinits in the low half.  Refusals are sends that
        # esp_now_send() rejected outright, so they never reach the send callback
        # and never move txfail — a mount can go completely silent with txfail
        # frozen, which is what mount 5 did on 2026-08-11.  Shown next to txfail
        # because that is the number it has to be read against.  The low half is
        # unchanged, so older logs still read the same.
        n32 = h.node_u32
        if h.node_name == "bridge":
            # High half is the bridge's PERSISTENT isolation-restart count, kept
            # in its NVS so it survives both the restart that rescues it and a
            # power cycle. Every other counter on a wedged mount is reset by the
            # very restart that ends the wedge, which is why cam1 wedging twice
            # in 44 hours was only visible to someone willing to read 44 hours
            # of log.
            #
            # It used to carry the refused-send count, on the reasoning that a
            # refusal climbing is the wedge starting. The field log disproved
            # that: zero in every health report, including cam1's own 18 seconds
            # before it went down. A refusal means the radio will not accept a
            # send, so the report carrying the number is the one thing that
            # cannot get out. It survives in the post-mortem MOUNT_EVENT, which
            # is the only place it has ever actually been read.
            # wedges(8) | leak floor(8) | drain deferrals(8) | reinit(8).
            # All four saturate; reinit gave up its top byte when the drain
            # bound went in, having never exceeded 3.
            wedges = (n32 >> 24) & 0xFF
            leak   = (n32 >> 16) & 0xFF
            defer  = (n32 >> 8) & 0xFF
            n32txt = "n32 %d" % (n32 & 0xFF)
            if wedges:
                n32txt += " | WEDGES %d" % wedges
            # Whether the burst bound ever engaged. A mount that stops wedging
            # while this reads 0 did NOT stop because of the bound, and that is
            # worth knowing before anyone calls the fault fixed.
            if defer:
                n32txt += " | sends held back %d%s" % (defer,
                                                      "+" if defer == 255 else "")
            # The leak, and the only number here that moves BEFORE the fault.
            # esp_now_send() takes a buffer from a small pool and the send
            # callback returns it; this is how many were taken and never given
            # back. Every other counter on a wedged mount moves only once the
            # pool is already empty, and by then the radio cannot report it —
            # cam1's own health read a clean zero 18 seconds before it went.
            #
            # Anything but 0 is worth knowing. A floor that climbs across
            # reports is the wedge arriving in slow motion.
            if leak:
                n32txt += " | TX BUFFERS LEAKED %d" % leak
        elif h.node_type == HEALTH_NODE_SATELLITE:
            # Same packing, different pair: refusals high, self-restart streak
            # low.  The streak is spelled out rather than left as a number
            # because a satellite sitting at its cap has stopped trying to fix
            # itself, and that is the state that left two mounts unreachable
            # with every other counter reading zero.
            nomem  = (n32 >> 16) & 0xFFFF
            sect   = (n32 >> 8) & 0xFF
            streak = n32 & 0xFF
            n32txt = "nomem %d" % nomem
            if streak:
                n32txt += " | SELF-RESTARTS %d" % streak
            # Which part of the satellite's loop owned the worst pass this
            # window.  Only worth printing when the pass was slow enough to
            # mean something: on an idle loop the winning section is whichever
            # one happened to take 3ms instead of 2, which is noise dressed as
            # a finding.  The satellite windows loop_max_ms now, so this
            # section always belongs to the number beside it.
            if h.loop_max_ms >= self._SAT_LOOP_NOTE_MS:
                n32txt += " | worst section '%s'" % self._SAT_SECTION_NAMES.get(
                    sect, str(sect))
        elif who == "hub":
            # sends held back(8) | ring overflows(8) | ghost drops(16).
            #
            # "held back" is not a fault — it is the TX burst bound doing its
            # job, and it is here because a hub that stops wedging while this
            # reads 0 did not stop because of the bound.  Ghosts are in the LOW
            # half so a hub running older firmware, which sends the bare count
            # in the whole word, still reads as exactly that many ghosts.
            ghosts = n32 & 0xFF
            gheld  = (n32 >> 24) & 0xFF
            gdrop  = (n32 >> 16) & 0xFF
            gleak  = (n32 >> 8) & 0xFF
            n32txt = "n32 %d" % ghosts
            # Windowed, unlike the mount's, and it has to SAY so: cumulative it
            # saturated four minutes into a boot, and a reader who takes a
            # windowed 17 for a since-boot total concludes the bound is barely
            # firing when it is firing constantly.
            if gheld:
                n32txt += " | sends held back %d%s per 10s" % (
                    gheld, "+" if gheld == 255 else "")
            # This one IS a fault: sixteen deep behind a 400 ms escape means the
            # radio stopped entirely, and a command was thrown away.
            if gdrop:
                n32txt += " | TX QUEUE OVERFLOWED %d%s" % (gdrop,
                                                           "+" if gdrop == 255 else "")
            # Buffers the radio took and never returned. It is subtracted from
            # in_flight before the bound is applied, so a rising floor is a
            # permanent loss and NOT a reason for the cap to hold everything.
            if gleak:
                n32txt += " | TX BUFFERS LEAKED %d%s" % (gleak,
                                                         "+" if gleak == 255 else "")
        else:
            n32txt = "n32 %d" % n32
        line = ("NODE HEALTH %-12s up %6.2fh | heap %5dk (min %5dk) | "
                "loopmax %4dms | txfail %d | rssi %d | %s | reset %d%s") % (
            who, h.uptime_s / 3600.0,
            h.free_heap // 1024, h.min_free_heap // 1024,
            h.loop_max_ms, h.tx_fail, h.rssi, n32txt, h.reset_reason, ble)
        # Any node whose uptime goes BACKWARDS has restarted.  Derived from
        # CMD_HEALTH rather than the hub's USB byte counter, so it works on
        # every transport and for every node — the counter-based HUB REBOOTED
        # is USB-only, which meant that switching to TCP (the thing that stops
        # the host resetting the hub) silently removed the only way to notice
        # the hub rebooting.  This also covers reboots that happen while the
        # app is closed: the first report after reconnecting shows a low uptime
        # against the last one we saw.
        rname = self._RESET_REASON_NAMES.get(h.reset_reason, f"code {h.reset_reason}")

        # Within this session an exact comparison is enough.
        prev = self._node_uptime.get(who)
        if prev is not None and h.uptime_s < prev:
            log.warning("NODE REBOOTED — %s uptime %.2fh -> %.2fh | reset reason: %s",
                        who, prev / 3600.0, h.uptime_s / 3600.0, rname)
        elif prev is None:
            # First report this session.  Compare against what was persisted,
            # allowing for the wall-clock time since: a node up X seconds then,
            # and still running, must be up at least X + elapsed now.
            #
            # Without this a reboot during any gap in our own coverage is
            # invisible — and the gap that matters most is the app being closed
            # or restarted.  One capture missed a mount self-restarting and
            # vanishing for 57 minutes purely because the app was reopened in
            # the middle of it, which is exactly when you least want the
            # detector to go quiet.
            was = self._node_state_prev.get(who)
            if was:
                elapsed  = max(0.0, time.time() - was.get("at", 0.0))
                expected = was.get("uptime_s", 0) + elapsed
                if h.uptime_s + self.NODE_UPTIME_TOLERANCE_S < expected:
                    log.warning("NODE REBOOTED (while we were not watching) — %s "
                                "uptime %.2fh, expected ~%.2fh | reset reason: %s",
                                who, h.uptime_s / 3600.0, expected / 3600.0, rname)
        self._node_uptime[who] = h.uptime_s
        self._node_state_save(who, h.uptime_s, h.reset_reason)

        if h.anomaly:
            log.warning("%s [ANOMALY]", line)
        else:
            log.info(line)

    def _note_hub_event(self, pkt: Packet) -> None:
        """Log a hub event (Cmd.HUB_EVENT).

        kind 0: mount online (real connect — negative RSSI, real state)
        kind 1: ghost STATUS frame dropped (rssi==0 phantom-camera guard)
        kind 2: hub ran an AUTONOMOUS ESP-NOW reinit (wedge; state=wedge secs,
                flags=send-fail run)
        kind 3: hub is about to AUTONOMOUSLY RESTART (wedge persisted;
                state=wedge secs) — expect a brief disconnect + HUB REBOOTED
        kind 4: hub is about to do a MAINTENANCE restart (state=uptime hours)
        """
        if pkt.cmd != Cmd.HUB_EVENT or len(pkt.payload) < 9:
            return
        kind   = pkt.payload[0]
        mount  = pkt.payload[1]
        rssi   = pkt.payload[2] - 256 if pkt.payload[2] >= 128 else pkt.payload[2]
        state  = pkt.payload[3]
        flags  = pkt.payload[4]
        uptime = int.from_bytes(pkt.payload[5:9], "big")
        hrs    = uptime / 3600.0
        if kind == 1:
            sname = self._STATE_NAMES.get(state, f"0x{state:02X}")
            log.warning("HUB EVENT: GHOST frame dropped — mount=%d rssi=%d dBm "
                        "state=%s flags=0x%02X | hub uptime %.1fh (%ds) | total ghost drops=%d",
                        mount, rssi, sname, flags, hrs, uptime, self._hub_ghost_drops)
        elif kind == 2:
            log.warning("HUB EVENT: hub SELF-REINIT of ESP-NOW — TX wedge on %s "
                        "for %ds (fail run %d) | hub uptime %.1fh",
                        self._wedge_who(mount), state, flags, hrs)
        elif kind == 3:
            log.warning("HUB EVENT: hub SELF-RESTART imminent — TX wedge on %s "
                        "persisted %ds despite reinit | hub uptime %.1fh "
                        "(brief disconnect expected)",
                        self._wedge_who(mount), state, hrs)
        elif kind == 4:
            log.info("HUB EVENT: hub MAINTENANCE RESTART at %dh uptime (system idle) "
                     "— brief disconnect expected", state)
        elif kind == 5:
            log.info("HUB EVENT: mount PAIRED — CAM %d bound to MAC ..:%02X:%02X "
                     "(rssi=%d dBm) | hub uptime %.1fh",
                     mount, state, flags, rssi, hrs)
        elif kind == 6:
            log.info("HUB EVENT: mount RENUMBERED — CAM %d → CAM %d (MAC ..:%02X) "
                     "| hub uptime %.1fh",
                     state, mount, flags, hrs)
        elif kind == 7:
            log.warning("HUB EVENT: PAIRING CONFLICT — device ..:%02X:%02X claims "
                        "CAM %d but that slot is bound to another mount; claim "
                        "rejected (renumber one of them on its setup screen)",
                        state, flags, mount)
        elif kind == 8:
            # Own layout: [1..4] peer IP, [5..6] reply port, [7..8] messages
            # sent since the last report.  The hub's USB serial carries the
            # binary packet stream, so this is the only readable place to say
            # where OSC feedback is going — and UDP reports no error, so a
            # count climbing next to dark buttons is the distinguishing symptom.
            ip   = ".".join(str(b) for b in pkt.payload[1:5])
            port = int.from_bytes(pkt.payload[5:7], "big")
            sent = int.from_bytes(pkt.payload[7:9], "big")
            log.info("HUB EVENT: OSC feedback → %s:%d — %d message(s) sent%s",
                     ip, port, sent,
                     "" if sent else "  (nothing sent since last report)")
        elif kind == 9:
            # Own layout: [1] worst loop section, [2..3] that section's worst
            # pass (ms), [4..5] worst whole pass (ms), [6..7] loops/s,
            # [8] serial frames dropped.  The hub had no loop timing at all
            # until a 13.8 s stall wedged a mount and nothing could say where
            # the time had gone.
            names = ("top", "accept", "sat", "osc", "tcp", "usb", "ws",
                     "disp", "relay")
            sec  = names[pkt.payload[1]] if pkt.payload[1] < len(names) else "?"
            sms  = int.from_bytes(pkt.payload[2:4], "big")
            pms  = int.from_bytes(pkt.payload[4:6], "big")
            lps  = int.from_bytes(pkt.payload[6:8], "big")
            drop = pkt.payload[8]          # percent of serial frames dropped
            fn = log.warning if pms >= 1000 else log.info
            fn("HUB LOOP: %d loops/s | worst pass %d ms | worst section '%s' "
               "%d ms", lps, pms, sec, sms)

            # Said on a CHANGE, not on every report.
            #
            # It used to ride on the line above, every 30 s, for ever: the hub's
            # USB port has nothing reading it because the app is on TCP and USB
            # is power and flashing only, so "all frames dropped" was the
            # permanent normal state. A warning that is always on is not a
            # warning — it trains the eye past the line, and the case that
            # matters (something IS reading and losing frames) would have
            # scrolled by in the same words.
            #
            # The first report after a restart states the state whatever it is,
            # so a rotated log always establishes its baseline rather than
            # leaving it to be inferred from silence.
            state = "unread" if drop >= 100 else ("partial" if drop else "clean")
            if state != self._hub_usb_state:
                first = self._hub_usb_state is None
                self._hub_usb_state = state
                if state == "unread":
                    log.info("HUB USB serial: nothing is reading it, frames "
                             "dropped%s — normal when the app is on TCP and USB "
                             "is power and flashing only", " (at startup)" if first else "")
                elif state == "partial":
                    log.warning("HUB USB serial: %d%% of frames dropped — "
                                "something IS reading the port and not keeping "
                                "up, which loses hub diagnostics", drop)
                else:
                    log.info("HUB USB serial: being read cleanly, no frames "
                             "dropped")
        elif kind == 10:
            # Frames the hub SHED to a slow TCP client — counted since 2026-06,
            # reported only to a serial port nothing reads, in a build where the
            # report was compiled out.  It is the hub's one deliberate
            # frame-discard path, and it does not distinguish a STATUS (replaced
            # 200 ms later, safe to drop) from an ACK (never repeated, so a
            # dropped one looks exactly like a command the mount ignored).
            drop = int.from_bytes(pkt.payload[1:5], "big")
            sent = int.from_bytes(pkt.payload[5:9], "big")
            log.warning("HUB SHED %d of %d client writes (%.1f%%) — a dropped ACK "
                        "is indistinguishable from a command that never landed",
                        drop, sent, 100.0 * drop / max(1, sent))
        elif kind == 11:
            # The relay queue refusing a frame — until now the one discard on
            # this rig with no counter at all: xQueueSend() with a zero timeout
            # and its result thrown away.  It only bites mounts on the hub's own
            # radio, whose frames are enqueued by the WiFi task asynchronously;
            # satellite frames are enqueued inside loop() in the same pass that
            # drains the queue, so they never find it full.
            drop = int.from_bytes(pkt.payload[1:5], "big")
            q    = int.from_bytes(pkt.payload[5:9], "big")
            log.warning("HUB RELAY QUEUE FULL — dropped %d of %d frames (%.1f%%) "
                        "arriving from mounts; a dropped ACK reads as a command "
                        "the mount ignored",
                        drop, q, 100.0 * drop / max(1, q))
        elif kind == 12:
            # The hub rebinding a mount.  Every one of these zeroes the hub's
            # last-seen for that slot, so the next STATUS reads as a fresh
            # connect: the display flaps the mount in and out and the log fills
            # with "mount N ONLINE" for no stated reason.  Until now these were
            # announced only by Serial.printf — on a bench rig the PC app owns
            # that port and reads it as a packet stream, so they were discarded
            # as noise between frames.
            acts = {1: "RENUMBERED to an empty slot", 2: "REPLACED (rebound)",
                    3: "FORGOTTEN (unbound)"}
            mac = ":".join(f"{b:02X}" for b in pkt.payload[3:9])
            log.warning("HUB PAIRING: cam%d %s — %s | this resets the hub's "
                        "last-seen, so the next STATUS looks like a new connect",
                        pkt.payload[1], acts.get(pkt.payload[2], f"action {pkt.payload[2]}"), mac)
        elif kind == 14:
            # One outage, as it ends.  The duration is the gap in that mount's
            # own traffic — last packet before it went quiet to first packet
            # after — so it counts a bridge reboot, an ESP-NOW reinit, a radio
            # wedge and a pulled plug alike.  From the operator's chair those
            # are the same event: the mount did not answer.
            secs = (pkt.payload[3] << 8) | pkt.payload[4]
            log.warning("MOUNT OUTAGE: cam%d was uncontrollable for %ds", mount, secs)
        elif kind == 13:
            # An OSC camera command, by name.
            #
            # Every camera command travels as one CMD_CAM_CONTROL, so the
            # satellite's per-type counter cannot tell a tally from a focus, and
            # that counter is printed as a top-3-per-window summary — so a few
            # camera commands disappear behind PING and JOG completely.  Asked
            # which commands had gone out, the log could only say "at least 3,
            # some of them CAM_CONTROL".  This is the hub naming each one.
            #
            # Tally arrives as the raw 5.11 fixed-point value that goes to the
            # camera, so what is logged is what was sent, not what was meant.
            verb  = pkt.payload[2]
            value = (pkt.payload[3] << 8) | pkt.payload[4]
            if verb == 0:
                what = "autofocus"
            elif verb in (1, 2, 3):
                lamp = {1: "both", 2: "front", 3: "rear"}[verb]
                what = f"tally {lamp} = {value / 2048.0:.2f}"
            elif verb == 4:
                what = "record START" if value == 2 else "record STOP"
            elif verb == 5:
                # TEMPORARY — an OSC jog, which the PC app cannot otherwise see:
                # these reach the mounts through the hub and never pass through
                # this app at all.
                mask, neg = value & 0x0F, (value >> 8) & 0x0F
                if not mask:
                    what = "jog STOP"
                else:
                    ax = [n for b, n in ((1, "pan"), (2, "tilt"),
                                         (4, "slider"), (8, "zoom")) if mask & b]
                    sign = ["-" if neg & b else "+"
                            for b, _ in ((1, 0), (2, 0), (4, 0), (8, 0)) if mask & b]
                    what = "jog " + ", ".join(f"{n}{s}" for n, s in zip(ax, sign))
            else:
                what = f"verb {verb} = {value}"
            log.info("OSC CMD: cam%d %s", mount, what)
        else:
            sname = self._STATE_NAMES.get(state, f"0x{state:02X}")
            log.info("HUB EVENT: mount %d ONLINE — rssi=%d dBm state=%s flags=0x%02X "
                     "| hub uptime %.1fh (%ds)",
                     mount, rssi, sname, flags, hrs, uptime)

    def _log_wedge_side(self, now: float, mount: int = 0) -> None:
        """At wedge time, say WHERE the stall is — with only the evidence to hand.

        The hub's USB-RX counters answer one question: are our bytes reaching
        the hub?  Frozen means they are not (host-side, a Windows USB-CDC OUT
        halt).  Climbing means they are.

        Climbing does NOT mean the hub is at fault, and this used to say it did:
        "HUB-SIDE — bytes ARE reaching the hub but aren't forwarded; fix is in
        hub firmware".  That is a false dichotomy — it rules out the host and
        then blames the hub without ever having looked past it.  The mount is
        the third possibility and, on this rig, the usual one.

        Logged on 2026-09-04 19:47, one line apart:

            → HUB-SIDE — ... fix is in hub firmware
            Mount 1 unreachable 12.8s, but mount 4 is still ACKing — ...
            this is mount-side. Not touching the hub; mount 1 likely needs a
            power cycle.

        The second was right: cam1's bridge had run its ESP-NOW stack out of
        memory and rebooted itself a second later.  The first was the more
        alarming of the two and would have sent someone into hub firmware.

        So the mount is ruled out before the hub is blamed, using the check
        that already existed for it — an ACK from any OTHER mount proves the
        hub's transmit path works, which leaves the silent mount as the fault."""
        if self._hub_diag_rx_t is None:
            # Say what is actually unknown.  "Firmware may predate the
            # diagnostic" was written when USB was the only transport and was
            # simply wrong once the hub was current but the packet was still
            # Serial-only — it sent people looking at the wrong thing.
            log.warning("WEDGE mount=%d host/hub: no HUB_DIAG received, so we cannot "
                        "tell whether our bytes are reaching the hub. Either the hub "
                        "predates the diagnostic, or it is current but not sending it "
                        "on this transport (it was Serial-only before 2026-08-04).",
                        mount)
            return
        since_diag = now - self._hub_diag_rx_t
        since_adv  = (now - self._hub_rx_last_advance_t
                      if self._hub_rx_last_advance_t is not None else 1e9)
        if since_diag > 3.0:
            verdict = "INCONCLUSIVE — hub diag itself stale (hub→PC also affected?)"
        elif since_adv >= 2.5:
            verdict = ("HOST-SIDE — our bytes are NOT reaching the hub "
                       "(Windows USB-CDC OUT halt); fix is host port/cable/driver")
        else:
            peer = self._hub_tx_proven_ok(now, mount) if mount else 0
            if peer:
                verdict = (f"MOUNT-SIDE — our bytes reach the hub, and mount {peer} "
                           f"is still ACKing, so the hub forwards fine. Mount {mount} "
                           "is the fault; check its bridge, not the hub")
            elif mount:
                verdict = ("HUB-SIDE OR MOUNT-SIDE — our bytes reach the hub, but no "
                           "other mount is ACKing either, so nothing here separates a "
                           "hub that has stopped forwarding from a mount that has "
                           "stopped answering. Power one other mount on to tell them "
                           "apart")
            else:
                verdict = ("PAST THE HUB — our bytes reach the hub, so the host and "
                           "the OUT pipe are fine; the fault is downstream of it")
        log.warning("WEDGE mount=%d host/hub: hub_rx_bytes=%d pkts=%d | hub last received "
                    "PC bytes %.1fs ago | last diag %.1fs ago → %s",
                    mount, self._hub_rx_bytes, self._hub_rx_pkts, since_adv, since_diag, verdict)

    # ------------------------------------------------------------------
    # Diagnostic helpers — TX command / ACK round-trip tracking
    # ------------------------------------------------------------------

    @staticmethod
    def _new_cmd_stat() -> dict:
        return {"sent": 0, "acked": 0, "lost": 0, "slow": 0,
                "rtt_sum": 0.0, "rtt_max": 0.0}

    def _cmd_ledger_tick(self, now: float) -> None:
        """Age out commands nothing answered, and report per mount.

        The report is what the operator actually asked for: not "did anything
        reboot", but what fraction of what the app asked a mount to do, the
        mount did.  Those are different questions and a rig can pass the first
        while failing the second — mount 4 spent an hour looking perfectly
        connected while dropping 79% of its commands.
        """
        with self._diag_lock:
            for seq in [q for q, (t, _m) in self._cmd_ledger.items()
                        if now - t > self.CMD_DEADLINE_S]:
                _t, mnt = self._cmd_ledger.pop(seq)
                self._cmd_stat.setdefault(mnt, self._new_cmd_stat())["lost"] += 1
            if now - self._cmd_report_t < self.CMD_REPORT_S:
                return
            self._cmd_report_t = now
            snapshot = {m: dict(v) for m, v in self._cmd_stat.items()}
            self._cmd_stat = {}
        for mnt in sorted(snapshot):
            v = snapshot[mnt]
            done = v["acked"]
            # In flight at the cut-off: neither answered nor yet late.  Excluded
            # from the denominator rather than counted as either.
            settled = done + v["lost"]
            if not settled:
                continue
            pct  = 100.0 * done / settled
            mean = (v["rtt_sum"] / done) if done else 0.0
            who  = "hub" if mnt == 0xFE else f"cam{mnt}"
            fn = log.warning if v["lost"] or v["slow"] else log.info
            fn("CMD %-5s %d/%d acknowledged (%.3f%%) | lost %d, slow %d | "
               "rtt mean %.0f ms, worst %.0f ms",
               who, done, settled, pct, v["lost"], v["slow"], mean, v["rtt_max"])

    def _note_tx_command(self, data: bytes) -> None:
        """Called from the TX thread after an interesting command is written.
        Logs it and records its seq so the matching ACK can be timed."""
        if len(data) <= _PKT_OFF_CMD:
            return
        cmd_val = data[_PKT_OFF_CMD]
        if cmd_val in _TX_QUIET_CMDS:
            return
        seq = (data[_PKT_OFF_SEQ_HI] << 8) | data[_PKT_OFF_SEQ_LO]
        mount = data[_PKT_OFF_MOUNT]
        name = _cmd_name(cmd_val)
        now = time.monotonic()
        self._tx_cmd_sent += 1
        self._last_tracked_tx_t = now
        with self._diag_lock:
            self._pending_acks[seq] = (now, name, mount)
            self._cmd_ledger[seq] = (now, mount)
            self._cmd_stat.setdefault(mount, self._new_cmd_stat())["sent"] += 1
        log.info("TX → %-18s mount=%d seq=%d (%d bytes) — awaiting ACK",
                 name, mount, seq, len(data))

    def seconds_since_tracked_tx(self) -> float:
        """Seconds since the last ACK-tracked command was written.  The idle
        probe uses this to inject a probe only when the user isn't operating."""
        return time.monotonic() - self._last_tracked_tx_t

    def _note_rx_ack(self, pkt: Packet) -> None:
        """Called from the RX thread for every received packet.  If it's an
        ACK/NACK matching a tracked command, logs the round-trip time."""
        if pkt.cmd not in (Cmd.ACK, Cmd.NACK):
            return
        if len(pkt.payload) < 2:
            return
        acked_seq = (pkt.payload[0] << 8) | pkt.payload[1]
        # Ledger first, and unconditionally: this must not inherit the pruning
        # below, which is what hid late answers from the record.
        with self._diag_lock:
            rec = self._cmd_ledger.pop(acked_seq, None)
            if rec is not None:
                sent_t, mnt = rec
                st = self._cmd_stat.setdefault(mnt, self._new_cmd_stat())
                rtt = (time.monotonic() - sent_t) * 1000.0
                st["acked"] += 1
                st["rtt_sum"] += rtt
                if rtt > st["rtt_max"]: st["rtt_max"] = rtt
                if rtt > self.CMD_SLOW_MS: st["slow"] += 1
        with self._diag_lock:
            entry = self._pending_acks.pop(acked_seq, None)
            # An ACK proves the mount is responding *now*, so anything we sent it
            # earlier is lost, not outstanding.  Drop those too.
            #
            # Without this, one dropped ACK left a stale entry ageing for the full
            # 60 s prune window, _oldest_overdue() reported it as an unacked
            # command, and the wedge logic escalated to reinit-then-restart the
            # hub — while that mount was demonstrably healthy and ACKing every
            # subsequent command.  A single lost packet rebooted the hub, and the
            # reboot is what actually dropped the mounts.
            if entry is not None:
                sent_at = entry[0]
                for s in [s for s, (t, _n, m) in self._pending_acks.items()
                          if m == entry[2] and t <= sent_at]:
                    del self._pending_acks[s]
        if entry is None:
            return   # ACK for a periodic/quiet command we didn't track
        sent_t, name, mount = entry
        now_t = time.monotonic()
        dt_ms = (now_t - sent_t) * 1000.0
        self._tx_cmd_acked += 1
        self._mount_last_ack[mount] = now_t
        # This mount has now proven it can respond — its future silence is a real
        # wedge signal, unlike a mount that has never been heard from.  (The ACK
        # also removes this seq from _pending_acks above, so it no longer counts
        # toward the wedge age — that is what clears a suspected wedge.)
        self._acked_mounts.add(mount)
        if pkt.cmd == Cmd.NACK and len(pkt.payload) >= 3:
            log.warning("RX ← NACK  %-18s seq=%d err=%d (%.0f ms)",
                        name, acked_seq, pkt.payload[2], dt_ms)
        else:
            log.info("RX ← ACK   %-18s seq=%d (%.0f ms)", name, acked_seq, dt_ms)

    def _wedge_check(self, now: float) -> tuple[float, int, int]:
        """Inspect in-flight tracked commands for a wedged command link.

        Returns (oldest_overdue_age, oldest_mount, pending):
          oldest_overdue_age — age (s) of the oldest unacked command to a mount
                               that has ACKed before.  When this exceeds
                               WEDGE_DETECT_S the PC→hub command path is wedged.
                               Commands to mounts never heard from (absent) are
                               ignored — they would otherwise force a reconnect
                               loop when not all mounts are connected.  Mounts
                               that ACKed inside MOUNT_ACK_STALE_S are ignored
                               too: a live mount with one lost ACK is not a
                               wedge, and treating it as one restarts the hub
                               (all mounts down) to cure a mount that was never
                               ill.  A mount that is sending but not ACKing is
                               NOT excused — that is the hub→mount send wedge
                               and it has to escalate.
          oldest_mount       — mount_id that owns that oldest command (0 if none).
                               Lets the log attribute a wedge to a specific mount,
                               so with several connected we can see whether wedges
                               cluster on one unit or spread across the shared link.
          pending            — total commands still awaiting an ACK.

        Also prunes entries older than 60 s so the dict can't grow without bound
        (e.g. a permanently-absent mount queried every few seconds).
        """
        oldest = 0.0
        oldest_mount = 0
        with self._diag_lock:
            for s in [s for s, (t, _n, _m) in self._pending_acks.items()
                      if now - t > 60.0]:
                del self._pending_acks[s]
            for t, _name, mt in self._pending_acks.values():
                if mt not in self._acked_mounts and mt not in self._rx_mounts:
                    continue                      # absent mount — never escalate
                if now - self._mount_last_ack.get(mt, 0.0) < self.MOUNT_ACK_STALE_S:
                    continue                      # still ACKing — not wedged
                if now - self._mount_last_rx.get(mt, 0.0) > self.MOUNT_SILENT_S:
                    continue                      # not talking at all — it is
                                                  # switched off, not wedged
                age = now - t
                if age > oldest:
                    oldest = age
                    oldest_mount = mt
            pending = len(self._pending_acks)
        return oldest, oldest_mount, pending

    def cam_ble_link(self, mount_id: int):
        """One of True, False, "unpaired", or None.

        Four states because each wants a different sentence from the UI:
        True = usable; False = camera off or out of range, try the camera;
        "unpaired" = the mount has no bond and has given up, reflash it;
        None = firmware predates camera support, reflash it with anything
        current.  Collapsing any pair of these sends someone to check the
        wrong thing.
        """
        return self._cam_ble.get(mount_id)

    def _hub_tx_proven_ok(self, now: float, exclude_mount: int) -> int:
        """Return a mount_id (other than exclude_mount) that has ACKed inside
        MOUNT_ACK_STALE_S over the HUB'S OWN RADIO, or 0 if none has.

        An ACK from another mount on this radio is proof the hub's ESP-NOW
        transmit path works — the command reached that mount and its reply came
        back.  So a single unreachable mount alongside a healthy one is a
        MOUNT-side fault, and no hub-level recovery can help: reinit and restart
        both take every other mount down for nothing.

        A SATELLITE-RELAYED mount is not that proof and never was.  Its commands
        go out over Ethernet to the satellite, which transmits them on its own
        radio; the hub's ESP-NOW path is not involved at any point, so its ACK
        is silent on the only question being asked.  On 2026-09-09 the hub's
        send callback stalled twice, and both times this said:

            Mount 1 unreachable 8.5s, but mount 4 is still ACKing —
            hub TX is healthy, so this is mount-side.  Not touching the hub;
            mount 1 likely needs a power cycle.

        cam4 was via Foyer.  The verdict was exactly backwards, and the hub's own
        detector — which does make this distinction — was reinitialising its
        radio at that moment.  The hub firmware learned it the same way, after a
        rig logged 61 escalations all naming the satellite-attached mount
        (esp32_hub_eth.ino, "A satellite-relayed mount is NOT judged by this
        radio"); this copy of the judgement never got the exclusion.

        Seen 2026-07-30 14:37: mount 4 went silent (bridge and Teensy together,
        never returned, needed a power cycle) while mounts 1 and 5 sat at 100%
        ACK throughout.  The ladder restarted a demonstrably healthy hub anyway.
        The hub-wedge signature is different and unmistakable: EVERY mount stops
        ACKing at once and the hub's own txfail counter stops advancing.
        """
        with self._diag_lock:
            for mt, t in self._mount_last_ack.items():
                if mt == exclude_mount or (now - t) >= self.MOUNT_ACK_STALE_S:
                    continue
                # Unknown route reads as direct: that is what an older hub, or
                # the first seconds of a session, look like, and it keeps the
                # old behaviour rather than silently answering 0 for everything.
                if 1 <= mt <= len(self._mount_route) and self._mount_route[mt - 1]:
                    continue                       # via a satellite — not our radio
                return mt
        return 0

    def _monitor_loop(self) -> None:
        """Periodic health snapshot — diagnostic aid for the stall where the UI
        still shows the camera connected but commands stop reaching the hub.

        Reading the HEALTH line:
          - tx_thread=False                  → TX thread died (root cause found)
          - rx_thread=False                  → RX thread died
          - qdepth climbing, tx_written flat → TX thread stuck/dead; commands
                                               pile up unsent (the reported bug)
          - rx 'last' age keeps growing       → no packets arriving (hub/link)
          - transport_alive=False            → port/socket dropped
          - everything healthy but unresponsive → look upstream of the bridge
                                               (UI not calling send(), or the
                                               mount_manager not dispatching)
        """
        last_health = 0.0
        while self._running:
            time.sleep(self.MONITOR_TICK_S)
            if not self._running:
                break
            now = time.monotonic()

            # ── Wedge detection (every tick) ──────────────────────────────
            oldest, wedged_mount, pending = self._wedge_check(now)
            wedged     = oldest > self.WEDGE_DETECT_S
            in_grace   = (now - self._last_connect_t) < self.POST_CONNECT_GRACE_S
            off_cool   = (now - self._last_forced_reconnect) >= self.RECONNECT_COOLDOWN_S
            if wedged and not in_grace and off_cool and self._reconnect_params:
                self._last_forced_reconnect = now   # paces this block to the cooldown
                is_tcp = self._reconnect_params.get('type') == 'tcp'
                self._log_wedge_side(now, wedged_mount)
                # "Link or mount?" is asked FIRST, before anything
                # transport-specific.  This check used to sit AFTER the is_tcp
                # branch, so on TCP a single dead mount forced a reconnect every
                # cooldown while every other mount ACKed normally — 456 of them
                # in one hour, each tearing down a working socket and re-running
                # the hub's accept path to reach a mount whose own radio is the
                # fault.  Reconnecting cannot fix a mount-side problem on any
                # transport, so the discrimination must not be transport-specific.
                if self._hub_tx_proven_ok(now, wedged_mount):
                    # Another mount is ACKing, so the hub can transmit.  This is
                    # one dead mount, not a hub wedge; no hub-level action can
                    # reach it and every rung below would drop the healthy mounts
                    # too — including, on TCP, whatever a satellite is relaying.
                    log.warning("Mount %d unreachable %.1fs, but mount %d is still "
                                "ACKing — hub TX is healthy, so this is mount-side. "
                                "Not touching the hub; mount %d likely needs a power "
                                "cycle.", wedged_mount, oldest,
                                self._hub_tx_proven_ok(now, wedged_mount), wedged_mount)
                elif is_tcp:
                    # TCP: reconnecting re-runs the hub's accept() path, which
                    # refreshes the mount ESP-NOW peers — a real recovery action,
                    # but only once the fault is known NOT to be mount-side.
                    log.warning("Command link wedged on mount %d — oldest tracked command "
                                "unacked %.1fs (> %.1fs); forcing reconnect",
                                wedged_mount, oldest, self.WEDGE_DETECT_S)
                    # Only MARK it dead — the RX loop is the single owner of
                    # close()+reconnect; closing here too races pyserial's Windows
                    # close() and can crash the RX thread.
                    with self._lock:
                        if self._transport:
                            self._transport.mark_dead()
                else:
                    # Serial: the USB link is healthy when this fires (hub_rx still
                    # climbing) — this is a hub→mount ESP-NOW send wedge that reopening
                    # COM can't fix.  PC→hub works, so command the hub to recover.
                    # Escalate: first a cheap ESP-NOW reinit; if the wedge persists
                    # past HUB_RESTART_AFTER_S the reinit didn't clear it, so command
                    # a full hub esp_restart() — the only confirmed cure.
                    if (oldest >= self.HUB_RESTART_AFTER_S
                            and now - self._last_hub_restart_t >= self.HUB_RESTART_MIN_INTERVAL_S):
                        self._last_hub_restart_t = now
                        try:
                            self.send(build_packet(0xFE, Cmd.HUB_RESTART))
                            log.warning("Command link wedged on mount %d %.1fs despite "
                                        "ESP-NOW reinit — commanding full hub restart "
                                        "(CMD_HUB_RESTART)", wedged_mount, oldest)
                        except Exception as e:
                            log.error("Failed to send hub restart: %s", e)
                    elif now - self._last_hub_reinit_t >= self.HUB_REINIT_MIN_INTERVAL_S:
                        self._last_hub_reinit_t = now
                        try:
                            self.send(build_packet(0xFE, Cmd.HUB_REINIT_ESPNOW))
                            log.warning("Command link wedged on mount %d %.1fs — sent "
                                        "CMD_HUB_REINIT_ESPNOW to clear the hub→mount "
                                        "ESP-NOW wedge", wedged_mount, oldest)
                        except Exception as e:
                            log.error("Failed to send hub ESP-NOW reinit: %s", e)
                    else:
                        log.warning("Command link wedged on mount %d — oldest tracked command "
                                    "unacked %.1fs (> %.1fs); awaiting hub recovery effect",
                                    wedged_mount, oldest, self.WEDGE_DETECT_S)

            # ── Health snapshot (slow cadence) ────────────────────────────
            self._cmd_ledger_tick(now)

            if self.HEALTH_LOG_INTERVAL > 0 and (now - last_health) >= self.HEALTH_LOG_INTERVAL:
                last_health = now
                rx_age = (now - self._last_rx_t) if self._last_rx_t is not None else -1.0
                tx_age = (now - self._last_tx_t) if self._last_tx_t is not None else -1.0
                rx_alive = self._rx_thread.is_alive() if self._rx_thread else False
                tx_alive = self._tx_thread.is_alive() if self._tx_thread else False
                transport_alive = bool(self._transport and self._transport.alive)
                hub_diag_age = (now - self._hub_diag_rx_t
                                if self._hub_diag_rx_t is not None else -1.0)
                log.info(
                    "HEALTH connected=%s transport_alive=%s rx_thread=%s tx_thread=%s "
                    "qdepth=%d | rx_pkts=%d (last %.1fs ago) | "
                    "tx_queued=%d tx_written=%d (last %.1fs ago) | "
                    "cmds sent=%d acked=%d pending=%d | "
                    "hub_rx_bytes=%d pkts=%d (diag %.1fs ago) | ghost_drops=%d",
                    self._connected, transport_alive, rx_alive, tx_alive,
                    self._send_queue.qsize(),
                    self._rx_pkt_count, rx_age,
                    self._tx_queued_count, self._tx_written_count, tx_age,
                    self._tx_cmd_sent, self._tx_cmd_acked, pending,
                    self._hub_rx_bytes, self._hub_rx_pkts, hub_diag_age,
                    self._hub_ghost_drops,
                )
