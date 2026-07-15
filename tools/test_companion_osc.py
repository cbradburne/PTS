#!/usr/bin/env python3
"""
test_companion_osc.py — end-to-end drill of the Companion/QLab OSC surface.

Chain under test (every layer real, zero hardware):

    UDP OSC client (this script, sending what Companion sends)
      → OscServer → MountManager → Bridge (TCP) → mount_sim (hub + 5 mounts)

Asserts against the simulator's mount state, so a pass means an actual
Companion button press would move an actual mount.  Run:

    python3 tools/test_companion_osc.py     # exit 0 = PASS
"""
from __future__ import annotations

import socket
import struct
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc_app"))
sys.path.insert(0, str(ROOT / "tools"))

from PyQt6.QtCore import QCoreApplication          # noqa: E402
from comms.bridge import Bridge                    # noqa: E402
from comms.mount_manager import MountManager       # noqa: E402
from comms.osc_server import OscServer             # noqa: E402
from comms.protocol import MountState              # noqa: E402
from mount_sim import HubSim                       # noqa: E402


def osc_msg(addr: str, *args) -> bytes:
    """Encode one OSC message the way Companion's Generic OSC module does."""
    def pstr(s: str) -> bytes:
        b = s.encode() + b"\0"
        return b + b"\0" * (-len(b) % 4)
    tags = ","
    body = b""
    for a in args:
        if isinstance(a, int):
            tags += "i"; body += struct.pack(">i", a)
        elif isinstance(a, float):
            tags += "f"; body += struct.pack(">f", a)
        else:
            tags += "s"; body += pstr(str(a))
    return pstr(addr) + pstr(tags) + body


def wait_for(cond, timeout=2.0, dt=0.02) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(dt)
    return False


def main() -> int:
    _app = QCoreApplication([])   # QObject context for MountManager

    # Free ports for the sim (TCP) and OSC (UDP)
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    tcp_port = s.getsockname()[1]; s.close()
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.bind(("127.0.0.1", 0))
    osc_port = u.getsockname()[1]; u.close()

    sim = HubSim(port=tcp_port, verbose=False)
    threading.Thread(target=sim.serve, daemon=True).start()
    time.sleep(0.3)

    bridge = Bridge()
    assert bridge.connect_tcp("127.0.0.1", tcp_port), "bridge connect failed"
    mm  = MountManager(bridge)
    osc = OscServer(mm, port=osc_port, host="127.0.0.1")
    assert osc.start(), "OSC bind failed"
    time.sleep(0.5)                       # let STATUS populate manager state

    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    def send(addr, *args):
        tx.sendto(osc_msg(addr, *args), ("127.0.0.1", osc_port))

    fails = 0
    def check(name, cond):
        nonlocal fails
        okc = "✓" if cond else "✗"
        print(f"  {okc} {name}")
        if not cond:
            fails += 1

    print("[companion-osc e2e]")
    m2, m3 = sim.mounts[2], sim.mounts[3]

    # store → slot appears in the simulator
    send("/pts/cam/2/store", 5)
    check("store: slot 5 lands in the mount",
          wait_for(lambda: 4 in m2.slots))

    # speed preset
    send("/pts/cam/2/speed/pt", 4)
    check("speed/pt: active preset becomes 4",
          wait_for(lambda: m2.pt_preset == 4))

    # hold-to-jog: single press message, server must re-stream past the
    # mount's 500 ms dead-man
    send("/pts/cam/2/jog", 600, 0, 0, 0)
    check("jog: mount starts jogging",
          wait_for(lambda: m2.state == MountState.JOGGING))
    time.sleep(1.2)
    check("jog: still jogging after 1.2 s (20 Hz re-stream beats dead-man)",
          m2.state == MountState.JOGGING)
    pan_when_held = m2.pos[0]
    check("jog: pan actually moved", pan_when_held > 0)

    send("/pts/cam/2/jog/stop")
    check("jog/stop: mount back to IDLE",
          wait_for(lambda: m2.state == MountState.IDLE))

    # QLab-style float arguments must coerce (network cues send floats)
    send("/pts/cam/2/goto", 5.0)
    check("goto: recall with float arg (QLab-style) starts the move",
          wait_for(lambda: m2.target_slot == 4 or
                           abs(m2.pos[0] - pan_when_held) > 0))

    # look-at: seed a calibrated subject + session ref in the sim, then
    # select + launch entirely over OSC
    m3.subjects[2] = ("Presenter", 350.0, 200.0, 2000.0)
    m3.ref_set = True
    send("/pts/cam/3/subject", 2)
    send("/pts/cam/3/lookat", 1)
    check("lookat: PRE_AIM/LOOK_AT_MOVE running with selected subject",
          wait_for(lambda: m3.state in (MountState.LOOK_AT_PRE_AIM,
                                        MountState.LOOK_AT_MOVE)
                   and m3.active_subj == 2))

    # broadcast E-STOP kills an active jog everywhere
    send("/pts/cam/4/jog", 0, 500, 0, 0)
    wait_for(lambda: sim.mounts[4].state == MountState.JOGGING)
    send("/pts/estop")
    check("estop: every mount idle",
          wait_for(lambda: all(m.state == MountState.IDLE
                               for m in sim.mounts.values())))

    osc.stop(); bridge.disconnect(); sim.running = False
    print("[companion-osc e2e] " + ("PASS" if fails == 0 else f"FAIL ({fails})"))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
