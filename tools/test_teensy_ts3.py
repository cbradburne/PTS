"""The mount's TeensyStep 3 test build: the rig's motion code, another library under it.

firmware/teensy41_mount_ts3 is the production Teensy firmware with TeensyStep 3.0.0
in place of our TeensyStep4 copy, for bench tests (asked for on 2026-11-28).  The
port is one header, ts4_on_ts3.h, that gives MountMotion TeensyStep4's interface
on TeensyStep 3; everything else is a copy.

1. The copies stay copies.  MountMotion.cpp is the production file; MountMotion.h
   and the .ino differ only in the lines that say so.  A change to production
   fails here until it is copied, so the bench never tests yesterday's code.
2. Every TeensyStep4 call MountMotion makes is one ts4_on_ts3.h answers.
3. The mapping, compiled on this machine against a stand-in for TeensyStep 3 that
   records what it is told: what each TeensyStep4 call becomes, both ways of
   stopping a rotation (TS3_SOFT_STOP), and the TeensyStep4 rules kept on purpose
   (an override only for a rotation, in whole steps/s; acceleration at most v²/2;
   settings that wait for the next start).
4. The library and the build target: TeensyStep 3.0.0 pinned, `teensyts3` in both
   build scripts.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re, shutil, subprocess, tempfile
REPO = pathlib.Path(__file__).resolve().parent.parent
PROD = REPO / "firmware/teensy41_mount"
TS3 = REPO / "firmware/teensy41_mount_ts3"
ADAPTER = (TS3 / "ts4_on_ts3.h").read_text()
TMP = pathlib.Path(tempfile.mkdtemp())


def lines_without(text, pred):
    return [ln for ln in text.split("\n") if not pred(ln)]


# ---- 1. the copies stay copies -------------------------------------------------
print("1. the test build is the production code:")
for name in ("MountMotion.cpp", "protocol.h"):
    assert (TS3 / name).read_text() == (PROD / name).read_text(), \
        f"{name} differs from production — copy the change into firmware/teensy41_mount_ts3"
print("   MountMotion.cpp and protocol.h: the same files          OK")
link = TS3 / "shared"
assert link.is_symlink() and os.readlink(link) == "../shared", \
    "the test build's `shared` is not the symlink production has"

pairs = {
    "MountMotion.h": ("MountMotion.h", {"#include <teensystep4.h>"}),
    "teensy41_mount_ts3.ino": ("teensy41_mount.ino", set()),
}
for ts3_name, (prod_name, dropped) in pairs.items():
    ours = lines_without((TS3 / ts3_name).read_text(), lambda ln: "(TS3)" in ln)
    theirs = lines_without((PROD / prod_name).read_text(), lambda ln: ln in dropped)
    assert ours == theirs, \
        f"{ts3_name} differs from production {prod_name} beyond its (TS3) lines —\n" \
        "    copy the change, or mark a deliberate one (TS3)"
h = (TS3 / "MountMotion.h").read_text()
assert '#include "ts4_on_ts3.h"' in h and "teensystep4.h>" not in h, \
    "the test build's MountMotion.h does not take TeensyStep4's interface from ts4_on_ts3.h"
ino = (TS3 / "teensy41_mount_ts3.ino").read_text()
loop = ino[ino.index("void loop() {"):]
assert "TS4::reportErrors(Serial);" in loop[:loop.index("\n}")], \
    "TeensyStep 3's reports are never printed"
print("   MountMotion.h, the .ino: the same but for (TS3) lines    OK")

# ---- 2. every call answered ------------------------------------------------------
print("\n2. what MountMotion asks of TeensyStep4:")
mm = (PROD / "MountMotion.cpp").read_text()
called = set(re.findall(r"_stepper\[[^\]]*\]->([A-Za-z_]+)\(", mm))
called |= {"isMoving"} if "->isMoving" in mm else set()
cls = ADAPTER[ADAPTER.index("class Stepper"):ADAPTER.index("class StepperGroup")]
missing = sorted(c for c in called
                 if not re.search(rf"\b{c}\s*\(", cls) and not re.search(rf"\}}\s*{c};", cls))
assert not missing, f"MountMotion calls {missing}, which ts4_on_ts3.h does not answer"
for call in ("TS4::begin()", "_group.add("):
    assert call in mm, f"MountMotion no longer uses {call}: check ts4_on_ts3.h still needs it"
print(f"   {len(called)} calls, every one answered: {', '.join(sorted(called))}")

# ---- 3. the mapping, against a stand-in TeensyStep 3 -------------------------------
print("\n3. the mapping, compiled here against a stand-in for TeensyStep 3:")
stub = TMP / "stub"
stub.mkdir()
(stub / "Arduino.h").write_text(r'''
#pragma once
#include <cstdint>
#include <cstdio>
#include <cstdarg>
#include <string>
extern uint32_t fakeMillis;
inline uint32_t millis() { return fakeMillis; }
struct Print {
    std::string text;
    int printf(const char *fmt, ...) {
        char b[256]; va_list a; va_start(a, fmt); int n = vsnprintf(b, sizeof b, fmt, a); va_end(a);
        text += b; return n;
    }
};
''')
(stub / "TeensyStep.h").write_text(r'''
#pragma once
#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>
extern std::vector<std::string> calls;
namespace TeensyStep {
    enum class errorCode { OK = 0, speedLimited = -1000, noTimer = 1002 };
    constexpr bool isWarning(errorCode c) { return static_cast<int>(c) < 0; }
    constexpr bool isError(errorCode c) { return static_cast<int>(c) > 0; }
    inline const char* describe(errorCode c) { return c == errorCode::noTimer ? "no timer" : c == errorCode::speedLimited ? "speed limited" : "?"; }
    inline void (*errFunc)(errorCode) = nullptr;
    inline void attachErrFunc(void (*f)(errorCode)) { errFunc = f; calls.push_back("attachErrFunc"); }
    class Stepper {
     public:
        bool moving = false; int32_t pos = 0;
        Stepper(int s, int d) { calls.push_back("Stepper(" + std::to_string(s) + "," + std::to_string(d) + ")"); }
        Stepper(const Stepper&) = delete;
        static std::string f(float v) { char b[32]; snprintf(b, sizeof b, "%g", v); return b; }
        Stepper& setMaxSpeed(float v)      { calls.push_back("setMaxSpeed(" + f(v) + ")"); return *this; }
        Stepper& setAcceleration(float a)  { calls.push_back("setAcceleration(" + f(a) + ")"); return *this; }
        void setPositionCounter(int32_t p) { calls.push_back("setPositionCounter(" + std::to_string(p) + ")"); pos = p; }
        errorCode moveAbsAsync(int32_t p, float = 0) { calls.push_back("moveAbsAsync(" + std::to_string(p) + ")"); moving = true; return errorCode::OK; }
        errorCode rotateAsync(float v)     { calls.push_back("rotateAsync(" + f(v) + ")"); moving = v != 0; return errorCode::OK; }
        errorCode stopAsync()              { calls.push_back("stopAsync()"); return errorCode::OK; }
        void emergencyStop()               { calls.push_back("emergencyStop()"); moving = false; }
        int32_t getPosition() const        { return pos; }
        bool isMoving() const              { return moving; }
    };
}
''')
(TMP / "drive.cpp").write_text(r'''
#include "ts4_on_ts3.h"
#include <cstdio>
#include <string>
#include <vector>
std::vector<std::string> calls;
uint32_t fakeMillis = 5000;
// The stand-in motor inside the adapter, to make it stand still or move.
static TeensyStep::Stepper& inner(TS4::Stepper& s) {
    return *reinterpret_cast<TeensyStep::Stepper*>(&s);   // motor is the first member
}
// A report on one line, its line breaks shown as '~'.
static std::string flat(std::string t) {
    for (auto& c : t) if (c == '\n') c = '~';
    return t;
}
static void step(const char* what) {
    printf("%s:", what);
    for (auto& c : calls) printf(" %s", c.c_str());
    printf("\n");
    calls.clear();
}
int main() {
    TS4::begin();                                  step("begin");
    TS4::Stepper s(2, 3);                          step("make");

    s.setMaxSpeed(-5000); s.setAcceleration(20000); step("settings");
    s.rotateAsync();                               step("rotate");
    s.overrideSpeed(0.5f);                         step("override");
    s.overrideSpeed(0.5f);                         step("override-same");
    s.overrideSpeed(0.33333f);                     step("override-trunc");
    s.setMaxSpeed(9000); s.setAcceleration(1000);  step("settings-wait");
    s.overrideSpeed(1.0f);                         step("override-base-kept");
    inner(s).moving = false;                       // stopped by something else
    s.overrideSpeed(0.9f);                         step("override-stopped-elsewhere");
    inner(s).moving = true;
    printf("moving-before: %d\n", (bool)s.isMoving ? 1 : 0);
    s.overrideSpeed(0.0001f);                      step("override-to-zero");
    printf("moving-after: %d\n", !s.isMoving ? 0 : 1);
    s.overrideSpeed(0.5f);                         step("override-after-stop");

    s.setMaxSpeed(4000); s.setAcceleration(30000);
    s.rotateAsync();                               step("rotate2");
    s.stopAsync();                                 step("stop-rotation");
    inner(s).moving = false;
    s.overrideSpeed(0.7f);                         step("override-idle");

    s.setMaxSpeed(-3000); s.setAcceleration(25000);
    s.setTargetAbs(1234); s.moveAsync();           step("target");
    s.overrideSpeed(0.5f);                         step("override-during-target");
    s.stopAsync();                                 step("stop-target");
    s.emergencyStop();                             step("estop");

    inner(s).moving = false;
    s.setMaxSpeed(0); s.rotateAsync();             step("rotate-zero-idle");
    s.setMaxSpeed(2000); s.rotateAsync();          step("rotate3");
    s.setMaxSpeed(0); s.rotateAsync();             step("rotate-zero-moving");

    s.setMaxSpeed(200); s.setAcceleration(50000); s.rotateAsync();   step("accel-limit");
    s.setMaxSpeed(300000);                         s.rotateAsync();  step("speed-limit");

    s.setPosition(77);                             step("setpos");
    printf("pos: %d\n", (int)s.getPosition());
    s.cleanupTimer();                              step("cleanup");
    TS4::StepperGroup g; g.add(s);                 step("group");

    Print out;
    TeensyStep::errFunc(TeensyStep::errorCode::noTimer);
    TeensyStep::errFunc(TeensyStep::errorCode::speedLimited);
    TeensyStep::errFunc(TeensyStep::errorCode::speedLimited);
    TS4::reportErrors(out);
    printf("report1: %s|\n", flat(out.text).c_str()); out.text.clear();
    TS4::reportErrors(out);
    printf("report2: %s|\n", flat(out.text).c_str()); out.text.clear();
    TeensyStep::errFunc(TeensyStep::errorCode::noTimer);
    fakeMillis += 500;  TS4::reportErrors(out);
    printf("report3: %s|\n", flat(out.text).c_str()); out.text.clear();
    fakeMillis += 600;  TS4::reportErrors(out);
    printf("report4: %s|\n", flat(out.text).c_str());
}
''')
cxx = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
assert cxx, "no C++ compiler"


def run(soft):
    exe = TMP / f"drive{soft}"
    r = subprocess.run([cxx, "-std=c++17", "-Wall", "-Wextra", "-Werror", f"-DTS3_SOFT_STOP={soft}",
                        "-I", str(stub), "-I", str(TS3), str(TMP / "drive.cpp"), "-o", str(exe)],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"ts4_on_ts3.h does not compile:\n{r.stderr[:2500]}"
    out = subprocess.run([str(exe)], capture_output=True, text=True, check=True).stdout
    res = {}
    for line in out.split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            res[k] = v.strip()
    return res


hard, soft = run(0), run(1)
for res in (hard, soft):
    assert res["begin"] == "attachErrFunc", f"begin(): {res['begin']}"
    assert res["make"] == "Stepper(2,3)", f"construction: {res['make']}"
    assert res["settings"] == "", \
        f"setMaxSpeed()/setAcceleration() acted at once on the motor: {res['settings']} — " \
        "TeensyStep4 kept them for the next start"
    assert res["rotate"] == "setMaxSpeed(100000) setAcceleration(20000) rotateAsync(-5000)", \
        f"rotateAsync() after setMaxSpeed(-5000): {res['rotate']} — the speed is the kept one, " \
        "its sign the direction, and TeensyStep 3's maximum only a limit"
    assert res["override"] == "rotateAsync(-2500)", f"overrideSpeed(0.5): {res['override']}"
    assert res["override-same"] == "", \
        f"the same override again sent {res['override-same']!r}: a loop that repeats it every pass " \
        "should cost nothing"
    assert res["override-trunc"] == "rotateAsync(-1666)", \
        f"overrideSpeed(1/3): {res['override-trunc']} — TeensyStep4 kept whole steps/s"
    assert res["settings-wait"] == "", \
        f"setMaxSpeed()/setAcceleration() acted at once on a moving motor: {res['settings-wait']}"
    assert res["override-base-kept"] == "rotateAsync(-5000)", \
        f"overrideSpeed(1) after a new setMaxSpeed: {res['override-base-kept']} — the factor is of " \
        "the speed the rotation started with"
    assert res["override-after-stop"] == "", \
        f"an override after the rotation stopped restarted it: {res['override-after-stop']}"
    assert res["override-stopped-elsewhere"] == "", \
        "an override restarted a rotation that had stopped without a command from here: " \
        f"{res['override-stopped-elsewhere']}"
    assert res["override-idle"] == "", \
        f"an override on a motor standing still started it: {res['override-idle']}"
    assert res["target"] == "setMaxSpeed(3000) setAcceleration(25000) moveAbsAsync(1234)", \
        f"setTargetAbs + moveAsync: {res['target']}"
    assert res["override-during-target"] == "", \
        f"an override changed a move to a target: {res['override-during-target']} — " \
        "TeensyStep4 applied it to rotations only"
    assert res["stop-target"] == "stopAsync()", \
        f"a stop during a move to a target: {res['stop-target']} — at that move's own " \
        "deceleration, as TeensyStep4"
    assert res["estop"] == "emergencyStop()", f"emergencyStop(): {res['estop']}"
    assert res["rotate-zero-idle"] == "", \
        f"rotateAsync() at speed 0 on a motor standing still did {res['rotate-zero-idle']}"
    assert res["accel-limit"].endswith("setAcceleration(20000) rotateAsync(200)"), \
        f"acceleration at 200 steps/s: {res['accel-limit']} — TeensyStep4 limited it to v²/2"
    assert res["speed-limit"].endswith("rotateAsync(100000)"), \
        f"a speed above TeensyStep4's 100,000 steps/s: {res['speed-limit']}"
    assert res["setpos"] == "setPositionCounter(77)" and res["pos"] == "77", \
        f"setPosition: {res['setpos']}, getPosition: {res['pos']}"
    assert res["cleanup"] == "" and res["group"] == "", "cleanupTimer() or the group did something"
    assert res["moving-before"] == "1", "isMoving read false for a rotating motor"

print("   rotate, override (whole steps/s, of the starting speed), target,")
print("   emergency stop, position: as TeensyStep4 did them        OK")
print("   an override only for a running rotation; settings wait for a start  OK")

# The two ways to stop a rotation.
assert hard["override-to-zero"] == "emergencyStop()" and hard["stop-rotation"] == "emergencyStop()" \
    and hard["rotate-zero-moving"] == "emergencyStop()", \
    f"TS3_SOFT_STOP=0 should stop a rotation at once, as production: {hard}"
assert hard["moving-after"] == "0" and soft["moving-after"] == "1", \
    "isMoving does not follow the motor: standing after a stop at once, still moving while " \
    f"it decelerates ({hard['moving-after']} / {soft['moving-after']})"
# TS3_SOFT_STOP=1 decelerates as TeensyStep4 itself was written to (before our
# fix made it stop dead): a stop at the acceleration set last, an override that
# comes out at 0 at the one the rotation started with (here 20000, while 1000
# has been set since).
assert soft["stop-rotation"] == "setAcceleration(30000) stopAsync()" and \
    soft["rotate-zero-moving"] == "setAcceleration(25000) stopAsync()", \
    "TS3_SOFT_STOP=1: a stop should decelerate at the acceleration set last: " \
    f"{soft['stop-rotation']} / {soft['rotate-zero-moving']}"
assert soft["override-to-zero"] == "stopAsync()", \
    f"TS3_SOFT_STOP=1: an override to 0 changed the acceleration: {soft['override-to-zero']}"
assert re.search(r"#ifndef TS3_SOFT_STOP\s*\n#define TS3_SOFT_STOP 0\b", ADAPTER), \
    "the default is no longer production's stop at once"
print("   a rotation's stop: at once by default (production), decelerating with 1   OK")

# Reports: counted wherever they happen, printed from loop(), at most once a second.
r1, r2, r3, r4 = (hard[f"report{i}"] for i in (1, 2, 3, 4))
assert "1 error(s), last: no timer" in r1 and "2 warning(s), last: speed limited" in r1, r1
assert r2 == "|", f"nothing new, yet it printed: {r2}"
assert r3 == "|", f"printed again within a second: {r3}"
assert "1 error(s), last: no timer" in r4 and "warning" not in r4, r4
print("   TeensyStep 3's reports: counted, printed from loop() once a second   OK")

# ---- 4. the library and the target -----------------------------------------------------
print("\n4. the library and the build target:")
props = (REPO / "libraries/TeensyStep/library.properties").read_text()
assert "name=TeensyStep\n" in props and "version=3.0.0\n" in props, \
    "libraries/TeensyStep is not TeensyStep 3.0.0"
assert '#include <TeensyStep.h>' in ADAPTER
sh = (REPO / "tools/build.sh").read_text()
bat = (REPO / "tools/build.bat").read_text()
assert 'teensyts3) echo "$REPO/firmware/teensy41_mount_ts3" ;;' in sh and \
       'teensyts3) echo "$FQBN_TEENSY" ;;' in sh, "build.sh has no teensyts3 target"
assert re.search(r'targets="\$\{\*:-[^"]*\bteensyts3\b', sh), \
    "a plain tools/build.sh no longer compiles the test build, so it can rot unseen"
assert re.search(r'if /i "%~1"=="teensyts3" \(\s*\r?\n\s*set "FQBN=teensy:avr:teensy41[^"]*"\s*\r?\n\s*'
                 r'set "SKETCH=firmware\\teensy41_mount_ts3"', bat), "build.bat has no teensyts3 target"
print("   TeensyStep 3.0.0 pinned; teensyts3 in build.sh and build.bat   OK")

print("\nALL CHECKS PASSED")
