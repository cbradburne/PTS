"""The web app's speed dials are off until a connected mount reports its speeds.

The operator, 2026-10-14: "When the webapp loads both dials are set to 2 ...
When I see them both set to 2 I believe the mount is active and working."  The
dials started at a lit 2 before anything had been heard, and never went off for
a mount that was not there.  Now every dial draws off (grey, a dash, nothing
lit) unless the selected mount is connected, and an off dial takes no taps.
Jogs keep carrying the last speed: only the dials go off.

This renders the real page, EXTRACTED FROM web_app.h, in WebKit, the iPad's
engine, and reads every dial (portrait, game-controller view, Extended page,
landscape arcs) by its state and by the pixels it drew:

  1. at load, nothing heard: all off, nothing lit, taps send nothing, and a
     jog still carries a real speed;
  2. a STATUS from the selected mount: its speeds, lit, and taps work;
  3. another camera, never heard: off; back to the first: on again;
  4. the mount silent past the presence timeout: off again;
  5. a mount with no slider: pan/tilt on, the slider dial off and deaf;
  6. heard again after silence from a STATUS too short to carry speeds: on.

tools/wkshot.swift does the rendering (macOS: swiftc and WebKit).  It skips
cleanly where swiftc is absent: a missing tool, not a failure.  Run directly,
or via tools/run_tests.sh with the rest.
"""
import json, pathlib, re, shutil, subprocess, sys, tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
WEB = (REPO / "firmware/shared/web_app.h").read_text()

if sys.platform != "darwin" or not shutil.which("swiftc"):
    print("swiftc and WebKit not here (macOS only): skipping the dial run.")
    print("\nALL CHECKS PASSED")
    sys.exit(0)

m = re.search(r'R"rawhtml\((.*)\)rawhtml"', WEB, re.S)
assert m, "the page's raw string not found in web_app.h"

SETUP = r"""
const _sent = [];
wsSend = b => _sent.push(Array.from(b));          // the hub, as far as this page knows
const _DIALS = {dialPT, dialSZ, gcDialPT, gcDialSZ, extPosDialPT, extPosDialSZ};
const _ARCS  = {arcPT, arcSZ};
const _rgb = h => { const n = parseInt(h.slice(1), 16); return [n >> 16, (n >> 8) & 255, n & 255]; };
// Pixels drawn in the dial's lit colour.
function _lit(cv, rgb) {
    const d = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data;
    let k = 0;
    for (let i = 0; i < d.length; i += 4)
        if (d[i + 3] > 200 && Math.abs(d[i] - rgb[0]) + Math.abs(d[i + 1] - rgb[1])
                              + Math.abs(d[i + 2] - rgb[2]) < 40) k++;
    return k;
}
function snap() {
    for (const a of Object.values(_ARCS)) a.resize(240);   // only a laid-out arc draws
    const o = {};
    for (const [k, d] of Object.entries(_DIALS)) o[k] = [d.preset, _lit(d.cv, [0, 174, 239])];
    for (const [k, a] of Object.entries(_ARCS))  o[k] = [a.preset, _lit(a.cv, _rgb(a.color))];
    return o;
}
function status(cam, pt, sl, slider, short) {
    const p = new Uint8Array(short ? 2 : 11);
    p[1] = slider ? FLAG_HAS_SLIDER : 0;
    if (!short) { p[2] = pt; p[3] = sl; p[8] = 0xFF; p[9] = 0xFF; }
    _onOnePkt(buildPkt(cam, CMD_STATUS, p), 0);
}
// A tap on every dial: [mount, group, preset] of each speed change it sent.
function taps() {
    _sent.length = 0;
    for (const d of [...Object.values(_DIALS), ...Object.values(_ARCS)])
        d.cv.dispatchEvent(new MouseEvent('click', {bubbles: true}));
    return _sent.filter(b => b[6] === CMD_SET_ACTIVE_PRESET).map(b => [b[3], b[7], b[8]]);
}
const jog = () => Array.from(mkJog(1, 0, 0, 0, 0)).slice(15, 17);   // its [pt, sz] speeds
const pick = cam => document.querySelector('#p-cam-bar .cam-btn[data-cam="' + cam + '"]').click();
const silence = cam => { _stTime[cam] = Date.now() - MOUNT_PRESENCE_TIMEOUT_MS - 1; checkPresence(); };

// Each step records what it saw, or why it could not run, so a page that
// breaks one step still reports the steps before it.
const R = window.__results = {stepErrors: []};
window.__measure = () => Object.assign({errors: window.__errors}, window.__results);
function step(f) { try { f(); } catch (e) { R.stepErrors.push(String(e)); } }
step(() => { R.load = snap(); R.loadTaps = taps(); R.loadJog = jog(); });
step(() => { status(1, 3, 1, true); R.on = snap(); R.onTaps = taps(); });
step(() => { pick(2); R.other = snap(); R.otherTaps = taps(); pick(1); R.back = snap(); });
step(() => { silence(1); R.lost = snap(); R.lostTaps = taps(); R.lostJog = jog(); });
step(() => { status(1, 4, 2, false); R.noSlider = snap(); R.noSliderTaps = taps(); });
step(() => { silence(1); status(1, 0, 0, true, true); R.short = snap(); });
"""

tmp = pathlib.Path(tempfile.mkdtemp(prefix="wkdials-"))
page = tmp / "webapp.html"
page.write_text(m.group(1))
(tmp / "setup.js").write_text(SETUP)
tool = tmp / "wkshot"
r = subprocess.run(["swiftc", "-O", str(REPO / "tools/wkshot.swift"), "-o", str(tool)],
                   capture_output=True, text=True)
assert r.returncode == 0, "wkshot.swift does not compile:\n" + r.stderr[-2000:]
rr = subprocess.run([str(tool), str(page), "390", "844", "-", str(tmp / "setup.js")],
                    capture_output=True, text=True, timeout=60)
shutil.rmtree(tmp, ignore_errors=True)
assert rr.returncode == 0, f"wkshot failed ({rr.returncode}): {rr.stderr.strip()}"
got = json.loads(rr.stdout.strip().splitlines()[-1])
assert not got["errors"], f"the page threw: {got['errors']}"


class Steps(dict):
    """A step that threw left its results out: say why, not KeyError."""
    def __missing__(self, key):
        raise AssertionError(f"the step that reads {key!r} did not run: {got['stepErrors']}")


d = Steps(got)

PT = ["dialPT", "gcDialPT", "extPosDialPT", "arcPT"]
SZ = ["dialSZ", "gcDialSZ", "extPosDialSZ", "arcSZ"]
NAMES = {"dialPT": "portrait Pan/Tilt", "dialSZ": "portrait Slider",
         "gcDialPT": "GC-view Pan/Tilt", "gcDialSZ": "GC-view Slider",
         "extPosDialPT": "Extended Pan/Tilt", "extPosDialSZ": "Extended Slider",
         "arcPT": "landscape Pan/Tilt arc", "arcSZ": "landscape Slider arc"}
assert sorted(d["load"]) == sorted(PT + SZ), f"not every dial read: {sorted(d['load'])}"


def off(when, snap, which=PT + SZ):
    for k in which:
        preset, lit = snap[k]
        assert preset == 0 and lit == 0, \
            f"{when}: the {NAMES[k]} dial shows {preset} ({lit} px lit) — want off"


def on(when, snap, pt, sz):
    for keys, want in ((PT, pt), (SZ, sz)):
        for k in keys:
            preset, lit = snap[k]
            assert preset == want and lit > 0, \
                f"{when}: the {NAMES[k]} dial shows {preset} ({lit} px lit) — want {want}, lit"


def speed(jog, when):
    assert all(1 <= s <= 4 for s in jog), f"{when}: a jog carried speeds {jog} — want 1-4"


print("1. at load, nothing heard:")
off("at load", d["load"])
assert d["loadTaps"] == [], f"at load, a tap on an off dial sent {d['loadTaps']}"
speed(d["loadJog"], "at load")
print("   all eight dials off, nothing lit, taps send nothing        OK")
print(f"   a jog still carries a real speed {d['loadJog']}                OK")

print("\n2. a STATUS from the selected mount (pan/tilt 3, slider 1):")
on("after its STATUS", d["on"], 3, 1)
want = sorted([[1, 0, 4]] * 4 + [[1, 1, 2]] * 4)
assert sorted(d["onTaps"]) == want, f"taps on a live mount sent {d['onTaps']}, want {want}"
print("   every dial shows its speed, lit; a tap asks for the next    OK")

print("\n3. another camera, never heard, then back:")
off("camera 2 selected", d["other"])
assert d["otherTaps"] == [], f"camera 2 (never heard): a tap sent {d['otherTaps']}"
on("back on camera 1", d["back"], 3, 1)
print("   camera 2: off and deaf; camera 1 again: its speeds        OK")

print("\n4. the mount silent past the presence timeout:")
off("after the presence timeout", d["lost"])
assert d["lostTaps"] == [], f"a silent mount's dial sent {d['lostTaps']}"
speed(d["lostJog"], "after the presence timeout")
print(f"   all off and deaf; jogs keep the last speed {d['lostJog']}        OK")

print("\n5. a mount with no slider (pan/tilt 4):")
for k in PT:
    assert d["noSlider"][k][0] == 4 and d["noSlider"][k][1] > 0, \
        f"no slider: the {NAMES[k]} dial shows {d['noSlider'][k]}"
off("no slider", d["noSlider"], SZ)
want = [[1, 0, 1]] * 4
assert sorted(d["noSliderTaps"]) == want, \
    f"no slider: taps sent {d['noSliderTaps']}, want only pan/tilt {want}"
print("   pan/tilt on; the slider dials off, and a tap on them sends nothing  OK")

print("\n6. heard again from a STATUS too short to carry speeds:")
for k in PT:
    assert d["short"][k][0] == 4 and d["short"][k][1] > 0, \
        f"short STATUS after silence: the {NAMES[k]} dial shows {d['short'][k]}, want 4"
for k in SZ:
    assert d["short"][k][0] == 2 and d["short"][k][1] > 0, \
        f"short STATUS after silence: the {NAMES[k]} dial shows {d['short'][k]}, want 2"
print("   back on, with the last speeds it reported                  OK")

print("\nALL CHECKS PASSED")
