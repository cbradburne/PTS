"""The hub escalates when its stack refuses to take a frame.

Measured on the rig 2026-09-12 at 19:49, with the rejection events added that
morning: ESP_ERR_ESPNOW_NO_MEM to all three directly-radioed mounts, about 100
refusals per mount per 30 s, continuously. The self-rescue ladder fired zero
times. The same shape ran 2 h 40 m earlier the same day and fourteen hours on
2026-09-08, and every one of them ended because a person rebooted the hub.

Why nothing caught it. The original rungs arm on a run of send FAILURES, and a
refused send never goes on air, so txfail — which counts sends that failed on
air — cannot move. The callback rung added after the 14-hour outage arms on
sends accepted with nothing coming back; this fault is the state after that one,
where the pool is already empty, so there is nothing outstanding and the stall
clock never starts. Between them the two existing rungs cover every case except
the one that keeps happening.

WHAT THIS TEST IS PROTECTING. The rung reboots the hub, which drops every mount
that is working, including whatever a satellite is relaying. Two properties make
that acceptable and both are checked below:

  it is armed ONLY by NO_MEM     NOT_FOUND is one unbound peer and IF is a
                                 configuration mistake. Neither is cured by a
                                 reboot, and a hub that reboots over them would
                                 be worse than the fault.
  ANY accepted send clears it    The pool is one global resource, so a single
                                 frame the driver takes proves it is not
                                 exhausted. This is what stops a queue that
                                 briefly fills — 30-90 ms to drain on the
                                 bench — from ever reaching the threshold.

Run directly, or via tools/run_tests.sh with the rest.
"""
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
INO = (REPO / "firmware/esp32_hub_eth/esp32_hub_eth.ino").read_text()
FLAT = re.sub(r"\s+", " ", INO)

fail = []

# ---- 1. armed only by NO_MEM ------------------------------------------------
print("1. what arms the clock:")
m = re.search(r"if \(e == ESP_ERR_ESPNOW_NO_MEM && !_nomem_since_ms\) "
              r"_nomem_since_ms = now", FLAT)
if not m:
    fail.append("the NO_MEM clock is not started under an ESP_ERR_ESPNOW_NO_MEM\n"
                "    guard. Armed by any rejection, a single unbound peer\n"
                "    (NOT_FOUND) would reboot the hub and drop every working mount.")
else:
    print("   started on NO_MEM only                             OK")

# The clock must be stamped in the send path, not in the ladder — the ladder
# runs on a 500 ms tick and would miss a refusal that recovered between passes,
# which is exactly the transient it must be able to tell from the fault.
send = INO[INO.index("static bool espnow_send_now("):]
send = send[:send.index("\n}\n")]
if "_nomem_since_ms" not in send:
    fail.append("the clock is not stamped inside espnow_send_now(), so it samples\n"
                "    the refusal rather than observing it.")
else:
    print("   stamped in the send path, not sampled later        OK")

# ---- 2. any accepted send clears it -----------------------------------------
print("\n2. what clears it:")
ok_branch = send[send.index("if (e == ESP_OK)"):send.index("return true;")]
if "_nomem_since_ms = 0;" not in ok_branch:
    fail.append("an accepted send does not clear the clock. It would then measure\n"
                "    time since the FIRST refusal of the boot, and any hub that\n"
                "    ever saw one transient full queue would escalate for ever.")
else:
    print("   cleared on ESP_OK, in the same function             OK")

# Clearing it anywhere else — a timer, the ladder, a recovery path — would break
# the one property that makes the threshold safe.
others = [ln for ln in INO.splitlines()
          if "_nomem_since_ms = 0" in ln and not ln.strip().startswith("//")]
if len(others) != 1:
    fail.append(f"{len(others)} places clear the clock, not 1:\n      "
                + "\n      ".join(o.strip() for o in others) +
                "\n    Only an accepted send may clear it — anything else can zero it\n"
                "    while the stack is still refusing, and the ladder never fires.")
else:
    print("   exactly one place clears it                        OK")

# ---- 3. it reaches the ladder ----------------------------------------------
print("\n3. that it actually escalates:")
if not re.search(r"nomem_age > SELF_NOMEM_STALL_MS", FLAT):
    fail.append("nothing compares the clock against SELF_NOMEM_STALL_MS, so the\n"
                "    clock runs and no rung is ever armed by it.")
else:
    print("   compared against the threshold                     OK")

# worst_age/worst_i are what the rungs below read. Setting the clock without
# these is a counter with no consequence — the exact shape of the bug this
# replaces, where _entx_deferred was incremented and read by nothing.
blk = FLAT[FLAT.index("nomem_age > SELF_NOMEM_STALL_MS"):]
blk = blk[:2000]
for token in ("worst_age = nomem_age", "worst_i = 0", "_cb_stall_active = true"):
    if token not in blk:
        fail.append(f"'{token}' is not set when the threshold is passed, so the\n"
                    "    ladder below never sees the wedge this detected.")
if not any(f for f in fail if "worst_age" in f or "_cb_stall_active" in f):
    print("   feeds worst_age/worst_i and suppresses the veto     OK")

# The signed difference, for the same reason every other clock here has one:
# an unsigned underflow puts it instantly past every rung.
if "int32_t d = (int32_t)(now - _nomem_since_ms)" not in FLAT:
    fail.append("the age is an unsigned subtraction. _nomem_since_ms is stamped\n"
                "    from the send path while now comes from the top of the loop\n"
                "    pass, so a refusal landing between them underflows to ~4.29e9\n"
                "    and reboots the hub over a race. This exact bug cost days.")
else:
    print("   signed difference, as the callback clock learned    OK")

# ---- 4. the threshold is not trigger-happy ----------------------------------
print("\n4. the threshold:")
if not re.search(r"#define SELF_NOMEM_STALL_MS\s+CB_STALL_MS", INO):
    fail.append("SELF_NOMEM_STALL_MS is no longer derived from CB_STALL_MS. If it\n"
                "    is being set independently, check it is still far above the\n"
                "    30-90 ms a full queue takes to drain on the bench.")
else:
    print("   3 s, two orders above a queue drain                OK")

if fail:
    print("\nFAILED:")
    for f in fail:
        print("  - " + f)
    sys.exit(1)

print("\nALL CHECKS PASSED")
