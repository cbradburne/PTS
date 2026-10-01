"""The web app's Move popup, laid out by WebKit, the iPad's own engine.

On 2026-10-01 the operator put the popup's focus button up and to the right of
the stick, drawn on a phone screenshot and on an iPad one.  Where a button lands
is layout, and layout is what reading the source cannot show.  So this renders
the real page, EXTRACTED FROM web_app.h, in a WKWebView at the sizes it is used
at, and measures it:

  - the button's right edge is on the stick's right edge;
  - its middle is halfway down the clear space above the stick's ring: from
    the top of the panel's body side by side, from the slider stacked;
  - it overlaps nothing: not the faders, not the ring, not the panel's edges;
  - the stick still fits.  It keeps its full size on the iPad and on an upright
    phone, and a short screen gives up no more of it than the button's room;
  - side by side, the stick stays level with the faders wherever there is room;
  - the button costs the faders nothing;
  - with the focus buttons off, it is hidden and the stick gets its room back;
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

if sys.platform != "darwin" or not shutil.which("swiftc"):
    print("swiftc and WebKit not here (macOS only): skipping the layout run.")
    print("\nALL CHECKS PASSED")
    sys.exit(0)

m = re.search(r'R"rawhtml\((.*)\)rawhtml"', WEB, re.S)
assert m, "the page's raw string not found in web_app.h"

# The viewports the popup is used at: the operator's 13" iPad standalone (below
# the status bar) both ways up, an 11" one, phones upright and on their side.
SIZES = {
    'iPad 13" landscape': (1376, 1008),
    'iPad 13" portrait':  (1032, 1352),
    'iPad 11" landscape': (1180, 820),
    'iPad 11" portrait':  (820, 1180),
    "phone upright":      (390, 844),
    "small phone upright": (375, 667),
    "phone on its side":  (844, 390),
    "small phone on its side": (667, 375),
}
FULL_STICK = 320            # sizeMoveControls' cap
BUTTON_ROOM = 56 + 12       # the button and one gap: all a short screen may cost the stick
MIN_GAP = 8                 # clear space the button keeps from the slider and the ring
TOL = 1.0                   # px

SETUP = """
setFocusBtns(%s);
openMove();
sizeMoveControls();     // openMove sizes on the next frame, and off screen none comes
window.__measure = () => {
    const q = s => document.querySelector(s);
    const box = e => { const b = e.getBoundingClientRect();
                       return {l: b.left, t: b.top, r: b.right, b: b.bottom, w: b.width, h: b.height}; };
    const body = q('#move-body'), bs = getComputedStyle(body);
    const btn = q('#mv-focus'), col = q('#mv-joy').parentElement, cs = getComputedStyle(col);
    return {
        errors: window.__errors,
        tall: body.classList.contains('tall'),
        body: box(body),
        pad: [bs.paddingTop, bs.paddingRight, bs.paddingBottom, bs.paddingLeft].map(parseFloat),
        faders: box(q('#move-body .move-faders')),
        zoom: box(q('#mv-hsl-zoom')), slider: box(q('#mv-hsl-slider')),
        focus: box(btn), shown: getComputedStyle(btn).display !== 'none',
        joy: box(q('#mv-joy')), R: mvJoy._R(),
        colMargin: parseFloat(cs.marginTop), colPad: parseFloat(cs.paddingTop),
    };
};
"""

tmp = pathlib.Path(tempfile.mkdtemp(prefix="wkmove-"))
page = tmp / "webapp.html"
page.write_text(m.group(1))
tool = tmp / "wkshot"
r = subprocess.run(["swiftc", "-O", str(REPO / "tools/wkshot.swift"), "-o", str(tool)],
                   capture_output=True, text=True)
assert r.returncode == 0, "wkshot.swift does not compile:\n" + r.stderr[-2000:]
pics = os.environ.get("WKSHOT_DIR")


def render(name, w, h, focus):
    js = tmp / ("setup_%s.js" % ("on" if focus else "off"))
    out = "-"
    if pics:
        out = str(pathlib.Path(pics) / ("%s, %s.png" % (name.replace('"', "in"),
                                                        "focus" if focus else "no focus")))
    rr = subprocess.run([str(tool), str(page), str(w), str(h), out, str(js)],
                        capture_output=True, text=True, timeout=60)
    assert rr.returncode == 0, f"{name}: wkshot failed ({rr.returncode}): {rr.stderr.strip()}"
    return json.loads(rr.stdout.strip().splitlines()[-1])


for focus in (True, False):
    (tmp / ("setup_%s.js" % ("on" if focus else "off"))).write_text(
        SETUP % ("true" if focus else "false"))
jobs = [(n, w, h, f) for n, (w, h) in SIZES.items() for f in (True, False)]
with ThreadPoolExecutor(max_workers=4) as ex:
    got = dict(zip([(n, f) for n, _, _, f in jobs], ex.map(lambda j: render(*j), jobs)))
shutil.rmtree(tmp, ignore_errors=True)

for (name, focus), d in got.items():
    assert not d["errors"], f"{name}: script errors {d['errors']}"


def content(d):
    """The body's content box: inside its padding."""
    b, (pt, pr, pb, pl) = d["body"], d["pad"]
    return b["l"] + pl, b["t"] + pt, b["r"] - pr, b["b"] - pb


def mid(bx):
    return (bx["t"] + bx["b"]) / 2


# ---- 1. where the operator put it -------------------------------------------
print("1. focus on: the button up and to the right of the stick")
for name, (w, h) in SIZES.items():
    d = got[(name, True)]
    f, j = d["focus"], d["joy"]
    assert d["shown"], f"{name}: the focus button is hidden with the focus buttons on"
    assert abs(f["r"] - j["r"]) <= TOL, \
        f"{name}: the button's right edge {f['r']:.1f} is not the stick's {j['r']:.1f}"
    ring = j["t"] + j["h"] / 2 - d["R"]
    top = d["faders"]["b"] if d["tall"] else content(d)[1]
    want = (top + ring) / 2
    assert abs(mid(f) - want) <= TOL, \
        f"{name}: the button's middle {mid(f):.1f} is not halfway down the space " \
        f"above the ring ({top:.1f} to {ring:.1f}: {want:.1f})"
    above, below = f["t"] - top, ring - f["b"]
    assert above >= MIN_GAP and below >= MIN_GAP, \
        f"{name}: the button is {above:.1f} px below the " \
        f"{'slider' if d['tall'] else 'top of the panel'} and {below:.1f} px above the ring"
    print(f"   {name:24} {'stacked' if d['tall'] else 'side by side':13} right edges "
          f"{f['r']:.0f} = {j['r']:.0f}, {above:.0f} px clear above, {below:.0f} below   OK")

# ---- 2. the stick around it ---------------------------------------------------
print("\n2. focus on: the stick fits, keeps its size, keeps its line")
for name, (w, h) in SIZES.items():
    d = got[(name, True)]
    j = d["joy"]
    l, t, r, b = content(d)
    assert j["l"] >= l - TOL and j["r"] <= r + TOL and j["t"] >= t - TOL and j["b"] <= b + TOL, \
        f"{name}: the stick {j} spills out of the panel's body {(l, t, r, b)}"
    off = got[(name, False)]["joy"]["w"]
    assert j["w"] >= min(off, FULL_STICK) - BUTTON_ROOM - TOL, \
        f"{name}: the stick is {j['w']:.0f} with the button, {off:.0f} without: " \
        f"more than the button's room ({BUTTON_ROOM}) given up"
    if name.startswith("iPad") or name == "phone upright":
        assert j["w"] == FULL_STICK, f"{name}: the stick is {j['w']:.0f}, not {FULL_STICK}"
    # Side by side on an iPad there is room above the stick for the button, so
    # the stick keeps its line with the faders.  A phone on its side has to
    # push it down a little.
    level = not d["tall"] and abs(mid(j) - mid(d["faders"])) <= TOL
    if name.startswith("iPad"):
        assert level, f"{name}: the stick's middle {mid(j):.1f} is off the faders' " \
                      f"{mid(d['faders']):.1f}"
    print(f"   {name:24} stick {j['w']:.0f} ({off:.0f} without the button)"
          f"{', level with the faders' if level else ''}   OK")

# ---- 3. what it costs everything else -----------------------------------------
print("\n3. the button costs the faders nothing; off, it is gone")
for name in SIZES:
    on, off = got[(name, True)], got[(name, False)]
    for k in ("zoom", "slider"):
        assert (on[k]["w"], on[k]["h"]) == (off[k]["w"], off[k]["h"]), \
            f"{name}: the {k} fader is {on[k]['w']:.0f}x{on[k]['h']:.0f} with the button, " \
            f"{off[k]['w']:.0f}x{off[k]['h']:.0f} without"
    assert not off["shown"], f"{name}: the focus button shows with the focus buttons off"
    assert off["colMargin"] == 0 and off["colPad"] == 0, \
        f"{name}: room kept for a hidden button ({off['colMargin']} / {off['colPad']})"
print(f"   faders the same size with and without it, at all {len(SIZES)} sizes      OK")
print(f"   off: hidden, and no room kept for it                                 OK")

print("\nALL CHECKS PASSED")
