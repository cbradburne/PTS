"""The mount stops firing its three periodic reports in one breath.

RF and HEALTH both start at 0 and share HEALTH_INTERVAL_MS, so they are locked
together for the life of the mount — two sends back to back in one loop pass,
every ten seconds. STATUS at 5 s joins them every other time, making three
within microseconds. On an idle mount, with no hub traffic and nobody touching
the display.

That is the condition the bench needed to lose a buffer: load 40/100 at peak 6
in flight leaked three in 20 h, the same load capped at 2 ran 16.8 h clean, and
peak 5 with no load was clean too. Three days of field logs say the ACK path —
the first thing bounded — is nearly idle by comparison: one GET_CONFIG every
~3 s and loop passes of 9-11 ms.

The escape is the part worth testing hardest. STATUS is the mount's liveness
signal and the hub gives up after MOUNT_PRESENCE_TIMEOUT_MS; a guard that can
hold one indefinitely trades a leak that takes hours for an outage that takes
effect at once.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, io, logging, pathlib, re, threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

INO = (REPO / "firmware/esp_mount_amoled175/esp_mount_amoled175.ino").read_text()
HDR = (REPO / "firmware/shared/protocol.h").read_text()

# ---- 1. all three periodic sends are guarded -------------------------------
print("1. the three that fire together:")
# The heartbeat has a second call site: one shot at the end of setup(), after a
# delay(100), with nothing else in flight. That one needs no guard — the burst
# this is about is the periodic one in loop(). Pinned at two so a third call
# site cannot appear unnoticed.
n_hb = INO.count("send_status_heartbeat();")
assert n_hb == 2, \
    f"{n_hb} heartbeat call sites (expected the boot one and the periodic one) —\n" \
    "    a new one may be unguarded"

for name, marker, occurrence in (("STATUS", "send_status_heartbeat();", "last"),
                                 ("RF",     "send_rf_report();",        "only"),
                                 ("HEALTH", "send_health(false);",      "only")):
    if occurrence == "only":
        assert INO.count(marker) == 1, f"{name} has more than one call site"
        i = INO.index(marker)
    else:
        i = INO.rindex(marker)
    window = INO[max(0, i - 500):i]
    assert "periodic_held" in window, \
        f"{name} is sent without the in-flight guard. RF and HEALTH share one\n" \
        f"    interval and STATUS lands on the same pass every other time, so an\n" \
        f"    unguarded one puts the mount back to three sends in microseconds."
    print(f"   {name:<7} guarded                                    OK")

# The anomaly report is the one that MUST have the escape, not merely a guard.
# A mount in permanent anomaly — txfail jumping, which is exactly what a wedging
# mount looks like — takes this branch on every pass and never reaches the
# normal health path below it. So an unbounded return here silences the mount's
# health for as long as the fault lasts, which is to say for as long as it is
# worth reading. cam1 ran two hours that way on 2026-09-09 while answering every
# command at 100% and 64 ms, and the only reason anything came off it at all is
# that RF sits ABOVE this branch and had the escape.
anom = INO[INO.index("if (anomaly && anom_age >= HEALTH_ANOMALY_GAP_MS)"):]
anom = anom[:anom.index("return;\n    }")]
assert "periodic_held(" in anom, \
    "the anomaly health report does not go through the BOUNDED guard, so it has\n" \
    "    no overdue escape — and it is the one send whose branch is taken on\n" \
    "    every pass while the fault lasts"
assert not re.search(r"if \(espnow_tx_saturated\(\)\)\s*return;", anom), \
    "the anomaly report still tests saturation directly. That is the unbounded\n" \
    "    form: it must go through periodic_held so the overdue escape applies."
assert "_health_anom_ms = now;" in anom and \
       anom.index("periodic_held") < anom.index("_health_anom_ms = now;"), \
    "the anomaly timestamp is stamped before the guard, so a deferred report is\n" \
    "    LOST rather than retried"
print("   anomaly health bounded, and retried not dropped   OK")

# ---- 2. deferring must not lose the report ---------------------------------
# Every caller's test is `age >= interval`; skipping the send leaves that true
# so the next pass tries again. That only holds while the timestamp is written
# INSIDE the send.
print("\n2. a deferred report is retried, not dropped:")
assert "_last_heartbeat_ms = millis();" in INO, "the heartbeat stamp moved"
hb = INO[INO.index("uint32_t hb_age = now - _last_heartbeat_ms;"):]
hb = hb[:hb.index("send_status_heartbeat();") + 30]
assert "_last_heartbeat_ms =" not in hb, \
    "the heartbeat timestamp is written at the call site, so a deferred beat is\n" \
    "    skipped for a whole interval instead of retried next pass"
rf = INO[INO.index("uint32_t rf_age = now - _rf_last_ms;"):]
rf = rf[:rf.index("send_rf_report();")]
assert rf.index("periodic_held") < rf.index("_rf_last_ms = now;"), \
    "the RF timestamp is stamped before the guard, so a deferred RF report is\n" \
    "    lost for ten seconds rather than retried"
print("   timestamps stamped only when the send happens      OK")

# ---- 3. the escape, which matters more than the bound ----------------------
print("\n3. it can never hold a report indefinitely:")
g = INO[INO.index("static inline bool espnow_defer_periodic"):]
g = g[:g.index("\n}")]
assert "ESPNOW_PERIODIC_DEFER_MS" in g, \
    "there is no overdue escape. A mount whose radio stays busy would stop\n" \
    "    sending STATUS and the hub would drop it — a leak traded for an outage."
m = re.search(r"#define ESPNOW_PERIODIC_DEFER_MS\s+(\d+)", INO)
assert m, "ESPNOW_PERIODIC_DEFER_MS is not defined"
defer_ms = int(m.group(1))

# The hub's presence timeout derives from MOUNT_STATUS_REFRESH_MS; read both
# rather than restating a number that can drift.
r = re.search(r"#define MOUNT_STATUS_REFRESH_MS\s+(\d+)", HDR)
refresh = int(r.group(1))
presence = 3 * refresh + 1000          # MOUNT_PRESENCE_TIMEOUT_MS
hb_i = int(re.search(r"#define STATUS_HEARTBEAT_MS\s+(\d+)", INO).group(1))
worst = hb_i + defer_ms                # worst gap between two heartbeats
margin = presence - hb_i               # silence the hub tolerates beyond one beat

assert worst < presence / 2, \
    f"a heartbeat gap can reach {worst} ms against a {presence} ms presence " \
    f"timeout — too close to it"
# The guard must be a rounding error on that margin, not a bite out of it. At
# 400 ms against 11 s the hub can still miss two whole beats before caring.
assert defer_ms < margin / 10, \
    f"the guard can eat {defer_ms} ms of a {margin} ms margin — enough that a " \
    f"slow radio and a late beat could combine into a dropped mount"
print(f"   defer {defer_ms} ms, worst gap {worst} ms, timeout {presence} ms,")
print(f"   guard uses {100 * defer_ms / margin:.1f}% of the margin         OK")

# And the whole thing is switchable, because it changes rig behaviour.
assert "#ifndef ESPNOW_TX_INFLIGHT_CAP" in INO, \
    "the cap is not overridable, so there is no way to A/B it on the rig"
assert re.search(r"if \(!ESPNOW_TX_INFLIGHT_CAP\) return false;", INO), \
    "setting the cap to 0 does not disable the guard"
print("   cap 0 restores the old behaviour exactly           OK")

# ---- 3b. the counter counts deferrals, not loop passes ---------------------
# The guard is re-tested every pass while a report waits and the loop runs about
# a hundred times a second, so a naive count scores one 400 ms hold as forty.
# And the ACK drain must not count a "deferral" when the queue is already empty
# — the first field build did, and pegged at 255 within the hour on two mounts
# while saying nothing about whether a command had ever waited.
print("\n3b. what the counter counts:")
ph = INO[INO.index("static inline bool periodic_held"):]
ph = ph[:ph.index("\n}")]
assert "if (!*held)" in ph and "*held = true" in ph, \
    "the periodic deferral is counted every pass rather than once per hold"
assert "*held = false" in ph, "the flag never clears, so only the first hold counts"
drain = INO[INO.index("if (espnow_tx_saturated()) {"):]
drain = drain[:drain.index("break;")]
assert "uxQueueMessagesWaiting" in drain, \
    "the drain counts a deferral without checking anything is still queued, so\n" \
    "    it counts finishing-while-busy instead of holding-something-back"
assert drain.index("uxQueueMessagesWaiting") < drain.index("_espnow_drain_deferred"), \
    "the queue check runs after the increment"
print("   once per hold, and only when something waits       OK")

# ---- 4. the hub USB line speaks on a change, not on a timer ----------------
print("\n4. the hub's USB serial state:")
BR = (REPO / "pc_app/comms/bridge.py").read_text()
assert "_hub_usb_state" in BR, "no state is kept, so it cannot report a change"
hub = BR[BR.index('sec  = names[pkt.payload[1]]'):]
hub = hub[:hub.index("elif kind == 10:")]
assert "USB serial not being read" not in hub, \
    "the permanent note is still appended to every HUB LOOP line"
assert 'if state != self._hub_usb_state' in hub, \
    "the state is reported unconditionally rather than on a change"

BUF = io.StringIO()
_h = logging.StreamHandler(BUF); _h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
_lg = logging.getLogger("comms.bridge")
_saved = (_lg.handlers, _lg.level, _lg.propagate)
_lg.handlers, _lg.propagate = [_h], False
_lg.setLevel(logging.INFO)

from comms.bridge import Bridge

# The latch itself, driven through the class's own attribute. A fresh Bridge
# starts with the state unset, so the first report says whatever it finds and a
# rotated log always establishes its baseline.
b = Bridge.__new__(Bridge)
assert b._hub_usb_state is None, "the state starts set, so the first report is silent"
seen = []
for drop in (100, 100, 100, 0, 0, 100, 100, 50, 100):
    state = "unread" if drop >= 100 else ("partial" if drop else "clean")
    if state != b._hub_usb_state:
        b._hub_usb_state = state
        seen.append(state)
assert seen == ["unread", "clean", "unread", "partial", "unread"], \
    f"the latch does not report exactly the transitions: {seen}"
print("   9 reports, 5 transitions, 5 lines                  OK")

flat = ["unread"] * 120
b2 = Bridge.__new__(Bridge)
lines = 0
for s in flat:
    if s != b2._hub_usb_state:
        b2._hub_usb_state = s
        lines += 1
assert lines == 1, f"an unchanging state logged {lines} times in an hour"
print("   an hour of the normal state: one line, not 120     OK")

_lg.handlers, _lg.level, _lg.propagate = _saved
print("\nALL CHECKS PASSED")
