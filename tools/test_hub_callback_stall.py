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

snd = block("static void espnow_send_if_present(")
assert re.search(r"_espnow_issued\s*=\s*_espnow_issued \+ 1;", snd), \
    "sends are not counted"
assert snd.index("e == ESP_OK") < snd.index("_espnow_issued"), \
    "a REJECTED send is counted as issued — it never took a buffer, so it would\n" \
    "    read as an outstanding send that can never complete"
print("   sends counted on ESP_OK only                        OK")

# ---- 2. the rung that the outage needed ------------------------------------
print("\n2. armed by the stall, not by failures:")
assert re.search(r"#define SELF_CB_STALL_MS\s+(\d+)", HUB), "no stall threshold"
stall_ms = int(re.search(r"#define SELF_CB_STALL_MS\s+(\d+)", HUB).group(1))
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
assert "_espnow_cb_last_ms &&" in chk, \
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
