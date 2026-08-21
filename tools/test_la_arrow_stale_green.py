"""A green look-at arrow means "parked at that end" — and stops meaning it.

The slot 9/10 arrows go green when a commanded look-at move finishes, asserting
that the slider is parked at that end of the rail. That assertion is latched:
target_slot only reports a move IN PROGRESS, so once the move ends there is
nothing left in STATUS saying where the slider came to rest, and the app holds
the green until something clears it.

The latch is right for a commanded move and wrong the moment the operator
slides away from that end by hand — which, since manual sliding began keeping
the subject tracked, is an ordinary thing to do. The arrow kept claiming the
slider was at the end while it sat in the middle of the rail.

AT_MIN_LIMIT / AT_MAX_LIMIT cannot answer this. They are set inside the jog
loop by whichever axis hits a limit and cleared by whichever axis is processed
last, so they say nothing reliable about the slider specifically — which is why
an earlier version of the arrow logic dropped them as a heuristic.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent

MM = (REPO / "pc_app/comms/mount_manager.py").read_text()
MW = (REPO / "pc_app/ui/main_window.py").read_text()
GRID = (REPO / "pc_app/ui/widgets/position_grid.py").read_text()

# ---- 1. the signal comes from the one place jogs are sent -------------------
print("1. where a manual slide is noticed:")
assert "slider_jogged" in MM, "there is no signal for a manual slider jog"
jog = MM[MM.index("def send_jog("):]
jog = jog[:jog.index("\n    def ", 1)]
assert "if slider != 0:" in jog and "self.slider_jogged.emit(mount_id)" in jog, \
    "send_jog does not report a real slider deflection"
print("   send_jog — every jog this app sends goes through it OK")

# A zero slider must NOT fire it: the jog stream ends with a zero packet, and
# the CV path sends slider=0 continuously while it owns pan/tilt.
assert re.search(r"if slider != 0:\s*\n\s*self\.slider_jogged\.emit", jog), \
    "the guard is not on a non-zero slider — the trailing zero packet would fire it"
print("   a zero slider does not fire it                     OK")

# ---- 2. it clears only a latched green ------------------------------------
print("\n2. what it clears:")
handler = MW[MW.index("def _on_slider_jogged("):]
handler = handler[:handler.index("\n    def ", 1)]
assert "('left_done', 'right_done')" in handler, \
    "the handler clears more than the latched green states"
assert "self._set_la_arrow(mount_id, None)" in handler, "the arrow is not cleared"
print("   only 'left_done' / 'right_done' -> grey            OK")

# It must not stomp a move in progress: those are the flashing states, and a
# commanded look-at move re-asserts them from STATUS every packet.
assert "left_moving" not in handler and "right_moving" not in handler, \
    "a slide would clear the flashing state of a move in progress"
print("   a move in progress is left alone                   OK")

assert "self._mm.slider_jogged.connect(self._on_slider_jogged)" in MW, \
    "the signal is not connected"
print("   wired up                                           OK")

# ---- 3. a COMMANDED look-at move must not trip it --------------------------
# Pressing arrow 9/10 starts a look-at move; it does not jog the slider. If it
# ever did, the arrow would grey itself the instant the move began.
print("\n3. the commanded move still goes green:")
start = MW[MW.index("def _on_slider_jog_start("):]
start = start[:start.index("\n    def ", 1)]
assert "send_start_look_at_move" in start, "the arrow no longer starts a look-at move"
assert "send_jog" not in start, \
    "the arrow button sends a jog — it would clear its own green immediately"
print("   arrow buttons send START_LOOK_AT_MOVE, not a jog   OK")

# And the STATUS path that sets green in the first place is untouched.
assert "self._set_la_arrow(mount_id, 'left_done')" in MW and \
       "self._set_la_arrow(mount_id, 'right_done')" in MW, \
    "the arrival-to-green transition has gone"
print("   arrival still latches green                        OK")

# ---- 4. grey is what the grid actually draws -------------------------------
print("\n4. the border itself:")
border = GRID[GRID.index("if self._look_at_mode[mount_id] and slot >= 8:"):]
border = border[:border.index("# Subject buttons")]
assert "'left_done'" in border and "'right_done'" in border, \
    "the arrow border no longer distinguishes the done states"
assert border.count('"#37474F"') == 2, \
    "the neutral grey fallback for either arrow has changed"
print("   None falls through to the neutral grey             OK")

print("\nALL CHECKS PASSED")
