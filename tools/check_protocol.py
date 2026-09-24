#!/usr/bin/env python3
"""
check_protocol.py — protocol single-source-of-truth verifier.

Canonical definition:  firmware/shared/protocol.h
Verified mirrors:      pc_app/comms/protocol.py   (Python enums/constants)
                       firmware/esp32_hub/web_app.h (JS constants in the web app)
                       firmware/teensy41_mount/protocol.h (must be a shim)

Plus a CROSS-LANGUAGE GOLDEN TEST: compiles firmware/shared/protocol.h with the
local C++ compiler, builds real packets on the C side and in Python, and
verifies both directions parse each other byte-for-byte (including CRC-16
vectors, float payloads, resync after garbage, and rejection of corrupt/truncated
packets).  If no C++ compiler is available the golden section is skipped with a
warning; the constant checks still run.

Usage:
    python3 tools/check_protocol.py          # full check, exit 1 on any failure
Run automatically by .githooks/pre-commit (git config core.hooksPath .githooks).
"""
from __future__ import annotations

import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
C_HEADER  = REPO / "firmware" / "shared" / "protocol.h"
TEENSY_H  = REPO / "firmware" / "teensy41_mount" / "protocol.h"
WEB_APP_H = REPO / "firmware" / "esp32_hub" / "web_app.h"
PC_APP    = REPO / "pc_app"
GOLDEN_C  = REPO / "tools" / "protocol_golden.cpp"

_failures: list[str] = []
_warnings: list[str] = []


def fail(msg: str) -> None:
    _failures.append(msg)
    print(f"  ✗ {msg}")


def warn(msg: str) -> None:
    _warnings.append(msg)
    print(f"  ! {msg}")


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


# ---------------------------------------------------------------------------
# Parse the canonical C header
# ---------------------------------------------------------------------------

def parse_c_header(text: str) -> dict[str, int]:
    """Return {name: value} for all #define literals and all enum members."""
    out: dict[str, int] = {}

    for m in re.finditer(r"^#define\s+(\w+)\s+(0x[0-9A-Fa-f]+|\d+)\b",
                         text, re.MULTILINE):
        out[m.group(1)] = int(m.group(2), 0)

    # Enum blocks: typedef enum : uint8_t { ... } Name;
    for em in re.finditer(
            r"typedef\s+enum\s*:\s*uint8_t\s*\{(.*?)\}\s*(\w+)\s*;",
            text, re.DOTALL):
        body = em.group(1)
        for line in body.splitlines():
            line = line.split("//")[0]
            mm = re.match(r"\s*(\w+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)\s*,?", line)
            if mm:
                out[mm.group(1)] = int(mm.group(2), 0)

    # Computed constant mirrored in Python
    if "MAX_SUBJECTS" in out and "SUBJECT_RECORD_LEN" in out:
        out["SUBJECT_LIST_PAYLOAD_LEN"] = out["MAX_SUBJECTS"] * out["SUBJECT_RECORD_LEN"]

    return out


# ---------------------------------------------------------------------------
# Check 1: Python mirror
# ---------------------------------------------------------------------------

# (C prefix, python enum name) — every C member must exist in the Python enum
# with the same value.
ENUM_MAP = [
    ("CMD_",   "Cmd"),
    ("STATE_", "MountState"),
    ("FLAG_",  "MountFlag"),
    ("NACK_",  "NackError"),
    ("AXIS_",  "Axis"),
    ("GROUP_", "AxisGroup"),
    ("CALIB_", "CalibPrompt"),
    # The hub encodes one of these into a hub-event byte and the app decodes it
    # back to a name. Declaring them in both files is not the same as them
    # agreeing, and a silent disagreement here would misname the fault in the
    # one log line written to explain it.
    ("ESPNOW_REJ_", "EspnowRejectCode"),
]

# C scalar constant → (python attribute, required)
SCALAR_MAP = {
    "PKT_START_1":              ("PACKET_START_1", True),
    "PKT_START_2":              ("PACKET_START_2", True),
    "MOUNT_BROADCAST":          ("MOUNT_BROADCAST", True),
    "NUM_MOUNTS":               ("NUM_MOUNTS", True),
    "NUM_POSITIONS":            ("NUM_POSITIONS", True),
    "TARGET_SLOT_NONE":         ("TARGET_SLOT_NONE", True),
    "TARGET_SLOT_LA_MIN":       ("TARGET_SLOT_LA_MIN", True),
    "TARGET_SLOT_LA_MAX":       ("TARGET_SLOT_LA_MAX", True),
    "PACKET_MIN_SIZE":          ("PACKET_MIN_SIZE", True),
    "PACKET_MAX_PAYLOAD":       ("PACKET_MAX_PAYLOAD", True),
    "STATE_REPORT_PAYLOAD_LEN": ("STATE_REPORT_PAYLOAD_LEN", True),
    "STATE_REPORT_LITE_LEN":    ("STATE_REPORT_LITE_LEN", True),
    "STATE_REPORT_LIMITS_LEN":  ("STATE_REPORT_LIMITS_LEN", True),
    "SLIDER_END_MARGIN_MM_DEFAULT": ("SLIDER_END_MARGIN_MM_DEFAULT", True),
    "SLIDER_END_MARGIN_MM_MIN":     ("SLIDER_END_MARGIN_MM_MIN", True),
    "SLIDER_END_MARGIN_MM_MAX":     ("SLIDER_END_MARGIN_MM_MAX", True),
    "SAVE_SPEEDS_PAYLOAD_LEN":  ("SAVE_SPEEDS_PAYLOAD_LEN", True),
    "MAX_SUBJECTS":             ("MAX_SUBJECTS", True),
    "MAX_SLIDER_MOVES":         ("MAX_SLIDER_MOVES", True),
    "SUBJECT_NAME_LEN":         ("SUBJECT_NAME_LEN", True),
    "SUBJECT_RECORD_LEN":       ("SUBJECT_RECORD_LEN", True),
    "SUBJECT_LIST_PAYLOAD_LEN": ("SUBJECT_LIST_PAYLOAD_LEN", True),
    "CONFIG_REPORT_PAYLOAD_LEN": ("CONFIG_REPORT_PAYLOAD_LEN", False),
    # The NO_MEM snapshot and the bridge health tail are layouts two ends share
    # byte for byte, so their lengths and bits are held here and the golden
    # vectors below build them in C and decode them in Python.
    "MOUNT_EVENT_PAYLOAD_LEN":       ("MOUNT_EVENT_PAYLOAD_LEN", True),
    "MOUNT_EVENT_NOMEM_REBOOT":      ("MOUNT_EVENT_NOMEM_REBOOT", True),
    "MOUNT_EVENT_NOMEM_CURED":       ("MOUNT_EVENT_NOMEM_CURED", True),
    "MOUNT_EVENT_NOMEM_SNAP_LEN":    ("MOUNT_EVENT_NOMEM_SNAP_LEN", True),
    "MOUNT_EVENT_NOMEM_LADDER_LEN":  ("MOUNT_EVENT_NOMEM_LADDER_LEN", True),
    "MOUNT_NOMEM_STEP_BLE_PAUSED":   ("MOUNT_NOMEM_STEP_BLE_PAUSED", True),
    "MOUNT_NOMEM_STEP_SCAN_FREED":   ("MOUNT_NOMEM_STEP_SCAN_FREED", True),
    "MOUNT_NOMEM_STEP_RESERVE":      ("MOUNT_NOMEM_STEP_RESERVE", True),
    "MOUNT_NOMEM_STEP_NO_RESERVE":   ("MOUNT_NOMEM_STEP_NO_RESERVE", True),
    "MOUNT_EVENT_NOMEM_PAYLOAD_LEN": ("MOUNT_EVENT_NOMEM_PAYLOAD_LEN", True),
    "MOUNT_NOMEM_BLE_SCANNING":      ("MOUNT_NOMEM_BLE_SCANNING", True),
    "MOUNT_NOMEM_BLE_CONNECTING":    ("MOUNT_NOMEM_BLE_CONNECTING", True),
    "MOUNT_NOMEM_BLE_LINKED":        ("MOUNT_NOMEM_BLE_LINKED", True),
    "MOUNT_NOMEM_BLE_BONDED":        ("MOUNT_NOMEM_BLE_BONDED", True),
    "HEALTH_BRIDGE_TAIL_LEN":        ("HEALTH_BRIDGE_TAIL_LEN", True),
}

# C enum members that are C-side only (none currently).  Add here if a value
# genuinely must not exist in Python.
PYTHON_EXEMPT: set[str] = set()


def check_python(canon: dict[str, int]):
    print("\n[python] pc_app/comms/protocol.py vs shared/protocol.h")
    sys.path.insert(0, str(PC_APP))
    try:
        import comms.protocol as pyproto  # noqa: E402
    except Exception as e:
        fail(f"cannot import pc_app/comms/protocol.py: {e}")
        return None

    for prefix, enum_name in ENUM_MAP:
        pyenum = getattr(pyproto, enum_name, None)
        if pyenum is None:
            fail(f"Python enum {enum_name} missing")
            continue
        members = {m.name: int(m.value) for m in pyenum}
        n_checked = 0
        for cname, cval in canon.items():
            if not cname.startswith(prefix) or cname in PYTHON_EXEMPT:
                continue
            # scalar #defines that share a prefix with enums (e.g. STATE_REPORT_
            # PAYLOAD_LEN) are handled by SCALAR_MAP, not here
            if cname in SCALAR_MAP:
                continue
            pyname = cname[len(prefix):]
            n_checked += 1
            if pyname not in members:
                fail(f"{enum_name}.{pyname} missing (C {cname} = 0x{cval:02X})")
            elif members[pyname] != cval:
                fail(f"{enum_name}.{pyname} = 0x{members[pyname]:02X} "
                     f"but C {cname} = 0x{cval:02X}")
        # extras in Python that the C header doesn't know
        cnames = {n[len(prefix):] for n in canon
                  if n.startswith(prefix) and n not in SCALAR_MAP}
        for extra in sorted(set(members) - cnames):
            warn(f"{enum_name}.{extra} exists in Python but not in C header")
        ok(f"{enum_name}: {n_checked} members checked")

    for cname, (pyname, required) in SCALAR_MAP.items():
        if cname not in canon:
            warn(f"C header missing scalar {cname} (expected by checker)")
            continue
        pyval = getattr(pyproto, pyname, None)
        if pyval is None:
            if required:
                fail(f"protocol.py missing {pyname} (C {cname} = {canon[cname]})")
        elif int(pyval) != canon[cname]:
            fail(f"protocol.py {pyname} = {pyval} but C {cname} = {canon[cname]}")
    ok(f"scalar constants checked")
    return pyproto


# ---------------------------------------------------------------------------
# Check 2: JS mirror (constants embedded in web_app.h)
# ---------------------------------------------------------------------------

def check_js(canon: dict[str, int]):
    print("\n[js] firmware/esp32_hub/web_app.h vs shared/protocol.h")
    text = WEB_APP_H.read_text(encoding="utf-8", errors="replace")
    n_checked = 0
    for m in re.finditer(r"^\s*const\s+([A-Z][A-Z0-9_]*)\s*=\s*"
                         r"(0x[0-9A-Fa-f]+|\d+)\s*;", text, re.MULTILINE):
        name, val = m.group(1), int(m.group(2), 0)
        if name in canon:
            n_checked += 1
            if canon[name] != val:
                fail(f"JS {name} = 0x{val:02X} but C header says 0x{canon[name]:02X}")
    ok(f"{n_checked} JS constants matched against canonical names")


# ---------------------------------------------------------------------------
# Check 3: Teensy header must be a shim
# ---------------------------------------------------------------------------

def check_teensy_shim():
    print("\n[teensy] firmware/teensy41_mount/protocol.h must be a shim")
    text = TEENSY_H.read_text(encoding="utf-8", errors="replace")
    if '#include "../shared/protocol.h"' not in text:
        fail("teensy41_mount/protocol.h does not include ../shared/protocol.h")
        return
    # Any enum/struct/#define of protocol values means it's drifted back into a copy.
    stripped = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    stripped = re.sub(r"^\s*//.*$", "", stripped, flags=re.MULTILINE)
    if re.search(r"\b(typedef\s+enum|typedef\s+struct|#define\s+CMD_|#define\s+FLAG_)",
                 stripped):
        fail("teensy41_mount/protocol.h contains protocol definitions — it must "
             "stay a one-line shim including ../shared/protocol.h")
    else:
        ok("shim only — no local protocol definitions")


# ---------------------------------------------------------------------------
# Check 4: cross-language golden vectors
# ---------------------------------------------------------------------------

# name → (mount_id, seq, cmd_value, payload_builder)
def golden_cases(pyproto) -> dict[str, tuple[int, int, int, bytes]]:
    Cmd = pyproto.Cmd
    return {
        "empty_get_state": (0x00, 0x0000, Cmd.GET_STATE, b""),
        "calib_prompt":    (3, 0xBEEF, Cmd.CALIB_PROMPT, bytes([0x05])),
        "status_10b":      (5, 0x1234, Cmd.STATUS,
                            struct.pack(">BBBBHHBB", 7, 0xA5, 2, 3,
                                        0x03FF, 0x0001, 0xFF, 0x07)),
        "ack":             (2, 0x0102, Cmd.ACK, struct.pack(">H", 0x0304)),
        "nack_no_ref":     (4, 0x0505, Cmd.NACK, struct.pack(">HB", 0x0607, 0x06)),
        "subject_list_232b": (1, 0xFFFF, Cmd.SUBJECT_LIST,
                              bytes(i & 0xFF for i in range(232))),
        "look_at_status_floats": (3, 42, Cmd.LOOK_AT_STATUS,
                                  struct.pack(">fffBB", 123.456, -45.5, 12.25,
                                              2, 0x60)),
        "health_24b": (0xFE, 7, Cmd.HEALTH,
                       struct.pack(">BBIIIHHbBI", 1, 3, 15000, 114688, 98304,
                                   12, 3, -58, 0x01, 2)),
        "position_17b": (4, 99, Cmd.POSITION,
                         struct.pack(">fffiB", -12.5, 3.25, 456.75, -2048, 0x05)),
        "health_bridge_tail": (2, 0x0203, Cmd.HEALTH,
                               struct.pack(">BBIIIHHbBI", 1, 1, 36000, 8400000,
                                           8390000, 10, 3, -33, 0x02, 0x07010010)
                               + struct.pack(">IIIHHHHHH", 142000, 118000, 0xF00D,
                                             2, 0x04D2, 12288, 3, 87, 0x0102)
                               + struct.pack(">HHHHBBH", 0x03E8, 0x0A0B, 2, 0xFFFF,
                                             8, 6, 0x0180)),
        "mount_event_nomem": (1, 0x0A0B, Cmd.MOUNT_EVENT,
                              bytes([pyproto.MOUNT_EVENT_NOMEM_REBOOT, 0x00, 0x03,
                                     0x00, 0x01, 0x00, 0x02, 0x00, 0x03, 0x00,
                                     0xC2, 0x30, 0x67, 0x00])
                              + struct.pack(">IIHHHHHIIIBBHBBBHHH",
                                            0x01020304, 0x0A0B0C0D, 0xFFFF, 110,
                                            2100, 1, 0, 41234, 38000, 30100, 1, 6,
                                            10, 1,
                                            pyproto.MOUNT_NOMEM_BLE_LINKED
                                            | pyproto.MOUNT_NOMEM_BLE_BONDED,
                                            int(Cmd.STATUS), 0, 4, 18)
                              + struct.pack(">BBHHHHHII",
                                            pyproto.MOUNT_NOMEM_STEP_BLE_PAUSED
                                            | pyproto.MOUNT_NOMEM_STEP_SCAN_FREED
                                            | pyproto.MOUNT_NOMEM_STEP_RESERVE,
                                            pyproto.MOUNT_NOMEM_BLE_SCANNING,
                                            0, 204, 1000, 0x0BBC, 12288,
                                            0x0001B8A0, 0x0001E8C0)),
    }


CRC_VECTORS = [b"", b"\x00", b"123456789"]


def find_cxx() -> str | None:
    for c in ("c++", "clang++", "g++"):
        if shutil.which(c):
            return c
    return None


def check_golden(pyproto):
    print("\n[golden] cross-language wire-format test (C ↔ Python)")
    cxx = find_cxx()
    if cxx is None:
        warn("no C++ compiler found — golden vectors SKIPPED "
             "(constants were still checked)")
        return

    with tempfile.TemporaryDirectory() as td:
        exe = str(Path(td) / "golden")
        r = subprocess.run([cxx, "-std=gnu++17", "-O1", "-o", exe, str(GOLDEN_C)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            fail(f"golden harness failed to compile:\n{r.stderr.strip()[:800]}")
            return
        ok(f"harness compiled with {cxx} (includes the real shared/protocol.h)")

        # ── emit: C builds packets, Python must build identical bytes ───────
        r = subprocess.run([exe, "emit"], capture_output=True, text=True)
        if r.returncode != 0:
            fail("golden harness 'emit' failed to run")
            return
        c_pkts: dict[str, bytes] = {}
        c_crcs: list[tuple[bytes, int]] = []
        for line in r.stdout.splitlines():
            parts = line.split()
            if parts[0] == "CRC":
                data = b"" if parts[1] == "-" else bytes.fromhex(parts[1])
                c_crcs.append((data, int(parts[2], 16)))
            elif parts[0] == "PKT":
                c_pkts[parts[1]] = bytes.fromhex(parts[2])

        # CRC vectors (includes the classic CCITT-FALSE check value 0x29B1)
        for data, c_val in c_crcs:
            py_val = pyproto.crc16(data)
            if py_val != c_val:
                fail(f"crc16({data!r}): C=0x{c_val:04X} Python=0x{py_val:04X}")
        if pyproto.crc16(b"123456789") != 0x29B1:
            fail("Python crc16 known-answer test failed (expected 0x29B1)")
        ok(f"{len(c_crcs)} CRC vectors match (incl. known-answer 0x29B1)")

        cases = golden_cases(pyproto)
        if set(cases) != set(c_pkts):
            fail(f"case tables differ: python={sorted(cases)} c={sorted(c_pkts)}")
            return

        for name, (mount, seq, cmd, payload) in cases.items():
            py_bytes = pyproto.build_packet(mount, cmd, payload, seq=seq)
            if py_bytes != c_pkts[name]:
                fail(f"build mismatch [{name}]:\n"
                     f"      C:  {c_pkts[name].hex()}\n"
                     f"      Py: {py_bytes.hex()}")
        ok(f"{len(cases)} packets built identically by C and Python")

        # Python must parse every C-built packet back to the same fields
        for name, (mount, seq, cmd, payload) in cases.items():
            reader = pyproto.PacketReader()
            reader.feed(c_pkts[name])
            pkts = reader.packets()
            if len(pkts) != 1:
                fail(f"Python failed to parse C packet [{name}]")
                continue
            p = pkts[0]
            if (p.mount_id, p.seq, int(p.cmd), p.payload) != (mount, seq, int(cmd), payload):
                fail(f"Python parse fields differ for [{name}]")
        ok("Python parses all C-built packets back to identical fields")

        # Semantic decoders on C-built payloads
        st = pyproto.decode_status(cases["status_10b"][3])
        if (int(st.state) != 7 or st.flags != 0xA5 or st.active_pt_preset != 2
                or st.slot_occupied_mask != 0x03FF or st.slot_at_mask != 0x0001
                or st.target_slot != 0xFF or st.active_la_subject != 0x07):
            fail("decode_status() gave wrong fields for the C-built STATUS "
                 "(is MountState missing a value?)")
        las = pyproto.decode_look_at_status(cases["look_at_status_floats"][3])
        if (abs(las.slider_pos_mm - 123.456) > 1e-3 or las.pan_deg != -45.5
                or las.tilt_deg != 12.25 or las.subject_id != 2 or las.flags != 0x60):
            fail("decode_look_at_status() float fields differ from C-built payload")
        hh = pyproto.decode_health(c_pkts["health_24b"][7:-2])
        if (hh.node_type != 1 or hh.uptime_s != 15000 or hh.free_heap != 114688
                or hh.min_free_heap != 98304 or hh.loop_max_ms != 12
                or hh.tx_fail != 3 or hh.rssi != -58 or not hh.anomaly
                or hh.node_u32 != 2):
            fail("decode_health() fields differ from the C-built HEALTH payload")
        pp = pyproto.decode_position(c_pkts["position_17b"][7:-2])
        if (pp.pan_deg != -12.5 or pp.tilt_deg != 3.25 or pp.slider_mm != 456.75
                or pp.zoom_steps != -2048 or pp.moving_mask != 0x05
                or not pp.axis_moving(pyproto.Axis.PAN) or not pp.any_moving):
            fail("decode_position() fields differ from the C-built POSITION payload")
        ok("semantic decoders (STATUS, LOOK_AT_STATUS, HEALTH, POSITION) verified on C bytes")

        # The bridge tail and the NO_MEM snapshot, decoded from C-built bytes.
        # A 24-byte health must still decode with no tail at all.
        if hh.iram_free is not None:
            fail("decode_health() invented a bridge tail on a 24-byte payload")
        ht = pyproto.decode_health(c_pkts["health_bridge_tail"][7:-2])
        if (ht.uptime_s != 36000 or ht.node_u32 != 0x07010010
                or (ht.iram_free, ht.iram_min, ht.iram_largest,
                    ht.nomem_healed, ht.nomem_healed_max_ms, ht.reserve_held,
                    ht.nomem_cured, ht.scan_devices, ht.scan_freed_kb)
                != (142000, 118000, 0xF00D, 2, 0x04D2, 12288, 3, 87, 0x0102)):
            fail("decode_health() misread the C-built bridge tail")
        if ((ht.cam_wr_ok, ht.cam_wr_refused, ht.cam_wr_unanswered,
             ht.cam_wr_unsent, ht.cam_fail_cat, ht.cam_fail_param,
             ht.cam_fail_status) != (0x03E8, 0x0A0B, 2, 0xFFFF, 8, 6, 0x0180)):
            fail("decode_health() misread the camera replies in the bridge tail")
        # A tail from 466477d (16 bytes) must still read its fields, and none
        # of the ladder's.
        h16 = pyproto.decode_health(c_pkts["health_bridge_tail"][7:-2][:24 + 16])
        if h16.iram_free != 142000 or h16.reserve_held is not None:
            fail("decode_health() mishandles the 16-byte tail of the older firmware")
        # A tail from the ladder's firmware (24 bytes, up to 42af949) must read
        # the ladder's fields and invent no camera replies.
        h24 = pyproto.decode_health(c_pkts["health_bridge_tail"][7:-2][:24 + 24])
        if (h24.reserve_held != 12288 or h24.scan_freed_kb != 0x0102
                or h24.cam_wr_ok is not None):
            fail("decode_health() mishandles the 24-byte tail of 42af949")
        ev = c_pkts["mount_event_nomem"][7:-2]
        if len(ev) != pyproto.MOUNT_EVENT_NOMEM_PAYLOAD_LEN or ev[0] != 4:
            fail("the C-built NO_MEM event is not the length or kind Python expects")
        sn = pyproto.decode_mount_nomem_snapshot(ev[pyproto.MOUNT_EVENT_PAYLOAD_LEN:])
        if ((sn.uptime_s, sn.accepted, sn.since_cb_ms, sn.since_ok_ms, sn.since_rx_ms,
             sn.in_flight, sn.leak_floor, sn.iram_free, sn.iram_min, sn.iram_largest,
             sn.chan_now, sn.chan_hub, sn.loop_max_ms, sn.loop_section, sn.ble,
             sn.first_cmd, sn.cb_during, sn.rx_during, sn.refused_during)
                != (0x01020304, 0x0A0B0C0D, 0xFFFF, 110, 2100, 1, 0, 41234, 38000,
                    30100, 1, 6, 10, 1, 0x0C, int(pyproto.Cmd.STATUS), 0, 4, 18)):
            fail("decode_mount_nomem_snapshot() misread the C-built snapshot")
        ld = pyproto.decode_mount_nomem_ladder(
            ev[pyproto.MOUNT_EVENT_PAYLOAD_LEN + pyproto.MOUNT_EVENT_NOMEM_SNAP_LEN:])
        if ((ld.steps, ld.ble_stopped, ld.t_pause_ms, ld.t_free_ms, ld.t_reserve_ms,
             ld.t_end_ms, ld.reserve_bytes, ld.iram_after_free, ld.iram_after_reserve)
                != (0x07, 0x01, 0, 204, 1000, 0x0BBC, 12288, 0x0001B8A0, 0x0001E8C0)):
            fail("decode_mount_nomem_ladder() misread the C-built ladder")
        ok("bridge health tail, NO_MEM snapshot and ladder verified on C bytes")

        # ── parse: Python builds / mangles packets, C must agree ────────────
        AxisGroup = pyproto.AxisGroup
        status_bytes = c_pkts["status_10b"]
        corrupted = bytearray(status_bytes); corrupted[9] ^= 0xFF  # payload byte, CRC stale
        inputs: list[tuple[str, bytes, bool]] = [   # (label, bytes, expect_ok)
            ("jog",          pyproto.pkt_jog(2, 100, -100, 500, -500), True),
            ("set_preset",   pyproto.pkt_set_active_preset(3, AxisGroup.SLIDER_ZOOM, 4), True),
            ("store_pos",    pyproto.pkt_store_pos(1, 9), True),
            ("start_la",     pyproto.pkt_start_look_at_move(5, 2, 1, 3), True),
            ("resync_junk",  b"\xAA\x00\xFF" + status_bytes, True),
            ("corrupt_crc",  bytes(corrupted), False),
            ("truncated",    status_bytes[:-2], False),
        ]
        stdin_data = "\n".join(x[1].hex() for x in inputs) + "\n"
        r = subprocess.run([exe, "parse"], input=stdin_data,
                           capture_output=True, text=True)
        lines = r.stdout.splitlines()
        if len(lines) != len(inputs):
            fail(f"harness 'parse' returned {len(lines)} lines for {len(inputs)} inputs")
            return
        for (label, raw, expect_ok), line in zip(inputs, lines):
            parts = line.split()
            got_ok = parts[2] == "OK"
            if got_ok != expect_ok:
                fail(f"C parser [{label}]: expected {'OK' if expect_ok else 'REJ'}, "
                     f"got {parts[2]}")
                continue
            # Python must agree with itself AND with C on every accepted packet
            reader = pyproto.PacketReader()
            reader.feed(raw)
            pkts = reader.packets()
            if expect_ok:
                if len(pkts) != 1:
                    fail(f"Python parser [{label}]: expected 1 packet, got {len(pkts)}")
                    continue
                p = pkts[0]
                c_mount, c_seq = int(parts[3]), int(parts[4])
                c_cmd, c_plen  = int(parts[5], 16), int(parts[6])
                c_payload = b"" if parts[7] == "-" else bytes.fromhex(parts[7])
                if (p.mount_id, p.seq, int(p.cmd), len(p.payload), p.payload) != \
                        (c_mount, c_seq, c_cmd, c_plen, c_payload):
                    fail(f"C and Python disagree on parsed fields [{label}]")
            else:
                if pkts:
                    fail(f"Python parser [{label}]: accepted a packet C rejected")
        ok(f"{len(inputs)} parse cases agree in both parsers "
           "(incl. resync, corrupt-CRC reject, truncation)")


# ---------------------------------------------------------------------------

def main() -> int:
    print(f"protocol check — canonical: {C_HEADER.relative_to(REPO)}")
    canon = parse_c_header(C_HEADER.read_text(encoding="utf-8", errors="replace"))
    n_cmds = sum(1 for k in canon if k.startswith("CMD_"))
    print(f"  parsed {len(canon)} canonical names ({n_cmds} commands)")

    pyproto = check_python(canon)
    check_js(canon)
    check_teensy_shim()
    if pyproto is not None:
        check_golden(pyproto)

    print()
    if _failures:
        print(f"FAILED — {len(_failures)} problem(s), {len(_warnings)} warning(s)")
        return 1
    print(f"OK — all protocol sources in sync ({len(_warnings)} warning(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
