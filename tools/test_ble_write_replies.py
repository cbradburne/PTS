"""The camera's answer to every command the mount relays — counted, not thrown away.

The Blackmagic protocol never acknowledges a command.  The GATT write it rides
in does: the camera answers each one, accepted or refused with an ATT error.
The mount wrote with response and passed no callback, so a command the camera
refused looked exactly like one it applied.

On 2026-09-24 saturation and contrast sent to cam2 and cam4 changed nothing on
the camera and were never echoed, while white balance and gain on the same
links worked.  Refused, or accepted and ignored?  Nothing could say.

WHAT THIS TEST IS PROTECTING.

  a callback at all          the write passes one; a nullptr silently throws
                             every answer away again
  three failures apart       refused (the camera's own ATT error) is a verdict;
                             unanswered (timeout, link gone) is a link problem;
                             unsent (never started) is the mount's own queue
  a disconnect buries        the link dropping fails every write in flight at
  nothing                    once, and must not overwrite the refusal that
                             said why
  which command              the last failure carries its category and
                             parameter, from the command's own bytes 4 and 5
  one word, two cores        the NimBLE task writes the last failure, loop()
                             reads it: one 32-bit word, read once
  saturating counts          a long run never wraps to a small number
  old tails read as old      a 24-byte tail from 42af949 invents no replies

Run directly, or via tools/run_tests.sh with the rest.
"""
import io
import logging
import os
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

INO = (REPO / "firmware/esp_mount_amoled175/esp_mount_amoled175.ino").read_text()
BLE = (REPO / "firmware/esp_mount_amoled175/ble_camera.h").read_text()


def body(src: str, head: str) -> str:
    """The text of one function, from its signature to its closing brace."""
    s = src[src.index(head):]
    return s[:s.index("\n}\n")]


# ---- 1. the firmware's own code, compiled and driven ------------------------------
# Not a regex over the source: the real functions, cut out of ble_camera.h and
# run against a stand-in NimBLE, so what is tested is what ships.
print("1. the reply callback, compiled from ble_camera.h:")
start = BLE.index("// ── The camera's reply to every command write")
send = BLE.index("bool ble_cam_send(", start)
end = BLE.index("\n}\n", send) + 3
SRC = BLE[start:end]

HARNESS = r'''
#include <cstdint>
#include <cstdio>
#include <cstring>
#define CAM_CONTROL_MAX_LEN 40
#define BLE_HS_ENOMEM        6
#define BLE_HS_ENOTCONN      7
#define BLE_HS_ETIMEOUT      13
#define BLE_HS_ERR_ATT_BASE  0x100
struct ble_gatt_error { uint16_t status; uint16_t att_handle; };
struct ble_gatt_attr  { uint16_t handle; uint16_t offset; void *om; };
typedef int ble_gatt_attr_fn(uint16_t, const struct ble_gatt_error *,
                             struct ble_gatt_attr *, void *);
static struct { int printf(const char *, ...) { return 0; } } Serial;
static bool     _bc_connected   = true;
static uint16_t _bc_ctrl_handle = 0x2A;
static uint16_t _bc_conn        = 1;
static bool     _bc_write_err   = false;

static ble_gatt_attr_fn *g_cb;
static void *g_arg;
static int g_rc = 0;
int ble_gattc_write_flat(uint16_t, uint16_t, const void *, uint16_t,
                         ble_gatt_attr_fn *cb, void *arg) {
    if (g_rc) return g_rc;
    g_cb = cb; g_arg = arg;
    return 0;
}
''' + SRC + r'''
static void reply(uint16_t st) {
    struct ble_gatt_error e = { st, 0x2A };
    if (g_cb) g_cb(1, &e, nullptr, g_arg);
    g_cb = nullptr;
}
static void state(const char *what) {
    printf("%s %u %u %u %u %08X %d\n", what, (unsigned)_bc_wr_ok,
           (unsigned)_bc_wr_refused, (unsigned)_bc_wr_unanswered,
           (unsigned)_bc_wr_unsent, (unsigned)_bc_wr_fail, (int)_bc_write_err);
}
int main() {
    // Real commands as the app frames them: hue/saturation (8.6), contrast
    // (8.4), white balance (1.2) — header, then category, parameter, type, op.
    const uint8_t sat[]  = {0xFF, 8, 0, 0, 8, 6, 128, 0, 0, 0, 0, 8};
    const uint8_t con[]  = {0xFF, 8, 0, 0, 8, 4, 128, 0, 0, 4, 0, 8};
    const uint8_t wb[]   = {0xFF, 8, 0, 0, 1, 2, 2, 0, 0x78, 0x15, 0, 0};
    ble_cam_send(sat, sizeof sat); reply(0);                         state("accepted");
    ble_cam_send(sat, sizeof sat); reply(BLE_HS_ERR_ATT_BASE + 0x80); state("refused");
    ble_cam_send(con, sizeof con); reply(BLE_HS_ENOTCONN);          state("linkgone");
    ble_cam_send(wb,  sizeof wb);  reply(BLE_HS_ETIMEOUT);          state("timeout");
    g_rc = BLE_HS_ENOMEM;
    bool took = ble_cam_send(wb, sizeof wb);                         state(took ? "unsent-true" : "unsent");
    g_rc = 0;
    for (long i = 0; i < 70000; i++) { ble_cam_send(wb, sizeof wb); reply(0); }
    state("saturated");
    return 0;
}
'''

cxx = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
assert cxx, "no C++ compiler — this suite runs the firmware's own code"
with tempfile.TemporaryDirectory() as td:
    src = pathlib.Path(td) / "h.cpp"
    exe = pathlib.Path(td) / "h"
    src.write_text(HARNESS)
    cc = subprocess.run([cxx, "-std=c++17", "-Wall", "-Werror", "-Wno-unused-function",
                         "-o", str(exe), str(src)], capture_output=True, text=True)
    assert cc.returncode == 0, "the extracted firmware code no longer compiles:\n" + cc.stderr
    run = subprocess.run([str(exe)], capture_output=True, text=True, check=True)
rows = {}
for line in run.stdout.split("\n"):
    if line:
        k, *v = line.split()
        rows[k] = v
print("   " + "\n   ".join(run.stdout.strip().split("\n")))


def row(k):
    ok, ref, una, uns, fail, err = rows[k]
    return int(ok), int(ref), int(una), int(uns), int(fail, 16), int(err)


assert row("accepted")[:4] == (1, 0, 0, 0), \
    f"an accepted write was not counted as accepted — is a callback passed at all? {rows['accepted']}"
assert row("refused")[:5] == (1, 1, 0, 0, 0x08060180), \
    f"an ATT refusal is not counted as refused, with 8.6 and its status: {rows['refused']}"
assert row("linkgone")[:5] == (1, 1, 1, 0, 0x08060180), \
    f"a write the link took with it must count as unanswered and must NOT bury the " \
    f"refusal before it: {rows['linkgone']}"
assert row("timeout")[:5] == (1, 1, 2, 0, 0x0102000D), \
    f"a write the camera never answered must count as unanswered and name itself: {rows['timeout']}"
assert "unsent" in rows, "a write that could not start reported success"
assert row("unsent")[3] == 1 and row("unsent")[5] == 1, \
    f"a write that could not start is not counted, or no longer sets the latch: {rows['unsent']}"
assert row("saturated")[0] == 0xFFFF, \
    f"the accepted count wrapped instead of saturating: {rows['saturated']}"
print("   accepted / refused / unanswered / unsent kept apart; a disconnect buries nothing   OK")

# ---- 2. the arg is the command's own category and parameter ----------------------
print("\n2. which command a reply answers:")
send_fn = body(BLE, "bool ble_cam_send(")
assert "bc_on_write" in send_fn and ", nullptr, nullptr)" not in send_fn, \
    "ble_cam_send passes no reply callback — the camera's answers are thrown away again"
assert re.search(r"cmd\[4\]\s*<<\s*8\)\s*\|\s*cmd\[5\]", send_fn), \
    "the reply no longer carries the command's category (byte 4) and parameter (byte 5)"
print("   callback passed, category.parameter from bytes 4 and 5   OK")

# ---- 3. two cores ------------------------------------------------------------------
print("\n3. two cores:")
assert re.search(r"static volatile uint32_t\s+_bc_wr_fail", BLE), \
    "the last failure is not one volatile 32-bit word — loop() could read the category " \
    "of one failure beside the status of the next"
health = body(INO, "static void send_health(")
assert health.count("_bc_wr_fail") == 1, \
    "send_health reads the last failure more than once — two reads can straddle a new failure"
for f in ("cam_wr_ok", "cam_wr_refused", "cam_wr_unanswered", "cam_wr_unsent",
          "cam_fail_cat", "cam_fail_param", "cam_fail_status"):
    assert f"t.{f}" in health, f"send_health never fills {f}"
print("   one word, read once; every field filled   OK")

# ---- 4. what the app makes of it ---------------------------------------------------
print("\n4. the health line:")
from comms.protocol import (Cmd, decode_health, HEALTH_BRIDGE_TAIL_LEN,
                            HEALTH_FLAG_BLE_BUILD, HEALTH_FLAG_BLE_LINK,
                            HEALTH_FLAG_CAM_SUBSCR, HEALTH_FLAG_CAM_RX)
assert HEALTH_BRIDGE_TAIL_LEN == 36, HEALTH_BRIDGE_TAIL_LEN

BUF = io.StringIO()
_h = logging.StreamHandler(BUF); _h.setFormatter(logging.Formatter("%(message)s"))
_lg = logging.getLogger("comms.bridge")
_saved = (_lg.handlers, _lg.level, _lg.propagate)
_lg.handlers, _lg.propagate = [_h], False
_lg.setLevel(logging.INFO)
from comms.bridge import Bridge

FLAGS = HEALTH_FLAG_BLE_BUILD | HEALTH_FLAG_BLE_LINK | HEALTH_FLAG_CAM_SUBSCR | HEALTH_FLAG_CAM_RX
LADDER = struct.pack(">IIIHHHHHH", 142000, 118000, 60000, 0, 0, 12288, 0, 0, 0)


class _P:
    cmd = Cmd.HEALTH
    mount_id = 2
    def __init__(self, payload): self.payload = payload


def health(tail: bytes):
    b = Bridge.__new__(Bridge)
    b._diag_lock = threading.Lock(); b._node_uptime = {}; b._sat_names = {}
    b._cam_ble = {}; b._node_state_cur = {}; b._node_state_prev = {}
    b._node_state_written = 0.0; b._node_state_path = None
    BUF.truncate(0); BUF.seek(0)
    b._note_node_health(_P(struct.pack(">BBIIIHHbBI", 1, 1, 36000, 8400000, 8390000,
                                       9, 6, -33, FLAGS, 1 << 4) + tail))
    return BUF.getvalue().strip()


def replies(ok, ref, una, uns, cat=0, par=0, st=0):
    return LADDER + struct.pack(">HHHHBBH", ok, ref, una, uns, cat, par, st)


old = health(LADDER)
assert "camera commands" not in old, f"a 42af949 tail is shown with replies it never sent: {old}"
assert decode_health(struct.pack(">BBIIIHHbBI", 1, 1, 1, 1, 1, 1, 1, 0, FLAGS, 0)
                     + LADDER).cam_wr_ok is None
quiet = health(replies(0, 0, 0, 0))
assert "camera commands" not in quiet, f"a mount that sent nothing reports replies: {quiet}"
fine = health(replies(57, 0, 0, 0))
assert "| camera commands: 57 accepted" in fine and "failure" not in fine \
    and "REFUSED" not in fine, fine
refused = health(replies(12, 45, 0, 0, 8, 6, 0x180))
assert "12 accepted, 45 REFUSED" in refused and \
    "(last failure 8.6 hue/saturation: ATT 0x80 application error)" in refused, refused
timed = health(replies(3, 0, 1, 7, 1, 2, 13))
assert "1 UNANSWERED, 7 NOT SENT" in timed and \
    "(last failure 1.2 white balance: no answer in 30 s)" in timed, timed
full = health(replies(0xFFFF, 0, 0, 0))
assert "65535+ accepted" in full, f"a saturated count is shown as a measurement: {full}"
dropped = health(replies(5, 0, 4, 0))
assert "4 UNANSWERED" in dropped and "failure" not in dropped, \
    f"writes lost to a disconnect claim a failure the firmware never recorded: {dropped}"
assert refused.index("BLE PAIRED") < refused.index("camera commands"), \
    "the replies are not beside the camera link they belong to"
print("   " + refused[refused.index("BLE PAIRED"):])
print("   absent -> silent, nothing sent -> silent, each failure named   OK")

_lg.handlers, _lg.level, _lg.propagate = _saved

print("\nALL CHECKS PASSED")
