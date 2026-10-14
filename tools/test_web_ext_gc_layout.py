"""GC on the Extended view's Home page, laid out by WebKit, the iPad's engine.

On 2026-10-14 the operator put an iPad mini in a GameSir G8+: the pad does the
moving, so the Extended view's faders and stick were dead space, and it had no
GC switch to clear them.  Asked for: the faders and the stick gone, the speed
dials to the sides, the cameras and CLEAR / RUN / SET lower, and the positions
stretched into the room given back.  The first cut was "stretched a bit too
far": the positions shorter again, a bigger gap between the cameras and the
buttons, the dials in toward the centre a bit.  Then: CLEAR / RUN / SET lifted
off the bottom of the screen, and the gap down from the positions to the
cameras the same as theirs up from the bottom.  Where things land is layout,
which reading the source cannot show, so this renders the real page, EXTRACTED
FROM web_app.h, in a WKWebView at the sizes it is used at, and measures it:

  - GC on: no faders, no stick; the two dials at the strip's ends, in a little
    from the edges; the cameras between them; CLEAR, RUN, SET under the
    cameras with a clear gap, in that order, centred; nothing overlapping; all
    of it on screen;
  - GC on: CLEAR / RUN / SET well up off the bottom, and the cameras the same
    distance down from the positions;
  - GC on: the strip is shorter and the positions grow by what it gave up, but
    the strip keeps its gap: the positions do not take it all;
  - GC off: the page as it was: faders left, stick right, dials either side
    of the buttons, and the stick sized again when GC is switched off;
  - the switch is remembered, shows when it is on, and is restored at start;
  - switching on lets go of the on-screen controls and stops their jog;
  - no script errors.

tools/wkshot.swift does the rendering (macOS: swiftc and WebKit).  It skips
cleanly where swiftc is absent: a missing tool, not a failure.  Set WKSHOT_DIR
to a folder to keep a picture of every size.  Run directly, or via
tools/run_tests.sh with the rest.
"""
import json, os, pathlib, re, shutil, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor

REPO = pathlib.Path(__file__).resolve().parent.parent
WEB = (REPO / "firmware/shared/web_app.h").read_text()

# ---- 0. the source: wired, remembered, safe to switch -------------------------
print("0. the switch, in the source:")
assert re.search(r"<button class=\"view-toggle-btn\" id=\"btn-ext-gc\">&#127918; GC</button>", WEB), \
    "the Extended view has no GC switch: the operator's complaint"
assert re.search(r"getElementById\('btn-ext-gc'\)\.addEventListener\('click',\s*"
                 r"\(\) => setExtGc\(!_extGc\)\);\s*setExtGc\(_loadExtGcPref\(\)\);", WEB), \
    "the switch must toggle GC, and the page must start the way it was left"
fn = WEB[WEB.find("function setExtGc(on) {"):WEB.find("function setExtView(on) {")]
assert "localStorage.setItem(_EXT_GC_KEY, on ? '1' : '0')" in fn, \
    "the switch is not remembered: an iPad left in its pad would come back to the stick"
assert re.search(r"if \(on\) \{.*?\[extPosJoy, extPosHslZoom, extPosHslSlider\]\s*"
                 r"\.forEach\(w => w && w\.cancel && w\.cancel\(\)\);\s*_stopJogLoop\(\);", fn, re.S), \
    "switching GC on must let go of the stick and faders and stop their jog: a\n" \
    "    control hidden mid-drag would keep its capture and keep the mount moving"
assert re.search(r"\} else \{\s*requestAnimationFrame\(sizeExtPosControls\);", fn), \
    "switching GC off must size the stick again: it was hidden, so never sized"
print("   wired, remembered, restored at start; lets go of the controls; resizes after")

if sys.platform != "darwin" or not shutil.which("swiftc"):
    print("swiftc and WebKit not here (macOS only): skipping the layout run.")
    print("\nALL CHECKS PASSED")
    sys.exit(0)

m = re.search(r'R"rawhtml\((.*)\)rawhtml"', WEB, re.S)
assert m, "the page's raw string not found in web_app.h"

# Standalone, below the status bar: the operator's iPad mini both ways up, and
# the 11" and 13" iPads the Extended view was built on.
SIZES = {
    "iPad mini landscape": (1133, 720),
    "iPad mini portrait":  (744, 1109),
    'iPad 11" landscape':  (1180, 796),
    'iPad 11" portrait':   (820, 1156),
    'iPad 13" landscape':  (1376, 1008),
    'iPad 13" portrait':   (1032, 1352),
}
IN_MIN = 24     # px: a dial "in a bit" from the strip's edge is at least this far in
IN_MAX = 0.10   # ...and no further than this share of the strip's width: still at the side
GAP_MIN = 32    # px: the clear gap between the cameras and CLEAR / RUN / SET
LIFT_MIN = 32   # px: CLEAR / RUN / SET up off the bottom edge, at the least
TOL = 1.0

SETUP = """
setExtView(true);
showExtPage('positions');
setExtGc(%s);
sizeExtPosControls();     // off screen requestAnimationFrame never comes
window.__measure = () => {
    const q = s => document.querySelector(s);
    const box = e => { const b = e.getBoundingClientRect();
                       return {l: b.left, t: b.top, r: b.right, b: b.bottom, w: b.width, h: b.height}; };
    const shown = e => getComputedStyle(e).display !== 'none' && e.getBoundingClientRect().width > 0;
    return {
        errors: window.__errors,
        gcClass: q('#ext-view').classList.contains('gc'),
        btnOn: q('#btn-ext-gc').classList.contains('on'),
        stored: localStorage.getItem('cc_ext_gc'),
        strip: box(q('.ext-pos-ctrl-strip')), grid: box(q('.ext-pos-grid-area')),
        faders: shown(q('#ext-pos-sliders')), stick: shown(q('#ext-pos-joy-wrap')),
        joyW: q('#ext-pos-joy').getBoundingClientRect().width,
        sz: box(q('#ext-pos-dial-sz-wrap')), pt: box(q('#ext-pos-dial-pt-wrap')),
        cams: box(q('#ext-pos-cam-bar')), camBtn: box(q('#ext-pos-cam-bar .cam-btn')),
        lastRow: box(q('#ext-pcell-5-0')),
        clear: box(q('#ext-pos-clear-btn')), run: box(q('#ext-pos-run-btn')),
        set: box(q('#ext-pos-set-btn')),
        slidersBox: box(q('#ext-pos-sliders')), joyBox: box(q('#ext-pos-joy-wrap')),
        vw: innerWidth, vh: innerHeight,
    };
};
"""

tmp = pathlib.Path(tempfile.mkdtemp(prefix="wkextgc-"))
page = tmp / "webapp.html"
page.write_text(m.group(1))
tool = tmp / "wkshot"
r = subprocess.run(["swiftc", "-O", str(REPO / "tools/wkshot.swift"), "-o", str(tool)],
                   capture_output=True, text=True)
assert r.returncode == 0, "wkshot.swift does not compile:\n" + r.stderr[-2000:]
pics = os.environ.get("WKSHOT_DIR")
for gc in (True, False):
    (tmp / ("setup_%s.js" % ("on" if gc else "off"))).write_text(SETUP % ("true" if gc else "false"))


def render(name, w, h, gc):
    out = "-"
    if pics:
        out = str(pathlib.Path(pics) / ("%s, GC %s.png" % (name.replace('"', "in"),
                                                            "on" if gc else "off")))
    rr = subprocess.run([str(tool), str(page), str(w), str(h), out,
                         str(tmp / ("setup_%s.js" % ("on" if gc else "off")))],
                        capture_output=True, text=True, timeout=60)
    assert rr.returncode == 0, f"{name}: wkshot failed ({rr.returncode}): {rr.stderr.strip()}"
    return json.loads(rr.stdout.strip().splitlines()[-1])


jobs = [(n, w, h, g) for n, (w, h) in SIZES.items() for g in (True, False)]
with ThreadPoolExecutor(max_workers=4) as ex:
    got = dict(zip([(n, g) for n, _, _, g in jobs], ex.map(lambda j: render(*j), jobs)))
shutil.rmtree(tmp, ignore_errors=True)
for (name, gc), d in got.items():
    assert not d["errors"], f"{name}, GC {'on' if gc else 'off'}: script errors {d['errors']}"


def overlap(a, b):
    return a["l"] < b["r"] - TOL and b["l"] < a["r"] - TOL and \
           a["t"] < b["b"] - TOL and b["t"] < a["b"] - TOL


# ---- 1. GC on: what the operator asked for ------------------------------------
print("\n1. GC on: no faders, no stick; dials at the sides, in a bit; cameras, a gap, the buttons")
for name in SIZES:
    d = got[(name, True)]
    assert d["gcClass"] and d["btnOn"] and d["stored"] == "1", \
        f"{name}: GC is not on, not shown as on, or not remembered: {d['gcClass'], d['btnOn'], d['stored']}"
    assert not d["faders"] and not d["stick"], \
        f"{name}: the faders ({d['faders']}) or the stick ({d['stick']}) still showing with GC on"
    s, sz, pt, cams = d["strip"], d["sz"], d["pt"], d["cams"]
    sz_in, pt_in, most = sz["l"] - s["l"], s["r"] - pt["r"], IN_MAX * s["w"]
    assert IN_MIN <= sz_in <= most and IN_MIN <= pt_in <= most, \
        f"{name}: the dials are not at the sides, in a bit: Slider {sz_in:.0f} px in, " \
        f"Pan/Tilt {pt_in:.0f} px in (want {IN_MIN}..{most:.0f})"
    assert cams["l"] >= sz["r"] - TOL and cams["r"] <= pt["l"] + TOL, \
        f"{name}: the cameras {cams['l']:.0f}..{cams['r']:.0f} run into the dials " \
        f"(..{sz['r']:.0f}, {pt['l']:.0f}..)"
    btns = [d["clear"], d["run"], d["set"]]
    assert all(b["t"] >= cams["b"] - TOL for b in btns), \
        f"{name}: CLEAR / RUN / SET are not under the cameras"
    gap = min(b["t"] for b in btns) - cams["b"]
    assert gap >= GAP_MIN, \
        f"{name}: only {gap:.0f} px between the cameras and CLEAR / RUN / SET (want {GAP_MIN})"
    below = d["vh"] - max(b["b"] for b in btns)
    above = d["camBtn"]["t"] - d["lastRow"]["b"]
    assert below >= LIFT_MIN, \
        f"{name}: CLEAR / RUN / SET only {below:.0f} px off the bottom (want {LIFT_MIN})"
    assert abs(above - below) <= TOL, \
        f"{name}: {above:.1f} px down from the positions to the cameras, but " \
        f"{below:.1f} px up from the bottom to the buttons: the two should match"
    assert d["clear"]["r"] <= d["run"]["l"] + TOL and d["run"]["r"] <= d["set"]["l"] + TOL, \
        f"{name}: the buttons are not CLEAR, RUN, SET left to right"
    # The three as one group: CLEAR is wider than SET, so RUN alone sits a few
    # pixels off — as it always has in the strip.
    centre = (cams["l"] + cams["r"]) / 2
    group = (d["clear"]["l"] + d["set"]["r"]) / 2
    assert abs(group - centre) <= 2, \
        f"{name}: CLEAR..SET's middle {group:.0f} is off the cameras' {centre:.0f}: not centred"
    parts = {"Slider dial": sz, "Pan/Tilt dial": pt, "cameras": cams,
             "CLEAR": d["clear"], "RUN": d["run"], "SET": d["set"]}
    names = list(parts)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            assert not overlap(parts[names[i]], parts[names[j]]), \
                f"{name}: {names[i]} overlaps {names[j]}"
    for k, b in parts.items():
        assert b["l"] >= -TOL and b["r"] <= d["vw"] + TOL and b["b"] <= d["vh"] + TOL, \
            f"{name}: {k} is off the screen: {b}"
    print(f"   {name:21} dials {sz_in:.0f}/{pt_in:.0f} px in; buttons {gap:.0f} px under "
          f"the cameras, {below:.0f} px off the bottom; cameras {above:.0f} px down   OK")

# ---- 2. the room it gives back --------------------------------------------------
print("\n2. GC on: the strip shrinks, the positions get the height")
for name in SIZES:
    on, off = got[(name, True)], got[(name, False)]
    gave = off["strip"]["h"] - on["strip"]["h"]
    grew = on["grid"]["h"] - off["grid"]["h"]
    assert gave > 40, f"{name}: the strip gave up only {gave:.0f} px"
    assert abs(grew - gave) <= 2, \
        f"{name}: the strip gave up {gave:.0f} px but the positions grew {grew:.0f}"
    print(f"   {name:21} strip {off['strip']['h']:.0f} -> {on['strip']['h']:.0f} px, "
          f"positions +{grew:.0f} px   OK")

# ---- 3. GC off: the page as it was --------------------------------------------
print("\n3. GC off: faders left, stick right, dials either side of the buttons")
for name in SIZES:
    d = got[(name, False)]
    assert not d["gcClass"] and not d["btnOn"] and d["stored"] == "0", \
        f"{name}: GC still on, or shown as on, after switching it off"
    assert d["faders"] and d["stick"], f"{name}: the faders or the stick are missing with GC off"
    assert d["slidersBox"]["r"] <= d["cams"]["l"] + TOL and d["joyBox"]["l"] >= d["cams"]["r"] - TOL, \
        f"{name}: the faders are not left of the middle, or the stick not right of it"
    assert d["sz"]["l"] >= d["slidersBox"]["r"] - TOL and d["pt"]["r"] <= d["joyBox"]["l"] + TOL, \
        f"{name}: with GC off the dials left the middle"
    assert d["joyW"] >= 80, f"{name}: the stick is {d['joyW']:.0f} px: not sized again"
    print(f"   {name:21} stick {d['joyW']:.0f} px, dials in the middle   OK")

print("\nALL CHECKS PASSED")
