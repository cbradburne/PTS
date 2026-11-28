"""The hub display's RUN button: the web app's Run, on the console.

Asked for on 2026-11-26, on the P4 display: a Run button on the per-camera
screen, looking like the web app's CLEAR / RUN / SET.  Neither hub display had
one; the PC app, the web app and the mount's own screen did.

1. The display could not see a run.  The mount reports one in STATUS byte [10],
   which the hub read for the phones but never passed on.  It now rides on every
   UPDATE_CAM as a seventh byte: DISP_RUN_UNKNOWN when the mount's STATUS has no
   byte [10], and a display reads a six-byte UPDATE_CAM (an older hub) the same.
2. What a tap does is shared/disp_run.h, compiled here — and the same cases run
   through the web app's own toggleRun() in node must come out the same.
3. Both displays draw it from the same code: CLEAR, RUN, SET in the web app's
   order, one size and one row with FOCUS, in the web app's colours, read from
   its stylesheet rather than copied into this file.
4. The button shows what the MOUNT reports, not what was tapped: a run started
   from the PC app or a phone shows on the console, and a start that the mount
   refused never shows as running.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re, shutil, subprocess, tempfile, json
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))
from comms import protocol as P  # noqa: E402  the PC's own packet parser

SHARED = REPO / "firmware/shared"
PROTO = (SHARED / "protocol.h").read_text()
UART = (SHARED / "disp_uart.h").read_text()
WEB = (SHARED / "web_app.h").read_text()
HUB = (REPO / "firmware/esp32_hub_eth/esp32_hub_eth.ino").read_text()
DISPLAYS = {
    "S3": REPO / "firmware/esp32_display",
    "P4": REPO / "firmware/esp32_display_p4",
}
SRC = {k: (d / "hub_display.cpp").read_text() for k, d in DISPLAYS.items()}
HDR = {k: (d / "hub_display.h").read_text() for k, d in DISPLAYS.items()}
INO = {"S3": (DISPLAYS["S3"] / "esp32_display.ino").read_text(),
       "P4": (DISPLAYS["P4"] / "esp32_display_p4.ino").read_text()}
TMP = pathlib.Path(tempfile.mkdtemp())


def code_only(src):
    """Comments out, so a sentence about a call is never taken for the call."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def body_of(src, signature):
    """The block whose opening starts with `signature`, skipping a forward
    declaration (a ';' before its '{')."""
    i = src.index(signature)
    while ";" in src[i:src.index("{", i)]:
        i = src.index(signature, i + 1)
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError(f"unbalanced braces after {signature!r}")


def define(src, name):
    m = re.search(rf"^#define\s+{name}\s+(0x[0-9A-Fa-f]+|\d+)\b", src, re.M)
    assert m, f"no #define {name}"
    return int(m.group(1), 0)


def mask(*bits):
    return sum(1 << b for b in bits)


# ---- 1. the run byte, hub to display -------------------------------------------
print("1. the mount's run, from its STATUS to the display:")
RUN_BYTE = define(PROTO, "STATUS_RUN_BYTE")
RUN_POS = define(PROTO, "STATUS_RUN_POSITIONS")
RUN_LA = define(PROTO, "STATUS_RUN_LOOK_AT")
UNKNOWN = define(UART, "DISP_RUN_UNKNOWN")
assert UNKNOWN not in (0, RUN_POS, RUN_LA, RUN_POS | RUN_LA), \
    f"DISP_RUN_UNKNOWN is {UNKNOWN:#x}, a value byte [10] can really take"
print(f"   DISP_RUN_UNKNOWN {UNKNOWN:#x} is no real byte [10]            OK")

upd = code_only(body_of(HUB, "static void disp_update_cam("))
m = re.search(r"uint8_t buf\[(\d+)\] = \{ mount_id, state, flags, \(uint8_t\)rssi, "
              r"cf, sat, run \};", upd)
assert m and m.group(1) == "7", \
    "the hub's UPDATE_CAM does not carry the run as its seventh byte"
st = code_only(body_of(HUB, "static void process_status_for_display("))
assert re.search(r"uint8_t run = \(pkt\.payload_len > STATUS_RUN_BYTE\) \? "
                 r"pkt\.payload\[STATUS_RUN_BYTE\]\s*: DISP_RUN_UNKNOWN;", st), \
    "the hub does not take the run from STATUS byte [10], or reads a short\n" \
    "    STATUS's byte [10] anyway — whatever follows the payload"
assert "disp_update_cam(mount_id, state, flags, msg.rssi, run);" in st, \
    "the run the hub read is not the one it sends"
print(f"   the hub sends STATUS byte [{RUN_BYTE}] as UPDATE_CAM byte 7         OK")

for k in DISPLAYS:
    disp = code_only(INO[k])
    case = disp[disp.index("case DISP_MSG_UPDATE_CAM:"):]
    case = case[:case.index("case DISP_MSG_", 10)]
    assert "(len >= 7) ? d[6] : DISP_RUN_UNKNOWN" in case, \
        f"the {k} display reads a seventh byte an older hub never sends"
    assert "uint8_t run = DISP_RUN_UNKNOWN" in HDR[k] and \
           re.search(r"uint8_t\s+run\s+= DISP_RUN_UNKNOWN;", HDR[k]), \
        f"the {k} display's run does not start unknown"
print("   both displays read six bytes as 'unknown', not 'idle'  OK")

# ---- 2. what a tap does: shared/disp_run.h, against the web app ---------------
print("\n2. what a tap does — shared/disp_run.h, compiled here:")
cxx = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
assert cxx, "no C++ compiler"
(TMP / "d.cpp").write_text(r'''
#include <stdio.h>
#include "disp_run.h"
static const char *NAME[] = { "STOP", "START_POSITIONS", "START_LOOK_AT",
                              "OFFLINE", "OLD_FIRMWARE", "TOO_FEW", "NO_SUBJECT" };
int main() {
    int conn, run, la, occ, subj, pre;
    while (scanf("%d %d %d %d %d %d", &conn, &run, &la, &occ, &subj, &pre) == 6) {
        DispRunDecision d = disp_run_decide(conn != 0, (uint8_t)run, la != 0,
                                            (uint16_t)occ, (int8_t)subj, (uint8_t)pre);
        printf("%s %d %d %d %d %d\n", NAME[(int)d.act], d.payload[0], d.payload[1],
               d.payload[2], d.payload[3], disp_run_active((uint8_t)run) ? 1 : 0);
    }
}
''')
r = subprocess.run([cxx, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-Wno-unused-function",
                    "-I", str(SHARED), str(TMP / "d.cpp"), "-o", str(TMP / "d")],
                   capture_output=True, text=True)
assert r.returncode == 0, f"disp_run.h does not compile on its own:\n{r.stderr[:2000]}"


def decide(cases):
    lines = [f"{int(c['conn'])} {c['run']} {int(c['la'])} {c['occ']} {c['subj']} {c['pre']}"
             for c in cases]
    out = subprocess.run([str(TMP / "d")], input="\n".join(lines) + "\n",
                         capture_output=True, text=True, check=True).stdout.split("\n")
    res = []
    for line in out[:len(cases)]:
        f = line.split()
        res.append((f[0], tuple(int(x) for x in f[1:5]), f[5] == "1"))
    return res


def case(conn=True, run=0, la=False, occ=0, subj=-1, pre=2):
    return dict(conn=conn, run=run, la=la, occ=occ, subj=subj, pre=pre)


CASES = [
    # (case, the act, the look-at payload) — the web app's toggleRun, by hand
    (case(conn=False, run=UNKNOWN),                   "OFFLINE", None),
    (case(conn=False, run=RUN_POS),                   "OFFLINE", None),
    (case(run=RUN_POS, occ=mask(0, 1)),               "STOP", None),
    (case(run=RUN_LA, la=True, subj=1),               "STOP", None),
    (case(run=UNKNOWN, occ=mask(0, 1)),               "OLD_FIRMWARE", None),
    (case(run=0, la=True, subj=3, pre=4),             "START_LOOK_AT", (3, 0, 4, 1)),
    (case(run=0, la=True, occ=mask(2, 5), pre=0),     "START_LOOK_AT", (2, 0, 2, 1)),
    (case(run=0, la=True),                            "NO_SUBJECT", None),
    (case(run=0, occ=mask(7)),                        "TOO_FEW", None),
    (case(run=0, occ=mask(0, 9)),                     "START_POSITIONS", None),
    # Only the ten position slots count: bits above them are not positions.
    (case(run=0, occ=mask(3, 10, 11, 15)),            "TOO_FEW", None),
]
got = decide([c for c, _, _ in CASES])
for (c, act, pl), (gact, gpl, _) in zip(CASES, got):
    assert gact == act, f"{c}: {gact}, expected {act}"
    if pl is not None:
        assert gpl == pl, f"{c}: look-at payload {gpl}, expected {pl} " \
                          "(subject, ◄ first, the slider preset, repeat)"
print(f"   {len(CASES)} cases, each decided as the web app decides     OK")

# Every occupancy, positions mode: a run needs two stored positions of the ten.
occs = list(range(1 << 12))
res = decide([case(run=0, occ=o) for o in occs])
for o, (gact, _, _) in zip(occs, res):
    want = "START_POSITIONS" if bin(o & 0x3FF).count("1") >= 2 else "TOO_FEW"
    assert gact == want, f"occupancy {o:#06x}: {gact}, expected {want}"
print(f"   all {len(occs)} occupancies: two of the ten positions to run     OK")

# disp_run_active: the bits, and never the sentinel.
act = decide([case(run=v) for v in (0, RUN_POS, RUN_LA, RUN_POS | RUN_LA, UNKNOWN, 0x04)])
assert [a for _, _, a in act] == [False, True, True, True, False, False], \
    f"run byte -> running: {[a for _, _, a in act]}"
print("   'running' is byte [10]'s two run bits, never 0xFF     OK")

print("\n   the same cases through web_app.h's toggleRun, in node:")
if not shutil.which("node"):
    print("   node not installed — skipped")
else:
    def js_const(name):
        mm = re.search(rf"^const {name}\s*=[^;]*;", WEB, re.M)
        assert mm, name
        return mm.group(0)
    FLAG_LA = define(PROTO, "FLAG_HAS_SLIDER") | define(PROTO, "FLAG_LOOK_AT_MODE")
    js = "\n".join([js_const(n) for n in (
        "CMD_JOG", "CMD_START_LOOK_AT_MOVE", "CMD_START_RUN", "CMD_STOP_RUN",
        "STATUS_RUN_POSITIONS", "STATUS_RUN_LOOK_AT", "NUM_SLOTS", "FLAG_HAS_SLIDER",
        "FLAG_LOOK_AT_MODE")] + [
        "let _ptPreset = 2, _szPreset = 2;", "let _seq = 0;",
        body_of(WEB, "function nextSeq("), body_of(WEB, "function crc16("),
        body_of(WEB, "function buildPkt("), body_of(WEB, "function clamp("),
        body_of(WEB, "function mkJog("), body_of(WEB, "function mkStartLookAtMove("),
        body_of(WEB, "function camIsLookAt("), body_of(WEB, "function camRunning("),
        body_of(WEB, "function toggleRun("),
        r"""
let camSt = {}; const sent = [], toasts = [];
function wsSend(b) { sent.push(Array.from(b)); }
function toast(m) { toasts.push(m); }
function camName(i) { return 'CAM ' + i; }
const out = [];
for (const c of JSON.parse(require('fs').readFileSync(process.argv[2]))) {
    sent.length = 0; toasts.length = 0;
    camSt[1] = c;
    toggleRun(1);
    out.push({sent: sent.slice(), toasts: toasts.slice()});
}
console.log(JSON.stringify(out));
"""])
    (TMP / "run.js").write_text(js)
    web_cases = [dict(connected=c["conn"], runFlags=None if c["run"] == UNKNOWN else c["run"],
                      flags=FLAG_LA if c["la"] else 0, slotOccupied=c["occ"],
                      activeLaSubject=c["subj"], activeSlPreset=c["pre"])
                 for c, _, _ in CASES]
    (TMP / "cases.json").write_text(json.dumps(web_cases))
    r = subprocess.run(["node", str(TMP / "run.js"), str(TMP / "cases.json")],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"node failed:\n{r.stderr[:1500]}"

    def cmd_value(name):
        mm = re.search(rf"\b{name}\s*=\s*(0x[0-9A-Fa-f]+)", PROTO)
        assert mm, f"no {name} in protocol.h"
        return int(mm.group(1), 16)

    CMD_ACT = {cmd_value("CMD_STOP_RUN"): "STOP",
               cmd_value("CMD_START_RUN"): "START_POSITIONS",
               cmd_value("CMD_START_LOOK_AT_MOVE"): "START_LOOK_AT"}

    def web_act(res):
        """The web app's result in disp_run.h's words."""
        if res["sent"]:
            p = P.parse_packet(bytes(res["sent"][0]))   # framing and CRC, the PC's way
            act = CMD_ACT.get(int(p.cmd), f"sent {int(p.cmd):#x}")
            return act, tuple(p.payload) if act == "START_LOOK_AT" else None
        t = " ".join(res["toasts"])
        for word, act in (("offline", "OFFLINE"), ("firmware", "OLD_FIRMWARE"),
                          ("subject", "NO_SUBJECT"), ("two or more", "TOO_FEW")):
            if word in t:
                return act, None
        return f"toast {t!r}", None

    for (c, _, _), (gact, gpl, _), wres in zip(CASES, got, json.loads(r.stdout)):
        wact, wpl = web_act(wres)
        assert gact == wact, f"{c}: the display does {gact}, the web app {wact} ({wres})"
        if wpl is not None:
            assert gpl == wpl, f"{c}: the display sends {gpl}, the web app {wpl}"
    print(f"   {len(CASES)} of {len(CASES)} the same as the phone's Run            OK")

# ---- 3. drawn the same on both displays, as the web app draws it ---------------
print("\n3. the row, on both displays:")
RUN_CODE = {}
for k, src in SRC.items():
    code = code_only(src)
    assert '#include "../shared/disp_run.h"' in src, f"the {k} display decides for itself"
    assert 'make_button(_scr_detail, "RUN", C_SURF2, ev_detail_run)' in code, \
        f"the {k} display has no RUN button"
    D = {}
    for name in ("HSL_X", "HSL_W", "DET_BTN_W", "DET_BTN_H", "DET_BTN_Y",
                 "DET_BTN_GAP", "DET_RUN_X", "DET_CLEAR_X", "DET_SET_X"):
        mm = re.search(rf"^#define\s+{name}\s+(.+?)\s*(?://.*)?$", src, re.M)
        assert mm, f"{k}: no #define {name}"
        D[name] = int(eval(mm.group(1).replace("/", "//"), {}, dict(D)))

    def geom(name, kind):
        mm = re.search(rf"lv_obj_set_{kind}\({name},\s*([^,]+),\s*([^)]+)\);", code)
        assert mm, f"{k}: no lv_obj_set_{kind} for {name}"
        return tuple(int(eval(g.strip().replace("/", "//"), {}, dict(D))) for g in mm.groups())

    box = {b: geom(f"_det_{b}_btn", "pos") + geom(f"_det_{b}_btn", "size")
           for b in ("focus", "clear", "run", "set")}
    sizes = {b: v[2:] for b, v in box.items()}
    assert len(set(sizes.values())) == 1, f"{k}: one row, different sizes: {sizes}"
    assert len({v[1] for v in box.values()}) == 1, f"{k}: the row is not one row: {box}"
    order = sorted(box, key=lambda b: box[b][0])
    assert order == ["focus", "clear", "run", "set"], \
        f"{k}: left to right the row reads {order} — the web app's is CLEAR, RUN, SET"
    for a, b in zip(order, order[1:]):
        assert box[a][0] + box[a][2] < box[b][0], f"{k}: {a} and {b} overlap: {box}"
    run_centre = box["run"][0] + box["run"][2] / 2
    assert run_centre == 400, f"{k}: RUN is centred at x={run_centre}, not the centre line"
    joy = re.search(r"build_joystick_widget\(_scr_detail, (\d+),", code)
    assert joy and box["set"][0] + box["set"][2] < int(joy.group(1)), \
        f"{k}: SET runs into the PAN / TILT stick at x={joy and joy.group(1)}"
    # The code that decides and draws RUN, for the parity check below.
    start = src.index("// ── RUN — the web app's and the PC app's")
    RUN_CODE[k] = src[start:src.index("\n}\n", src.index("static void ev_detail_run(")) + 3]
    RUN_CODE[k] += body_of(src, "static void det_btn_look(")
    RUN_CODE[k] += src[src.index("struct BtnLook"):src.index("static void det_btn_look(")]
    print(f"   {k}: FOCUS | CLEAR RUN SET, {sizes['run'][0]}x{sizes['run'][1]}, "
          f"RUN on x=400            OK")
assert RUN_CODE["S3"] == RUN_CODE["P4"], \
    "the two displays' RUN code has drifted apart — port the change to both"
print("   the same RUN code in both files                    OK")

# The colours: the web app's stylesheet, resolved, against the display's looks.
css_var = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{3,6})", WEB))


def rgb(v):
    v = v.strip()
    if v.startswith("var(--"):
        v = css_var[v[6:-1]]
    v = v.lstrip("#")
    if len(v) == 3:
        v = "".join(ch * 2 for ch in v)
    return int(v, 16)


def css_rule(selector):
    mm = re.search(re.escape(selector) + r"\{([^}]*)\}", WEB)
    assert mm, f"no {selector} in the web app's stylesheet"
    decl = dict(re.findall(r"([a-z-]+):([^;]+);", mm.group(1)))
    border = decl["border"].split() if "border" in decl else None
    out = {}
    if "background" in decl:
        out["bg"] = rgb(decl["background"])
    if "border-color" in decl:
        out["border"] = rgb(decl["border-color"])
    if border:
        out["border"] = rgb(border[-1])
        out["border_w"] = int(border[0].rstrip("px"))
    if "color" in decl:
        out["text"] = rgb(decl["color"])
    return out


base = css_rule(".ctrl-btn")
want = {
    "DET_LOOK_IDLE": base,
    "DET_LOOK_ARMED": {**base, **css_rule(".ctrl-btn.armed")},
    "DET_LOOK_CLEAR": {**base, **css_rule(".ctrl-btn.clear-armed")},
    "DET_LOOK_RUNNING": css_rule(".cam-btn.running"),
}
src = SRC["S3"]
CDEF = {n: int(v, 16) for n, v in re.findall(r"^#define\s+(C_[A-Z0-9_]+)\s+0x([0-9A-Fa-f]+)",
                                             src, re.M)}
for look, w in want.items():
    mm = re.search(rf"static const BtnLook {look}\s*=\s*\{{\s*([^}}]+)\}};", src)
    assert mm, f"no {look}"
    vals = [v.strip() for v in mm.group(1).split(",")]
    have = dict(zip(("bg", "border", "text", "border_w"),
                    [CDEF[v] if v in CDEF else int(v, 0) for v in vals]))
    for key, value in w.items():
        assert have[key] == value, \
            f"{look}.{key} is {have[key]:#08x}, the web app's is {value:#08x}"
print("   rest, armed SET, armed CLEAR, running: the web app's colours   OK")
assert not re.search(r"_det_set_btn[^;]*C_ACCENT", code_only(src)), \
    "SET is still drawn blue somewhere; the web app's rests grey like CLEAR"
print("   SET rests grey, as the web app's does                OK")

# ---- 4. it shows the mount's run, not the last tap -----------------------------
print("\n4. what the button shows:")
for k, src in SRC.items():
    code = code_only(src)
    refresh = body_of(code, "static void det_refresh_run(")
    assert "disp_run_active(_cam[_detail_cam].run)" in refresh, \
        f"{k}: RUN is not drawn from what the mount reports"
    ev = body_of(code, "static void ev_detail_run(")
    assert "det_btn_look(_det_run_btn" not in ev and "_det_run_lbl" not in ev, \
        f"{k}: a tap redraws RUN itself, so a start the mount ignored shows as running"
    assert "CMD_STOP_RUN" in ev and "CMD_START_RUN" in ev and "CMD_START_LOOK_AT_MOVE" in ev
    upd = body_of(code, "void hub_ui_update_cam(")
    assert "_cam[i].run          = run;" in upd and \
        re.search(r"if \(_cam\[i\]\.run != old_run && _detail_cam == i\) det_refresh_run\(\);",
                  upd), f"{k}: RUN does not change when the mount's report does"
    gone = body_of(code, "void hub_ui_set_disconnected(")
    assert "_cam[i].run = DISP_RUN_UNKNOWN;" in gone, \
        f"{k}: an offline mount keeps showing the run it had"
    switch = body_of(code, "static void det_switch_cam(")
    assert "det_refresh_run();" in switch, f"{k}: RUN shows the last camera's run"
print("   drawn from the mount's report; a tap only sends       OK")
print("   follows each report, a camera switch and a disconnect OK")

print("\nALL CHECKS PASSED")
