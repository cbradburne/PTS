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
assert re.search(r"RTC_NOINIT_ATTR static uint8_t _evt_snap\[MOUNT_EVENT_NOMEM_SNAP_LEN \+\s*"
                 r"MOUNT_EVENT_NOMEM_LADDER_LEN\]", INO), \
    "the snapshot and ladder are not in RTC memory, so the reboot erases them"
stash = body(INO, "static void nomem_stash_event(")
for need in ("encode_mount_nomem_snapshot(_evt_snap", "_evt_magic   = MOUNT_EVT_MAGIC",
             "_evt_kind    = MOUNT_EVENT_NOMEM_REBOOT"):
    assert need in stash, f"nomem_stash_event() is missing: {need}"
send = FLAT[FLAT.index("if (_evt_pending && hub_ok)"):]
send = send[:send.index("send_to_hub(CMD_MOUNT_EVENT")]
assert ("if (_evt_kind == MOUNT_EVENT_NOMEM_REBOOT)" in send and
        "memcpy(p + MOUNT_EVENT_PAYLOAD_LEN, _evt_snap, sizeof(_evt_snap))" in send and
        "n = MOUNT_EVENT_NOMEM_PAYLOAD_LEN" in send), \
    "the post-reboot event does not carry the snapshot and the ladder"
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
from comms.protocol import (Cmd, MOUNT_EVENT_NOMEM_REBOOT, MOUNT_EVENT_NOMEM_CURED,
                            MOUNT_NOMEM_BLE_LINKED, MOUNT_NOMEM_BLE_BONDED,
                            MOUNT_NOMEM_BLE_SCANNING, MOUNT_NOMEM_STEP_BLE_PAUSED,
                            MOUNT_NOMEM_STEP_SCAN_FREED, MOUNT_NOMEM_STEP_RESERVE,
                            MOUNT_NOMEM_STEP_NO_RESERVE)

MMSRC = (REPO / "pc_app/comms/mount_manager.py").read_text()
branch = MMSRC[MMSRC.index("if kind in (MOUNT_EVENT_NOMEM_REBOOT, MOUNT_EVENT_NOMEM_CURED):"):]
branch = branch[:branch.index("return")]
assert "_nomem_event_text(kind, b, txf, rei, ref, err)" in branch, \
    "the MOUNT_EVENT handler does not route kinds 4 and 5 to the decoder"


def head14(kind=MOUNT_EVENT_NOMEM_REBOOT):
    return bytes([kind, 0, 3, 0, 0, 0, 2, 0, 3, 0, 194, 0x30, 0x67, 0])


def event(cb_during=0, in_flight=1, chan_now=1, chan_hub=1, ble=0, since_cb=0xFFFF,
          kind=MOUNT_EVENT_NOMEM_REBOOT, ladder=None):
    snap = struct.pack(">IIHHHHHIIIBBHBBBHHH", 2196, 21402, since_cb, 110, 2100,
                       in_flight, 0, 41234, 38000, 30100, chan_now, chan_hub, 10, 1,
                       ble, int(Cmd.STATUS), cb_during, 4, 18)
    return head14(kind) + snap + (struct.pack(">BBHHHHHII", *ladder) if ladder else b"")


txf, rei, ref, err = 3, 0, 194, 0x3067
# A 57-byte event is firmware from before the ladder: snapshot, no ladder.
lines = mm._nomem_event_text(MOUNT_EVENT_NOMEM_REBOOT, event(), txf, rei, ref, err)
head, detail = lines[0], lines[-1]
print("   " + head[:110] + "...")
print("   " + detail[:110] + "...")
assert len(lines) == 2, f"a pre-ladder event grew a ladder line: {lines}"
assert "REBOOTED ON REFUSED SENDS" in head, head
assert "NOT ONE completed" in head, \
    f"outstanding sends with no completion in 3 s is not read as the radio\n    stopping: {head}"
for want in ("in flight 1", "iram 40k free", "largest block 29k",
             "refused first: STATUS", "until the reboot: 18 refused, 0 completions, "
             "4 frames heard", "last completion >=65 s before", "worst loop 10 ms in 'teensy'",
             "since boot: txfail 3"):
    assert want in detail, f"missing {want!r} in: {detail}"
assert "CHANNEL MOVED" not in head, "a channel that did not move is reported as moved"


def h(**kw):
    return mm._nomem_event_text(kw.pop("kind", MOUNT_EVENT_NOMEM_REBOOT),
                                event(**kw), txf, rei, ref, err)[0]


# 21402 accepted in 2196 s is ~29 due in the 3 s. A quarter of that or more is
# a queue that is moving; fewer is a radio that has nearly stopped — the first
# real capture had ONE against ~20 due and was misreported as "not stalled".
h2 = h(cb_during=25)
assert "still COMPLETING" in h2 and "~29" in h2 and "NOT ONE" not in h2, h2
h2b = h(cb_during=1)
assert "only 1 send(s) completed" in h2b and "nearly stopped" in h2b, \
    f"one completion in 3 s against ~29 due reads as a working queue: {h2b}"
assert "still COMPLETING" not in h2b, h2b
assert "nothing was outstanding" in h(in_flight=0)
assert "CHANNEL MOVED: radio on 1, hub on 6" in h(chan_now=1, chan_hub=6)
d5 = mm._nomem_event_text(MOUNT_EVENT_NOMEM_REBOOT,
                          event(ble=MOUNT_NOMEM_BLE_SCANNING | MOUNT_NOMEM_BLE_LINKED
                                | MOUNT_NOMEM_BLE_BONDED), txf, rei, ref, err)[-1]
assert "BLE: SCANNING, camera linked" in d5, d5
print("   four verdicts, channel and BLE read correctly           OK")

only = mm._nomem_event_text(MOUNT_EVENT_NOMEM_REBOOT, head14(), txf, rei, ref, err)
assert len(only) == 1 and "(no snapshot in this event)" in only[0], \
    "a 14-byte event of the new kind is not handled — a firmware that sends the\n" \
    "    kind without the snapshot must still log, not throw"
print("   an event without the snapshot still logs               OK")

# The ladder. A cure is named by the step the run ended SOONEST after, with the
# gap printed, because 12 ms after a step and 900 ms after it are not the same
# evidence. A reboot names what was tried and did not work.
ALL3 = MOUNT_NOMEM_STEP_BLE_PAUSED | MOUNT_NOMEM_STEP_SCAN_FREED | MOUNT_NOMEM_STEP_RESERVE
cured = mm._nomem_event_text(
    MOUNT_EVENT_NOMEM_CURED,
    event(kind=MOUNT_EVENT_NOMEM_CURED, cb_during=6,
          ladder=(ALL3, MOUNT_NOMEM_BLE_SCANNING, 0, 204, 1000, 1012, 12288, 48000, 60300)),
    txf, rei, ref, err)
print("   " + cured[0][:110] + "...")
print("   " + cured[1][:110] + "...")
assert len(cured) == 3, f"a cured run is not head, ladder, snapshot: {cured}"
assert cured[0].startswith("NO_MEM ENDED WITHOUT A REBOOT"), cured[0]
assert "1012 ms long, ending 12 ms after the reserve was released" in cured[0], \
    f"the cure is not attributed to the last step before the end: {cured[0]}"
assert "its 1012 ms" in cured[0], \
    f"completions are not judged over the cured run's own length: {cured[0]}"
for want in ("BLE scan stopped at 0 ms", "scan freed at 204 ms (iram 46k)",
             "reserve 12k released at 1000 ms (iram 58k)", "ended at 1012 ms"):
    assert want in cured[1], f"missing {want!r} in: {cured[1]}"
assert "until the end:" in cured[2], cured[2]

reboot = mm._nomem_event_text(
    MOUNT_EVENT_NOMEM_REBOOT,
    event(ladder=(MOUNT_NOMEM_STEP_NO_RESERVE, 0, 0xFFFF, 0xFFFF, 0xFFFF, 3004, 0, 0, 0)),
    txf, rei, ref, err)
assert "the ladder did not end it" in reboot[0], reboot[0]
for want in ("BLE was not scanning or connecting", "no reserve was held", "rebooted at 3004 ms"):
    assert want in reboot[1], f"missing {want!r} in: {reboot[1]}"
print("   a cure names its step and gap; a reboot what failed      OK")

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
assert "reserve" not in new, f"a 16-byte tail is shown with the ladder's fields: {new}"
healed = health(struct.pack(">IIIHH", 142000, 118000, 1500, 2, 1234))
assert "largest 1.5k" in healed, \
    f"a small block is rounded to a whole k, hiding the buffer-sized difference: {healed}"
assert "NO_MEM CLEARED ITSELF 2 (longest 1234 ms)" in healed, healed
assert new.index("n32 1") < new.index("iram"), \
    "the tail is not appended after the existing fields"
lad = health(struct.pack(">IIIHHHHHH", 142000, 118000, 60000, 0, 0, 12288, 3, 87, 58))
for want in ("reserve 12k", "NO_MEM ENDED BY THE LADDER 3", "last scan held 87 devices (58k)"):
    assert want in lad, f"missing {want!r} in: {lad}"
none = health(struct.pack(">IIIHHHHHH", 9800, 400, 5000, 1, 1505, 0, 0, 0, 0))
assert "no reserve" in none and "LADDER" not in none and "last scan" not in none, none
print("   " + lad[lad.index("n32"):])
print("   absent -> silent, present -> iram, the ladder's fields   OK")

_lg.handlers, _lg.level, _lg.propagate = _saved

# ---- 8. the ladder, in the firmware ---------------------------------------------
print("\n8. the ladder:")
step = body(INO, "static void nomem_ladder_step(")
i_pause, i_free, i_res = (step.index("ble_cam_nomem_pause("), step.index("ble_cam_free_scan()"),
                          step.index("nomem_reserve_release_into("))
assert i_pause < i_free < i_res, "the steps are not in order: BLE, scan, reserve"
assert "age >= (uint32_t)l.t_pause_ms + NOMEM_SCAN_FREE_GAP_MS" in step, \
    "the scan is freed without waiting after the stop — a report the BLE task was\n" \
    "    already handling would be walking the map as it is deleted"
assert "(l.ble_stopped & MOUNT_NOMEM_BLE_SCANNING)" in step, \
    "the scan is freed when no scan was stopped"
assert "age >= NOMEM_RESERVE_AFTER_MS" in step, "the reserve is released at once"
crits = [step[a:step.index("portEXIT_CRITICAL", a)]
         for a in [i for i in range(len(step)) if step.startswith("portENTER_CRITICAL", i)]]
for c in crits:
    for bad in ("ble_cam_", "heap_caps_", "Serial."):
        assert bad not in c, f"the ladder calls {bad} inside the critical section"
rung3 = INO[INO.index("Refused sends → reboot"):]
rung3 = rung3[:rung3.index("One-way transmit wedge")]
assert rung3.index("nomem_ladder_step(") < rung3.index("span >= (int32_t)NOMEM_RESTART_MS"), \
    "the reboot is checked before the ladder has had its turn"

pause = body(BLE, "static uint8_t ble_cam_nomem_pause(")
assert "ble_gap_terminate" not in pause, \
    "the pause drops an established camera link — it is meant to stop a scan or\n" \
    "    an attempt, not disconnect a working camera"
assert "BLEDevice::getScan()->stop()" in pause and "ble_gap_disc_cancel" not in pause, \
    "the scan is cancelled behind BLEScan's back, so the library's own state (and\n" \
    "    bc_scan_done) never hear that it ended"
assert "clearResults" not in pause and "ble_cam_free_scan" not in pause, \
    "the pause frees the scan in the same breath as it stops it"
poll = body(BLE, "static void ble_cam_poll(")
assert "!busy && !held &&" in poll, "the hold is not honoured when starting a scan"

heal = body(INO, "static void nomem_heal(")
# The expression, not the declaration above it ("bool cured = false;").
cexpr = heal[heal.index("cured = since =="):]
cexpr = cexpr[:cexpr.index(";")]
assert "MOUNT_NOMEM_STEP_RESERVE" in cexpr and "MOUNT_NOMEM_STEP_NO_RESERVE" not in cexpr, \
    "a run in which no reserve was HELD counts as cured by it"
assert "_nomem_cured_due          = true" in heal and "_nomem_cured_snap = _nomem_snap" in heal, \
    "a cured run is not copied out before the next run can overwrite it"
cured_send = body(INO, "static void nomem_send_cured(")
assert "MOUNT_EVENT_NOMEM_CURED" in cured_send and "sizeof(p)" in cured_send, \
    "a cured run is not reported as kind 5 with the full payload"
assert "nomem_send_cured();" in INO[INO.index("if (_evt_pending && hub_ok)"):], \
    "nothing in loop() sends the cured-run report"

tend = body(INO, "static void nomem_reserve_tend(")
for need, why in (("_nomem_since_ms) return", "while a run is open"),
                  ("ble_gap_disc_active()", "while a scan is at its peak"),
                  ("NOMEM_RESERVE_BYTES + NOMEM_RESERVE_MARGIN", "without the margin"),
                  ("NOMEM_RESERVE_RETAKE_MS", "straight after a release"),
                  ("MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA", "from outside the WiFi driver's pool")):
    assert need in tend, f"the reserve can be taken {why}"
print("   BLE, then scan, then reserve; outside the lock; no link dropped  OK")

# ---- 9. the scan's haul -------------------------------------------------------------
print("\n9. what the camera scan collects:")
consume = poll[poll.index("_bc_scan_ready = false;"):poll.index("if (!bc_choose())")]
assert "ble_cam_free_scan();" in consume, \
    "a finished scan's results are kept after they have been read — the last scan\n" \
    "    before the camera connects sits in internal RAM for the whole boot"
free = body(BLE, "static uint32_t ble_cam_free_scan(")
assert "clearResults()" in free and "getCount()" in free, \
    "the free does not count what it freed, so the log cannot say what a room cost"
assert "if (!n) return 0;" in free, \
    "an empty free overwrites the figures of the one before it"
cb = BLE[BLE.index("class BcScanCb"):]
cb = cb[:cb.index("\n};")]
assert "void onResult(BLEAdvertisedDevice dev)" in cb and "BLEAdvertisedDevice *" not in cb, \
    "the scan callback keeps a pointer into the library's results — freeing them\n" \
    "    would leave it dangling. It must copy what it needs, as it did."
print("   freed once read, counted, and nothing points into them  OK")

# ---- 10. a camera switched off must not send the mount scanning ------------------
# The foyer test on 2026-09-23: camera off for ten minutes, and the mount
# scanned 5 s in every 20 because its 10 s retry landed inside a 15 s connect
# attempt, NimBLE refused it (EALREADY), and that read as "could not start —
# rescan". Every scan held 127-165 devices; all eight NO_MEM runs came out of
# them. And two of the eight began after a scan had FINISHED, while its results
# sat waiting up to five seconds for the next retry to free them.
print("\n10. an absent camera, and a finished scan:")
FLATB = re.sub(r"\s+", " ", BLE)
assert re.search(r"bool busy = _bc_connected \|\| _bc_conn != BLE_HS_CONN_HANDLE_NONE "
                 r"\|\| _bc_scanning \|\| ble_gap_conn_active\(\);", FLATB), \
    "a connection attempt in progress is not busy, so the 10 s retry lands inside\n" \
    "    the 15 s attempt, fails, and drops the camera into a scan"
gap = body(BLE, "static int bc_gap_event(")
fail_blk = gap[gap.index("if (ev->connect.status != 0) {"):]
fail_blk = fail_blk[:fail_blk.index("return 0;")]
assert "_bc_retry_ms = t ? t : 1;" in fail_blk, \
    "a failed attempt does not restart the retry clock, so with the busy fix the\n" \
    "    next attempt is already due and BLE holds the radio back to back"
done = body(BLE, "static void bc_scan_done(")
assert "_bc_scan_end_ms" in done and done.index("_bc_scan_end_ms") < done.index("_bc_scan_ready  = true"), \
    "the scan's end is not stamped before it is announced, so the gap below is\n" \
    "    measured from an older scan"
assert "ble_cam_free_scan" not in done and "clearResults" not in done, \
    "results are freed on the BLE task, inside the library's own callback"
early = poll[poll.index("if (_bc_scan_ready && !ble_gap_disc_active() &&"):]
early = early[:early.index(";") + 1]
assert "(uint32_t)(now - _bc_scan_end_ms) >= BC_SCAN_FREE_GAP_MS" in early and \
       "ble_cam_free_scan()" in early, \
    "a finished scan is freed without the gap after it ended — after a CANCEL a\n" \
    "    report may still be walking the results"
assert poll.index("if (_bc_scan_ready && !ble_gap_disc_active() &&") < \
       poll.index("if (!busy && !held &&"), \
    "the early free sits behind the retry gate, so it still waits for `due`"
assert re.search(r"#define NOMEM_SCAN_FREE_GAP_MS\s+BC_SCAN_FREE_GAP_MS", INO), \
    "the ladder and the camera code keep separate numbers for the same safety gap"
assert "_bc_scan_freed_at_ms = t ? t : 1;" in body(BLE, "static uint32_t ble_cam_free_scan("), \
    "the free is not stamped, so the ladder cannot see one made during its run"
assert "(int32_t)(fa - since) >= 0" in step and "_bc_scan_freed_at_ms" in step, \
    "the ladder does not record a scan freed during the run by ble_cam_poll(), so\n" \
    "    that cure would be credited to whatever step came next"
print("   an attempt in progress is busy; a finished scan is freed at once  OK")

print("\nALL CHECKS PASSED")
