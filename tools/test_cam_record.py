"""Record: camera report -> state -> button, and the hub's OSC command bytes.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))
import socket, time

from PyQt6.QtWidgets import QApplication
app = QApplication([])

from comms.protocol import (pkt_cam_transport, decode_cam_status, describe_cam_param,
                            bmd_command, TRANSPORT_RECORD, TRANSPORT_PREVIEW)
from comms.mount_manager import MountState_


def rep(cat, par, dtype, data=b""):
    return bytes([1, 4 + len(data), 0, 0, cat, par, dtype, 0]) + data


# ---- 1. the packet we send to start recording ----------------------------
pkt = pkt_cam_transport(1, TRANSPORT_RECORD)
print(f"1. record packet {len(pkt)} bytes: {pkt.hex()}")
# The BMD command as it goes on the wire, read back through our own decoder.
body = bmd_command(category=10, parameter=1, data_type=1,
                   data=bytes([TRANSPORT_RECORD, 0, 0, 0, 0]))
assert body in pkt, "the transport packet does not carry the bytes we think"
print(f"   its payload decodes as: {describe_cam_param(body)}")
assert decode_cam_status(body) == {"transport": 2, "recording": True}, decode_cam_status(body)
# Byte-for-byte against jeppo7745's Magic Button 4k, a BMPCC4k remote known to
# work over the same BLE characteristic.  Its buffer is 16 with trailing zeros.
REF_REC = [255, 9, 0, 0, 10, 1, 1, 0, 2, 0, 0, 0, 0, 0, 0, 0]
got = list(body) + [0] * (16 - len(body))
assert got == REF_REC, (got, REF_REC)
print(f"   matches Magic Button 4k record[] exactly: {got}")
# And the OLD category must now decode to nothing — it is not the transport.
assert decode_cam_status(
    bmd_command(category=9, parameter=1, data_type=1,
                data=bytes([TRANSPORT_RECORD, 0, 0]))) == {}, "cat 9 still claims transport"
print("   category 9 parameter 1 no longer claims to be transport")
stopb = bmd_command(category=10, parameter=1, data_type=1,
                    data=bytes([TRANSPORT_PREVIEW, 0, 0, 0, 0]))
assert stopb in pkt_cam_transport(1, TRANSPORT_PREVIEW)
assert decode_cam_status(stopb)["recording"] is False
print("   stop packet decodes as recording=False")

# ---- 2. camera report -> manager state + signal ---------------------------
sent, tally = [], []


class FakeMM:
    """The real transport-mode handling, without a radio."""
    from PyQt6.QtCore import QObject, pyqtSignal
    def __init__(self):
        self._states = {i: MountState_(mount_id=i) for i in range(1, 6)}
    def state(self, m): return self._states[m]
    def send_cam_record(self, m, on):
        sent.append((m, on))
        self.feed(m, rep(10, 1, 1, bytes([TRANSPORT_RECORD if on else 0, 0, 64, 0, 2])))
    def feed(self, m, raw):
        upd = decode_cam_status(raw)
        st = self._states[m]
        if "recording" in upd:
            was = st.cam_recording
            st.cam_recording = upd["recording"]
            if was != st.cam_recording:
                tally.append((m, st.cam_recording))


mm = FakeMM()
print("\n2. camera reports transport:")
for mode, want in ((2, True), (2, True), (0, False), (1, False), (2, True)):
    mm.feed(1, rep(10, 1, 1, bytes([mode, 0, 64, 0, 2])))
    print(f"   mode {mode} -> cam_recording={mm.state(1).cam_recording}")
    assert mm.state(1).cam_recording is want
print(f"   tally fired {len(tally)} times for 5 reports: {tally}")
assert len(tally) == 3, "tally should fire only on CHANGE"

# ---- 3. the Record button ------------------------------------------------
from ui.dialogs.camera_control_dialog import _CamRow


class FakeBridge:
    def cam_ble_link(self, m): return True


mm.state(1).connected = True
mm.state(1).cam_recording = None
row = _CamRow(1, "Cam 1", mm, FakeBridge())
print(f"\n3. before the camera has said anything: text {row._rec.text()!r}, "
      f"enabled {row._rec.isEnabled()}")
assert not row._rec.isEnabled(), "button must not guess before the camera reports"

mm.state(1).cam_recording = False
row.refresh()
red = "#C62828" in row._rec.styleSheet()
print(f"   camera says stopped:  text {row._rec.text()!r}, enabled {row._rec.isEnabled()}, red {red}")
assert row._rec.text() == "Record" and row._rec.isEnabled() and not red

mm.state(1).cam_recording = True
row.refresh()
red = "#C62828" in row._rec.styleSheet()
print(f"   camera says RECORDING: text {row._rec.text()!r}, red {red}")
assert row._rec.text() == "Stop" and red

sent.clear()
row._rec.click()
print(f"   pressing it while rolling sent: {sent}  (stop)")
assert sent == [(1, False)]
row.refresh()
sent.clear()
row._rec.click()
print(f"   pressing it while stopped sent: {sent}  (start)")
assert sent == [(1, True)]

# ---- 4. the HUB's tally/record, checked against this app's builders --------
# The OSC surface moved into the hub firmware, so what has to be proven is that
# the C the hub actually ships produces the same bytes this app produced.  Two
# checks, because they fail differently: the compiled harness proves the
# ARITHMETIC (fixed-point conversion, byte order, clamping), and the source
# scan proves the harness has not drifted from the .ino it claims to mirror.
import re, subprocess, tempfile, pathlib

INO = pathlib.Path(REPO / "firmware/esp32_hub_eth/esp32_hub_eth.ino").read_text()

print("\n4. hub firmware — tally/record bytes:")

# -- 4a. the constant fields, read out of the real firmware source ------------
def literal(name, n):
    m = re.search(r"uint8_t " + name + r"\[" + str(n) + r"\] = \{(.*?)\};", INO, re.S)
    assert m, f"{name}[{n}] initialiser not found in esp32_hub_eth.ino"
    out = [t.strip() for t in m.group(1).replace("\n", " ").split(",")]
    assert len(out) == n, (name, len(out), out)
    return out

T = literal("T", 12)
R = literal("R", 16)
# Tally: length 6 is the body BEFORE padding (4 header + 2 fixed16), not 8.
assert [T[i] for i in (0,1,2,3,4,6,7,10,11)] == \
       ["0xFF","0x06","0x00","0x00","0x05","0x80","0x00","0x00","0x00"], T
assert T[5] == "param", T
# Record: category 10 (0x0A), length 9, five data bytes with mode first.
assert [R[i] for i in (0,1,2,3,4,5,6,7,9,10,11,12,13,14,15)] == \
       ["0xFF","0x09","0x00","0x00","0x0A","0x01","0x01","0x00",
        "0x00","0x00","0x00","0x00","0x00","0x00","0x00"], R
assert R[8] == "mode", R
print("   .ino literals match Magic Button 4k framing        OK")
# The hub must read the camera's transport from category 10, not 9.
assert "pkt.payload[4] == 10 && pkt.payload[5] == 1" in INO, \
       "hub still parses the wrong category for recording"
print("   hub reads transport from category 10               OK")

# -- 4b. the arithmetic, compiled from the same expressions ------------------
C = r"""
#include <stdio.h>
#include <math.h>
#include <stdint.h>
#include <string.h>
int main(void){
  const char *subs[3] = {0, "front", "rear"};
  float bs[5] = {1.0f, 0.5f, 0.35f, 0.0f, 2.0f};
  for (int si = 0; si < 3; si++) for (int bi = 0; bi < 5; bi++) {
    int nt = si ? 5 : 4; const char *sub = subs[si]; float b = bs[bi];
    uint8_t param = 0;
    if      (nt >= 5 && sub && !strcmp(sub, "front")) param = 1;
    else if (nt >= 5 && sub && !strcmp(sub, "rear"))  param = 2;
    if (b < 0.0f) b = 0.0f;
    if (b > 1.0f) b = 1.0f;
    int16_t fx = (int16_t)lroundf(b * 2048.0f);
    uint8_t T[12] = { 0xFF,0x06,0x00,0x00, 0x05,param,0x80,0x00,
                      (uint8_t)(fx & 0xFF), (uint8_t)((fx >> 8) & 0xFF), 0x00,0x00 };
    printf("t %d %.2f", param, bs[bi]);
    for (int i=0;i<12;i++) printf(" %02x", T[i]); printf("\n");
  }
  for (int on = 0; on <= 1; on++) {
    uint8_t mode = on ? 2 : 0;
    uint8_t R[16] = { 0xFF,0x09,0x00,0x00, 0x0A,0x01,0x01,0x00,
                      mode,0x00,0x00,0x00, 0x00,0x00,0x00,0x00 };
    printf("r %d", on);
    for (int i=0;i<16;i++) printf(" %02x", R[i]); printf("\n");
  }
  return 0;
}
"""
d = tempfile.mkdtemp()
open(d + "/h.c", "w").write(C)
subprocess.run(["cc", "-O2", "-o", d + "/h", d + "/h.c", "-lm"], check=True)
out = subprocess.run([d + "/h"], capture_output=True, text=True, check=True).stdout

from comms.protocol import _fixed16, BMD_CAT_TALLY, BMD_CAT_MEDIA, BMD_TYPE_FIXED16, BMD_TYPE_I8
def py_tally(param, b):
    return bmd_command(BMD_CAT_TALLY, param, BMD_TYPE_FIXED16, 0,
                       _fixed16(max(0.0, min(1.0, b))))
def py_rec(on):
    b = bmd_command(BMD_CAT_MEDIA, 1, BMD_TYPE_I8, 0,
                    bytes([(2 if on else 0), 0, 0, 0, 0]))
    return b + b"\x00" * (16 - len(b))

n = 0
for line in out.strip().splitlines():
    f = line.split()
    # "t <param> <brightness> <12 bytes>"  vs  "r <on> <12 bytes>"
    if f[0] == "t":
        got, want = bytes(int(x, 16) for x in f[3:]), py_tally(int(f[1]), float(f[2]))
    else:
        got, want = bytes(int(x, 16) for x in f[2:]), py_rec(int(f[1]))
    assert got == want, (line, want.hex(" "))
    n += 1
print(f"   {n} hub-built commands byte-identical to this app  OK")
print("   (0.35 -> cd 02 confirms fixed16 is little-endian)")
print("   (2.0 clamps to 1.0, not to a wrapped value)")

# -- 4c. the transport parse the hub does on CMD_CAM_STATUS ------------------
# Hub reads payload[4]==9, payload[5]==1, payload[8]==2 -> recording.
print("\n5. hub's transport parse agrees with this app's decoder:")
for mode, want in ((TRANSPORT_RECORD, True), (TRANSPORT_PREVIEW, False), (1, False)):
    # The camera's own notification shape, from the Magic Button 4k source:
    #   rec on  is 255 9 0 0 10 1 1 2  2 0 64 0 2
    payload = bmd_command(category=10, parameter=1, data_type=1,
                          data=bytes([mode, 0, 64, 0, 2]))
    hub_says = (payload[4] == 10 and payload[5] == 1 and payload[8] == 2)
    app_says = decode_cam_status(rep(10, 1, 1, bytes([mode, 0, 64, 0, 2]))).get("recording", False)
    print(f"   transport={mode}: hub {hub_says}, app {app_says}")
    assert hub_says == app_says == want, (mode, hub_says, app_says, want)

# ---- 6. the hub naming its OSC commands into the PC log -------------------
# Every camera command is one CMD_CAM_CONTROL, so the satellite counter cannot
# tell them apart and prints only a top-3-per-window summary.  Kind 13 is the
# hub saying which verb it actually was.  The verb codes are read out of the
# .ino call sites so this cannot drift from the firmware.
import logging, io

sites = re.findall(r"send_osc_cam_event\(\(uint8_t\)mid, ([^,]+), ([^)]+)\)", INO)
assert len(sites) == 3, sites
assert sites[0][0].strip() == "0", sites            # autofocus
assert "param + 1" in sites[1][0], sites            # tally -> 1/2/3
assert sites[2][0].strip() == "4", sites            # record
print("\n6. hub -> PC log, OSC commands by name:")
print("   .ino call sites use verbs 0 / param+1 / 4          OK")

from comms.bridge import Bridge

class _P:
    def __init__(self, payload):
        from comms.protocol import Cmd
        self.cmd, self.payload = Cmd.HUB_EVENT, payload

def ev(mount, verb, value):
    return bytes([13, mount, verb, (value >> 8) & 0xFF, value & 0xFF, 0, 0, 0, 0])

def fx16(v):
    return int(round(max(0.0, min(1.0, v)) * 2048.0))

_buf = io.StringIO()
_h = logging.StreamHandler(_buf); _h.setFormatter(logging.Formatter("%(message)s"))
_lg = logging.getLogger("comms.bridge")
_saved = _lg.handlers, _lg.level, _lg.propagate
_lg.handlers, _lg.propagate = [_h], False
_lg.setLevel(logging.INFO)
_b = Bridge.__new__(Bridge)

def shows(payload):
    _buf.truncate(0); _buf.seek(0)
    _b._note_hub_event(_P(payload))
    return _buf.getvalue().strip()

for want, payload in (
        ("OSC CMD: cam5 autofocus",              ev(5, 0, 0)),
        ("OSC CMD: cam5 tally both = 1.00",      ev(5, 1, fx16(1.0))),
        ("OSC CMD: cam5 tally both = 0.35",      ev(5, 1, fx16(0.35))),
        ("OSC CMD: cam3 tally front = 1.00",     ev(3, 2, fx16(1.0))),
        ("OSC CMD: cam1 tally rear = 0.75",      ev(1, 3, fx16(0.75))),
        ("OSC CMD: cam5 record START",           ev(5, 4, 2)),
        ("OSC CMD: cam5 record STOP",            ev(5, 4, 0))):
    got = shows(payload)
    assert got == want, (got, want)
print(f"   7 events named correctly, tally value exact       OK")

# The pre-existing kinds must still decode — this is a shared dispatcher.
assert "HUB PAIRING: cam4" in shows(bytes([12, 4, 2, 0xAA,0xBB,0xCC,0xDD,0xEE,0xFF]))
print("   kind 12 (pairing) still decodes                    OK")
_lg.handlers, _lg.level, _lg.propagate = _saved

print("\nALL CHECKS PASSED")
