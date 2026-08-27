"""An inactive mount reports nothing a desk can act on.

Reported on 2026-08-27: pressing the speed increment on the Stream Deck changed
the reported pan/tilt and slider presets for a mount that was not even there.

The presets the hub reports are its last-known values, and they survive the
mount being powered off — so a dark mount went on displaying a live-looking
speed. Worse, the increment was accepted: the hub walked its own shadow copy of
a preset the mount could not be told about, so the desk would have shown that
walked number the moment the mount came back, until its next status put it
right.

The rule this file pins: a mount that is not being heard reports 0 for both
presets, and refuses to have them changed. 0 already meant "no rail" on the
slider address, so it already meant "there is nothing to set the speed of" —
this widens it to include "there is nothing there at all", which a surface
reads the same way: do not offer it.

The same shape as the slot addresses, which read empty for an inactive mount
(see test_osc_peer_persist.py) and for the same reason: a speed, like a stored
location, is a property of something present.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent

HUB = (REPO / "firmware/esp32_hub_eth/esp32_hub_eth.ino").read_text()


def code_only(text):
    return "\n".join(l if l.find("//") < 0 else l[:l.find("//")]
                     for l in text.splitlines())


# ---- 1. one definition of "is this mount being heard" ----------------------
# It was written out six times. Everything an offline mount must not do depends
# on all of them agreeing, and three copies of a predicate is how they stop.
print("1. how activity is decided:")
assert "static inline bool mount_is_active(int i, uint32_t now)" in HUB, \
    "the activity test is no longer a single function"
helper = code_only(HUB[HUB.index("static inline bool mount_is_active("):])
helper = helper[:helper.index("\n}")]
assert "_mount_last_seen[i] &&" in helper, \
    "the zero check is gone — a mount never seen has _mount_last_seen 0, and\n" \
    "    now - 0 is a large number, so it would read as ACTIVE"
assert "SELF_WEDGE_ALIVE_MS" in helper, "the presence window is not used"

body = code_only(HUB)
stragglers = len(re.findall(r"_mount_last_seen\[\w+\]\s*&&", body))
assert stragglers == 1, \
    f"{stragglers} places still spell the activity test out by hand; it should\n" \
    "    exist once, inside mount_is_active()"
print("   one mount_is_active(), no hand-written copies left  OK")

# ---- 2. an inactive mount reports 0 for both presets -----------------------
print("\n2. what an inactive mount reports:")
fb = code_only(HUB[HUB.index("static void osc_feedback_mount("):])
fb = fb[:fb.index("\n}\n")]
assert "if (!act) { pt = 0; sl = 0; }" in fb, \
    "an inactive mount still reports its last-known presets, which look live"

# It has to happen before the values are compared and sent, or it changes
# nothing about what goes out.
for addr, var in (("speed/pt", "pt"), ("speed/sl", "sl")):
    send = fb.index(addr)
    assert fb.index("if (!act) { pt = 0; sl = 0; }") < send, \
        f"the {addr} value is taken before the inactive check, so the check has\n" \
        "    no effect on what is sent"
print("   both zeroed before either is sent                   OK")

# The rail-less rule must survive: it is the same value for a related reason.
assert "if (!mount_has_slider(i)) sl = 0;" in fb, \
    "a rail-less mount no longer reports 0 for slider speed"
print("   a rail-less mount still reports 0 for the slider    OK")

# And the slots stay empty for the same mount, which is the neighbouring rule.
assert "if (!act) { occ = 0; at = 0; tgt = 0; }" in fb, \
    "the slot clearing has been lost while changing the presets around it"
print("   its slots still read empty                          OK")

# ---- 3. and refuses to have them changed -----------------------------------
print("\n3. what a speed button does to it:")
osc = code_only(HUB[HUB.index('if      (strcmp(tok[4], "pt") == 0)') - 2000:])
osc = osc[:osc.index("ui_send_to_mount((uint8_t)mid, CMD_SET_ACTIVE_PRESET")]
assert "if (!mount_is_active(idx, millis())) return;" in osc, \
    "a speed change is still accepted for a mount that is not there — the hub\n" \
    "    walks its own copy of a preset the mount cannot be told about"
assert osc.index("if (!mount_is_active(idx, millis())) return;") < osc.index("cur = &_mount_pt_preset[idx]"), \
    "the refusal happens after the preset pointer is taken, so the value can\n" \
    "    still be written before anything checks"
print("   ignored before any value is touched                 OK")

# The rail-less refusal is the precedent and must still be there.
assert "if (!mount_has_slider(idx)) return;" in osc, \
    "the rail-less refusal is gone; a slider button on a rail-less mount would\n" \
    "    walk a number that controls nothing"
print("   rail-less refusal still in place                    OK")

print("\nALL CHECKS PASSED")
