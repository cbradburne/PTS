"""A stack rebuild must reconcile the in-flight counters, on every node.

`esp_now_deinit()` discards whatever is queued WITHOUT firing its callbacks.
Nothing ever returns those, so `issued - callbacks` keeps them for the life of
the boot and the leak floor never comes back down. The node then reports a
number that says "buffers lost" and means "my upstream went away while I was
sending".

Measured on the rig, 2026-09-10/11, and the control is what makes it certain:

    cam1  floor 10 -> 11 -> 14 -> 15 -> 16 -> 17
    cam2  floor 11 -> 12 -> 13 -> 14 -> 15 -> 16
          every step on a HUB event — the 23:02 flash, the 07:02 maintenance
          restart, the 10:33 reflash, the 21:50 and 09:00 stall recoveries

    cam4  floor  2 ->  3 ->  5
          ignored every one of those. It is relayed by a satellite, and its two
          steps are Foyer's two restarts, 10:55 and 18:01.

Two mounts tracking the hub and a third tracking its own satellite instead is
not a coincidence, and it is not buffer loss.

The satellite has had the reconcile since its counter was written. The hub
gained it on 2026-09-11, and its own comment recorded that this was "the whole
reason its floor kept climbing". The mount was the third, fixed after the log
above. That is twice this has been fixed on one node and left on another, which
is the argument for a test rather than a third comment.

The high-water mark matters as much as the reconcile: clearing the floor without
keeping the worst value first would erase the evidence the rebuild was called to
deal with — the opposite failure, and a quieter one.

Run directly, or via tools/run_tests.sh with the rest.
"""
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent

# (sketch, floor, worst, callbacks, the sends counter callbacks are squared to)
NODES = (
    ("firmware/esp32_hub_eth/esp32_hub_eth.ino",
     "_espnow_leak_floor", "_espnow_leak_worst", "_espnow_cb_total", "_espnow_issued"),
    ("firmware/esp32_satellite/esp32_satellite.ino",
     "_sat_leak_floor", "_sat_leak_worst", "_sat_cb_total", "_sat_dn_sent_total"),
    ("firmware/esp_mount_amoled175/esp_mount_amoled175.ino",
     "_espnow_leak_floor", "_espnow_leak_worst", "_espnow_cb_total", "_espnow_issued"),
)

fail = []
print("every node reconciles its in-flight count on a rebuild:")

for rel, floor, worst, cbs, issued in NODES:
    src = (REPO / rel).read_text()
    name = rel.split("/")[-1]

    # The three statements have to be one block in one order, so the block is
    # what gets looked at. Searching the whole file instead finds the DECLARATION
    # `static uint32_t <floor> = 0;` and calls it the clear — which is how the
    # first draft of this test failed three firmwares that were all correct.
    keep = re.search(rf"{re.escape(worst)}\s*=\s*{re.escape(floor)}\s*;", src)
    if not keep:
        fail.append(f"{name}: nothing keeps a high-water mark of {floor}, so either\n"
                    f"    it is never cleared (the leak-floor-for-ever bug) or a\n"
                    f"    rebuild erases the evidence it was called to deal with.")
        continue

    block = src[keep.start():keep.start() + 400]

    if not re.search(rf"{re.escape(cbs)}\s*=\s*{re.escape(issued)}\s*;", block):
        fail.append(f"{name}: the high-water mark is kept, but {cbs} is never squared\n"
                    f"    with {issued} beside it. The difference survives the\n"
                    f"    rebuild, so the floor climbs straight back on the next\n"
                    f"    window and the clear achieves nothing.")
        continue

    clear = re.search(rf"{re.escape(floor)}\s*=\s*0\s*;", block)
    if not clear:
        fail.append(f"{name}: {floor} is not cleared in the same block, so a\n"
                    f"    discarded queue is counted as lost buffers for the life\n"
                    f"    of the boot.")
        continue

    # 4. and the report has to read the larger of the two, or the number the
    #    operator sees drops to zero at each rebuild.
    if not re.search(rf"{re.escape(floor)}\s*>\s*{re.escape(worst)}", src):
        fail.append(f"{name}: nothing compares {floor} against {worst}, so the\n"
                    f"    reported figure is whichever one the author reached for\n"
                    f"    and a rebuild can still hide a real leak.")
        continue

    print(f"   {name:<28} reconciled, worst kept, both read  OK")

if fail:
    print("\nFAILED:")
    for f in fail:
        print("  - " + f)
    sys.exit(1)

print("\nALL CHECKS PASSED")
