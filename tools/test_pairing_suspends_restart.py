"""Camera pairing is not isolation, and must not restart the chip.

Five attempts to pair a camera to mount 5 all failed the same way: the AMOLED
rebooted part-way through typing the six-digit code. It read as the pairing
screen timing out. It was not — the screen's own timeout is two minutes and
restarts on every keypress. The board was rebooting underneath it.

Camera pairing STOPS WIFI so BLE can have the radio. So during pairing there is
no RX and no TX, by design. The isolation check sees silence on both and
restarts the ESP-NOW stack to recover it.

Worse, it takes the SHORT window. The two-minute window is for when the recovery
ladder is running — send failures or reinits still accruing, something being
attempted. During pairing nothing is attempted at all, because there is no WiFi
to attempt it on, so the counters do not move and ESPNOW_RESTART_STALLED_MS
applies: twenty seconds, against a person typing a code and confirming it on the
camera.

The SETUP screen already suspended this. Pairing did not, and pairing is the one
that takes the radio away.

The screen keeps its own bound, which is the right shape for "wandered off and
left it open": it EXITS after two idle minutes rather than restarting the chip,
and any keypress restarts that clock.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent

INO = (REPO / "firmware/esp_mount_amoled175/esp_mount_amoled175.ino").read_text()

# ---- 1. pairing suspends the isolation restart -----------------------------
print("1. the isolation restart:")
m = re.search(r"if \(_cfg_valid && !_setup_active && !_pair_active &&\s*\n\s*"
              r"!hub_ok && rx_stale && tx_stale\) \{", INO)
assert m, "the isolation restart is not suspended during camera pairing"
print("   suspended while the pairing screen is open         OK")

# SETUP must keep its suspension too — both take the mount off its normal duty.
assert "!_setup_active" in m.group(0), "SETUP lost its suspension"
print("   and still suspended on the SETUP screen             OK")

# ---- 2. why the short window is the one that bit --------------------------
print("\n2. which window applied:")
stalled = int(re.search(r"#define ESPNOW_RESTART_STALLED_MS\s+\((\d+)UL\*1000UL\)",
                        INO).group(1))
full = int(re.search(r"#define ESPNOW_RESTART_MS\s+\((\d+)UL\*60UL\*1000UL\)",
                     INO).group(1)) * 60
assert stalled < full, "the stalled window is no longer the shorter one"
print(f"   ladder running -> {full} s;  nothing attempted -> {stalled} s")

# The window is picked by whether anything is being ATTEMPTED. With WiFi off
# nothing can be, so pairing always lands on the short one.
assert "bool ladder_running = _iso_since_ms &&" in INO, \
    "the ladder test changed shape"
assert "uint32_t iso_window = ladder_running ? ESPNOW_RESTART_MS" in INO, \
    "the window is no longer chosen by whether recovery is running"
print(f"   pairing attempts nothing, so it got {stalled} s          OK")
assert stalled < 60, "the short window is long enough that this would not bite"
print("   which is not long enough to type a 6-digit code    OK")

# ---- 3. the screen keeps its own bound -------------------------------------
print("\n3. what still bounds the pairing screen:")
m = re.search(r"#define CAMPAIR_IDLE_MS\s+\((\d+)UL \* 60UL \* 1000UL\)", INO)
assert m, "the pairing screen has no idle bound at all"
idle_min = int(m.group(1))
print(f"   CAMPAIR_IDLE_MS = {idle_min} min                            OK")

key = INO[INO.index("static void campair_key("):]
key = key[:key.index("\n}\n")]
assert "_pair_idle_ms = millis();" in key, \
    "a keypress no longer restarts the idle clock — typing slowly would time out"
print("   restarted by every keypress                        OK")

# And it must EXIT, not reboot. That distinction is the whole point.
exit_fn = INO[INO.index("static void campair_exit("):]
exit_fn = exit_fn[:exit_fn.index("\n}\n")]
assert "_pair_active = false;" in exit_fn, "the pairing screen cannot exit"
assert "ESP.restart" not in exit_fn, \
    "the pairing timeout restarts the chip; it should just leave the screen"
print("   leaves the screen rather than restarting the chip  OK")

# ---- 4. the E-STOP watchdog is separate, and self-heals --------------------
# It fires during pairing too — no hub contact for 10 s — but that only stops
# motion, which is correct, and it clears when contact returns.
print("\n4. the estop watchdog:")
assert "if (hub_ok) _watchdog_fired = false;" in INO, \
    "the estop watchdog no longer clears when the hub comes back — pairing would\n" \
    "    leave the mount stopped afterwards"
print("   fires during pairing, clears when the hub returns  OK")

print("\nALL CHECKS PASSED")
