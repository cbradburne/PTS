"""LOOK_AT_STATUS is sent on change, never on a timer.

It used to go out every STATUS_INTERVAL_MS for the whole of a look-at move: a
14-byte packet at 10 Hz, per moving mount, of which every consumer in the system
read exactly one byte — payload[12], the subject id.

  The hub forwards only that byte to the display board.
  The web app reads only that byte, and acts only when it changes.
  The PC app reads only la.subject_id.

Nothing read slider_mm, pan_deg, tilt_deg or flags. The run advancement that
once did was moved onto the mount, and the panel that displayed them as live
telemetry was deleted as unreachable.

And that one byte is already carried by the ordinary STATUS packet as
active_la_subject, which goes out unconditionally at the same interval whether
a move is running or not. So this was a second 10 Hz stream duplicating the
first — and the hub set ws_force on each one, pushing to every connected
browser ten times a second for a value that had not changed.

It was not the liveness signal, which is the question worth asking before
deleting anything periodic. STATUS is ungated, and every presence check
upstream refreshes on ANY packet, so removing this left "I'm alive" exactly
where it was.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent

INO  = (REPO / "firmware/teensy41_mount/teensy41_mount.ino").read_text()
HUB  = (REPO / "firmware/esp32_hub_eth/esp32_hub_eth.ino").read_text()
WEB  = (REPO / "firmware/esp32_hub/web_app.h").read_text()
MW   = (REPO / "pc_app/ui/main_window.py").read_text()

# ---- 1. no timer drives it --------------------------------------------------
print("1. how LOOK_AT_STATUS is sent:")
assert not re.search(r"la_active && millis\(\) - _last_la_ms", INO), \
    "the periodic look-at broadcast is back"
assert "_last_la_ms" not in INO, "the periodic send's timer variable is still there"
print("   no periodic send during a move                     OK")

# ---- 2. but every change still reports ------------------------------------
print("\n2. what still sends it:")
n = INO.count("send_look_at_status();")
assert n >= 5, f"only {n} send sites left; the change-driven reports have gone too"
print(f"   {n} change-driven sends remain                      OK")

# The two that matter most: a manual pan/tilt deselects, and that must reach
# every client at once or the UI keeps claiming a subject is tracked.
desel = INO.count("mount.clearLaSubject();\n                send_look_at_status();")
assert desel == 2, \
    f"expected both manual-deselect paths to report, found {desel}"
print("   both manual-deselect paths report immediately      OK")

# And the end of a move still reports, including the end-flag persistence.
end = INO[INO.index("// Send once when the look-at sequence ends entirely"):]
end = end[:end.index("_prev_state = cur_state;")]
assert "send_look_at_status();" in end, "the end-of-move update has gone"
assert "setLookAtEndFlag" in end, "the arrived-end flag is no longer persisted"
print("   end of move still reports, and persists the end    OK")

# ---- 3. the one consumed byte is still available continuously --------------
# This is what makes the removal safe: STATUS carries it anyway.
print("\n3. the subject id is still on the wire at 10 Hz:")
assert re.search(r"if \(millis\(\) - _last_status_ms >= STATUS_INTERVAL_MS\) \{", INO), \
    "the unconditional STATUS broadcast changed shape"
seg = INO[INO.index("// Periodic status broadcast"):][:400]
assert "la_active" not in seg and "STATE_LOOK_AT" not in seg, \
    "STATUS is now gated on state — it must stay unconditional"
print("   STATUS is ungated and still 10 Hz                  OK")

# ---- 4. nobody was reading the fields we stopped sending -------------------
print("\n4. what the consumers actually read:")
assert "disp_look_at_status(msg.src_idx + 1, pkt.payload[12]);" in HUB, \
    "the hub forwards more than the subject id now — re-check this removal"
print("   hub  → display: payload[12] only                   OK")
assert "const subjId = buf[off + 7 + 12];" in WEB, \
    "the web app reads more than the subject id now"
print("   web app: byte 12 only                              OK")
la = MW[MW.index("def _on_look_at_status_from_mount"):]
la = la[:la.index("\n    def ", 1)]
assert "la.subject_id" in la, "the PC app no longer reads the subject id"
for dead in ("pan_deg", "tilt_deg", "slider_pos_mm", "look_at_active"):
    assert f"la.{dead}" not in la, \
        f"the PC app now reads la.{dead} — it is no longer sent 10 times a second"
print("   PC app: subject_id only                            OK")

# ---- 5. liveness is unaffected ---------------------------------------------
print("\n5. the 'I'm alive' path:")
seg = HUB[HUB.index("_mount_last_seen[msg.src_idx] = millis();") - 600:]
seg = seg[:700]
assert "CMD_LOOK_AT_STATUS" not in seg and "CMD_STATUS" not in seg, \
    "the hub's last-seen refresh is now tied to a specific command"
print("   hub refreshes last-seen on ANY packet              OK")
mm = (REPO / "pc_app/comms/mount_manager.py").read_text()
assert "ANY packet from a mount proves it is alive" in mm, \
    "the PC app's any-packet liveness comment/behaviour has changed"
print("   PC app treats any packet as proof of life          OK")

print("\nALL CHECKS PASSED")
