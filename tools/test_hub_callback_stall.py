"""The hub can see its own transmitter stop.

On 2026-09-08 the hub's ESP-NOW send path stopped at 6.00 h uptime and stayed
stopped for FOURTEEN HOURS. Every mount on its radio went deaf while their
health kept arriving, so the PC app showed them connected and not answering;
cam1 logged 193 stack reinits and 4,916 send failures trying to reach it. The
one mount reached through a satellite never noticed, which is what identified
the hub as the fault rather than the mounts.

The hub had a self-rescue ladder — ESP-NOW reinit at 6 s, WiFi bounce at 14 s,
reboot at 25 s — and not one rung fired. Every one of them is armed by a RUN OF
SEND FAILURES, and the fault produces none: the send callback stops firing, so
nothing is ever reported as failed. The hub's own txfail read 83 at the first
minute and 83 at the last.

A failure count cannot report the failure of the thing that reports failures.

Its other backstop, an 8 h maintenance restart, was disabled too: it wants
20 minutes with no client command, and the PC app polls GET_CONFIG every ~3 s
for as long as it is open.

Run directly, or via tools/run_tests.sh with the rest.
"""
import pathlib, re

REPO = pathlib.Path(__file__).resolve().parent.parent
HUB  = (REPO / "firmware/esp32_hub_eth/esp32_hub_eth.ino").read_text()


def block(start, end_marker="\n}"):
    """Body of a function, found by its DEFINITION.

    Several of these have a forward declaration earlier in the file, and a
    plain index() lands on that instead — a block that then contains none of
    the code being asserted about, so every check inside it fails for the
    wrong reason.
    """
    i = HUB.index(start + " {") if (start + " {") in HUB else HUB.index(start)
    return HUB[i:i + HUB[i:].index(end_marker)]


# ---- 1. both halves of the subtraction ------------------------------------
print("1. what the hub counts:")
cb = block("static void on_espnow_sent(")
# Whitespace-tolerant: these lines are column-aligned in the source and a
# tidy-up should not fail the suite.
assert re.search(r"_espnow_cb_total\s*=\s*_espnow_cb_total \+ 1;", cb), \
    "callbacks are not counted, so nothing can notice them stopping"
assert cb.index("_espnow_cb_total") < cb.index("for (int i = 0"), \
    "the callback is counted INSIDE the per-mount loop, so one for a MAC no\n" \
    "    longer in the table is not counted — and the whole point is to notice\n" \
    "    callbacks not arriving, which must not depend on whose they are"
assert re.search(r"_espnow_cb_last_ms\s*=\s*millis\(\);", cb), \
    "nothing records WHEN the last callback arrived, so a stall has no clock"
print("   callbacks counted first, and timestamped            OK")

# espnow_send_now() is the one place esp_now_send() is called on this node; the
# queue and the bound sit in front of it.
snd = block("static bool espnow_send_now(")   # returns whether the stack took it
assert re.search(r"_espnow_issued\s*=\s*_espnow_issued \+ 1;", snd), \
    "sends are not counted"
assert snd.index("e == ESP_OK") < snd.index("_espnow_issued"), \
    "a REJECTED send is counted as issued — it never took a buffer, so it would\n" \
    "    read as an outstanding send that can never complete"
# Code lines only: this file discusses esp_now_send() in several comments, and
# counting those would make the check pass or fail on prose.
calls = [l for l in HUB.splitlines()
         if "esp_now_send(" in l.split("//")[0]]
assert len(calls) == 1, \
    "esp_now_send() is called from %d places, so the counter and the burst bound\n" \
    "    no longer see every buffer taken:\n      %s" \
    % (len(calls), "\n      ".join(c.strip() for c in calls))
print("   sends counted on ESP_OK only, one send path         OK")

# ---- 2. the rung that the outage needed ------------------------------------
print("\n2. armed by the stall, not by failures:")
assert "SELF_CB_STALL_MS" in HUB, "no stall threshold"
# The value lives in shared/protocol.h now — three places had to agree and
# two were hand-copied. Read it from the canonical file, not the hub.
PROT = (REPO / "firmware/shared/protocol.h").read_text()
stall_ms = int(re.search(r"#define CB_STALL_MS\s+(\d+)", PROT).group(1))
assert "#define SELF_CB_STALL_MS         CB_STALL_MS" in HUB, \
    "the hub redefines the stall threshold instead of taking the shared one"
reinit_ms = int(re.search(r"#define SELF_REINIT_AFTER_MS\s+(\d+)", HUB).group(1))
assert stall_ms < reinit_ms, \
    f"the stall clock ({stall_ms} ms) starts no sooner than the first ladder rung " \
    f"({reinit_ms} ms), so it adds nothing"
assert stall_ms >= 1000, \
    f"{stall_ms} ms is inside the time a send can legitimately take with MAC " \
    f"retries — this would fire on a busy moment"
print(f"   stall {stall_ms} ms, first rung {reinit_ms} ms               OK")

chk = HUB[HUB.index("uint32_t cb_stall = 0;"):]
chk = chk[:chk.index("if (worst_i < 0) return;")]
assert "espnow_in_flight() > 0" in chk, \
    "the stall fires with nothing outstanding, so an idle hub with no mounts\n" \
    "    powered would restart itself on a timer"
# SIGNED difference. now is captured at the top of the loop pass and
# _espnow_cb_last_ms is written by the WiFi task, so a callback landing between
# them makes last > now; unsigned, that underflows to ~4.29e9 ms and clears
# every rung of the ladder instantly. The hub then reinits over a race lasting
# microseconds, and the deinit orphans whatever was in flight — which is how a
# healthy radio ends up reporting a leak floor of 32.
assert re.search(r"int32_t d = \(int32_t\)\(now - cb_last\);", chk), \
    "the stall clock uses an unsigned subtraction, so a callback arriving after\n" \
    "    'now' was sampled reads as a 49-day stall and fires the whole ladder"
assert "if (d > 0) cb_stall" in chk, "a negative difference is not discarded"
assert "cb_last  = _espnow_cb_last_ms;" in chk, \
    "the timestamp is read twice instead of latched, so it can change between\n" \
    "    the guard and the subtraction"
assert "cb_last &&" in chk, \
    "the clock runs before the first callback has ever arrived, so a hub that\n" \
    "    has not yet sent anything reads as stalled at boot"
assert "_cb_stall_since_ms = 0;" in chk, "the stall never clears when it recovers"
print("   needs sends outstanding, and clears on recovery     OK")

# ---- 3. it must not be talked out of it ------------------------------------
# tx_proven_ok exists so the hub does not reboot over one deaf mount. It reads
# other mounts' failure runs — which are all zero when the callback has stopped,
# so it would "prove" the radio fine at the exact moment it is not.
print("\n3. the mount-side get-out does not apply:")
tp = HUB[HUB.index("bool tx_proven_ok = false;"):]
tp = tp[:tp.index("if (tx_proven_ok)")]
assert "!_cb_stall_active" in tp, \
    "tx_proven_ok still runs during a callback stall. Every other mount's\n" \
    "    failure run is zero then — not because the radio is fine but because\n" \
    "    nothing is being reported at all — so it would suppress the one rung\n" \
    "    that can see this fault."
print("   suppressed while the callback is stalled            OK")

# And the log must not blame a mount for a hub-wide fault.
lad = HUB[HUB.index("char who[48];"):]
lad = lad[:lad.index("esp_restart();")]
assert "the send callback (hub-wide)" in lad, \
    "a hub-wide stall is reported against a mount id. It borrows worst_i = 0 to\n" \
    "    reach the ladder, so without this the log names mount 1 for a fault\n" \
    "    that has nothing to do with mount 1 — and the last outage was misread\n" \
    "    for fourteen hours."
assert lad.count("%s") >= 3, "not every rung names what it is escalating on"
print("   all three rungs name the hub, not mount 1           OK")

# ...and the EVENT must carry it too. The line above goes to Serial, which on a
# rigged hub is inside the enclosure; the event is what reaches comms.log. This
# check pinned only the Serial string and passed on 2026-09-09 while both real
# callback stalls were logged to the operator as "TX wedge on mount 1".
assert "uint8_t ev_mount = _cb_stall_active ? 0 :" in HUB, \
    "the event has no hub-wide form, so a stall is reported against a mount id\n" \
    "    in the ONE place the operator reads"
for k in ("2", "3"):
    assert f"send_hub_event({k}," in HUB, f"kind {k} event is gone"
assert "send_hub_event(2, (uint8_t)(worst_i + 1)" not in HUB and \
       "send_hub_event(3, (uint8_t)(worst_i + 1)" not in HUB, \
    "a ladder event still sends worst_i + 1 directly, so it names mount 1 for a\n" \
    "    hub-wide fault — the misattribution this whole rung exists to end"
assert HUB.count("send_hub_event(2, ev_mount") + HUB.count("send_hub_event(3, ev_mount") == 3, \
    "not all three ladder events use the hub-wide-aware mount id"
print("   and the EVENT carries it, not just Serial           OK")

# ---- 4. the backstop can actually fire -------------------------------------
print("\n4. the maintenance restart:")
act = block("static inline bool cmd_is_client_activity(")
for c in ("CMD_GET_CONFIG", "CMD_GET_STATUS", "CMD_PING"):
    assert c in act, f"{c} still counts as client activity"
assert act.index("return false") < act.index("default:"), \
    "the polls fall through to the default and still count"
print("   read-only polls do not count as activity            OK")

fwd = block("static void forward_to_mounts(const ParsedPacket &pkt)")
assert "if (cmd_is_client_activity(pkt.cmd)) _last_client_cmd_ms = millis();" in fwd, \
    "forward_to_mounts still stamps the activity clock unconditionally, so the\n" \
    "    keepalive poll keeps the 20-minute quiet window permanently shut and the\n" \
    "    8 h restart can never run while the app is open"
print("   the quiet window can now open                       OK")

# A write command must still defer it — the restart must never land mid-move.
assert re.search(r"default:\s*\n\s*return true;", act), \
    "commands not listed default to NOT being activity, so a move command would\n" \
    "    fail to defer the restart and the hub could reboot mid-shot"
print("   anything that changes something still defers it     OK")

# ---- 4b. the burst bound: stop it happening, not just survive it ------------
# Everything above measures the wedge and escalates on it. This is the first
# thing on the hub meant to PREVENT it. The bench needed a blocked loop AND 3+
# sends in flight; this hub had both on 2026-09-09 — loopmax 427 ms and 403 ms
# at the two stalls, and five GET_CONFIG forwarded back to back every ~4 s.
print("\n4b. the burst bound:")
cap = int(re.search(r"#define ESPNOW_TX_INFLIGHT_CAP\s+(\d+)", HUB).group(1))
assert cap == 2, f"the cap is {cap}; the bench proved 2"
sat_fn = block("static inline bool espnow_tx_saturated(")
assert "if (!ESPNOW_TX_INFLIGHT_CAP) return false;" in sat_fn, \
    "cap 0 does not restore the old behaviour, so this cannot be A/B'd on the rig"
assert "_cb_stall_active" in sat_fn, \
    "a callback stall parks in_flight high for ever. Counting that would stop the\n" \
    "    hub transmitting entirely at the moment it is already in trouble —\n" \
    "    trading a leak that takes hours for an outage that takes effect now."

pump = block("static void espnow_tx_pump(")
assert "ESPNOW_TX_DEFER_MS" in pump, \
    "no overdue escape: a busy radio could hold a command indefinitely"
defer_ms = int(re.search(r"#define ESPNOW_TX_DEFER_MS\s+(\d+)", HUB).group(1))
presence = 3 * int(re.search(r"#define MOUNT_STATUS_REFRESH_MS\s+(\d+)",
                             (REPO / "firmware/shared/protocol.h").read_text()).group(1)) + 1000
assert defer_ms * 10 < presence, \
    f"a {defer_ms} ms hold eats too much of the {presence} ms presence timeout"
print(f"   cap {cap}, escape {defer_ms} ms, stands aside on a stall   OK")

# The queue must be drained from loop() as well as from the enqueue — otherwise
# a frame held on the last command of a burst waits for the NEXT command, which
# on an idle rig could be seconds.
loop_body = HUB[HUB.index("void loop() {"):]
loop_body = loop_body[:loop_body.index("eth_report_once_if_down")]
assert "espnow_tx_pump(now);" in loop_body, \
    "the ring is never drained from loop(), so a held frame waits for the next\n" \
    "    command rather than the next pass"
enq = block("static void espnow_send_if_present(")
assert "espnow_tx_pump(now);" in enq, \
    "an idle radio pays for the ring: without pumping first, a frame arriving\n" \
    "    with nothing outstanding is queued instead of sent"
assert "_entx_deferred++" in enq, \
    "deferrals are not counted, so a hub that stops wedging cannot say whether\n" \
    "    the bound had anything to do with it"
print("   drained from loop() and on enqueue, and counted    OK")

# A counter that reports nowhere is the same as not having one. The first build
# of this had _entx_deferred incrementing and reaching nothing at all, so the
# morning's log would have shown "no wedge" and been unable to say whether the
# bound engaged — which is the whole question.
hh = HUB[HUB.index("h.node_u32      = ((uint32_t)(_entx_deferred"):]
hh = hh[:hh.index(";")]
assert "_entx_deferred" in hh and "_entx_dropped" in hh and "_ghost_rx_drops" in hh, \
    "the hub's node_u32 does not carry all three, so the burst bound is invisible"
assert hh.index("_ghost_rx_drops") > hh.index("_entx_dropped"), \
    "ghost drops are not in the LOW half. A hub on older firmware sends the bare\n" \
    "    ghost count in the whole word: with ghosts high, its 5 ghosts decode as\n" \
    "    5 discarded commands — a fault it does not have."
print("   and node_u32 carries it, old hubs still readable   OK")

# It has to be a RATE, not a total. Cumulative, this read 249 at 2.4 minutes of
# uptime and saturated at four: proof the bound engages and nothing else ever
# again — no rate, no change in rate, nothing to line up against a stall. The
# mount's own counter comment describes that trap; the hub hit it 15x faster
# because it defers ~100 times a minute against the mount's ~6 an hour.
hs = block("static void send_own_health(")
assert "_entx_deferred      = 0;" in hs, \
    "the held-back count is not cleared per health report, so it saturates four\n" \
    "    minutes into a boot and stops carrying information"
assert hs.index("h.node_u32") < hs.index("_entx_deferred      = 0;"), \
    "the counter is cleared before it is packed, so every report reads 0"
# Overflows are a fault and stay cumulative — a rate would hide a single drop.
assert "_entx_dropped" not in hs.split("_entx_deferred      = 0;")[1], \
    "ring overflows are being windowed too. A dropped command is rare and is a\n" \
    "    FAULT: the total is what matters, and a per-window count loses it."
print("   held-back windowed, overflows cumulative           OK")

# The bound must measure ABOVE the leaked floor. A stall leaks buffers and the
# reinit that ends it does not give them back, so in_flight keeps a permanent
# floor; counting it as live traffic makes the cap true on nearly every send and
# every frame then waits out the 400 ms escape. Measured on 2026-09-10: ACK p90
# went 78 ms -> 469 ms after one stall, and held-back tripled and stayed there.
sat_fn2 = block("static inline bool espnow_tx_saturated(")
assert "_espnow_leak_floor" in sat_fn2, \
    "the bound counts leaked buffers as outstanding sends, so after one stall it\n" \
    "    holds nearly every frame for the full escape — the bound then costs more\n" \
    "    than the fault it guards"
assert re.search(r"live\s*=\s*\(inf > gone\)", sat_fn2), \
    "the subtraction is gone; the floor must be taken OFF in_flight, not compared"
lp = block("static void espnow_leak_poll(")
assert "win_min > _espnow_leak_floor" in lp, "the floor can fall, so a quiet moment erases it"
assert re.search(r"static uint32_t\s+_espnow_leak_floor", HUB), \
    "the floor is narrower than in_flight — the mount's uint8_t latch, again"
for act in ("esp_restart", "hub_espnow_full_reinit", "_cb_stall_since_ms"):
    assert act not in lp, f"the leak poll calls {act}; it is an instrument, not a remedy"
print("   bound measures above the leaked floor               OK")

# An operator action must never queue behind housekeeping. The ring is FIFO and
# is almost entirely keepalive — 50 PINGs and a dozen GET_CONFIGs reach each
# satellite every 10 s, and the mount has no CMD_PING handler at all. A JOG
# waiting on that is the one latency the rig can feel.
print("\n4c. operator commands jump the queue:")
enq2 = block("static void espnow_send_if_present(")
assert "cmd_is_client_activity(raw[6])" in enq2, \
    "frames are not classified, so a jog queues behind every keepalive ahead of it"
assert enq2.index("cmd_is_client_activity") < enq2.index("espnow_tx_pump"), \
    "the classification happens after the queue is consulted, so an urgent frame\n" \
    "    has already been put behind the housekeeping"
urg = enq2[enq2.index("bool urgent"):enq2.index("// The common case")]
assert "espnow_send_now(idx, raw, len)" in urg, "an urgent frame is not sent directly"
assert "espnow_tx_saturated" not in urg, \
    "the urgent path still consults the cap, so a jog can still be held for the\n" \
    "    full 400 ms escape behind traffic nothing reads"
# On refusal it goes to the FRONT, which means moving tail backwards. Moving
# head instead would append it — the exact bug this path exists to avoid.
assert "_entx_tail   = prev;" in urg, \
    "a refused urgent frame is not placed at the front of the queue"
assert "_entx_tail + ESPNOW_TX_QUEUE_DEPTH - 1" in urg, \
    "the front-insert does not step the tail BACK, so the frame is appended"
print("   classified first, sent direct, front on refusal     OK")

# ---- 5. the PC app makes the same distinction ------------------------------
# The hub learned in its own firmware that a satellite-relayed mount is not
# evidence about THIS radio. The PC app's copy of that judgement never got the
# exclusion, so on 2026-09-09 it read cam4's ACK — cam4 being via Foyer, over
# Ethernet — as proof the hub was fine, at the exact moment the hub was
# reinitialising its stalled radio.
print("\n5. the PC app's verdict:")
import os, sys, threading
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(REPO / "pc_app"))
from comms.bridge import Bridge

assert Bridge._wedge_who(0) != Bridge._wedge_who(1), \
    "mount 0 and mount 1 render identically, so a hub-wide stall still reads\n" \
    "    as a mount fault in the log"
assert "hub-wide" in Bridge._wedge_who(0), "mount 0 is not named as hub-wide"
assert "mount 4" == Bridge._wedge_who(4), f"a real mount id changed: {Bridge._wedge_who(4)}"
print("   mount 0 renders as hub-wide, others unchanged      OK")


def proven(route, acked, exclude=1):
    b = Bridge.__new__(Bridge)
    b._diag_lock = threading.Lock()
    b._mount_route = route
    b._mount_last_ack = {m: 1000.0 for m in acked}
    return Bridge._hub_tx_proven_ok(b, 1000.0, exclude)


# The 2026-09-09 case exactly: cam1 silent, cam4 and cam5 both via Foyer.
assert proven([0, 0, 0, 2, 2], acked=[4]) == 0, \
    "a satellite-relayed mount's ACK is still read as proof the hub's radio\n" \
    "    works. Its commands never touch that radio."
# A direct mount still proves it, which is the whole point of the check.
assert proven([0, 0, 0, 2, 2], acked=[2, 4]) == 2, \
    "a mount on the hub's own radio no longer counts as proof"
# Unknown route (older hub, or the first seconds) must behave as before.
assert proven([0, 0, 0, 0, 0], acked=[4]) == 4, \
    "an all-direct route table suppresses the verdict, so a PC app that has not\n" \
    "    yet heard MOUNT_ROUTE would never blame a mount again"
# Every other mount on a satellite: nothing here can separate hub from mount.
assert proven([0, 2, 2, 2, 2], acked=[2, 3, 4, 5]) == 0, \
    "with every other mount relayed, the hub's radio is unproven — saying\n" \
    "    MOUNT-SIDE there is the error that sent someone to power-cycle a\n" \
    "    healthy mount while the hub was reinitialising itself"
print("   satellite ACKs excluded, direct ACKs still count   OK")

print("\nALL CHECKS PASSED")
