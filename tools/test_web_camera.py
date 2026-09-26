"""The web app's camera control, checked against the PC app's.

The phone and the PC drive the same cameras through the same mounts, so they
must send the same bytes and read the same reports the same way.  The web app's
camera code is a port of protocol.py, the two camera dialogs and the colour
wheel, and a port is exactly the thing that drifts.

The JavaScript is EXTRACTED FROM web_app.h (between the camera-pure markers)
and run in node, not transcribed here.  A transcription would only prove that
two copies of my own understanding agree; running the real thing proves the
page the hub serves does.

Also checked, because each would fail silently on the rig:
  - the hub forwards to WebSocket clients exactly the camera parameters the web
    app decodes — a parameter it filters out would just never appear on the
    phone, and one it forwards for nothing is radio time for nothing;
  - the web app reads the health flags at the byte protocol.py packs them at;
  - the colour wheel's puck, ring and drag agree about which way hue goes.

Skips the node half cleanly when node is absent — a missing tool, not a
failure.  Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))
import re, json, math, random, shutil, struct, subprocess, tempfile
from types import SimpleNamespace

from comms import protocol as P
from ui.dialogs.camera_control_dialog import _step, _WB_STEPS
from ui.dialogs.camera_advanced_dialog import _SHUTTERS, _Confirmed
from ui.widgets import colour_wheel as CW

WEB = (REPO / "firmware/shared/web_app.h").read_text()
HUB = (REPO / "firmware/esp32_hub_eth/esp32_hub_eth.ino").read_text()

m = re.search(r"// BEGIN camera-pure\n(.*?)// END camera-pure", WEB, re.S)
assert m, "camera-pure markers not found in web_app.h"
PURE = m.group(1)


def js_list(name):
    mm = re.search(r"const %s\s*=\s*\[([^\]]*)\]" % name, PURE)
    assert mm, f"{name} not found in the camera-pure block"
    return [int(x) for x in re.findall(r"\d+", mm.group(1))]


def js_const(name):
    mm = re.search(r"const %s\s*=\s*(0x[0-9A-Fa-f]+|\d+)\s*;" % name, WEB)
    assert mm, f"const {name} not found in web_app.h"
    return int(mm.group(1), 0)


# ---- 1. the tables and constants the two clients share ---------------------
print("1. web app tables and constants vs the PC app:")
assert js_list("ISO_STEPS") == P.ISO_STEPS, (js_list("ISO_STEPS"), P.ISO_STEPS)
assert js_list("WB_STEPS") == _WB_STEPS, (js_list("WB_STEPS"), _WB_STEPS)
assert js_list("SHUTTERS") == _SHUTTERS, (js_list("SHUTTERS"), _SHUTTERS)
print("   ISO stops, WB stops and shutter speeds identical            OK")
for js, py in (("CMD_HEALTH", P.Cmd.HEALTH), ("CMD_CAM_CONTROL", P.Cmd.CAM_CONTROL),
               ("CMD_CAM_STATUS", P.Cmd.CAM_STATUS),
               ("HEALTH_FLAG_BLE_BUILD", P.HEALTH_FLAG_BLE_BUILD),
               ("HEALTH_FLAG_BLE_LINK", P.HEALTH_FLAG_BLE_LINK),
               ("HEALTH_FLAG_CAM_UNPAIRED", P.HEALTH_FLAG_CAM_UNPAIRED)):
    assert js_const(js) == int(py), (js, js_const(js), int(py))
bridge = [k for k, v in P.HEALTH_NODE_NAMES.items() if v == "bridge"]
assert bridge == [js_const("HEALTH_NODE_BRIDGE")], (bridge, js_const("HEALTH_NODE_BRIDGE"))
print("   command numbers, health flags, bridge node type               OK")

# The flags byte: protocol.py packs the health record as >BBIIIHHbBI, so the
# flags sit after node, reset, uptime, heap, min heap, loop, txfail and rssi.
FLAGS_AT = struct.calcsize(">BBIIIHHb")
assert FLAGS_AT == 19, FLAGS_AT
assert f"buf[off + 7 + {FLAGS_AT}]" in WEB, \
    "the web app does not read the health flags at byte %d" % FLAGS_AT
print(f"   health flags read at byte {FLAGS_AT}, where protocol.py packs them    OK")

# ---- 2. what the hub lets through to a phone --------------------------------
hm = re.search(r"CAM_WS_PARAMS\[\]\[2\]\s*=\s*\{(.*?)\};", HUB, re.S)
assert hm, "CAM_WS_PARAMS not found in esp32_hub_eth.ino"
body = re.sub(r"//[^\n]*", "", hm.group(1))
HUB_PARAMS = {(int(a), int(b)) for a, b in re.findall(r"\{\s*(\d+)\s*,\s*(\d+)\s*\}", body)}
assert HUB_PARAMS, "CAM_WS_PARAMS parsed empty"
print(f"\n2. the hub forwards {len(HUB_PARAMS)} camera parameters to WebSocket clients")
# The forwarding itself, so a tidy-up cannot quietly drop it.
assert re.search(r"pkt\.cmd == CMD_CAM_STATUS\s*&&\s*cam_ws_due\(", HUB), \
    "the relay loop no longer sends CAM_STATUS through cam_ws_due()"
assert "_ws.binaryAll(_cam_health_pkt[msg.src_idx]" in HUB, \
    "the relay loop no longer forwards the bridge's health to WebSocket clients"
assert re.search(r"WS_EVT_CONNECT\) \{.*?_ws_connects = _ws_connects \+ 1", HUB, re.S), \
    "a WebSocket connect no longer resets the camera-report filter"
print("   CAM_STATUS filtered, bridge health forwarded, connect resets    OK")

if not shutil.which("node"):
    print("\n3. node not installed — skipping the browser-side run.")
    print("   (the constant and forwarding checks above still ran)")
    print("\nALL CHECKS PASSED")
    raise SystemExit(0)

# ---- 3. the REAL JavaScript, run in node -----------------------------------
rng = random.Random(20260926)

BUILDERS = [   # (js name, python pkt_cam_*, argument lists)
    ("bmdAutofocus", P.pkt_cam_autofocus, [[]]),
    ("bmdIso", P.pkt_cam_iso, [[100], [800], [1250], [25600], [0], [-1]]),
    ("bmdWhiteBalance", P.pkt_cam_white_balance,
     [[5600, 0], [3200, -10], [10000, 50], [2500, -50], [4321, 7]]),
    ("bmdLift", P.pkt_cam_lift, [[0, 0, 0, 0], [0.1, -0.2, 0.3, 0.0]]),
    ("bmdGamma", P.pkt_cam_gamma, [[1.0, -1.0, 0.5, -0.5]]),
    ("bmdGain", P.pkt_cam_gain, [[1, 1, 1, 1], [16, 0, 1.5, 2.25]]),
    ("bmdContrast", P.pkt_cam_contrast, [[0.5, 1.0], [0.0, 2.0], [1.0, 0.0]]),
    ("bmdLumaMix", P.pkt_cam_luma_mix, [[1.0], [0.0], [0.37]]),
    ("bmdHueSat", P.pkt_cam_hue_sat, [[0.0, 1.0], [-1.0, 2.0], [0.25, 0.8]]),
    ("bmdCcReset", P.pkt_cam_cc_reset, [[]]),
    ("bmdFocus", P.pkt_cam_focus, [[0.0], [0.5], [1.0]]),
    ("bmdIris", P.pkt_cam_iris, [[0.0], [0.62], [1.0]]),
    ("bmdAutoIris", P.pkt_cam_auto_iris, [[]]),
    ("bmdZoomNorm", P.pkt_cam_zoom_norm, [[0.0], [0.33], [1.0]]),
    ("bmdShutterSpeed", P.pkt_cam_shutter_speed, [[s] for s in _SHUTTERS]),
    ("bmdAutoWb", P.pkt_cam_auto_wb, [[]]),
    ("bmdRestoreAutoWb", P.pkt_cam_restore_auto_wb, [[]]),
    ("bmdTransport", P.pkt_cam_transport, [[0], [1], [2]]),
]
# Exact halves of a 5.11 step, where Python's round() and Math.round part
# company; values past the range, which must clamp rather than wrap; and a
# random spread, including every slider position a panel can send.
halves = [(2 * k + 1) / 4096 for k in range(-6, 6)]
edge = [15.99, 16.0, 16.01, -16.0, -16.01, 40.0, -40.0]
spread = [rng.uniform(-20, 20) for _ in range(150)] + [i / 100 for i in range(-200, 201)]
for name, fn in (("bmdFocus", P.pkt_cam_focus), ("bmdIris", P.pkt_cam_iris),
                 ("bmdLumaMix", P.pkt_cam_luma_mix)):
    BUILDERS.append((name, fn, [[v] for v in halves + edge + spread]))
BUILDERS.append(("bmdLift", P.pkt_cam_lift,
                 [[rng.uniform(-3, 3) for _ in range(4)] for _ in range(100)]
                 + [[halves[i], halves[-1 - i], edge[i % len(edge)], 0.0] for i in range(12)]))
BUILDERS.append(("bmdContrast", P.pkt_cam_contrast,
                 [[p / 100, a / 100] for p in range(0, 101, 7) for a in range(0, 201, 13)]))


def bmd_frame(cat, par, dtype, data):
    """A camera report as the mount relays it: BMD framing, unpadded."""
    body = [cat, par, dtype, 0] + list(data)
    return [0xFF, len(body), 0, 0] + body


def le(n, w):
    return list(int(n).to_bytes(w, "little", signed=True))


def f16(v):
    return le(max(-32768, min(32767, int(round(v * 2048)))), 2)


FRAMES = [
    bmd_frame(10, 1, 1, [0, 0, 64, 0, 0]), bmd_frame(10, 1, 1, [2, 0, 64, 0, 0]),
    bmd_frame(10, 1, 1, [1]),
    bmd_frame(0, 7, 2, le(24, 2)), bmd_frame(0, 7, 2, le(-1, 2)),
    bmd_frame(1, 14, 3, le(800, 4)), bmd_frame(1, 14, 3, le(1250, 4)),
    bmd_frame(1, 14, 3, [0x20, 0x03]), bmd_frame(1, 14, 3, [0x40, 0x06, 0x00]),
    bmd_frame(1, 14, 3, [0x20]),
    bmd_frame(1, 2, 2, le(5600, 2) + le(0, 2)), bmd_frame(1, 2, 2, le(3200, 2) + le(-10, 2)),
    bmd_frame(1, 12, 3, le(50, 4)), bmd_frame(1, 12, 3, le(2000, 4)),
    bmd_frame(0, 0, 128, f16(0.4)), bmd_frame(0, 3, 128, f16(0.62)),
    bmd_frame(0, 8, 128, f16(1.0)), bmd_frame(0, 3, 128, f16(-0.1)),
    bmd_frame(8, 0, 128, f16(0.1) + f16(-0.2) + f16(0.3) + f16(0)),
    bmd_frame(8, 1, 128, f16(1) + f16(-1) + f16(0.5) + f16(-0.5)),
    bmd_frame(8, 2, 128, f16(1) + f16(1) + f16(1) + f16(1)),
    bmd_frame(8, 4, 128, f16(0.5) + f16(1.0)), bmd_frame(8, 5, 128, f16(0.9)),
    bmd_frame(8, 6, 128, f16(0.25) + f16(1.2)),
    # parameters the phone does not show: {} there, whatever the PC makes of them
    bmd_frame(1, 9, 2, le(25, 2) + le(25, 2) + le(3840, 2) + le(2160, 2) + le(0, 2)),
    bmd_frame(1, 7, 1, [2]), bmd_frame(1, 13, 1, [12]), bmd_frame(1, 11, 3, le(18000, 4)),
    bmd_frame(1, 16, 128, f16(2)), bmd_frame(8, 3, 128, f16(0) * 4),
    bmd_frame(9, 0, 2, le(11669, 2) + le(98, 2)), bmd_frame(12, 9, 5, list(b"OLYMPUS")),
    # runts
    [0xFF, 4, 0, 0, 1, 14, 3], [], [0xFF],
]
FRAMES += [bmd_frame(0, 2, 128, le(k, 2)) for k in [0, 1, 2048, 4096, 6086, 8192, 16384, -2048]]
FRAMES += [bmd_frame(0, 2, 128, le(rng.randint(-4096, 30000), 2)) for _ in range(300)]
for _ in range(600):
    FRAMES.append(bmd_frame(rng.choice([0, 1, 5, 8, 9, 10, 12]), rng.randint(0, 16),
                            rng.choice([0, 1, 2, 3, 5, 128]),
                            [rng.randint(0, 255) for _ in range(rng.randint(0, 12))]))

STEPS = [[t, c, d] for t in ("ISO", "WB") for d in (1, -1)
         for c in (None, 50, 100, 150, 800, 1000, 1250, 1300, 2500, 5600, 5650,
                   10000, 25600, 30000)]

HUES = [i / 48 for i in range(49)] + [rng.random() for _ in range(80)] + [-0.25, 1.25, 7.3]
MAGS = [0, 0.1, 0.5, 1.0, 2.0]
RGBS = [[rng.uniform(-2, 2) for _ in range(3)] + [rng.choice([0.0, 1.0])] for _ in range(200)]
RGBS += [[0, 0, 0, 0], [1, 1, 1, 1], [0.3, 0.3, 0.3, 0], [1, 0, 0, 0], [0, 1, 0, 0],
         [0.5, 1, 0, 0], [1.2, 1.0, 1.0, 1.0]]
HSV = [[rng.random(), rng.random(), rng.random()] for _ in range(120)] + [[0.5, 0, 0.7]]
RGB01 = [[rng.random(), rng.random(), rng.random()] for _ in range(120)] + [[0.4, 0.4, 0.4]]


def confirmed_script(gated):
    """A random run of the calls the Advanced panel makes on one readout.

    As the panel uses it: only a gated readout (iris) is told what the camera
    says about the control itself, and it is always sent a numeric target."""
    ops, t = [], 0.0
    for _ in range(14):
        t += rng.choice([0.0, 0.3, 1.0, 2.5])
        r = rng.random()
        if r < 0.25:
            ops.append(["mark", round(rng.random(), 2) if gated else None])
        elif r < 0.45 and gated:
            ops.append(["note", round(rng.random(), 2), t])
        else:
            ops.append(["offer", rng.choice([2.8, 4.0, 5.6, 11.0, 24, 35]), t])
    return ops


SCRIPTS = [
    # iris: the camera confirms the control, and only THEN is an f-number new
    [True, [["offer", 2.8, 1], ["mark", 0.5], ["offer", 2.8, 2], ["note", 0.49, 3],
            ["offer", 4.0, 2.5], ["offer", 5.6, 4]]],
    # zoom: no confirmation of the control, so wait for the number to change
    [False, [["offer", 24, 1], ["mark", None], ["offer", 24, 2], ["offer", 35, 3]]],
    # a report heard no later than the last one is the stored value again
    [False, [["offer", 24, 5], ["offer", 35, 5], ["offer", 50, 4]]],
    # outside the tolerance is not confirmation
    [True, [["mark", 0.8], ["note", 0.7, 1], ["offer", 11.0, 2]]],
] + [[g, confirmed_script(g)] for g in [True, False] * 60]

cases = {
    "builders": [[name, args] for name, _fn, arglists in BUILDERS for args in arglists],
    "frames": FRAMES, "steps": STEPS, "hues": HUES, "mags": MAGS, "rgbs": RGBS,
    "hsv": HSV, "rgb01": RGB01, "scripts": SCRIPTS,
}

harness = PURE + r"""
const fs = require('fs');
const C = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const out = {};
out.builders = C.builders.map(([name, args]) => eval(name).apply(null, args));
out.frames = C.frames.map(f => decodeCamStatus(Uint8Array.from(f)));
out.steps = C.steps.map(([t, c, d]) => stepIn(t === 'ISO' ? ISO_STEPS : WB_STEPS, c, d));
out.offsets = C.hues.map(h => C.mags.map(m => hueToOffsets(h, m)));
out.units = C.hues.map(h => hueUnit(h));
out.huemag = C.rgbs.map(([r, g, b, c]) => hueMag(r, g, b, c));
out.hsv = C.hsv.map(([h, s, v]) => hsvToRgb(h, s, v));
out.rgb = C.rgb01.map(([r, g, b]) => rgbToHsv(r, g, b));
// Direction: every hue, placed on the wheel and read back, is itself; and
// the order runs counter-clockwise from red at the top.
out.roundtrip = C.hues.map(h => { const a = wheelAngle(h); return wheelHue(Math.cos(a), Math.sin(a)); });
out.at = [0, 1/6, 1/3, 1/2, 2/3, 5/6].map(h => [Math.cos(wheelAngle(h)), Math.sin(wheelAngle(h))]);
out.scripts = C.scripts.map(([gated, ops]) => {
    const c = new Confirmed(gated), trace = [];
    for (const op of ops) {
        if (op[0] === 'mark') c.markSent(op[1] === null ? undefined : op[1]);
        else if (op[0] === 'note') c.noteSetting(op[1], op[2]);
        else c.offer(op[1], op[2]);
        trace.push([c.value, c.confirmed]);
    }
    return trace;
});
// Which (category, parameter) pairs decode to anything, whatever the type
const seen = [];
for (let cat = 0; cat < 16; cat++) for (let par = 0; par < 32; par++) {
    for (const t of [1, 2, 3, 128]) {
        const f = [0xFF, 12, 0, 0, cat, par, t, 0, 1, 2, 3, 4, 5, 6, 7, 8];
        if (Object.keys(decodeCamStatus(Uint8Array.from(f))).length) { seen.push([cat, par]); break; }
    }
}
out.decodes = seen;
console.log(JSON.stringify(out));
"""
tmp = pathlib.Path(tempfile.mkdtemp())
(tmp / "h.js").write_text(harness)
(tmp / "cases.json").write_text(json.dumps(cases))
r = subprocess.run(["node", str(tmp / "h.js"), str(tmp / "cases.json")],
                   capture_output=True, text=True)
assert r.returncode == 0, f"node failed:\n{r.stderr[:1500]}"
J = json.loads(r.stdout)


def close(a, b, tol=1e-12):
    if isinstance(a, (list, tuple)):
        return len(a) == len(b) and all(close(x, y, tol) for x, y in zip(a, b))
    if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None:
        return a == b
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


print("\n3. web_app.h JavaScript in node vs the PC app:")

# 3a. every command, byte for byte
i = 0
for name, fn, arglists in BUILDERS:
    for args in arglists:
        want = list(fn(3, *args)[7:-2])      # payload of the CAM_CONTROL packet
        got = J["builders"][i]
        assert got == want, (name, args, got, want)
        i += 1
print(f"   {i} camera commands built byte for byte as protocol.py builds them")

# 3b. every report decoded to the same values — or, for a parameter the
# phone does not show, to nothing
NOT_SHOWN = {"rec_format", "dynamic_range", "gain_db", "shutter_angle", "nd", "offset"}
shown = ignored = 0
for frame, got in zip(FRAMES, J["frames"]):
    want = P.decode_cam_status(bytes(frame))
    want = {k: list(v) if isinstance(v, tuple) else v for k, v in want.items()}
    if set(want) & NOT_SHOWN:
        assert got == {}, (frame, got, want)
        ignored += 1
        continue
    assert set(got) == set(want), (frame, got, want)
    for k in want:
        assert close(got[k], want[k]), (frame, k, got[k], want[k])
    shown += bool(want)
print(f"   {len(FRAMES)} camera reports: {shown} decoded identically, "
      f"{ignored} not shown on the phone, the rest nothing in both")

# 3c. the everyday dialog's ISO / WB steps
for (t, c, d), got in zip(STEPS, J["steps"]):
    want = _step(P.ISO_STEPS if t == "ISO" else _WB_STEPS, c, d)
    assert got == want, (t, c, d, got, want)
print(f"   {len(STEPS)} ISO / WB steps land on the same stop")

# 3d. the colour wheel's arithmetic
import colorsys
for h, row in zip(HUES, J["offsets"]):
    for mg, got in zip(MAGS, row):
        assert close(got, CW._hue_to_offsets(h, mg)), (h, mg, got)
for h, got in zip(HUES, J["units"]):
    assert close(got, CW._hue_unit(h)), (h, got, CW._hue_unit(h))
for (rr, gg, bb, c), got in zip(RGBS, J["huemag"]):
    want = CW.ColourWheel._hue_mag(SimpleNamespace(_r=rr, _g=gg, _b=bb, _centre=c))
    assert close(got, want, 1e-9), ((rr, gg, bb, c), got, want)
for (h, s, v), got in zip(HSV, J["hsv"]):
    assert close(got, colorsys.hsv_to_rgb(h, s, v)), (h, s, v, got)
for (rr, gg, bb), got in zip(RGB01, J["rgb"]):
    assert close(got, colorsys.rgb_to_hsv(rr, gg, bb)), (rr, gg, bb, got)
print("   hue offsets, hue unit, puck from values, HSV both ways          OK")

# 3e. which way round the wheel goes: a vectorscope's order, counter-clockwise
# from red at the top — red, yellow, green, cyan, blue, magenta
for h, back in zip(HUES, J["roundtrip"]):
    assert close(back, h % 1.0, 1e-9) or close(abs(back - h % 1.0), 1.0, 1e-9), (h, back)
(rx, ry), (yx, yy), (gx, gy), (cx, cy), (bx, by), (mx, my) = J["at"]
assert abs(rx) < 1e-9 and ry < 0, "red is not at the top"
assert yx < 0 and yy < 0, "yellow is not up and to the LEFT"
assert gx < 0 and gy > 0, "green is not down and to the left"
assert abs(cx) < 1e-9 and cy > 0, "cyan is not at the bottom"
assert bx > 0 and by > 0 and mx > 0 and my < 0, "blue / magenta are not on the right"
# ...and the ring, the puck and a drag all go through those two functions.
wheel = WEB[WEB.index("class CcWheel"):]
wheel = wheel[:wheel.index("\n}\n")]
assert "wheelAngle(t0)" in wheel and "wheelAngle(hm[0])" in wheel and "wheelHue(dx, dy)" in wheel, \
    "the ring, the puck and the drag no longer share wheelAngle / wheelHue"
print("   hue round-trips the wheel; R top, Y G left, C bottom, B M right OK")

# 3f. the f-number and focal-length readouts: shown only once known to be new
for (gated, ops), got in zip(SCRIPTS, J["scripts"]):
    c = _Confirmed(gated=gated)
    for op, (gv, gc) in zip(ops, got):
        if op[0] == "mark":
            c.mark_sent(op[1])
        elif op[0] == "note":
            c.note_setting(op[1], op[2])
        else:
            c.offer(op[1], op[2])
        assert (gv, gc) == (c.value, c.confirmed), (gated, ops, got)
print(f"   {len(SCRIPTS)} readout histories agree step by step with _Confirmed")

# ---- 4. the hub forwards exactly what the phone reads ------------------------
decodes = {tuple(p) for p in J["decodes"]}
missing = decodes - HUB_PARAMS
extra = HUB_PARAMS - decodes
assert not missing, f"the web app shows {sorted(missing)} but the hub never forwards them"
assert not extra, f"the hub forwards {sorted(extra)} to phones, which decode none of it"
print(f"\n4. the hub's {len(HUB_PARAMS)} forwarded parameters are exactly the "
      f"ones the web app decodes   OK")

print("\nALL CHECKS PASSED")
