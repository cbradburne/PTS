"""A wedge verdict names the part that is actually broken.

The hub's USB-RX counters answer exactly one question: are our bytes reaching
the hub? Frozen means they are not — host-side, a Windows USB-CDC OUT halt.
Climbing means they are.

Climbing does NOT mean the hub is at fault, and the verdict used to say it did:
"HUB-SIDE — bytes ARE reaching the hub but aren't forwarded; fix is in hub
firmware". That is a false dichotomy — it rules out the host and then blames
the hub without ever looking past it. The mount is the third possibility and,
on this rig, the usual one.

Logged on 2026-09-04 19:47, one line apart:

    → HUB-SIDE — ... fix is in hub firmware
    Mount 1 unreachable 12.8s, but mount 4 is still ACKing — ... this is
    mount-side. Not touching the hub; mount 1 likely needs a power cycle.

The second was right: cam1's bridge had run its ESP-NOW stack out of memory and
rebooted itself a second later. The first was the more alarming of the two and
would have sent someone into hub firmware for a fault that was not there.

The evidence to tell them apart already existed — _hub_tx_proven_ok(), an ACK
from any OTHER mount, which proves the hub's transmit path works. It just was
not being consulted.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, io, logging, pathlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

from comms.bridge import Bridge

BUF = io.StringIO()
_h = logging.StreamHandler(BUF)
_h.setFormatter(logging.Formatter("%(levelname)s|%(message)s"))
_lg = logging.getLogger("comms.bridge")
_saved = (_lg.handlers, _lg.level, _lg.propagate)
_lg.handlers, _lg.propagate = [_h], False
_lg.setLevel(logging.INFO)


def verdict(*, bytes_arriving: bool, diag_fresh: bool, peer_acking: bool,
            mount: int = 1) -> str:
    """Drive the real classifier with the three things it can observe."""
    import threading, time
    b = Bridge.__new__(Bridge)
    b._diag_lock = threading.Lock()
    b._hub_rx_bytes = 4304213
    b._hub_rx_pkts  = 376873
    now = time.monotonic()
    b._hub_diag_rx_t = now - (0.5 if diag_fresh else 9.0)
    # "advance" is when the hub's RX counter last moved. Frozen = our bytes are
    # not arriving.
    b._hub_rx_last_advance_t = now - (0.5 if bytes_arriving else 9.0)
    b._mount_last_ack = {4: now - 0.5} if peer_acking else {}
    BUF.truncate(0); BUF.seek(0)
    b._log_wedge_side(now, mount)
    return BUF.getvalue().strip()


# ---- 1. the case that was wrong ---------------------------------------------
print("1. our bytes reach the hub, and another mount is fine:")
line = verdict(bytes_arriving=True, diag_fresh=True, peer_acking=True)
assert "MOUNT-SIDE" in line, f"the verdict is not mount-side: {line}"
assert "mount 4 is still ACKing" in line, \
    "the verdict does not say WHY it is mount-side, so it reads as an assertion\n" \
    f"    rather than a finding: {line}"
assert "fix is in hub firmware" not in line, \
    "the old hub-side wording survives on the very case it was wrong about"
print("   MOUNT-SIDE, and it names the mount that proves it   OK")

# ---- 2. the case that genuinely cannot be told apart -------------------------
# No other mount ACKing means there is no evidence either way — four of five
# mounts are off most of the time on this rig, so this is the common case and
# it must not be dressed up as a hub fault.
print("\n2. our bytes reach the hub, and no other mount is up:")
line = verdict(bytes_arriving=True, diag_fresh=True, peer_acking=False)
assert "HUB-SIDE OR MOUNT-SIDE" in line, \
    f"a verdict was reached with nothing to reach it from: {line}"
assert "Power one other mount on" in line, \
    "it does not say what would settle it, which is the only useful thing it " \
    f"can say here: {line}"
print("   says it cannot tell, and how to find out            OK")

# ---- 3. the host-side case is untouched -------------------------------------
# This one was always right: the hub's RX counter frozen means our bytes are
# genuinely not arriving, whatever any mount is doing.
print("\n3. our bytes are NOT reaching the hub:")
for peer in (True, False):
    line = verdict(bytes_arriving=False, diag_fresh=True, peer_acking=peer)
    assert "HOST-SIDE" in line, f"peer_acking={peer}: {line}"
    assert "USB-CDC" in line, "the host-side verdict lost its cause"
print("   HOST-SIDE either way — no mount can explain that     OK")

# ---- 4. and a stale diagnostic still refuses to guess ------------------------
print("\n4. the hub's own diagnostic is stale:")
line = verdict(bytes_arriving=True, diag_fresh=False, peer_acking=True)
assert "INCONCLUSIVE" in line, \
    f"a verdict was reached on a counter we cannot trust: {line}"
print("   INCONCLUSIVE — the counter itself is not current     OK")

# ---- 5. every verdict is a WARNING and names the mount ----------------------
print("\n5. every path:")
for ba in (True, False):
    for df in (True, False):
        for pa in (True, False):
            line = verdict(bytes_arriving=ba, diag_fresh=df, peer_acking=pa, mount=3)
            assert line.startswith("WARNING"), f"not a warning: {line}"
            assert "mount=3" in line, f"the wedged mount is not named: {line}"
print("   all 8 combinations warn and name the mount           OK")

_lg.handlers, _lg.level, _lg.propagate = _saved
print("\nALL CHECKS PASSED")
