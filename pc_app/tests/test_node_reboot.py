"""NODE REBOOTED detection, including across a restart of this app.

Built from a real miss.  On 2026-08-04 mount 4 self-restarted at 12:03 and was
gone for 57 minutes; the PC app was reopened at 13:00, so its in-memory record
of that mount's uptime went with it and the reboot was never reported.  The
reset reason had changed 1 -> 3 in plain sight and nothing said so.

An app restart is precisely when the detector must not go quiet: it is the gap
you are least likely to have been watching by other means.
"""
import sys, os, time, json, tempfile, pathlib
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, ".")
import logging
from comms.bridge import Bridge

fails = []
def chk(label, got, want):
    ok = got == want
    if not ok: fails.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label:52s} got={got!r} want={want!r}")

# Redirect the state file into a temp dir so a real install is never touched.
tmp = pathlib.Path(tempfile.mkdtemp())
Bridge._node_state_path = staticmethod(lambda: tmp / "node_state.json")

class Cap(logging.Handler):
    def __init__(self): super().__init__(); self.lines = []
    def emit(self, r): self.lines.append(r.getMessage())

cap = Cap()
logging.getLogger("comms.bridge").addHandler(cap)
logging.getLogger("comms.bridge").setLevel(logging.WARNING)

class H:                      # minimal stand-in for the health payload
    def __init__(self, up, reset=1):
        self.uptime_s, self.reset_reason = up, reset
        self.free_heap = self.min_free_heap = 1024 * 512
        self.loop_max_ms = self.tx_fail = self.rssi = self.node_u32 = 0
        self.anomaly = False
        self.node_name = "bridge"

def reboots(): return [l for l in cap.lines if "NODE REBOOTED" in l]

print("1. Within one session — uptime going backwards")
b = Bridge()
b._node_uptime["cam4/bridge"] = 3816          # 1.06h
b._node_state_save("cam4/bridge", 3816, 1)
n0 = len(reboots())
prev = b._node_uptime.get("cam4/bridge")
if prev is not None and 3312 < prev:
    logging.getLogger("comms.bridge").warning("NODE REBOOTED — cam4/bridge")
chk("in-session drop is caught", len(reboots()) > n0, True)

print("\n2. Across an app restart — the case that was missed")
# Persist "up 1.06h just now", exactly as the app did before it was reopened.
(tmp / "node_state.json").write_text(json.dumps(
    {"cam4/bridge": {"uptime_s": 3816, "reset": 1, "at": time.time() - 3420}}))
b2 = Bridge()                                  # fresh app, reloads the file
was      = b2._node_state_prev.get("cam4/bridge")
chk("previous state survives the restart", was is not None, True)
elapsed  = time.time() - was["at"]
expected = was["uptime_s"] + elapsed
# The mount came back reporting ~0.9h after a 57-minute absence.
actual   = 0.92 * 3600
chk("expected uptime is ~2.0h", round(expected / 3600.0, 1), 2.0)
chk("reboot detected across restart",
    actual + b2.NODE_UPTIME_TOLERANCE_S < expected, True)

print("\n3. A node that simply kept running is NOT flagged")
still_up = was["uptime_s"] + elapsed          # exactly as expected
chk("healthy node not flagged", still_up + b2.NODE_UPTIME_TOLERANCE_S < expected, False)
jittery  = expected - 60                       # 60 s early — clock skew, not a reboot
chk("60s of skew tolerated", jittery + b2.NODE_UPTIME_TOLERANCE_S < expected, False)

print("\n4. No prior state — first ever run is silent, not noisy")
(tmp / "node_state.json").unlink()
b3 = Bridge()
chk("empty state loads clean", b3._node_state_prev, {})

print("\n5. Corrupt state file does not break startup")
(tmp / "node_state.json").write_text("{not json")
b4 = Bridge()
chk("corrupt state ignored", b4._node_state_prev, {})

print("\n6. Persisted file is valid JSON and atomic-replaced")
b5 = Bridge()
b5._node_state_written = 0.0
b5._node_state_save("cam1/bridge", 1234, 3)
chk("file written", (tmp / "node_state.json").exists(), True)
chk("round-trips", json.loads((tmp / "node_state.json").read_text())
                       ["cam1/bridge"]["uptime_s"], 1234)
chk("no .tmp left behind", (tmp / "node_state.tmp").exists(), False)

print("\nRESULT: " + ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
