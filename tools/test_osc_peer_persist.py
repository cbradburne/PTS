"""OSC feedback must resume after a hub restart without being asked.

Reported on 2026-08-27: a mount goes inactive and its locations stay lit on the
Stream Deck. Pressing any button clears them — which makes it look as though
the press did it.

The slot values were never the problem. An inactive mount already reports every
slot as EMPTY (osc_feedback_mount zeroes occ/at/target when a mount has not
been heard inside MOUNT_PRESENCE_TIMEOUT_MS). What was wrong is that no
feedback was being SENT at all:

    if (!_osc_peer_port) return;    // nobody has talked to us yet

The hub learns where to send feedback from the source address of an incoming
OSC message, so until a surface says something, it has nowhere to send. That is
fine while the hub stays up. Restart it and the desk keeps showing whatever it
last received, with nothing arriving to correct it — and the first button press
both teaches the hub the address and triggers the full send, which is what
makes the press look causal.

So the peer is remembered across a restart, and a restored one queues the same
full send a newly-met one does.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent

HUB = (REPO / "firmware/esp32_hub_eth/esp32_hub_eth.ino").read_text()
PROTO = (REPO / "firmware/shared/protocol.h").read_text()


def code_only(text):
    return "\n".join(l if l.find("//") < 0 else l[:l.find("//")]
                     for l in text.splitlines())


# ---- 1. an inactive mount still reports EMPTY slots ------------------------
# This is what was asked for and it already held; the test pins it so the fix
# below cannot be mistaken for having changed it.
print("1. what an inactive mount reports:")
fb = code_only(HUB[HUB.index("static void osc_feedback_mount("):])
fb = fb[:fb.index("\n}\n")]
assert "if (!act) { occ = 0; at = 0; tgt = 0; }" in fb, \
    "an inactive mount's slots are no longer cleared, so a powered-off mount\n" \
    "    lights stored-location buttons on the desk"
assert fb.index("if (!act) { occ = 0;") < fb.index("for (int sN = 0"), \
    "the clearing happens after the slot states are computed, so it has no\n" \
    "    effect on what is sent"
assert "OSC_SLOT_EMPTY" in fb, "the empty state is gone"
print("   occ/at/target zeroed before the slots are built    OK")

# The presence window is what decides "inactive", and it must be short enough
# to matter inside a cue.
m = re.search(r"#define MOUNT_STATUS_REFRESH_MS\s+(\d+)UL", PROTO)
refresh = int(m.group(1))
timeout = 3 * refresh + 1000
assert timeout <= 20000, \
    f"a mount takes {timeout/1000:.0f}s to read as inactive; longer than a cue"
print(f"   inactive after {timeout/1000:.0f}s of silence                    OK")

# ---- 2. but none of it is sent without a peer ------------------------------
print("\n2. why it was not reaching the desk:")
assert "if (!_osc_peer_port) return;" in HUB, \
    "feedback no longer short-circuits without a peer — re-read this test, its\n" \
    "    whole premise is that it does"
assert "_osc_peer_port = from_port;" in HUB, \
    "the peer is no longer learned from an incoming message"
print("   nothing is sent until a surface has talked to us    OK")

# ---- 3. so the peer survives a restart -------------------------------------
print("\n3. what happens over a reboot:")
assert "static void osc_peer_save()" in HUB and "static void osc_peer_load()" in HUB, \
    "the peer is not persisted, so a hub restart leaves the desk showing the\n" \
    "    last thing it received until somebody presses something"

save = code_only(HUB[HUB.index("static void osc_peer_save()"):])
save = save[:save.index("\n}")]
assert '_prefs.begin("osc", false)' in save and "_prefs.end()" in save, \
    "the peer is not written to NVS"
assert "putUInt" in save and "putUShort" in save, "address or port is not saved"

load = code_only(HUB[HUB.index("static void osc_peer_load()"):])
load = load[:load.index("\n}")]
assert "if (!ip || !port) return;" in load, \
    "a hub that has never had a peer would restore 0.0.0.0:0 and treat it as one"
print("   address and port saved and restored                 OK")

# It has to be saved only when it CHANGES, or it is an NVS write per packet.
learn = HUB[HUB.index("bool new_peer = ("):]
learn = learn[:learn.index("osc_handle_packet")]
assert learn.index("if (new_peer) {") < learn.index("osc_peer_save();"), \
    "the peer is saved on every incoming message rather than on a change —\n" \
    "    that is an NVS write per OSC packet and will wear the flash out"
print("   written only when the peer actually changes         OK")

# ---- 4. and a restored peer gets everything, not just changes --------------
# Restoring the address is only half of it: the desk's contents are unknown
# after a restart, so it needs the full picture rather than a diff against a
# cache that was never populated.
print("\n4. what a restored peer receives:")
setup = HUB[HUB.index("    mount_table_load();"):]
setup = setup[:setup.index("\n}", setup.index("osc_peer_load();"))]
assert "osc_peer_load();" in setup, "the peer is not restored during setup"
assert re.search(r"static bool\s+_fb_valid\[NUM_MOUNTS\]\s*=\s*\{\}", HUB), \
    "the feedback cache no longer starts invalid, so a restored peer would get\n" \
    "    only changes and never the state it missed"
assert "bool all = force || !_fb_valid[i];" in HUB, \
    "an invalid cache no longer forces a full send"
print("   cache starts invalid, so the first pass sends all   OK")

# The restore must happen before the loop starts, or the first feedback pass
# still has nowhere to send and the whole thing waits for a press again.
assert HUB.index("osc_peer_load();") < HUB.index("void loop()"), \
    "the peer is restored after setup(), so the first feedback passes still\n" \
    "    have nowhere to send"
print("   restored in setup, before the first send            OK")

print("\nALL CHECKS PASSED")
