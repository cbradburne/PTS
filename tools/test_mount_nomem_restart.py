"""A mount reboots after three seconds of refused sends — and says why.

Every mount wedge on record, 28 to 2026-09-22, was esp_now_send() refusing
with ESP_ERR_ESPNOW_NO_MEM: 165-597 refusals each. Half the log called them
"isolated" and half "one-way", and the split was the ROUTE, not the fault — a
satellite keeps sending its mounts PINGs, a direct mount only hears the PC app,
and the app stops polling a mount that stops answering. All 28 ended in a
reboot. The one-way path first spent 30 s detecting and then tried a WiFi-level
restart that held for 12-13 s and failed, 21 times out of 21.

So the mount now does what the hub has done since 2026-09-12: the first NO_MEM
starts a clock, an accepted send stops it, and refusals spanning
NOMEM_RESTART_MS reboot the chip. And it records the moment of the first
refusal, because the hope is a cure that does NOT need a reboot, and nothing
has ever captured the state that would point at one.

WHAT THIS TEST IS PROTECTING.

  armed ONLY by NO_MEM       NOT_INIT is this mount's own ladder with the stack
                             torn down, NOT_FOUND a peer refresh; rebooting over
                             either would be a new fault, not a cure.
  ended ONLY by acceptance   one frame taken proves the stack is taking frames.
  a SPAN, not an age         refusals at both ends of the three seconds, so one
                             refusal and a quiet spell is not three seconds of
                             anything.
  two cores                  the camera forward sends from the NimBLE task, so
                             the clock is a critical section — and nothing that
                             can wait (the heap walks) runs inside it.
  bounded                    it shares the one-way detector's reboot quota, so a
                             fault a boot does not fix cannot boot-loop a mount.
  the snapshot survives      written to RTC before esp_restart(), sent after.
  it reads, never consumes   ble_cam_health_flags() clears a camera write error;
                             the snapshot must not swallow one the PC app has
                             not been sent.

Run directly, or via tools/run_tests.sh with the rest.
"""
import io
import logging
import os
import pathlib
import re
import struct
import sys
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

INO  = (REPO / "firmware/esp_mount_amoled175/esp_mount_amoled175.ino").read_text()
BLE  = (REPO / "firmware/esp_mount_amoled175/ble_camera.h").read_text()
FLAT = re.sub(r"\s+", " ", INO)


def body(src: str, head: str) -> str:
    """The text of one function, from its signature to its closing brace."""
    s = src[src.index(head):]
    return s[:s.index("\n}\n")]


# ---- 1. what arms the clock, and what ends it --------------------------------
print("1. the clock:")
tx = body(INO, "static void espnow_tx(")
assert "if (e == ESP_ERR_ESPNOW_NO_MEM) nomem_note(" in tx, \
    "the clock is not started under an ESP_ERR_ESPNOW_NO_MEM guard in the send\n" \
    "    path. Armed by any refusal, the mount's own reinit (NOT_INIT) or a peer\n" \
    "    refresh (NOT_FOUND) would reboot it."
ok_branch = tx[tx.index("if (e == ESP_OK)"):tx.index("if (e != ESP_OK)")]
assert "nomem_heal()" in ok_branch, \
    "an accepted send does not end the run, so a queue that filled for 50 ms\n" \
    "    and drained would still reboot the mount three seconds later"
assert FLAT.count("_nomem_since_ms = now") == 1 and \
       "_nomem_since_ms = now" in body(INO, "static void nomem_note("), \
    "the clock is started somewhere other than nomem_note()"
# Assignments only — the declaration initialises it to 0 as well.
clears = re.findall(r"(?<!uint32_t )\b_nomem_since_ms = 0;", INO)
assert len(clears) == 1 and \
       "_nomem_since_ms = 0;" in body(INO, "static void nomem_heal("), \
    "the run is ended somewhere other than nomem_heal(), which only the ESP_OK\n" \
    "    branch calls — the property the whole rung rests on"
print("   started on NO_MEM only, ended only by an accepted send  OK")

# ---- 2. two cores ------------------------------------------------------------
print("\n2. the send path runs on both cores:")
for fn in ("static void nomem_note(", "static void nomem_heal("):
    b = body(INO, fn)
    assert "portENTER_CRITICAL(&_nomem_mux)" in b and "portEXIT_CRITICAL(&_nomem_mux)" in b, \
        f"{fn[12:-1]}() touches the clock outside the lock. The camera forward\n" \
        "    sends from the NimBLE task on core 0 while loop() sends from core 1."
    crit = b[b.index("portENTER_CRITICAL"):b.index("portEXIT_CRITICAL")]
    for bad in ("heap_caps_", "Serial.", "nomem_snapshot_take", "esp_wifi_get_channel"):
        assert bad not in crit, \
            f"{fn[12:-1]}() calls {bad} inside the critical section — interrupts are\n" \
            "    off and a spinlock is held, and that call can wait on another lock"
note = body(INO, "static void nomem_note(")
assert note.index("portEXIT_CRITICAL") < note.index("nomem_snapshot_take"), \
    "the snapshot is taken before the lock is released"
assert "if (first) nomem_snapshot_take(" in note, \
    "the snapshot is not limited to the FIRST refusal of a run, so later ones\n" \
    "    overwrite the only picture of how it started"
assert "ble_cam_on_status" in INO and "espnow_tx(buf," in FLAT, \
    "the camera forward no longer sends from its callback — if that has moved\n" \
    "    into loop(), say so here and the lock can be reconsidered"
print("   armed and ended under _nomem_mux, heap walks outside    OK")

# ---- 3. the rung ---------------------------------------------------------------
print("\n3. the rung:")
loop = INO[INO.index("MARK(MSEC_ESPNOW);"):INO.index("MARK(MSEC_WEDGE);")]
rung = loop[loop.index("Refused sends → reboot"):loop.index("One-way transmit wedge")]
assert "NOMEM_RESTART_MS && _cfg_valid && !_setup_active && !_pair_active" in rung, \
    "the rung is not gated on being configured and off the setup and pairing\n" \
    "    screens — pairing stops WiFi on purpose"
assert re.search(r"since \? \(int32_t\)\(last - since\)", rung) and \
       "span >= (int32_t)NOMEM_RESTART_MS" in rung, \
    "the rung does not measure from the first refusal to the LATEST one. From\n" \
    "    the clock's age alone, one refusal followed by a quiet spell would count"
assert "_wedge_reboots < TXWEDGE_MAX_REBOOTS" in rung and "_wedge_reboots++" in rung, \
    "the rung has no reboot quota, so a fault a boot does not fix boot-loops"
i_stash, i_bump, i_rst = (rung.index("nomem_stash_event("), rung.index("wedge_count_bump()"),
                          rung.index("esp_restart()"))
assert i_stash < i_rst and i_bump < i_rst, \
    "esp_restart() runs before the event is stashed or the wedge counted"
assert "portENTER_CRITICAL(&_nomem_mux)" in rung, \
    "the rung reads the clock pair without the lock, so a heal on the other core\n" \
    "    between the two reads can hand it a span that never happened"
assert re.search(r"#ifndef NOMEM_RESTART_MS\s+#define NOMEM_RESTART_MS 3000UL", INO), \
    "NOMEM_RESTART_MS is not a 3 s default that can be overridden — 0 is the\n" \
    "    switch back to exactly the old ladder"
assert loop.index("Refused sends → reboot") < loop.index("One-way transmit wedge"), \
    "the rung sits after the one-way detector, which would reach its WiFi\n" \
    "    restart first"
print("   gated, span-measured, quota-bound, stashes then reboots OK")

# ---- 4. the snapshot ---------------------------------------------------------
print("\n4. the snapshot:")
snap = body(INO, "static void nomem_snapshot_take(")
for what, why in (("MALLOC_CAP_INTERNAL", "internal RAM, which free_heap cannot show"),
                  ("heap_caps_get_largest_free_block", "the largest block — fragmentation"),
                  ("_espnow_cb_last_ms", "when a send last COMPLETED"),
                  ("espnow_in_flight()", "what was outstanding"),
                  ("esp_wifi_get_channel", "whether the channel had moved"),
                  ("ble_cam_activity_bits()", "what BLE was doing")):
    assert what in snap, f"the snapshot does not record {why}"
assert "ble_cam_health_flags" not in snap, \
    "the snapshot calls ble_cam_health_flags(), which CLEARS the camera write-\n" \
    "    error latch — a snapshot would swallow a fault the PC app has not seen"
act = body(BLE, "static uint8_t ble_cam_activity_bits(")
assert "_bc_write_err" not in act, "ble_cam_activity_bits() touches the latch"
sent = body(INO, "static void on_espnow_sent(")
assert sent.index("_espnow_cb_last_ms") < sent.index("if (s == ESP_NOW_SEND_SUCCESS)"), \
    "the completion clock is set on one status only; a FAIL returns the buffer\n" \
    "    too, and 'completing' is what the snapshot is asking about"
assert "_espnow_rx_total = _espnow_rx_total + 1" in body(INO, "static void on_espnow_recv("), \
    "frames received are not counted, so 'was it still hearing' has no answer"
assert re.search(r"RTC_NOINIT_ATTR static uint8_t _evt_snap\[MOUNT_EVENT_NOMEM_SNAP_LEN\]", INO), \
    "the snapshot is not in RTC memory, so the reboot erases it"
stash = body(INO, "static void nomem_stash_event(")
for need in ("encode_mount_nomem_snapshot(_evt_snap", "_evt_magic   = MOUNT_EVT_MAGIC",
             "_evt_kind    = MOUNT_EVENT_NOMEM_REBOOT"):
    assert need in stash, f"nomem_stash_event() is missing: {need}"
send = FLAT[FLAT.index("if (_evt_pending && hub_ok)"):]
send = send[:send.index("send_to_hub(CMD_MOUNT_EVENT")]
assert ("if (_evt_kind == MOUNT_EVENT_NOMEM_REBOOT)" in send and
        "memcpy(p + MOUNT_EVENT_PAYLOAD_LEN, _evt_snap, MOUNT_EVENT_NOMEM_SNAP_LEN)" in send and
        "n = MOUNT_EVENT_NOMEM_PAYLOAD_LEN" in send), \
    "the post-reboot event does not carry the snapshot"
print("   records RAM, completions, channel, BLE; survives reboot  OK")

# ---- 5. the health tail ----------------------------------------------------------
print("\n5. the health tail:")
health = body(INO, "static void send_health(")
assert "uint8_t p[24 + HEALTH_BRIDGE_TAIL_LEN]" in health and \
       "encode_health_bridge_tail(p + 24, &t)" in health and \
       "send_to_hub(CMD_HEALTH, p, sizeof(p))" in health, \
    "the bridge's health does not carry the tail after the uniform 24 bytes"
print("   internal RAM and healed runs follow the 24 bytes          OK")

# ---- 6. what the app makes of it ------------------------------------------------
print("\n6. the app:")
from comms import mount_manager as mm
from comms.protocol import (Cmd, MOUNT_EVENT_NOMEM_REBOOT, MOUNT_NOMEM_BLE_LINKED,
                            MOUNT_NOMEM_BLE_BONDED, MOUNT_NOMEM_BLE_SCANNING)

MMSRC = (REPO / "pc_app/comms/mount_manager.py").read_text()
branch = MMSRC[MMSRC.index("if kind == MOUNT_EVENT_NOMEM_REBOOT:"):]
branch = branch[:branch.index("return")]
assert "_nomem_event_text(b, txf, rei, ref, err)" in branch, \
    "the MOUNT_EVENT handler does not route kind 4 to the decoder"

HEAD14 = bytes([MOUNT_EVENT_NOMEM_REBOOT, 0, 3, 0, 0, 0, 2, 0, 3, 0, 194, 0x30, 0x67, 0])


def event(cb_during=0, in_flight=1, chan_now=1, chan_hub=1, ble=0, since_cb=0xFFFF):
    snap = struct.pack(">IIHHHHHIIIBBHBBBHHH", 2196, 21402, since_cb, 110, 2100,
                       in_flight, 0, 41234, 38000, 30100, chan_now, chan_hub, 10, 1,
                       ble, int(Cmd.STATUS), cb_during, 4, 18)
    return HEAD14 + snap


txf, rei, ref, err = 3, 0, 194, 0x3067
head, detail = mm._nomem_event_text(event(), txf, rei, ref, err)
print("   " + head[:110] + "...")
print("   " + detail[:110] + "...")
assert "REBOOTED ON REFUSED SENDS" in head, head
assert "NOT ONE completed" in head, \
    f"outstanding sends with no completion in 3 s is not read as the radio\n    stopping: {head}"
for want in ("in flight 1", "iram 40k free", "largest block 29k",
             "refused first: STATUS", "until the reboot: 18 refused, 0 completions, "
             "4 frames heard", "last completion >=65 s before", "worst loop 10 ms in 'teensy'",
             "since boot: txfail 3"):
    assert want in detail, f"missing {want!r} in: {detail}"
assert "CHANNEL MOVED" not in head, "a channel that did not move is reported as moved"

# 21402 accepted in 2196 s is ~29 due in the 3 s. A quarter of that or more is
# a queue that is moving; fewer is a radio that has nearly stopped — the first
# real capture had ONE against ~20 due and was misreported as "not stalled".
h2, _ = mm._nomem_event_text(event(cb_during=25), txf, rei, ref, err)
assert "still COMPLETING" in h2 and "~29" in h2 and "NOT ONE" not in h2, h2
h2b, _ = mm._nomem_event_text(event(cb_during=1), txf, rei, ref, err)
assert "only 1 send(s) completed" in h2b and "nearly stopped" in h2b, \
    f"one completion in 3 s against ~29 due reads as a working queue: {h2b}"
assert "still COMPLETING" not in h2b, h2b
h3, _ = mm._nomem_event_text(event(in_flight=0), txf, rei, ref, err)
assert "nothing was outstanding" in h3, h3
h4, _ = mm._nomem_event_text(event(chan_now=1, chan_hub=6), txf, rei, ref, err)
assert "CHANNEL MOVED: radio on 1, hub on 6" in h4, h4
_, d5 = mm._nomem_event_text(event(ble=MOUNT_NOMEM_BLE_SCANNING | MOUNT_NOMEM_BLE_LINKED
                                   | MOUNT_NOMEM_BLE_BONDED), txf, rei, ref, err)
assert "BLE: SCANNING, camera linked" in d5, d5
print("   four verdicts, channel and BLE read correctly           OK")

h6, d6 = mm._nomem_event_text(HEAD14, txf, rei, ref, err)
assert "(no snapshot in this event)" in h6 and d6 == "", \
    "a 14-byte event of the new kind is not handled — a firmware that sends the\n" \
    "    kind without the snapshot must still log, not throw"
print("   an event without the snapshot still logs               OK")

# ---- 7. the health line ----------------------------------------------------------
BUF = io.StringIO()
_h = logging.StreamHandler(BUF); _h.setFormatter(logging.Formatter("%(message)s"))
_lg = logging.getLogger("comms.bridge")
_saved = (_lg.handlers, _lg.level, _lg.propagate)
_lg.handlers, _lg.propagate = [_h], False
_lg.setLevel(logging.INFO)
from comms.bridge import Bridge


class _P:
    cmd = Cmd.HEALTH
    mount_id = 1
    def __init__(self, payload): self.payload = payload


def health(tail: bytes = b""):
    b = Bridge.__new__(Bridge)
    b._diag_lock = threading.Lock(); b._node_uptime = {}; b._sat_names = {}
    b._cam_ble = {}; b._node_state_cur = {}; b._node_state_prev = {}
    b._node_state_written = 0.0; b._node_state_path = None
    BUF.truncate(0); BUF.seek(0)
    b._note_node_health(_P(struct.pack(">BBIIIHHbBI", 1, 1, 36000, 8400000, 8390000,
                                       9, 6, -33, 0, 1 << 4) + tail))
    return BUF.getvalue().strip()


print("\n7. the health line:")
old = health()
assert "iram" not in old and "NO_MEM" not in old, \
    f"a mount without the tail is shown with one: {old}"
new = health(struct.pack(">IIIHH", 142000, 118000, 60000, 0, 0))
assert "iram 138k (min 115k, largest 58k)" in new, new
assert "NO_MEM CLEARED ITSELF" not in new, \
    f"a mount on which no run ever healed claims one did: {new}"
healed = health(struct.pack(">IIIHH", 142000, 118000, 1500, 2, 1234))
assert "largest 1.5k" in healed, \
    f"a small block is rounded to a whole k, hiding the buffer-sized difference: {healed}"
assert "NO_MEM CLEARED ITSELF 2 (longest 1234 ms)" in healed, healed
assert new.index("n32 1") < new.index("iram"), \
    "the tail is not appended after the existing fields"
print("   " + healed[healed.index("n32"):])
print("   absent -> silent, present -> iram, healed runs shown    OK")

_lg.handlers, _lg.level, _lg.propagate = _saved
print("\nALL CHECKS PASSED")
