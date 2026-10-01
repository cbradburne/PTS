"""The web app's sticks take a touch only if it starts inside the ring.

The operator, 2026-10-01.  A stick's square has empty corners, and the stick
took a touch anywhere in it.  A touch in a corner reads as full deflection, so a
tap that missed what sits beside the corners could start a full-speed jog:
E-STOP and EDIT in the portrait stick's lower corners, and the Move popup's
focus button above the right one.  Now a touch must start inside the ring, its
2 px line included.  Once started, a drag may go anywhere, held at the ring.

This renders the real page, EXTRACTED FROM web_app.h, in WebKit, the iPad's
engine.  It touches all four sticks (portrait, landscape, Extended positions and
the Move popup) the way a finger does: the point is hit-tested
(elementFromPoint) and the pointer events go to whatever is there.

  1. the near misses this is for: a tap that misses E-STOP, EDIT or the Move
     popup's focus button, toward the stick, does nothing;
  2. every stick: its centre and its ring line take a touch; just outside the
     ring, and the square's corners, do nothing;
  3. once started: a drag out past the ring keeps full deflection and lets go
     on release; a touch that starts outside and slides in stays nothing.

WebKit will not capture a pointer it never saw go down, and these are
synthetic, so capture is a no-op here.  What is under test is where a touch may
start, not the capture.

tools/wkshot.swift does the rendering (macOS: swiftc and WebKit).  It skips
cleanly where swiftc is absent: a missing tool, not a failure.  Run directly,
or via tools/run_tests.sh with the rest.
"""
import json, pathlib, re, shutil, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor

REPO = pathlib.Path(__file__).resolve().parent.parent
WEB = (REPO / "firmware/shared/web_app.h").read_text()

if sys.platform != "darwin" or not shutil.which("swiftc"):
    print("swiftc and WebKit not here (macOS only): skipping the touch run.")
    print("\nALL CHECKS PASSED")
    sys.exit(0)

m = re.search(r'R"rawhtml\((.*)\)rawhtml"', WEB, re.S)
assert m, "the page's raw string not found in web_app.h"

PROBES = r"""
Element.prototype.setPointerCapture = function () {};
Element.prototype.releasePointerCapture = function () {};
let _pid = 100;
const _name = el => el ? (el.id || el.className || el.tagName) : null;
function fire(type, x, y, pid, el) {
    el = el || document.elementFromPoint(x, y);
    el.dispatchEvent(new PointerEvent(type, {pointerId: pid, pointerType: 'touch',
        isPrimary: true, clientX: x, clientY: y, bubbles: true, cancelable: true}));
    return el;
}
// A finger down at (x, y) and up again: what it landed on, whether the stick took it.
function tap(stick, x, y) {
    const pid = ++_pid, el = fire('pointerdown', x, y, pid), took = stick.active;
    fire('pointerup', x, y, pid, took ? stick.cv : el);
    return {at: [x, y], on: _name(el), took, after: stick.active};
}
function probe(stick) {
    const r = stick.cv.getBoundingClientRect();
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2, R = stick._R();
    const at = (rad, deg) => [cx + rad * Math.cos(deg * Math.PI / 180),
                              cy + rad * Math.sin(deg * Math.PI / 180)];
    const ANG = [20, 110, 200, 290];
    const corners = [[r.left + 3, r.top + 3], [r.right - 3, r.top + 3],
                     [r.left + 3, r.bottom - 3], [r.right - 3, r.bottom - 3]];
    const out = {id: stick.cv.id, size: [r.width, r.height], R,
        centre:  tap(stick, cx, cy),
        ring:    ANG.map(a => tap(stick, ...at(R, a))),
        outside: ANG.map(a => tap(stick, ...at(R + 4, a))),
        corners: corners.map(p => tap(stick, ...p))};
    // From the middle out past the ring to a corner, then let go.
    let pid = ++_pid;
    fire('pointerdown', cx, cy, pid);
    fire('pointermove', r.right - 3, r.top + 3, pid, stick.cv);
    out.dragOut = {took: stick.active, x: stick.x, y: stick.y};
    fire('pointerup', r.right - 3, r.top + 3, pid, stick.cv);
    out.dragOut.after = [stick.active, stick.x, stick.y];
    // From a corner into the middle.
    pid = ++_pid;
    const from = fire('pointerdown', r.left + 3, r.bottom - 3, pid);
    fire('pointermove', cx, cy, pid);
    out.slideIn = {on: _name(from), took: stick.active};
    fire('pointerup', cx, cy, pid);
    out.slideIn.after = stick.active;
    return out;
}
// A finger that missed a button toward the stick: 3 px off each side and corner
// of it, and the corner of the stick's square nearest it.  Only the points that
// land on the stick test anything; those are kept.
function nearMisses(stick, id) {
    const b = document.getElementById(id).getBoundingClientRect();
    const r = stick.cv.getBoundingClientRect();
    const mx = (b.left + b.right) / 2, my = (b.top + b.bottom) / 2, d = 3;
    const pts = [[b.left - d, my], [b.right + d, my], [mx, b.top - d], [mx, b.bottom + d],
                 [b.left - d, b.top - d], [b.right + d, b.top - d],
                 [b.left - d, b.bottom + d], [b.right + d, b.bottom + d],
                 [mx < (r.left + r.right) / 2 ? r.left + d : r.right - d,
                  my < (r.top + r.bottom) / 2 ? r.top + d : r.bottom - d]];
    return {button: id, taps: pts.filter(p => document.elementFromPoint(...p) === stick.cv)
                                 .map(p => tap(stick, ...p))};
}
window.__results = {};
"""

# Each scene: the viewport, then what to open and probe.  Off screen no frame
# ever comes, so whatever the page sizes "on the next frame" is called here.
SCENES = {
    "phone upright": (390, 844, """
        sizePortraitControls();
        __results.portrait = probe(pJoy);
        __results.portraitMiss = ['btn-estop-p', 'btn-edit'].map(id => nearMisses(pJoy, id));
        setFocusBtns(true); openMove(); sizeMoveControls();
        __results.move = probe(mvJoy);
        __results.moveMiss = [nearMisses(mvJoy, 'mv-focus')];
    """),
    "phone on its side": (844, 390, """
        sizeJoysticks();
        __results.landscape = probe(joyRight);
    """),
    'iPad 13" Extended': (1376, 1008, """
        setExtView(true); showExtPage('positions'); sizeExtPosControls();
        __results.extended = probe(extPosJoy);
    """),
    'iPad 13" Move': (1376, 1008, """
        setFocusBtns(true); openMove(); sizeMoveControls();
        __results.move = probe(mvJoy);
        __results.moveMiss = [nearMisses(mvJoy, 'mv-focus')];
    """),
}

tmp = pathlib.Path(tempfile.mkdtemp(prefix="wkstick-"))
page = tmp / "webapp.html"
page.write_text(m.group(1))
tool = tmp / "wkshot"
r = subprocess.run(["swiftc", "-O", str(REPO / "tools/wkshot.swift"), "-o", str(tool)],
                   capture_output=True, text=True)
assert r.returncode == 0, "wkshot.swift does not compile:\n" + r.stderr[-2000:]


def render(item):
    name, (w, h, js) = item
    setup = tmp / ("scene_%d.js" % list(SCENES).index(name))
    setup.write_text(PROBES + js + "\nwindow.__measure = () => "
                     "Object.assign({errors: window.__errors}, window.__results);\n")
    rr = subprocess.run([str(tool), str(page), str(w), str(h), "-", str(setup)],
                        capture_output=True, text=True, timeout=60)
    assert rr.returncode == 0, f"{name}: wkshot failed ({rr.returncode}): {rr.stderr.strip()}"
    return name, json.loads(rr.stdout.strip().splitlines()[-1])


with ThreadPoolExecutor(max_workers=4) as ex:
    got = dict(ex.map(render, SCENES.items()))
shutil.rmtree(tmp, ignore_errors=True)

for name, d in got.items():
    assert not d["errors"], f"{name}: script errors {d['errors']}"

STICKS = [(scene, key, d[key]) for scene, d in got.items()
          for key in ("portrait", "landscape", "extended", "move") if key in d]
assert len({k for _, k, _ in STICKS}) == 4, "not all four sticks probed"
for scene, key, p in STICKS:
    assert p["size"][0] == p["size"][1] > 80, f"{scene}: the {key} stick is not laid out: {p['size']}"


def where(t):
    return "(%.0f, %.0f)" % tuple(t["at"])


# ---- 1. the near misses this is for ------------------------------------------
print("1. a tap that misses a button toward the stick does nothing")
for scene, d in got.items():
    for key in ("portraitMiss", "moveMiss"):
        for miss in d.get(key, []):
            taps = miss["taps"]
            assert taps, f"{scene}: no point beside {miss['button']} lands on the stick: " \
                         "the probe tests nothing"
            for t in taps:
                assert not t["took"], \
                    f"{scene}: a tap at {where(t)}, missing {miss['button']}, moved the stick"
            print(f"   {scene:20} beside {miss['button']:12} {len(taps)} point(s) on the "
                  f"stick's square, none moved it   OK")

# ---- 2. where a touch may start ----------------------------------------------
print("\n2. every stick: inside the ring takes a touch, outside it does nothing")
for scene, key, p in STICKS:
    c = p["centre"]
    assert c["on"] == p["id"] and c["took"] and not c["after"], \
        f"{scene}: the {key} stick's centre did not take a touch: {c}"
    for t in p["ring"]:
        assert t["on"] == p["id"] and t["took"], \
            f"{scene}: a touch on the {key} stick's ring line at {where(t)} did nothing"
    for t in p["outside"]:
        assert t["on"] == p["id"], f"{scene}: {where(t)} is not on the {key} stick"
    for t in p["outside"] + p["corners"]:
        assert not t["took"], \
            f"{scene}: a touch outside the {key} stick's ring at {where(t)} moved it"
    print(f"   {scene:20} {key:9} stick {p['size'][0]:.0f}: centre and ring line take it; "
          f"4 px outside and the corners do not   OK")

# ---- 3. once started -----------------------------------------------------------
print("\n3. once started, a drag may leave the ring; one from outside never starts")
for scene, key, p in STICKS:
    g = p["dragOut"]
    assert g["took"] and max(abs(g["x"]), abs(g["y"])) >= 0.99, \
        f"{scene}: a drag out of the {key} stick's ring lost its deflection: {g}"
    assert g["after"] == [False, 0, 0], f"{scene}: the {key} stick did not let go: {g['after']}"
    s = p["slideIn"]
    assert s["on"] == p["id"] and not s["took"] and not s["after"], \
        f"{scene}: a touch from the {key} stick's corner took it on sliding in: {s}"
print(f"   all {len(STICKS)}: full deflection held past the ring, let go on release,"
      f" a slide-in ignored   OK")

print("\nALL CHECKS PASSED")
