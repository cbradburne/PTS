"""◀/▶ without a subject: one continuous slide that hands the aim around.

The operator, 2026-10-15: "<> should work without a subject selected, and
continue to work when a subject is selected mid move.  Also if cv tracking, and
press a subject, cv tracking should be disabled and the slider keep moving but
with the subject tracked."  And the whole of it: "set the slider moving to 1
end, select a subject and it follow the set subject 3D location, select cv
tracking person and pan tilt drops the subject and follows the person, then use
the joystick and the tracking stop, the reselect a subject all without the
slider blipping.  1 continuous slider move."  Also: closing the CV window
stopped tracking, when the main window is needed for the other cameras.

  1. the Teensy (its source — MountMotion is not built on the host):
     a slide needs no subject and no reference, never cuts a homing short, and
     stops nothing CV or the joystick is driving; a move that ends leaves
     jogged axes running as a jog; CMD_START_SLIDE is look-at mode only, lets
     go of a held subject, retargets a move already running and lights the
     arrow; a CV jog takes the head even at zero; a subject chosen without a
     reference is refused before anything is aimed;
  2. one continuous slider move: every path a hand-over goes through — a
     subject chosen, CV jogs, the joystick, CV stopping — leaves the slider
     alone, and the pieces this leans on are still there;
  3. the mount's ESP32 and the hub: a slide ends a run and reaches the Teensy;
     the hub flashes the 7" display's arrow for it;
  4. the PC app: the wire bytes; the grid sends ◀/▶ only in look-at mode; a
     subject → look-at move, none → a slide; a subject chosen while CV tracks
     that mount stops CV first; the stick moving pan/tilt stops CV and then
     jogs; the CV window hides instead of closing while tracking, draws
     nothing hidden, and closes for real when not tracking;
  5. the web app (WebKit): ◀/▶ with no subject send a slide, with one a
     look-at move — never the old silent "subject 0".

Run directly, or via tools/run_tests.sh with the rest.
"""
import json, os, pathlib, re, shutil, subprocess, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
FW = REPO / "firmware"
sys.path.insert(0, str(REPO / "pc_app"))

CPP = re.sub(r"//[^\n]*", "", (FW / "teensy41_mount/MountMotion.cpp").read_text())
INO = re.sub(r"//[^\n]*", "", (FW / "teensy41_mount/teensy41_mount.ino").read_text())
BRIDGE = re.sub(r"//[^\n]*", "", (FW / "esp_mount_amoled175/esp_mount_amoled175.ino").read_text())
HUB = re.sub(r"//[^\n]*", "", (FW / "esp32_hub_eth/esp32_hub_eth.ino").read_text())
PROTO = (FW / "shared/protocol.h").read_text()
WEB = (FW / "shared/web_app.h").read_text()


def fn(code, head):
    """A function from its head to the closing brace in column 0."""
    i = code.index(head)
    return code[i:code.index("\n}", i)]


def case(code, cmd):
    """One case of a switch, to its closing brace at case indent."""
    i = code.index(f"case {cmd}: {{")
    return code[i:code.index("\n        }\n", i)]


def assigns_state(body):
    """An assignment to _state — not a comparison with it."""
    return re.search(r"\b_state\s*=[^=]", body) is not None


def before(text, a, b, what):
    assert a in text and b in text and text.index(a) < text.index(b), what


# ---- 1. the Teensy ---------------------------------------------------------------
print("1. the Teensy:")
start = fn(CPP, "bool MountMotion::startLookAtMove(")
assert "bool aiming = (_la_subject_id != 0xFF);" in start, "a move does not know if it aims"
assert "if (aiming && !_ref_set)       return false;" in start, \
    "a slide still needs a reference — there is nothing to aim until a subject is chosen"
assert "if (!_limits_set[AXIS_SLIDER]) return false;" in start, "a move runs without slider limits"
assert "if (!aiming && _state == STATE_FINDING_LIMITS) return false;" in start, \
    "a slide can cut a homing short"
assert "if (aiming) emergencyStop();\n    else        _stopForSlide();" in start, \
    "a slide stops axes CV or the joystick is driving (emergencyStop takes all four)"
before(start, "if (aiming) {", "_aimFrom(_la_subject_x", "a slide pre-aims at a subject it does not have")
before(start, "_stopForSlide();", "_state = STATE_LOOK_AT_MOVE;", "the slide's state is set before its stop")
print("   a slide needs no subject or reference, spares homing, and does not")
print("   stop what CV or the joystick is driving                          OK")

stop4 = fn(CPP, "void MountMotion::_stopForSlide(")
before(stop4, "if (i != AXIS_SLIDER && _jog_dir[i] != 0) continue;", "_stepper[i]->emergencyStop();",
       "_stopForSlide stops jogged axes, or spares the slider")
for need in ("_goto_dir[i] = 0", "_has_goto_target = false;", "_state           = STATE_IDLE;"):
    assert need in stop4, f"_stopForSlide leaves a goto or state behind ({need})"
print("   _stopForSlide: jogged axes run on, the slider and any goto stop     OK")

end = fn(CPP, "void MountMotion::stopLookAtMove(")
before(end, "if (_jog_dir[i] != 0) { jogging = true; continue; }", "_stepper[i]->stopAsync();",
       "a move ending stops axes the jog is driving — CV stutters at every rail end")
assert "_state = jogging ? STATE_JOGGING : STATE_IDLE;" in end, \
    "a move ending with axes jogged does not hand them to the jog's dead-man"
assert "_jog_dir[i] = 0" not in end, "a move ending still clears the jogs it no longer stops"
print("   a move ending leaves jogged axes running, under the jog's dead-man OK")

slide = case(INO, "CMD_START_SLIDE")
assert "if (!_cfg.look_at_mode || !mount.hasLimits(AXIS_SLIDER) || homing) {" in slide and \
       "homing ? NACK_BUSY : NACK_INVALID_PARAM" in slide, \
    "a slide is allowed outside look-at mode, without limits, or mid-homing"
before(slide, "mount.clearLaSubject();", "arrow_dest(direction,", "a slide keeps a held subject")
assert "if (mount.getLaSubjectId() != 0xFF) {\n                mount.clearLaSubject();\n" \
       "                send_look_at_status();" in slide, "a slide drops a subject without saying so"
before(slide, "if (mount.getState() == STATE_LOOK_AT_MOVE) {", "mount.moveSliderTo(dest_log, preset);",
       "a slide already running is not retargeted")
before(slide, "mount.moveSliderTo(dest_log, preset);", "mount.startLookAtMove(cur_log, dest_log, preset,",
       "a slide already running is restarted — startLookAtMove() stops the rail dead first")
before(slide, "if (!started) {", "_target_slot = direction ? TARGET_SLOT_LA_MAX : TARGET_SLOT_LA_MIN;",
       "the arrow lights for a slide that did not start")
assert "_la_last_direction = (int8_t)direction;" in slide, "the end a slide reached is not remembered"
print("   CMD_START_SLIDE: look-at mode, lets a subject go, retargets a move")
print("   already running, lights the arrow only if it started              OK")

assert INO.count("go_to_phys_max") == 2 and \
       "arrow_dest(direction, &cur_phys, &phys_dest, &cur_log, &dest_log);" in \
       case(INO, "CMD_START_LOOK_AT_MOVE") and "arrow_dest(direction," in slide, \
    "◀/▶ work out the rail end in more than one place — they could disagree on left"
print("   ◀/▶ share one rail-end rule, inversion included                   OK")

jog = case(INO, "CMD_JOG")
cv = jog[jog.index("if (axis_mask == 0x03) {"):jog.index("} else if (axis_mask == 0x0C) {")]
before(cv, "mount.clearLaSubject();", "mount.jogPanTilt(pan, tilt, pt_preset);",
       "a CV jog leaves a held subject aiming until the person first moves")
assert "if (_cfg.look_at_mode && mount.getLaSubjectId() != 0xFF)" in cv, \
    "a CV jog drops a subject outside look-at mode, or when there is none"
print("   a CV jog takes the head at once, even at zero                     OK")

sw = case(INO, "CMD_SWITCH_SUBJECT")
before(sw, "if (!mount.isRefSet()) {", "mount.setLookAtSubject(",
       "a subject is aimed with no reference — pan/tilt driven off to nowhere")
ref = sw[sw.index("if (!mount.isRefSet()) {"):sw.index("mount.setLookAtSubject(")]
assert "NACK_NO_REF" in ref and "send_look_at_status();" in ref and "break;" in ref, \
    "a subject refused for want of a reference is not refused out loud, or not put back"
print("   a subject chosen with no reference is refused, and put back       OK")

# ---- 2. one continuous slider move -----------------------------------------------
print("\n2. one continuous slider move — no hand-over touches the slider:")
# The paths: CMD_SWITCH_SUBJECT mid-move → setLookAtSubject; a CV jog (0x03) →
# jogPanTilt (+ clearLaSubject); the stick and CV's parting zero jog (0x0F)
# during a move → jog()'s look-at branch → jogPanTilt.  None may stop, restart
# or retarget the slider, or leave the look-at state that keeps it running.
SLIDER_MOVES = ("AXIS_SLIDER]->stopAsync", "AXIS_SLIDER]->emergencyStop", "AXIS_SLIDER]->moveAsync",
                "AXIS_SLIDER]->setTargetAbs", "AXIS_SLIDER]->rotateAsync", "startLookAtMove(",
                "emergencyStop()", "stopAll()", "moveSliderTo(", "stopLookAtMove()")
sel = fn(CPP, "void MountMotion::setLookAtSubject(")
assert not any(m in sel for m in SLIDER_MOVES) and not assigns_state(sel), \
    "choosing a subject mid-move touches the slider or the move's state"
assert not re.search(r"->(stopAsync|emergencyStop|moveAsync|rotateAsync|setTargetAbs)\(", sel), \
    "choosing a subject drives a motor directly instead of leaving it to the tracker"
mid = sw[sw.index("mount.setLookAtSubject("):]
mid = mid[:mid.index("} else {")]
assert "aimAtSubject" not in mid and "STATE_LOOK_AT_MOVE" in mid, \
    "a subject chosen mid-move does more than retarget the aim"
print("   a subject chosen mid-move retargets the aim, nothing else          OK")

pt = fn(CPP, "void MountMotion::jogPanTilt(")
clr = fn(CPP, "void MountMotion::clearLaSubject(")
for name, body in (("jogPanTilt", pt), ("clearLaSubject", clr)):
    assert not any(m in body for m in SLIDER_MOVES), f"{name} touches the slider"
    loops = re.findall(r"for \(int i = 0; i < (\d); i\+\+\)", body)
    assert loops and all(n == "2" for n in loops), f"{name} reaches past pan and tilt: {loops}"
assert "if (!in_la_move) _state = STATE_JOGGING;" in pt, \
    "a pan/tilt jog mid-move takes the state away and orphans the slide"
print("   CV jogs and dropping a subject drive pan/tilt only, state kept     OK")

jg = fn(CPP, "void MountMotion::jog(")
branch = jg[jg.index("bool goto_zoom_only ="):jg.index("pt_preset = constrain(pt_preset, 1, 4);")]
assert not any(m in branch for m in SLIDER_MOVES) and "_stepper[i]" not in branch and \
       not assigns_state(branch), "the stick (or CV's parting zero jog) during a move touches the slider"
assert "if (!goto_zoom_only) jogPanTilt(pan, tilt, pt_preset);" in branch, \
    "the stick's pan/tilt during a move no longer reaches the head"
print("   the stick, and CV's parting zero jog, mid-move: zoom and pan/tilt  OK")

la = fn(CPP, "void MountMotion::_updateLookAt(")
assert "bool aiming = (_la_subject_id != 0xFF);" in la and \
       "if (aiming) {\n        _driveTowardTarget(AXIS_PAN," in la and "bool aim_there   = !aiming ||" in la, \
    "a move with no subject is no longer run as a slide to the end of the rail"
assert "bool reacquiring = (tracking_now && _la_subject_id == 0xFF && _ref_set);" in sel, \
    "a subject chosen mid-slide no longer picks the aim up from where the camera points"
print("   no subject: aim skipped, the rail runs on; a subject chosen")
print("   mid-slide eases in from where the camera points                  OK")

# ---- 3. the mount's ESP32 and the hub ----------------------------------------------
print("\n3. the mount's ESP32 and the hub:")
hb = fn(BRIDGE, "static void handle_hub_packet(")
blk = hb[hb.index("if (pkt.cmd == CMD_START_SLIDE) {"):]
blk = blk[:blk.index("}")]
assert "prun_stop(" in blk and "run_stop(" in blk and "return" not in blk, \
    "a slide does not end a run, or is consumed instead of reaching the Teensy"
before(hb, "if (pkt.cmd == CMD_START_SLIDE) {", "Serial1.write(fwd, build_packet(fwd, pkt.mount_id",
       "the slide is checked after the packet has gone to the Teensy")
print("   a slide ends any run and goes on to the Teensy                    OK")
assert re.search(r"if \(pkt\.cmd == CMD_START_SLIDE && pkt\.payload_len >= 1\) \{\s*"
                 r"intercept_la_move_dir\(pkt\.mount_id, pkt\.payload\[0\]\);", HUB), \
    "the hub does not flash the arrow for a slide, or reads its direction from the wrong byte"
demo = HUB[HUB.index("case CMD_START_SLIDE:"):]
demo = demo[:demo.index("break;")]
assert "intercept_la_move_dir((uint8_t)(i + 1), payload[0]);" in demo and "d.subject   = 0xFF;" in demo, \
    "the demo hub does not slide"
print("   the hub flashes the 7\" display's arrow for it (and the demo slides) OK")

# ---- 4. the PC app -----------------------------------------------------------------
print("\n4. the PC app:")
from types import SimpleNamespace
from comms.protocol import Cmd, pkt_start_slide, parse_packet
m = re.search(r"CMD_START_SLIDE\s*=\s*(0x[0-9A-Fa-f]+)", PROTO)
assert m and int(Cmd.START_SLIDE) == int(m.group(1), 0) == 0x2C, "START_SLIDE disagrees with protocol.h"
for (d, pre), want in (((0, 3), [0, 3]), ((1, 9), [1, 4]), ((5, 0), [1, 1])):
    p = parse_packet(pkt_start_slide(1, d, pre))
    assert p.cmd == Cmd.START_SLIDE and list(p.payload) == want, \
        f"pkt_start_slide(1, {d}, {pre}) sent {list(p.payload)}, want {want}"
print("   the wire: direction + speed preset, clamped                        OK")

from PyQt6.QtWidgets import QApplication, QWidget
from PyQt6.QtCore import QObject, pyqtSignal
app = QApplication.instance() or QApplication([])
from ui.widgets.position_grid import PositionGrid
from config.position_store import PositionStore
grid = PositionGrid(PositionStore())
got = []
grid.slider_jog_started.connect(lambda mid, d: got.append((mid, d)))
grid.set_has_slider(1, True)
grid.set_look_at_mode(1, False)
grid._buttons[(1, 8)].pressed.emit()
assert got == [], f"with look-at off, slot 9's press went to the arrows: {got}"
grid.set_look_at_mode(1, True)
grid._buttons[(1, 8)].pressed.emit()
grid._buttons[(1, 9)].pressed.emit()
assert got == [(1, -1), (1, 1)], f"◀/▶ pressed in look-at mode sent {got}"
print("   the grid sends ◀/▶ only in look-at mode                           OK")

import ui.main_window as mw


class Rec:
    def __init__(self, log):
        self.log = log

    def __getattr__(self, name):
        return lambda *a, **k: self.log.append((name,) + a)


log = []
me = SimpleNamespace(_active_la_subject={1: -1}, _mm=Rec(log),
                     _grid=SimpleNamespace(get_sl_preset=lambda mid: 3))
mw.MainWindow._on_slider_jog_start(me, 1, -1)
mw.MainWindow._on_slider_jog_start(me, 1, +1)
me._active_la_subject[1] = 2
mw.MainWindow._on_slider_jog_start(me, 1, +1)
assert log == [("send_start_slide", 1, 0, 3), ("send_start_slide", 1, 1, 3),
               ("send_start_look_at_move", 1, 2, 1, 3)], f"the arrows sent {log}"
print("   no subject → a slide; a subject → a look-at move; never a jog     OK")


class FakeCV:
    def __init__(self, log, tracking):
        self.log, self.tracking_mount_id = log, tracking

    def stop_tracking(self):
        self.log.append(("cv.stop_tracking",))
        self.tracking_mount_id = 0


def pick(slot, active, cv_mount):
    log = []
    me = SimpleNamespace(_active_la_subject={1: active}, _mm=Rec(log),
                         _grid=SimpleNamespace(set_active_la_subject=lambda *a: None),
                         _cv_window=FakeCV(log, cv_mount) if cv_mount is not None else None)
    me._mm.state = lambda mid: SimpleNamespace(slot_occupied_mask=0b1111)
    mw.MainWindow._on_look_at_subject_selected(me, 1, slot)
    return log


assert pick(2, -1, 1) == [("cv.stop_tracking",), ("send_switch_subject", 1, 2)], \
    f"choosing a subject while CV tracks this mount sent {pick(2, -1, 1)} — CV must stop first"
assert pick(2, -1, 3) == [("send_switch_subject", 1, 2)], "CV on another mount was stopped"
assert pick(2, -1, None) == [("send_switch_subject", 1, 2)], "no CV window: the switch did not go"
assert pick(2, 2, 1) == [], "deselecting a subject stopped CV or sent a switch"
print("   a subject chosen while CV tracks this mount: CV stops, then the")
print("   switch — and only for that mount                                  OK")

import motion.command_dispatcher as cd


class FakeJoy:
    connected = True

    def __init__(self):
        self.vals = (0, 0, 0, 0)

    def poll(self):
        return True

    @property
    def axes(self):
        v = self.vals
        return SimpleNamespace(to_int=lambda scale: v)


class KwRec:
    """Records (name, args, kwargs): the dispatcher passes the mask by keyword."""
    def __init__(self, log):
        self.log = log

    def __getattr__(self, name):
        return lambda *a, **k: self.log.append((name, a, k))


jlog = []
joy = FakeJoy()
dsp = cd.CommandDispatcher(joy, KwRec(jlog))
dsp._timer.stop()
dsp.active_mount_id = 1
taken = []


def on_taken(mid):
    taken.append(mid)
    jlog.append(("cv.stop_tracking", (mid,), {}))
    dsp.set_cv_tracking(mid, False)       # what the CV window's stop tells it


dsp.pan_tilt_taken.connect(on_taken)
dsp.set_cv_tracking(1, True)
joy.vals = (0, 0, 400, 0)                 # the slider stick only: CV keeps the head
dsp._tick()
assert taken == [] and [e[0] for e in jlog] == ["send_jog"] and \
       jlog[0][2].get("axis_mask") == 0x0C and jlog[0][1][1:3] == (0, 0), \
    f"a slider-only stick stopped CV, or was not sent slider/zoom only: {jlog}"
jlog.clear()
joy.vals = (500, 0, 0, 0)                 # pan
dsp._tick()
assert taken == [1], "moving pan with CV tracking did not stop CV"
assert [e[0] for e in jlog] == ["cv.stop_tracking", "send_jog"], \
    f"the stick's pan did not follow CV stopping: {jlog}"
name, args, kw = jlog[1]
assert args[1] == 500 and kw.get("axis_mask", 0x0F) == 0x0F, \
    f"after CV stopped, the stick's pan went as {args} {kw}, not a full jog with pan"
print("   the stick moving pan/tilt stops CV, then jogs; the slider stick")
print("   alone does not                                                   OK")

import ui.cv_window as cwm
from config.mount_config import AppConfig


class FakeCapture:
    is_open = False

    @staticmethod
    def list_devices(max_test=5):
        return [0]

    def close(self):
        pass


cwm.CaptureSource = FakeCapture


class FakeLoop:
    def __init__(self):
        self.is_tracking = True
        self.calls = []

    def stop_tracking(self):
        self.calls.append("stop_tracking")
        self.is_tracking = False

    def shutdown(self):
        self.calls.append("shutdown")


class FakeMM(QObject):
    def state(self, mount_id):
        return SimpleNamespace(active_pt_preset=2)

    def __getattr__(self, name):
        return lambda *a, **k: None


main = QWidget()
main.show()
w = cwm.CVWindow(1, FakeMM(), AppConfig(), main)
w.show()
app.processEvents()
gone = []
w.destroyed.connect(lambda *_: gone.append(True))
loop = FakeLoop()
w._tracking_loop = loop
drawn = []
w._video.set_frame = lambda f: drawn.append(1)
import numpy as np
frame = np.zeros((8, 8, 3), dtype=np.uint8)
w._on_frame(frame)
assert drawn == [1], "a visible window did not draw"
w.close()
app.processEvents()
# The window's own data first: if closing really closed it, the window is
# already deleted and asking it anything would raise instead of saying so.
assert not gone and loop.calls == [], \
    f"closing while tracking stopped it (calls {loop.calls}, deleted {bool(gone)})"
assert not w.isVisible(), "closing while tracking left the window on screen"
w._on_frame(frame)
assert drawn == [1], "a hidden window still draws every frame on the tracking thread"
print("   closed while tracking: hidden, still tracking, drawing nothing    OK")
w.show()
w._on_frame(frame)
assert drawn == [1, 1], "brought back, the window does not draw"
w.stop_tracking()                          # e.g. the main page's Stop Tracking
w.close()
for _ in range(5):
    app.processEvents()
assert "shutdown" in loop.calls and gone, "closed while NOT tracking, the window did not close for real"
print("   closed when not tracking: the feed stops and the window goes      OK")

w2 = cwm.CVWindow(1, FakeMM(), AppConfig(), main)
w2.show()
loop2 = FakeLoop()
w2._tracking_loop = loop2
main.hide()                                # the main window has gone
w2.close()
assert "shutdown" in loop2.calls, "with the main window gone, a tracking CV window lingered hidden"
print("   with the main window gone, it closes even while tracking          OK")

# ---- 5. the web app ----------------------------------------------------------------
print("\n5. the web app:")
if sys.platform == "darwin" and shutil.which("swiftc"):
    pm = re.search(r'R"rawhtml\((.*)\)rawhtml"', WEB, re.S)
    SETUP = r"""
const _sent = [];
wsSend = b => _sent.push(Array.from(b));
const R = window.__results = {stepErrors: []};
window.__measure = () => Object.assign({errors: window.__errors}, window.__results);
function step(f) { try { f(); } catch (e) { R.stepErrors.push(String(e)); } }
const take = () => { const s = _sent.filter(b => b[6] === CMD_START_SLIDE || b[6] === CMD_START_LOOK_AT_MOVE)
                                     .map(b => [b[6], b[3]].concat(b.slice(7, 7 + b[2] - 4)));
                     _sent.length = 0; return s; };
step(() => {
    const p = new Uint8Array(11);
    p[1] = FLAG_HAS_SLIDER | FLAG_LOOK_AT_MODE; p[2] = 2; p[3] = 3; p[8] = 0xFF; p[9] = 0xFF;
    _onOnePkt(buildPkt(1, CMD_STATUS, p), 0);
    _sent.length = 0;
    onPosClick(8); onPosClick(9);
    R.none = take();
    camSt[1].activeLaSubject = 2;
    onPosClick(9);
    R.subject = take();
    camSt[1].activeLaSubject = -1;
    setExtView(true); showExtPage('positions');
    document.getElementById('ext-pcell-1-8').click();
    R.ext = take();
});
"""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="wkslide-"))
    (tmp / "webapp.html").write_text(pm.group(1))
    (tmp / "setup.js").write_text(SETUP)
    r = subprocess.run(["swiftc", "-O", str(REPO / "tools/wkshot.swift"), "-o", str(tmp / "wkshot")],
                       capture_output=True, text=True)
    assert r.returncode == 0, "wkshot.swift does not compile:\n" + r.stderr[-2000:]
    rr = subprocess.run([str(tmp / "wkshot"), str(tmp / "webapp.html"), "390", "844", "-",
                         str(tmp / "setup.js")], capture_output=True, text=True, timeout=60)
    shutil.rmtree(tmp, ignore_errors=True)
    assert rr.returncode == 0, f"wkshot failed ({rr.returncode}): {rr.stderr.strip()}"
    got = json.loads(rr.stdout.strip().splitlines()[-1])
    assert not got["errors"], f"the page threw: {got['errors']}"
    assert not got["stepErrors"], f"the web steps threw: {got['stepErrors']}"
    SL, LA = 0x2C, 0x27
    assert got["none"] == [[SL, 1, 0, 3], [SL, 1, 1, 3]], \
        f"◀/▶ with no subject sent {got['none']} — want slides (once it was look-at on subject 0)"
    assert got["subject"] == [[LA, 1, 2, 1, 3]], f"▶ with subject 3 chosen sent {got['subject']}"
    assert got["ext"] == [[SL, 1, 0, 3]], f"the Extended page's ◀ with no subject sent {got['ext']}"
    print("   ◀/▶ with no subject slide, from both views; with one, a look-at   OK")
else:
    assert "function laArrow(cam, dir)" in WEB and WEB.count("laArrow(") >= 3
    print("   swiftc and WebKit not here: checked from the page's source")

print("\nALL CHECKS PASSED")
