"""Run mode ping-pongs the slider, instead of stopping after one leg.

Run on a look-at camera should go left, right, left, right until the operator
stops it. It went to one end and stayed there.

The run is owned by the mount's AMOLED bridge: the PC sends the first leg with
`repeat` set and the bridge sends every leg after it, watching the Teensy's
look-at status for the moment a leg ends. That is one bit in one byte, and the
bridge was reading the wrong one:

    bool la_now = (pkt.payload[13] & 0x01);      // FLAG_LOOK_AT_ACTIVE is 0x40

Bit 0 is a bit the Teensy never sets. la_now was false on every packet ever
received, so the rising edge never happened and the falling edge the flip hangs
off never did either. The first leg ran because the PC sent it; nothing sent a
second.

Two copies of one fact, one of them written as a literal, is how this happens,
so what is checked here is that the two agree — the byte the Teensy BUILDS is
run through the mask the bridge TESTS, and the answer has to come out true
while a move is running and false when it is not. A literal that is wrong fails
that whether or not it looks plausible.

Run directly, or via tools/run_tests.sh with the rest.
"""
import pathlib, re

REPO = pathlib.Path(__file__).resolve().parent.parent
PROTO  = (REPO / "firmware/shared/protocol.h").read_text()
TEENSY = (REPO / "firmware/teensy41_mount/teensy41_mount.ino").read_text()
BRIDGE = (REPO / "firmware/esp_mount_amoled175/esp_mount_amoled175.ino").read_text()


def flag(name):
    m = re.search(rf"#define\s+{name}\s+(0x[0-9A-Fa-f]+|\d+)", PROTO)
    assert m, f"{name} is not defined in shared/protocol.h"
    return int(m.group(1), 0)


REF_SET   = flag("FLAG_REF_SET")
LA_ACTIVE = flag("FLAG_LOOK_AT_ACTIVE")

# ---- 1. the byte the Teensy sends ------------------------------------------
print("1. what the mount puts in the status byte:")
snd = TEENSY[TEENSY.index("static void send_look_at_status()"):]
snd = snd[:snd.index("\n}")]
assert "payload[13] = flags;" in snd, "the flags no longer live in payload[13]"
assert "flags |= FLAG_LOOK_AT_ACTIVE;" in snd, \
    "the mount no longer reports whether a look-at move is running"
assert "STATE_LOOK_AT_MOVE" in snd and "STATE_LOOK_AT_PRE_AIM" in snd, \
    "the active flag is no longer set from the look-at states"

# The two bytes the bridge will actually see: mid-leg, and the one packet that
# says the leg is over.
RUNNING = REF_SET | LA_ACTIVE
STOPPED = REF_SET
print(f"   running 0x{RUNNING:02X}, finished 0x{STOPPED:02X}            OK")

# ---- 2. the mask the bridge tests it with ----------------------------------
print("\n2. what the bridge tests it against:")
edge = BRIDGE[BRIDGE.index("if (pkt.payload_len >= 14 && _run_active) {"):]
edge = edge[:edge.index("\n        }")]
m = re.search(r"la_now\s*=\s*\(pkt\.payload\[13\]\s*&\s*([A-Za-z0-9_]+)\)", edge)
assert m, "the leg-end edge is no longer read from payload[13]"
token = m.group(1)
mask = flag(token) if token.startswith("FLAG_") else int(token, 0)

assert bool(RUNNING & mask), (
    f"the bridge masks payload[13] with {token} (0x{mask:02X}), which is clear in "
    f"the\n    0x{RUNNING:02X} the mount sends WHILE A LEG IS RUNNING — la_now can never "
    "go true,\n    so the leg-end edge never fires and the run stops after the first leg")
assert not (STOPPED & mask), (
    f"{token} (0x{mask:02X}) is still set in the 0x{STOPPED:02X} sent once the leg has "
    "FINISHED —\n    the edge would never fall and the next leg would never be sent")
print(f"   masks with {token} = 0x{mask:02X}                OK")
print("   true while running, false once finished           OK")

# ---- 3. the edge, driven -----------------------------------------------------
# Two full legs through the same transition the bridge implements, so a mask
# that passes the checks above but sits on the wrong edge still fails here.
print("\n3. two legs:")
flips = []
leg_active = False           # the bridge's _run_leg_active, at run start
direction = 0
for packet in (STOPPED,                       # command sent, mount not moving yet
               RUNNING, RUNNING, RUNNING,     # leg 1
               STOPPED,                       # leg 1 done  -> flip
               RUNNING, RUNNING,              # leg 2
               STOPPED):                      # leg 2 done  -> flip
    la_now = bool(packet & mask)
    if leg_active and not la_now:
        direction ^= 1
        flips.append(direction)
    leg_active = la_now
assert flips == [1, 0], \
    f"two legs produced direction changes {flips}; expected [1, 0] — left, right, left"
print("   direction 0 -> 1 -> 0, and it keeps going         OK")

# ---- 4. the edge state cannot be left over from the last run ---------------
# It used to be a `static bool` inside the handler, so it kept whatever the
# previous run left there. A run stopped mid-leg left it true, and the first
# status of the NEXT run — sent before the mount had started moving — would
# read as a leg ending and flip the direction before a leg had run at all.
print("\n4. starting a second run:")
assert "static bool was_active" not in BRIDGE, \
    "the edge state is a function-static again — it survives run_stop()"
assert "_run_leg_active = false;" in BRIDGE.split("static void run_stop")[1][:400], \
    "run_stop() does not clear the edge state"
start = BRIDGE[BRIDGE.index("if (rep && !_run_active) {"):]
assert "_run_leg_active = false;" in start[:300], \
    "starting a run does not reset the edge state"
print("   cleared on stop and on start                      OK")

# ---- 5. and the run still ends when the operator takes over ----------------
# Owning the run locally removed the accidental deadman that the old PC-driven
# round trip provided; these are what replaced it.
print("\n5. what still stops it:")
for cmd in ("CMD_E_STOP", "CMD_JOG", "CMD_GOTO", "CMD_GOTO_SLOT", "CMD_MOVE_REL"):
    assert re.search(rf"pkt\.cmd == {cmd}\b", BRIDGE), \
        f"{cmd} no longer stops a run — the mount would ping-pong under the operator"
assert "RUN_DEADMAN_MS" in BRIDGE, "the run has no deadman"
print("   E-STOP, jog, goto, slot recall, move, deadman      OK")

print("\nALL CHECKS PASSED")
