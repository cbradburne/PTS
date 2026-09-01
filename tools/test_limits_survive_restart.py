"""Reopening the PC app does not un-know the rail's limits.

Find the slider limits with the app open and the Move panel's run-to-the-end
buttons go live. Close the app, reopen it, and they were grey again until
find-limits was run a second time — purely so the app could overhear the answer.

The mount was never the problem. It keeps its limits in EEPROM and restores
them at boot, so it has known them since the last find. The trouble was that
nothing ever ASKED and got told: CMD_LIMITS_FOUND fires once, while the find is
running, and CMD_STATE_REPORT — which the app requests the moment a mount comes
online, and which has fields for exactly this — was the 6-byte short form with
the limits left out. The PC's handler had been reading `sr.slider_min_steps`
for as long as it had existed, and getting a zero the decoder had made up.

So the fix is on the mount: report what it already knows. The short report
grows to 22 bytes, and 0/0 keeps meaning "not found" — which is what the PC has
always tested for.

Nothing here needs Qt: the fault is on the wire, so that is where it is tested.

Run directly, or via tools/run_tests.sh with the rest.
"""
import sys, pathlib, re, struct

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

from comms.protocol import (decode_state_report, STATE_REPORT_PAYLOAD_LEN,
                            STATE_REPORT_LITE_LEN, STATE_REPORT_LIMITS_LEN,
                            ParseError)

INO = (REPO / "firmware/teensy41_mount/teensy41_mount.ino").read_text()
MM  = (REPO / "pc_app/comms/mount_manager.py").read_text()

# A calibrated 1.5 m rail: home is 0 and the far end is 240000 steps. Note the
# min IS zero — any "known?" test that keys off the minimum alone reads a
# perfectly good rail as uncalibrated.
SL_MIN, SL_MAX = 0, 240000
ZM_MIN, ZM_MAX = -18000, 0          # inverted zoom stores (negative, 0)


def firmware_report(sl=(SL_MIN, SL_MAX), zm=(ZM_MIN, ZM_MAX)):
    """The 22 bytes send_state_report() builds, laid out from protocol.h."""
    return struct.pack(">HHBBiiii", 0x03FF, 0x0001, 3, 2, sl[0], sl[1], zm[0], zm[1])


# ---- 1. the report carries them --------------------------------------------
print("1. a state report from a calibrated mount:")
assert len(firmware_report()) == STATE_REPORT_LIMITS_LEN, \
    f"the layout packs to {len(firmware_report())} bytes, not " \
    f"{STATE_REPORT_LIMITS_LEN} — protocol.h and the packer disagree"
sr = decode_state_report(firmware_report())
assert (sr.slider_min_steps, sr.slider_max_steps) == (SL_MIN, SL_MAX), \
    f"slider limits decoded as {sr.slider_min_steps}/{sr.slider_max_steps}"
assert (sr.zoom_min_steps, sr.zoom_max_steps) == (ZM_MIN, ZM_MAX), \
    f"zoom limits decoded as {sr.zoom_min_steps}/{sr.zoom_max_steps}"
assert sr.active_pt_preset == 3 and sr.active_sl_preset == 2, \
    "the presets moved — the limits were appended, not inserted"
assert sr.slot_occupied_mask == 0x03FF and sr.slot_at_mask == 0x0001, \
    "the masks moved"
print(f"   slider {sr.slider_min_steps}..{sr.slider_max_steps}, "
      f"zoom {sr.zoom_min_steps}..{sr.zoom_max_steps}       OK")
print("   masks and presets still where they were           OK")

# A transposition decodes cleanly and is wrong on the rig, so check the fields
# are distinguishable rather than just present.
odd = decode_state_report(firmware_report(sl=(11, 22), zm=(33, 44)))
assert (odd.slider_min_steps, odd.slider_max_steps,
        odd.zoom_min_steps, odd.zoom_max_steps) == (11, 22, 33, 44), \
    "the four limit fields are transposed"
print("   slider before zoom, min before max                OK")

# ---- 2. and an uncalibrated axis says so ------------------------------------
print("\n2. a mount that has never found its limits:")
none = decode_state_report(firmware_report(sl=(0, 0), zm=(0, 0)))
assert (none.slider_min_steps, none.slider_max_steps) == (0, 0), \
    "an unlimited axis does not report 0/0"

# The PC's guard, as written. 0/0 must leave the previous value alone rather
# than overwrite a good limit with a zero.
guard = re.search(r"if (sr\.slider_min_steps != 0 or sr\.slider_max_steps != 0):", MM)
assert guard, "the STATE_REPORT handler no longer guards against 0/0 limits"
expr = guard.group(1)
assert eval(expr, {}, {"sr": sr}), "a calibrated rail is being ignored as 0/0"
assert not eval(expr, {}, {"sr": none}), "0/0 is being stored as a real limit"
print("   0/0 ignored, a real rail applied                  OK")

# min == 0 is normal — home is the datum. A guard on the minimum alone would
# throw away every properly calibrated slider there is.
assert eval(expr, {}, {"sr": decode_state_report(firmware_report(sl=(0, 240000)))}), \
    "a rail whose home is 0 reads as uncalibrated — the guard is testing the\n" \
    "    minimum, and every calibrated slider has a minimum of zero"
print("   a rail with home at 0 still counts as known       OK")

# ---- 3. older firmware is not a parse error ---------------------------------
print("\n3. a mount on older firmware:")
lite = struct.pack(">HHBB", 0x03FF, 0x0001, 3, 2)
assert len(lite) == STATE_REPORT_LITE_LEN
old = decode_state_report(lite)
assert old.active_pt_preset == 3 and old.slot_occupied_mask == 0x03FF, \
    "the 6-byte report no longer decodes — an un-flashed mount would go dark"
assert (old.slider_min_steps, old.slider_max_steps) == (0, 0), \
    "a report with no limit fields is inventing limits"
assert not eval(expr, {}, {"sr": old}), \
    "an older mount's absent limits are stored as real ones"
print("   6 bytes still decodes, and claims no limits       OK")

full = decode_state_report(bytes(STATE_REPORT_PAYLOAD_LEN))
assert full.slots[0] is not None, "the 182-byte form no longer decodes its slots"
print("   the 182-byte form still decodes                   OK")

for short in (b"", bytes(5)):
    try:
        decode_state_report(short)
    except ParseError:
        pass
    else:
        raise AssertionError(f"a {len(short)}-byte payload decoded instead of raising")
print("   anything shorter than 6 is still rejected         OK")

# ---- 4. the mount fills those fields in ------------------------------------
print("\n4. what the firmware writes:")
snd = INO[INO.index("static void send_state_report()"):]
snd = snd[:snd.index("\n}")]
assert f"send_packet(CMD_STATE_REPORT, payload, STATE_REPORT_LIMITS_LEN)" in snd, \
    "the mount still sends the short report without limits"
writes = re.findall(r"write_be32\(p \+\s*(\d+),[^;]*?"
                    r"(get(?:Min|Max)Limit)\(AXIS_(SLIDER|ZOOM)\)", snd, re.S)
assert [(int(o), w, a) for o, w, a in writes] == [
    (6,  "getMinLimit", "SLIDER"),
    (10, "getMaxLimit", "SLIDER"),
    (14, "getMinLimit", "ZOOM"),
    (18, "getMaxLimit", "ZOOM"),
], f"the limits are written at the wrong offsets or in the wrong order: {writes}"
print("   slider at +6/+10, zoom at +14/+18                 OK")

# An axis with no limits must send zeros, not whatever _min_limit happens to
# hold — the PC reads 0/0 as "unknown" and anything else as gospel.
assert "hasLimits(AXIS_SLIDER)" in snd and "hasLimits(AXIS_ZOOM)" in snd, \
    "the report sends limits without asking whether they were ever found"
print("   and zeros for an axis that has none               OK")

# ---- 5. nothing has to be done to trigger it -------------------------------
# The whole point is that the operator does not run find-limits to tell the app
# something the mount already knew.
print("\n5. when the app finds out:")
assert MM.count("self._send(pkt_get_state(mid))") >= 2, \
    "state is no longer requested when a mount comes online, so the limits would\n" \
    "    only arrive if something else happened to ask"
for marker in ("if not st.connected:", "if not was_online:"):
    blk = MM[MM.index(marker):]
    blk = blk[:blk.index("\n\n")] if "\n\n" in blk[:800] else blk[:800]
    assert "pkt_get_state(mid)" in blk, \
        f"the '{marker.strip()}' path does not ask for state"
print("   requested the moment a mount comes online         OK")
print("   so reopening the app is enough                    OK")

# ---- 6. and it never goes out on a timer -----------------------------------
# This is the reason the limits belong in STATE_REPORT rather than in STATUS.
# STATUS streams at 10 Hz; STATE_REPORT is asked for, once, when a client
# connects or a mount comes online. On this rig that distinction is not
# academic — ESP-NOW wedges at a rate that depends on traffic, and cutting the
# satellite's load took one from 36 restarts in 17 hours to none.
#
# The mount-side guarantee is the one that matters: whatever a client does, the
# mount must never volunteer a state report.
print("\n6. what this costs on the radio:")
sends = re.findall(r"^\s*send_state_report\(\);", INO, re.M)
assert len(sends) == 1, \
    f"send_state_report() is called from {len(sends)} places. It must be answered " \
    "only\n    on request — a periodic one puts 22 bytes per mount on the radio " \
    "forever,\n    and ESP-NOW on this rig wedges at a rate that depends on traffic."
handler = INO[INO.index("case CMD_GET_STATE: {"):]
handler = handler[:handler.index("break;")]
assert "send_state_report();" in handler, \
    "the one call site is not the CMD_GET_STATE handler — the mount is sending " \
    "state\n    reports of its own accord"
grew = STATE_REPORT_LIMITS_LEN - STATE_REPORT_LITE_LEN
print(f"   +{grew} bytes, answered on request only            OK")
print("   never volunteered, so never periodic              OK")

print("\nALL CHECKS PASSED")
