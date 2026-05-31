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

from .protocol import PacketReader, Packet

log = logging.getLogger(__name__)

PacketCallback = Callable[[Packet], None]

HUB_DEFAULT_HOST = "192.168.4.1"
HUB_DEFAULT_PORT = 7777


# ---------------------------------------------------------------------------
# Transport abstractions
# ---------------------------------------------------------------------------

class _SerialTransport:
    """Wraps a pyserial Serial object with a uniform read/write interface."""

    BAUD = 921600

    def __init__(self, port: str, read_timeout: float = 0.1):
        self._s = serial.Serial(port, baudrate=self.BAUD, timeout=read_timeout)

    def read(self, n: int) -> bytes:
        return self._s.read(n)

    def write(self, data: bytes) -> None:
        self._s.write(data)
        self._s.flush()

    def close(self) -> None:
        if self._s.is_open:
            self._s.close()

    @property
    def alive(self) -> bool:
        return self._s.is_open


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
    RECONNECT_DELAY  = 3.0   # seconds between auto-reconnect attempts

    def __init__(self):
        self._transport: Optional[_SerialTransport | _TcpTransport] = None
        self._send_queue: queue.Queue[bytes] = queue.Queue()
        self._callbacks: list[PacketCallback] = []
        self._reconnect_cbs: list[Callable] = []
        self._reader = PacketReader()
        self._running = False
        self._rx_thread: Optional[threading.Thread] = None
        self._tx_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._connected = False
        # Stored so the RX thread can auto-reconnect; cleared on explicit disconnect.
        self._reconnect_params: Optional[dict] = None

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
        self._reader = PacketReader()   # fresh reader per connection
        self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True,
                                           name="bridge-rx")
        self._tx_thread = threading.Thread(target=self._tx_loop, daemon=True,
                                           name="bridge-tx")
        self._rx_thread.start()
        self._tx_thread.start()

    def _rx_loop(self) -> None:
        while self._running:
            if not self._transport or not self._transport.alive:
                self._connected = False
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
                    self._reader.feed(data)
                    for pkt in self._reader.packets():
                        self._dispatch(pkt)
            except Exception as e:
                log.error(f"RX loop error: {e}")

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

            with self._lock:
                self._transport = new_transport
                self._connected = True
                self._reader    = PacketReader()

            log.info("Auto-reconnect successful")
            for cb in self._reconnect_cbs:
                try:
                    cb()
                except Exception as e:
                    log.error(f"Reconnect callback error: {e}")

        except Exception as e:
            log.warning(f"Auto-reconnect failed: {e}")

    def _tx_loop(self) -> None:
        while self._running:
            try:
                data = self._send_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            with self._lock:
                if self._transport and self._transport.alive:
                    try:
                        self._transport.write(data)
                    except Exception as e:
                        log.warning(f"TX error: {e}")
                        self._connected = False

    def _dispatch(self, pkt: Packet) -> None:
        for cb in self._callbacks:
            try:
                cb(pkt)
            except Exception as e:
                log.error(f"Packet callback error: {e}")
