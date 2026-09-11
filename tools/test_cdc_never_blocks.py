"""The USB console must never be able to stall loop().

`Serial` on every S3 target here is the native USB CDC, and HWCDC::write()
blocks on its TX ring for `tx_timeout_ms` at a time — 100 ms by default — up to
twenty consecutive times. The core's own comment names the case: the board is
still plugged in, the host has stopped reading, and the plug detector keeps
reporting connected, so nothing ends the wait but the deadline. One print can
therefore hold the loop for two seconds.

That is not a cosmetic problem on this rig. A blocked loop is the one condition
the bench PROVED leaks ESP-NOW buffers: with a synthetic block the in-flight
floor stepped 0 -> 1 -> 2 -> 3, each step 208 bytes of heap never returned, and
without one the same rate ran 13.3 h clean. An unguarded console write is that
condition arriving by accident.

It was found and fixed on esp32_hub_eth first, for the same reason and with the
same call, and stayed unfixed on three other targets for weeks — which is the
whole argument for a test rather than a note. The bench had even demonstrated it
against itself: a run was discarded after 37 seconds with no reader on the port,
during which the floor stepped and the board was not leaking.

Run directly, or via tools/run_tests.sh with the rest.
"""
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent

# Every sketch whose `Serial` is the native USB CDC. A target reaching this list
# is one whose console can block; add to it when a new S3 firmware appears.
TARGETS = (
    "firmware/esp32_hub_eth/esp32_hub_eth.ino",
    "firmware/esp32_satellite/esp32_satellite.ino",
    "firmware/esp_mount_amoled175/esp_mount_amoled175.ino",
    "firmware/espnow_bench/espnow_bench.ino",
)

print("the USB console cannot stall loop():")

fail = []
for rel in TARGETS:
    src = (REPO / rel).read_text()
    name = rel.split("/")[-1]

    m = re.search(r"Serial\.setTxTimeoutMs\(\s*(\d+)\s*\)", src)
    if not m:
        fail.append(
            f"{name} never calls Serial.setTxTimeoutMs(). Its console keeps the\n"
            f"    100 ms default, so one print can hold loop() for two seconds\n"
            f"    whenever a host stops draining the port while still plugged in."
        )
        continue
    if m.group(1) != "0":
        fail.append(
            f"{name} sets a NON-ZERO tx timeout ({m.group(1)} ms). Any value but 0\n"
            f"    still waits, and the wait is what stalls the loop."
        )
        continue

    # Setting it before the port exists does nothing, and reads as done.
    begin = src.index("Serial.begin(")
    if src.index(m.group(0)) < begin:
        fail.append(
            f"{name} calls setTxTimeoutMs() BEFORE Serial.begin(), where it cannot\n"
            f"    take effect — the guard reads as present and is not."
        )
        continue

    print(f"   {name:<28} tx timeout 0, after begin()   OK")

if fail:
    print("\nFAILED:")
    for f in fail:
        print("  - " + f)
    sys.exit(1)

# ---------------------------------------------------------------------------
# ...and `Serial` must actually BE the USB peripheral
# ---------------------------------------------------------------------------
# The USB Serial/JTAG block and UART0 are different hardware, and one Tools-menu
# item decides which one `Serial` is. With CDC On Boot off it falls back to
# UART0 — GPIO43/44 — where the mount has the TEENSY. Every console print would
# then be injected into the motor controller's receive line, silently, and the
# symptom would look like a flaky Teensy link rather than a board setting.
# build.sh pins CDCOnBoot=cdc so a repo build is safe; an Arduino IDE build is
# not, which is what the compile-time guard is for.
print("\n`Serial` cannot fall back to UART0:")
for rel in TARGETS:
    src = (REPO / rel).read_text()
    name = rel.split("/")[-1]
    guard = re.search(r"#if\s+!ARDUINO_USB_CDC_ON_BOOT\s*\n\s*#error", src)
    assert guard, (
        f"{name} has no compile-time guard on USB CDC On Boot. Built with that\n"
        f"    menu item off, Serial becomes UART0 on GPIO43/44 and the console\n"
        f"    leaves the chip on two pins — which on the mount are the Teensy's."
    )
    print(f"   {name:<28} guarded at compile time       OK")

# The bench is the instrument, so it carries the extra requirement: a ring big
# enough that ordinary reporting is never dropped. Dropping a sample is honest
# (the t= column jumps and says so) but it should not happen during normal
# logging, only during a real host outage.
bench = (REPO / "firmware/espnow_bench/espnow_bench.ino").read_text()
m = re.search(r"Serial\.setTxBufferSize\(\s*(\d+)\s*\)", bench)
assert m and int(m.group(1)) >= 4096, \
    "the bench does not enlarge its console TX ring. With the wait removed, a\n" \
    "    few hundred bytes of default ring drops report lines during any brief\n" \
    "    host hiccup — and the run file is the entire product."
assert bench.index(m.group(0)) < bench.index("Serial.begin("), \
    "setTxBufferSize() must be called BEFORE Serial.begin(), or the ring is\n" \
    "    already allocated at the default size and the call does nothing"
print(f"   bench ring {m.group(1)} bytes, set before begin()       OK")

print("\nALL CHECKS PASSED")
