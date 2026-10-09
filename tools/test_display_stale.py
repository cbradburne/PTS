"""The hub display does not blank a working mount's tile between two STATUS.

2026-10-09, during the concert: one camera tile on the hub's 7" display showed
disconnected, while the PC app never greyed the mount and the log showed no
outage.  The display clears any tile it has not had an UPDATE_CAM for in a set
time, and that time was 5000 ms, written when mounts streamed STATUS many times
a second.  The hub sends UPDATE_CAM only on a mount's STATUS, and an idle mount
now sends one every 5 s: one late or lost STATUS, or an update the display
dropped while LVGL held its mutex, blanked the tile for a second.  The hub's
own timeouts were moved to the 5 s refresh in a0c09d9; the display's was not.

The display's limit is now the hub's verdict plus one repeat of it.  This reads
every number involved from the firmware that ships:

  1. the display's sweep is against CAM_STALE_MS, not a figure of its own;
  2. the hub says "gone", and repeats it, before the display gives up alone;
  3. an hour of idle STATUS — jitter, losses, dropped updates — through the
     display's sweep: the old 5 s flickers, the new never does, and a mount
     that is really gone is still cleared when the hub has stopped too.

Run directly, or via tools/run_tests.sh.
"""
import pathlib, random, re

REPO = pathlib.Path(__file__).resolve().parent.parent
FW = REPO / "firmware"
PROTO = (FW / "shared/protocol.h").read_text()
MOUNT = (FW / "esp_mount_amoled175/esp_mount_amoled175.ino").read_text()
HUB = (FW / "esp32_hub_eth/esp32_hub_eth.ino").read_text()
DISP = (FW / "esp32_display/hub_display.cpp").read_text()


def c_value(expr: str, names: dict) -> int:
    """Evaluate a C constant expression made of numbers, + - * / ( ) and the
    names already known."""
    expr = re.sub(r"\b(\d+)[UuLl]+\b", r"\1", expr)
    for n, v in sorted(names.items(), key=lambda kv: -len(kv[0])):
        expr = re.sub(r"\b%s\b" % n, str(v), expr)
    assert re.fullmatch(r"[\d\s+\-*/()]+", expr), f"cannot evaluate: {expr!r}"
    return int(eval(expr))


def define(src: str, name: str, names: dict) -> int:
    m = re.search(r"^#define\s+%s\s+(.+?)\s*(?://.*)?$" % name, src, re.M)
    assert m, f"no #define {name}"
    return c_value(m.group(1), names)


K = {}
K["MOUNT_STATUS_REFRESH_MS"] = define(PROTO, "MOUNT_STATUS_REFRESH_MS", K)
K["MOUNT_PRESENCE_TIMEOUT_MS"] = define(PROTO, "MOUNT_PRESENCE_TIMEOUT_MS", K)
heartbeat = define(MOUNT, "STATUS_HEARTBEAT_MS", K)
refresh = define(MOUNT, "STATE_REFRESH_MS", K)
hub_timeout = define(HUB, "MOUNT_TIMEOUT_MS", K)
m = re.search(r"now - _last_disc_sweep_ms >= (\d+)", HUB)
assert m, "the hub's display reconciliation sweep changed shape"
hub_repeat = int(m.group(1))
stale = define(DISP, "CAM_STALE_MS", K)
print("from the firmware: a mount's STATUS every %d ms (heartbeat) / %d ms (refresh);"
      % (heartbeat, refresh))
print("the hub calls it gone after %d ms and repeats that every %d ms;" % (hub_timeout, hub_repeat))
print("the display gives up by itself after %d ms\n" % stale)

# ---- 1. one limit, from the shared numbers --------------------------------------------
print("1. the display's sweep:")
sweep = DISP[DISP.index("void hub_ui_tick()"):]
sweep = sweep[:sweep.index("\n}\n")]
assert re.search(r"if \(_cam\[i\]\.connected && now - _cam\[i\]\.last_seen_ms > CAM_STALE_MS\)"
                 r"\s*\n\s*hub_ui_set_disconnected\(i \+ 1\);", sweep), \
    "the display's sweep does not clear a stale tile against CAM_STALE_MS"
print("   clears a stale tile against CAM_STALE_MS, not a figure of its own  OK")

# ---- 2. the hub speaks first ------------------------------------------------------------
print("\n2. the hub says it, and repeats it, before the display gives up alone:")
assert stale >= hub_timeout + hub_repeat, \
    (f"the display gives up after {stale} ms, before the hub's verdict ({hub_timeout} ms) "
     f"and its repeat ({hub_repeat} ms)")
print("   %d >= %d + %d                                              OK" % (stale, hub_timeout, hub_repeat))

# ---- 3. an hour through the display's sweep --------------------------------------------
print("\n3. an hour of idle STATUS through the display's sweep:")


def updates(period, hours=1.0, seed=1):
    """When UPDATE_CAMs land on the display: a STATUS every `period` ms with
    up to 80 ms of jitter, one in 25 lost on the air and one in 40 dropped while
    LVGL held its mutex — far worse than a good day, to leave no doubt."""
    rnd, t, out = random.Random(seed), 0.0, []
    while t < hours * 3600e3:
        t += period + rnd.uniform(-80, 80)
        if rnd.random() < 1 / 25 or rnd.random() < 1 / 40:
            continue
        out.append(t)
    return out


def clears(limit, arrivals, until):
    """The display's sweep: once a second, clear the tile if nothing has
    refreshed it for more than `limit` ms.  The times it cleared a lit tile."""
    out, lit, last, k = [], True, 0.0, 0
    for now in range(0, int(until), 1000):
        while k < len(arrivals) and arrivals[k] <= now:
            last, lit, k = arrivals[k], True, k + 1
        if lit and now - last > limit:
            out.append(now)
            lit = False
    return out


period = max(heartbeat, refresh)                     # the slower stream, alone
idle = updates(period)
old = clears(5000, idle, 3600e3)
assert old, "the model does not reproduce the concert's flicker at the old 5 s"
print("   the old 5 s: %d blanked tiles in an hour, on a mount that never left" % len(old))
new = clears(stale, idle, 3600e3)
assert new == [], f"the display blanks a working mount: at {new[:5]} ms"
print("   CAM_STALE_MS: none                                             OK")

gone = [t for t in idle if t < 1800e3]               # stops at half time, hub silent too
cut = clears(stale, gone, 3600e3)
assert len(cut) == 1 and cut[0] - gone[-1] <= stale + 1000, cut
print("   really gone, with the hub silent too: cleared %.0f s after its last STATUS  OK"
      % ((cut[0] - gone[-1]) / 1000))

print("\nALL CHECKS PASSED")
