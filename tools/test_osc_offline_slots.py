"""An offline mount reports no stored locations over OSC.

Power a mount off and its location buttons on the desk stayed lit. The hub's
OSC feedback reads _mount_slot_occ / _mount_slot_at, which are the last values
that mount sent, and those survive it going dark. One slot could even keep
showing "moving to", for a move that ended when the power did.

The operator reads a lit location button as "this is there to recall". On an
offline mount that is exactly wrong — pressing it does nothing.

This is the same shape as two other faults this week: state latched from the
last report, with nothing invalidating it when the source went away. The PC app
kept a mount greyed while it was live, and every client kept a look-at arrow
green after the slider left the end. The fix is the same each time — derive the
display from a fact that is still true, rather than from the last thing heard.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent

HUB = (REPO / "firmware/esp32_hub_eth/esp32_hub_eth.ino").read_text()

fb = HUB[HUB.index("static void osc_feedback_mount("):]
fb = fb[:fb.index("\n}\n")]

# ---- 1. offline clears what the slot addresses are built from ---------------
print("1. an offline mount:")
assert "if (!act) { occ = 0; at = 0; tgt = 0; }" in fb, \
    "an offline mount still reports its last slot masks"
print("   occ / at / target all cleared                      OK")

# It has to happen BEFORE the slot loop reads them, or it changes nothing.
clear_at = fb.index("if (!act) { occ = 0; at = 0; tgt = 0; }")
loop_at  = fb.index("for (int sN = 0; sN < NUM_POSITIONS; sN++)")
assert clear_at < loop_at, "the clear runs after the slot loop has already read occ/at"
print("   cleared before the slot states are computed        OK")

# target matters as much as the masks: it is what produces MOVING, so a stale
# one would keep a single button showing a move that ended with the power.
body = fb[loop_at:]
assert "(tgt == sN + 1)  ? OSC_SLOT_MOVING" in body, \
    "the slot state no longer derives MOVING from target"
print("   so no slot can still read MOVING                   OK")

# With all three zero every slot must fall through to EMPTY.
assert "OSC_SLOT_EMPTY" in body, "there is no empty state to fall through to"
order = [body.index(x) for x in ("OSC_SLOT_AT", "OSC_SLOT_MOVING",
                                 "OSC_SLOT_OCCUPIED", "OSC_SLOT_EMPTY")]
assert order == sorted(order), "the slot-state precedence changed; EMPTY must be last"
print("   every slot falls through to EMPTY                  OK")

# ---- 2. "offline" is the hub's own liveness, not a new idea -----------------
print("\n2. what counts as offline:")
m = re.search(r"uint8_t\s+act\s*=\s*\(_mount_last_seen\[i\] &&\s*\n\s*"
              r"\(millis\(\) - _mount_last_seen\[i\] < SELF_WEDGE_ALIVE_MS\)\) \? 1 : 0;", fb)
assert m, "the liveness test changed shape"
assert "#define SELF_WEDGE_ALIVE_MS     MOUNT_PRESENCE_TIMEOUT_MS" in HUB, \
    "the hub's liveness window is no longer the shared presence timeout"
print("   the same last-seen window the rest of the hub uses OK")

# /active still carries the distinction, so a surface can tell "off" from
# "on but empty" if it wants to.
assert '"/pts/cam/%d/active"' in fb, "the active address is gone"
assert "act != _fb_active[i]" in fb, "active is no longer change-tracked"
print("   /active still says 0 alongside the empty slots     OK")

# ---- 3. it actually gets sent -----------------------------------------------
# Change-detected sends only happen when the function RUNS. If feedback were
# driven by incoming STATUS, a mount that had gone dark would never trigger it
# and this whole fix would be inert.
print("\n3. and it reaches the desk:")
poll = HUB[HUB.index("if (now - _fb_last_full_ms >= OSC_FB_FULL_MS)"):]
poll = poll[:poll.index("\n}")]
assert re.search(r"for \(int i = 0; i < NUM_MOUNTS; i\+\+\)\s*\n\s*"
                 r"if \(_fb_valid\[i\]\) osc_feedback_mount\(i, false\);", poll), \
    "feedback is no longer swept for every mount each pass"
print("   swept every pass, not driven by incoming STATUS    OK")

# And the caching must let it change back when the mount returns.
assert "_fb_slot[i][sN] = ss;" in fb, \
    "the per-slot cache is gone; the change test cannot work"
print("   cached per slot, so it repaints on return          OK")

print("\nALL CHECKS PASSED")
