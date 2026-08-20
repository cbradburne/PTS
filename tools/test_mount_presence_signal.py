"""A connected mount must never stay greyed on the PC app.

Opening the app on a Windows PC three times showed a different mount greyed out
each time, and all of them were live. The rows even carried real stored
positions and a real speed, because the offline overlay is only ~59% opaque and
STATUS is relayed by the hub whether or not the app thinks the mount is online.

Presence is two flags — `connected` (heard at all) and `unresponsive` (heard but
not acting on commands) — and the operator is shown online only when the mount
is both heard AND acting. They were written at six separate sites, each deciding
for itself whether to emit a signal. One of them did this:

    if not st.connected:
        st.connected = True              # state says online
        if not st.unresponsive:
            self.mount_connected.emit(mid)   # ...but the UI is only told
                                             #    when unresponsive is clear

After that line runs with `unresponsive` set, the mount is online internally and
greyed on screen, and NEITHER guard can fire again to correct it — `st.connected`
is now True, so `if not st.connected` is dead, and the STATUS path's
`if not was_connected` is dead for the same reason. The row stays grey for the
rest of the session.

Getting into that state needed only a heartbeat timeout on a mount that had been
marked unresponsive: the timeout cleared `connected` and left `unresponsive` set,
so the mount's very next packet re-entered through the branch above.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent

MM = (REPO / "pc_app/comms/mount_manager.py").read_text()
GRID = (REPO / "pc_app/ui/widgets/position_grid.py").read_text()

# ---- 1. presence changes in exactly one place -------------------------------
print("1. where presence is allowed to change:")
assert "def _set_mount_online" in MM, "the single presence helper is gone"
writes = re.findall(r"st\.(connected|unresponsive)\s*=\s*", MM)
assert len(writes) == 2, \
    f"presence is written {len(writes)} times; it must only be inside _set_mount_online"
helper = MM[MM.index("def _set_mount_online"):]
helper = helper[:helper.index("\n    def ", 1)]
for w in ("st.connected = connected", "st.unresponsive = unresponsive"):
    assert w in helper, f"'{w}' moved out of the helper"
print("   both flags written only inside the helper          OK")

# No site may emit presence signals directly any more — that is how six
# different opinions about the same state arose.
for sig in ("self.mount_connected.emit", "self.mount_disconnected.emit"):
    assert sig not in MM, f"{sig} is called directly again; only the helper may emit"
assert "(self.mount_connected if after else self.mount_disconnected).emit" in helper, \
    "the helper no longer chooses the signal from the effective state"
print("   both signals emitted only from the helper          OK")

# ---- 2. it emits on the EFFECTIVE state, not on either flag alone -----------
assert "before = st.connected and not st.unresponsive" in helper, \
    "the helper no longer computes the effective state before the change"
assert "after = st.connected and not st.unresponsive" in helper, \
    "the helper no longer computes the effective state after the change"
assert "if after == before:" in helper, "the helper emits even when nothing changed"
print("   emits only when heard-AND-acting actually flips    OK")

# ---- 3. a timeout must not leave unresponsive set ---------------------------
print("\n2. the way in to the stuck state is closed:")
seg = MM[MM.index("(now_ms - st.last_pong_ms) > HEARTBEAT_TIMEOUT_MS"):][:900]
assert "connected=False, unresponsive=False" in seg, \
    "the heartbeat timeout still leaves unresponsive set on a silent mount"
print("   heartbeat timeout clears both flags                OK")

# ---- 4. the state machine, run both ways ------------------------------------
print("\n3. the sequence that stranded a live mount:")


class Model:
    """The presence flags and the UI's view of them."""

    def __init__(self, fixed: bool):
        self.connected = False
        self.unresponsive = False
        self.fixed = fixed
        self.ui_online = False          # what the operator sees

    # -- the fixed implementation --
    def _set(self, connected=None, unresponsive=None):
        before = self.connected and not self.unresponsive
        if connected is not None:
            self.connected = connected
        if unresponsive is not None:
            self.unresponsive = unresponsive
        after = self.connected and not self.unresponsive
        if after != before:
            self.ui_online = after

    def packet_heard(self):
        if self.fixed:
            if not self.connected:
                self._set(connected=True)
        else:
            if not self.connected:
                self.connected = True
                if not self.unresponsive:
                    self.ui_online = True

    def commands_unacked(self):
        if self.fixed:
            self._set(unresponsive=True)
        else:
            if not self.unresponsive:
                self.unresponsive = True
                self.ui_online = False

    def heartbeat_timeout(self):
        if self.fixed:
            self._set(connected=False, unresponsive=False)
        else:
            if self.connected:
                self.connected = False
                self.ui_online = False


for fixed, label in ((False, "before"), (True, "after ")):
    m = Model(fixed)
    m.packet_heard()          # mount comes up, row goes live
    m.commands_unacked()      # a burst of commands outruns the ACKs
    m.heartbeat_timeout()     # link hiccup — mount goes quiet briefly
    m.packet_heard()          # ...and is heard again immediately
    print(f"   {label}: mount heard, flags connected={m.connected} "
          f"unresponsive={m.unresponsive} -> operator sees "
          f"{'ONLINE' if m.ui_online else 'GREYED'}")

before = Model(False)
after = Model(True)
for m in (before, after):
    m.packet_heard(); m.commands_unacked(); m.heartbeat_timeout(); m.packet_heard()
assert before.connected and not before.ui_online, \
    "the model no longer reproduces the stranded mount"
assert after.ui_online, "the fix does not bring the mount back online"
print("   a live mount is no longer stranded greyed          OK")

# ---- 5. and an offline row shows nothing -------------------------------------
# The overlay is translucent, so anything drawn underneath stays readable.
# Half-lit is the one state that cannot be read at a glance.
print("\n4. what an offline row draws:")
assert "if not self._connected.get(mount_id, False):" in GRID, \
    "borders are drawn regardless of connection again"
assert "self._connected:     dict[int, bool] = {mid: False for mid in range(1, 6)}" in GRID, \
    "mounts no longer start offline — startup would differ from a disconnect"
assert "self._pt_preset[mount_id] = preset" in GRID, \
    "the real preset is no longer held separately from the dial"
assert "return self._pt_preset[mount_id]" in GRID, \
    "get_pt_preset reads the blanked dial — a blank display would read as speed 0"
print("   no borders, no speed, and the real preset kept     OK")

print("\nALL CHECKS PASSED")
