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
            mount: int = 1, peer_via_sat: bool = False,
            mount_via_sat: str = "", relay_refused_s: float | None = None,
            direct_silent: tuple = ()) -> str:
    """Drive the real classifier with the four things it can observe.

    peer_via_sat is the 2026-09-09 case: the ACKing peer is reached THROUGH a
    satellite, so its reply travelled over Ethernet and says nothing about this
    hub's radio.
    """
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
    # Direct mounts that HAVE ACKed this session and have now gone stale. The
    # hub-radio verdict counts these, and one is not enough — see
    # _direct_mounts_silent().
    for mt in direct_silent:
        b._mount_last_ack[mt] = now - 99.0
    # Route table: mount 4 direct, or via satellite slot 2 (Basement).  The
    # WEDGED mount routes through slot 3 when mount_via_sat names it, which is
    # the separate question of whether the mount in trouble is on our radio.
    b._mount_route = [0, 0, 0, 2 if peer_via_sat else 0, 0]
    b._sat_names = {2: "Basement", 3: mount_via_sat or "Foyer"}
    b._sat_refusing_t = {}
    if mount_via_sat:
        b._mount_route[mount - 1] = 3
        if relay_refused_s is not None:
            b._sat_refusing_t[mount_via_sat] = now - relay_refused_s
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

# ---- 4b. the ACKing peer is behind a satellite -------------------------------
# 2026-09-09, twice: the hub's send callback stalled, cam4 was via Foyer, and
# this said "hub TX is healthy, so this is mount-side. Not touching the hub;
# mount 1 likely needs a power cycle." cam4's ACK crossed Ethernet to the
# satellite and never touched the hub's radio, so it was evidence about nothing.
# The hub's own detector was reinitialising that radio at the same moment.
print("\n4b. the only other mount is satellite-relayed:")
line = verdict(bytes_arriving=True, diag_fresh=True, peer_acking=True,
               peer_via_sat=True)
# Not a substring test: the correct fallback verdict is "HUB-SIDE OR
# MOUNT-SIDE", which contains "MOUNT-SIDE" and would pass one.
assert "so the hub forwards fine" not in line, \
    f"a satellite-relayed ACK is still read as proof the hub's radio works: {line}"
assert "no other mount is ACKing either" in line, \
    f"the verdict does not fall back to 'cannot separate them': {line}"
# And the same peer, reached directly, must still settle it — otherwise this
# fix has simply disabled the check.
direct = verdict(bytes_arriving=True, diag_fresh=True, peer_acking=True)
assert "MOUNT-SIDE" in direct and "mount 4 is still ACKing" in direct, \
    f"a DIRECT peer no longer proves the hub's radio: {direct}"
print("   via a satellite proves nothing; direct still does    OK")

# ---- 4c. the WEDGED mount is the one behind a satellite ----------------------
# The other half of 4b, and it cost ten wrong lines on 2026-09-11 17:40. Foyer
# relays cam4 and cam5 and was refusing every frame offered — "offered 72,
# sent 0, refused 72" in the same second — while the verdict said mount 4 was
# the fault and likely needed a power cycle. Mount 1 is direct, so its ACK does
# prove the hub's radio works; nothing reaches mount 4 by that radio, so the
# conclusion does not follow. The advice costs a truss climb, a re-home and a
# recalibration for a mount that recovered on its own when Foyer restarted.
print("\n4c. the wedged mount is behind a satellite:")
line = verdict(bytes_arriving=True, diag_fresh=True, peer_acking=True,
               mount=1, mount_via_sat="Foyer", relay_refused_s=1.2)
assert "SATELLITE-SIDE" in line, \
    f"a relayed mount is still blamed for its relay's refusal: {line}"
assert "Foyer" in line, \
    f"the verdict does not name the relay, which is the thing to go and look " \
    f"at: {line}"
assert "likely needs a power cycle" not in line and "is the fault" not in line, \
    f"the expensive advice survives on the case it was wrong about: {line}"
assert "1.2s ago" in line, \
    f"the refusal is not dated, so it reads as a guess about the relay rather " \
    f"than an observation of it: {line}"
print("   SATELLITE-SIDE, names the relay and dates it        OK")

# A relay that has NOT been seen refusing still takes the verdict — the route
# alone settles who the witness can speak for — but must not invent a refusal.
quiet = verdict(bytes_arriving=True, diag_fresh=True, peer_acking=True,
                mount=1, mount_via_sat="Foyer")
assert "SATELLITE-SIDE" in quiet and "has not been seen refusing" in quiet, \
    f"a quiet relay either loses the verdict or gains a refusal it never had: {quiet}"
# Only the verdict half: the WEDGE header always carries its own "Ns ago" for
# the hub counters, so the whole line is the wrong thing to look in.
assert "last refused a downlink" not in quiet.split("→")[-1], \
    f"a refusal time was reported that does not exist: {quiet}"
print("   a quiet relay is still the right place to look      OK")

# And a DIRECT mount must still get the old verdict, or this has just disabled it.
direct = verdict(bytes_arriving=True, diag_fresh=True, peer_acking=True, mount=1)
assert "MOUNT-SIDE" in direct and "SATELLITE-SIDE" not in direct, \
    f"a direct mount no longer reaches the mount-side verdict: {direct}"
print("   a direct mount is unaffected                        OK")

# ---- 4d. the hub's own radio, which ran 2 h 40 m being called ambiguous -----
# 2026-09-12: cam1/2/3 are direct and took 0 of 81, 0 of 80 and 0 of 78 commands
# each. cam4 and cam5 go through Foyer and ran 100% for the whole of it. The
# verdict said "nothing here separates a hub that has stopped forwarding from a
# mount that has stopped answering" — while the route table separated them
# completely. It ended because the hub was reflashed for an unrelated reason.
#
# A relayed ACK crosses Ethernet and never touches the radio, so it cannot prove
# the radio works (4b) — but it does prove the hub, its link and its relay path
# are alive, and that is the other half of the inference.
print("\n4d. several direct mounts silent, a relayed one answering:")
line = verdict(bytes_arriving=True, diag_fresh=True, peer_acking=True,
               mount=1, peer_via_sat=True, direct_silent=(1, 2, 3))
assert "HUB RADIO" in line, \
    f"three direct mounts down together with the relay clean is still being\n" \
    f"    called ambiguous: {line}"
assert "mount 4 is still ACKing" in line or "mount 4" in line, \
    f"the verdict does not name the relayed mount that proves the hub is up: {line}"
assert "No mount needs touching" in line, \
    f"it does not say the thing that saves a trip to the truss: {line}"
print("   HUB RADIO, and says no mount needs touching        OK")

# ONE direct mount silent must NOT reach that verdict. It is equally that
# mount's own radio, which is the common case on this rig — and claiming the
# hub on a single silent mount would send someone to reboot a healthy hub and
# drop the four mounts that were working.
one = verdict(bytes_arriving=True, diag_fresh=True, peer_acking=True,
              mount=1, peer_via_sat=True, direct_silent=(1,))
assert "HUB RADIO" not in one, \
    f"a single silent direct mount is enough to blame the hub's radio: {one}"
assert "no other mount is ACKing either" in one, \
    f"the ambiguous verdict no longer fires where it is still the right one: {one}"
print("   one silent mount stays ambiguous, as it must       OK")

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
