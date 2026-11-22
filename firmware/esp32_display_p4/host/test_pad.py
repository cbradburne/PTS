"""The P4 display's USB controller code, on the Mac: report parsing and the jog law.

  python3 firmware/esp32_display_p4/host/test_pad.py

1. Each controller family's report, built byte by byte from the layout its
   Linux driver documents, comes out as the right sticks and buttons — with Y
   one way up for all of them (the PC app's convention: down positive).
2. A HID pad known only by its report descriptor is found and read, including
   the descriptor traps: a logical max of 0xFF written in one byte, signed
   axes, a report ID, and PlayStation copies that put their TRIGGERS on Rx/Ry.
3. The jog law is the PC app's own, to the count: pad_map.h is driven with
   the same stick movements as pc_app's circ_to_sq(), deadzone and smoothing
   (imported, not copied), through the dispatcher's send rule.
"""
import os, sys, pathlib, random, struct, subprocess, tempfile

HERE = pathlib.Path(__file__).resolve().parent
P4 = HERE.parent
REPO = P4.parent.parent
sys.path.insert(0, str(REPO / "pc_app"))
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
from motion.joystick import circ_to_sq, DEFAULT_DEADZONE, _SMOOTH_ALPHA, JoystickHandler  # noqa: E402

DRIVER = r'''
#include <stdio.h>
#include <string.h>
#include <string>
#include <vector>
#include "pad_reports.h"
#include "pad_map.h"
static std::vector<uint8_t> hex(const char *s) {
    std::vector<uint8_t> v;
    for (; s[0] && s[1]; s += 2) { unsigned b; sscanf(s, "%2x", &b); v.push_back((uint8_t)b); }
    return v;
}
int main() {
    char cmd[32], arg[4096];
    HidLayout L; SwitchCal cal; PadJogMapper map; bool guide = false; PadInput in;
    while (scanf("%31s", cmd) == 1) {
        std::string c = cmd;
        if (c == "map") {
            PadInput m; scanf("%f %f %f %f", &m.lx, &m.ly, &m.rx, &m.ry);
            int16_t j[4];
            if (map.tick(m, j)) printf("send %d %d %d %d\n", j[0], j[1], j[2], j[3]);
            else printf("hold\n");
        } else if (c == "map_reset") { map.reset(); printf("ok\n"); }
        else if (c == "switch_reset") { cal = SwitchCal(); printf("ok\n"); }
        else if (c == "gip_reset") { guide = false; in = PadInput(); printf("ok\n"); }
        else {
            scanf("%4095s", arg);
            std::vector<uint8_t> b = hex(arg);
            bool ok = false;
            if (c == "desc") {
                ok = hid_parse_descriptor(b.data(), (int)b.size(), &L);
                if (ok) printf("ok %u %u/%u %u/%u %u/%u %u/%u %u/%u %u/%u %u/%u %u %u %d %d\n", L.report_id,
                               L.x.bit, L.x.size, L.y.bit, L.y.size, L.z.bit, L.z.size, L.rx.bit, L.rx.size,
                               L.ry.bit, L.ry.size, L.rz.bit, L.rz.size, L.hat.bit, L.hat.size,
                               L.btn_bit, L.btn_count, (int)L.x.lmin, (int)L.x.lmax);
                else printf("no\n");
                continue;
            }
            if (c == "ds5") ok = pad_parse_dualsense(b.data(), (int)b.size(), &in);
            else if (c == "ds4") ok = pad_parse_ds4(b.data(), (int)b.size(), &in);
            else if (c == "xinput") ok = pad_parse_xinput(b.data(), (int)b.size(), &in);
            else if (c == "gip") ok = pad_parse_gip(b.data(), (int)b.size(), &in, &guide);
            else if (c == "switch") ok = pad_parse_switch(b.data(), (int)b.size(), &in, &cal);
            else if (c == "hid") ok = pad_parse_hid(L, b.data(), (int)b.size(), &in);
            if (ok) printf("ok %.4f %.4f %.4f %.4f %lu\n", in.lx, in.ly, in.rx, in.ry, (unsigned long)in.buttons);
            else printf("no\n");
        }
    }
}
'''

BTN = dict(S=0x1, E=0x2, W=0x4, N=0x8, L1=0x10, R1=0x20, L2=0x40, R2=0x80, L3=0x100, R3=0x200,
           SYSTEM=0x400, SELECT=0x800, START=0x1000, CAPTURE=0x2000,
           UP=0x4000, DOWN=0x8000, LEFT=0x10000, RIGHT=0x20000)


def build():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="p4pad_"))
    (tmp / "driver.cpp").write_text(DRIVER)
    exe = tmp / "driver"
    r = subprocess.run(["c++", "-std=c++17", "-O1", "-Wall", "-Wextra", "-Wno-unused-result",
                        "-I", str(P4), str(tmp / "driver.cpp"), "-o", str(exe)],
                       capture_output=True, text=True)
    assert r.returncode == 0, "the parsers do not compile on their own:\n" + r.stderr
    warn = [l for l in r.stderr.splitlines() if "warning" in l and str(P4) in l]
    assert not warn, "warnings in the P4 headers:\n" + "\n".join(warn)
    return exe


EXE = build()


def run(lines):
    r = subprocess.run([str(EXE)], input="\n".join(lines) + "\n", capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines()


def parse(kind, data):
    out = run([f"{kind} {bytes(data).hex()}"])[-1].split()
    if out[0] == "no":
        return None
    lx, ly, rx, ry = (float(x) for x in out[1:5])
    return lx, ly, rx, ry, int(out[5])


def near(a, b, tol=0.01):
    return abs(a - b) <= tol


# ---- 1. the families ------------------------------------------------------------
print("1. each controller family:")

def ds5(lx=128, ly=128, rx=128, ry=128, b0=0x08, b1=0, b2=0, rid=0x01):
    r = bytearray(64); r[0] = rid
    r[1], r[2], r[3], r[4] = lx, ly, rx, ry
    r[8], r[9], r[10] = b0, b1, b2
    return r

v = parse("ds5", ds5())
assert v and all(near(x, 0, 0.01) for x in v[:4]) and v[4] == 0, f"DualSense at rest reads {v}"
v = parse("ds5", ds5(lx=255, ly=0, rx=0, ry=255))
assert near(v[0], 1) and near(v[1], -1) and near(v[2], -1) and near(v[3], 1), \
    f"DualSense sticks: left full right and UP, right full left and DOWN, read {v[:4]}"
v = parse("ds5", ds5(b0=0x20 | 0x00, b1=0x01 | 0x02, b2=0x01 | 0x04))
assert v[4] == BTN["S"] | BTN["UP"] | BTN["L1"] | BTN["R1"] | BTN["SYSTEM"] | BTN["CAPTURE"], \
    f"DualSense cross, d-pad up, L1, R1, PS, mute read as {v[4]:#x}"
v = parse("ds5", ds5(b0=0x03))
assert v[4] == BTN["DOWN"] | BTN["RIGHT"], "DualSense hat 3 is down-right"
assert parse("ds5", ds5(rid=0x31)) is None, "a DualSense report that is not 0x01 was read as one"
assert parse("ds5", ds5()[:9]) is None, "a short DualSense report was read"
print("   DualSense: sticks, hat, cross / L1 / R1 / PS / mute       OK")

def ds4(lx=128, ly=128, rx=128, ry=128, b0=0x08, b1=0, b2=0):
    r = bytearray(64); r[0] = 0x01
    r[1], r[2], r[3], r[4] = lx, ly, rx, ry
    r[5], r[6], r[7] = b0, b1, b2
    return r

v = parse("ds4", ds4(rx=255, b0=0x80 | 0x06, b1=0x20, b2=0x01))
assert near(v[2], 1) and v[4] == BTN["N"] | BTN["LEFT"] | BTN["START"] | BTN["SYSTEM"], \
    f"DualShock 4 right stick / triangle / left / options / PS read {v}"
print("   DualShock 4: the same, three bytes earlier                OK")

def xinput(lx=0, ly=0, rx=0, ry=0, b0=0, b1=0, lt=0, rt=0):
    return bytearray([0x00, 0x14, b0, b1, lt, rt]) + struct.pack("<hhhh", lx, ly, rx, ry) + bytes(6)

v = parse("xinput", xinput(lx=32767, ly=32767, rx=-32768, ry=-32768))
assert near(v[0], 1) and near(v[1], -1) and near(v[2], -1) and near(v[3], 1), \
    f"Xbox 360: its Y is up-positive and must come out down-positive, read {v[:4]}"
v = parse("xinput", xinput(b0=0x01 | 0x10, b1=0x01 | 0x10 | 0x04, lt=0xFF))
assert v[4] == BTN["UP"] | BTN["START"] | BTN["L1"] | BTN["S"] | BTN["SYSTEM"] | BTN["L2"], \
    f"Xbox 360 d-pad up / start / LB / A / guide / LT read {v[4]:#x}"
print("   Xbox 360 type: Y turned over, buttons, trigger            OK")

def gip(lx=0, ly=0, rx=0, ry=0, b0=0, b1=0):
    return bytearray([0x20, 0x00, 0x01, 0x0E, b0, b1]) + struct.pack("<HHhhhh", 0, 0, lx, ly, rx, ry)

out = run(["gip_reset",
           f"gip {gip(ly=32767, b0=0x10 | 0x04, b1=0x10).hex()}",
           f"gip {bytes([0x07, 0x20, 0x05, 0x02, 0x01, 0x5B]).hex()}",
           f"gip {gip().hex()}",
           f"gip {bytes([0x07, 0x20, 0x06, 0x02, 0x00, 0x5B]).hex()}"])
first, guide_down, still, guide_up = (int(l.split()[5]) for l in out[1:])
assert near(float(out[1].split()[2]), -1), "Xbox One: stick up must read y = -1"
assert first == BTN["S"] | BTN["START"] | BTN["L1"], f"Xbox One A / menu / LB read {first:#x}"
assert guide_down & BTN["SYSTEM"] and still & BTN["SYSTEM"], \
    "the Xbox button comes in its own message; a stick report after it dropped it"
assert not guide_up & BTN["SYSTEM"], "the Xbox button's release was missed"
print("   Xbox One / Series: sticks, buttons, the Xbox button kept   OK")

def sw(lx, ly, rx, ry, r=0, s=0, l=0):
    b = bytearray(49); b[0] = 0x30; b[3], b[4], b[5] = r, s, l
    b[6] = lx & 0xFF; b[7] = (lx >> 8) | ((ly & 0x0F) << 4); b[8] = ly >> 4
    b[9] = rx & 0xFF; b[10] = (rx >> 8) | ((ry & 0x0F) << 4); b[11] = ry >> 4
    return b

rest = (2100, 1990, 2010, 2060)
lines = ["switch_reset"] + [f"switch {sw(*rest).hex()}" for _ in range(8)]
lines += [f"switch {sw(rest[0] + 1500, rest[1] + 750, rest[2], rest[3] - 1500).hex()}"]
out = run(lines)
learning = [tuple(float(x) for x in l.split()[1:5]) for l in out[1:9]]
assert all(v == (0, 0, 0, 0) for v in learning), \
    "the Switch Pro's sticks read as moved while it was still learning where rest is"
lx, ly, rx, ry = (float(x) for x in out[9].split()[1:5])
assert near(lx, 1) and near(ly, -0.5) and near(rx, 0) and near(ry, 1), \
    f"Switch Pro, from its own rest: left full right, half UP; right full DOWN — read {lx, ly, rx, ry}"
held = (3600, 2048, 2048, 2048)          # a thumb on the stick while plugging in
out = run(["switch_reset"] + [f"switch {sw(*held).hex()}" for _ in range(8)]
          + [f"switch {sw(2048, 2048, 2048, 2048).hex()}"])
assert near(float(out[9].split()[1]), 0), \
    "a stick held while the Switch Pro was learning became its rest: centre should fall back to 2048"
v = parse("switch", sw(2048, 2048, 2048, 2048, r=0x04 | 0x40, s=0x10, l=0x02 | 0x40))
assert v[4] == BTN["S"] | BTN["R1"] | BTN["SYSTEM"] | BTN["UP"] | BTN["L1"], \
    f"Switch Pro B (bottom) / R / home / up / L read {v[4]:#x}"
print("   Switch Pro: 12-bit sticks from their own rest, buttons      OK")

# ---- 2. any HID pad ------------------------------------------------------------------
print("\n2. a pad known only by its report descriptor:")

def item(tag, typ, value, size=1):
    code = {0: 0, 1: 1, 2: 2, 4: 3}[size]
    return bytes([(tag << 4) | (typ << 2) | code]) + value.to_bytes(size, "little", signed=value < 0)

G, L, M = 1, 2, 0
def page(p): return item(0, G, p)
def usage(u, size=1): return item(0, L, u, size)
def lmin(v, size=1): return item(1, G, v, size)
def lmax(v, size=1): return item(2, G, v, size)
def rsize(v): return item(7, G, v)
def rcount(v): return item(9, G, v)
def rid(v): return item(8, G, v)
def umin(v): return item(1, L, v)
def umax(v): return item(2, L, v)
def inp(f): return item(8, M, f)
def coll(t): return item(10, M, t)
END = bytes([0xC0])

def pad_desc(right=(0x32, 0x35), extra=(), one_byte_max=False, signed=False):
    d = page(1) + usage(0x05) + coll(1) + rid(3)
    d += usage(0x30) + usage(0x31) + usage(right[0]) + usage(right[1])
    if signed: d += lmin(-127) + lmax(127)
    elif one_byte_max: d += lmin(0) + lmax(-1)               # 0xFF in one byte: the trap
    else: d += lmin(0) + lmax(255, 2)
    d += rsize(8) + rcount(4) + inp(0x02)
    for u in extra:
        d += usage(u) + lmin(0) + lmax(255, 2) + rsize(8) + rcount(1) + inp(0x02)
    d += usage(0x39) + lmin(0) + lmax(7) + rsize(4) + rcount(1) + inp(0x42)
    d += rsize(4) + rcount(1) + inp(0x01)
    d += page(9) + umin(1) + umax(12) + lmin(0) + lmax(1) + rsize(1) + rcount(12) + inp(0x02)
    d += rsize(4) + rcount(1) + inp(0x01)
    return d + END

def desc(d):
    out = run([f"desc {d.hex()}"])[-1].split()
    return None if out[0] == "no" else out

lay = desc(pad_desc())
assert lay, "a plain game pad descriptor was not recognised"
rid_, x, y, z, rx_, ry_, rz, hat, bbit, bcount, xmin, xmax = lay[1:]
assert rid_ == "3" and x == "0/8" and y == "8/8" and z == "16/8" and rz == "24/8", f"axes found at {lay}"
assert hat == "32/4" and bbit == "40" and bcount == "12", f"hat / buttons found at {lay}"
print("   axes, hat and buttons found where they are, report ID 3    OK")

def hid(layout_desc, report):
    out = run([f"desc {layout_desc.hex()}", f"hid {bytes(report).hex()}"])[-1].split()
    return None if out[0] == "no" else (*(float(v) for v in out[1:5]), int(out[5]))

btns = 1 << 4                                               # button 5: L1 on DirectInput pads
rep = [3, 0xFF, 0x00, 0x80, 0x80, 0x02, btns & 0xFF, btns >> 8]
v = hid(pad_desc(), rep)
assert near(v[0], 1) and near(v[1], -1) and near(v[2], 0) and near(v[3], 0), f"generic sticks read {v[:4]}"
assert v[4] == BTN["RIGHT"] | BTN["L1"], f"hat 2 and button 5 read as {v[4]:#x}"
assert hid(pad_desc(), [4] + rep[1:]) is None, "a report with another ID was read as the pad's"
print("   sticks, hat, button 5 = L1; other report IDs ignored       OK")

lay = desc(pad_desc(one_byte_max=True))
assert lay[-1] == "255", f"logical max 0xFF in one byte read as {lay[-1]}, not 255"
v = hid(pad_desc(one_byte_max=True), [3, 0xFF, 0, 0x80, 0x80, 0x08, 0, 0])
assert near(v[0], 1) and near(v[1], -1), "with the one-byte 0xFF maximum the sticks read wrong"
print("   logical max 0xFF in one byte is 255, not -1                OK")

v = hid(pad_desc(signed=True), [3, 0x81, 0x7F, 0, 0, 0x08, 0, 0])
assert near(v[0], -1) and near(v[1], 1) and near(v[2], 0), f"signed axes read {v[:4]}"
print("   signed axes                                                 OK")

# PlayStation copies: sticks X Y Z Rz, triggers on Rx Ry.  Rx/Ry must NOT steer.
sony = pad_desc(right=(0x32, 0x35), extra=(0x33, 0x34))
v = hid(sony, [3, 0x80, 0x80, 0xFF, 0x00, 0xFF, 0xFF, 0x08, 0, 0])
assert near(v[2], 1) and near(v[3], -1), \
    f"a pad with Z/Rz sticks and Rx/Ry triggers steered from its triggers: right stick {v[2:4]}"
xb = pad_desc(right=(0x33, 0x34))
v = hid(xb, [3, 0x80, 0x80, 0xFF, 0x00, 0x08, 0, 0])
assert near(v[2], 1) and near(v[3], -1), f"a pad with only Rx/Ry lost its right stick: {v[2:4]}"
print("   right stick from Z/Rz, Rx/Ry only when there is no Z/Rz     OK")

kbd = page(1) + usage(0x06) + coll(1) + page(7) + umin(0xE0) + umax(0xE7) + lmin(0) + lmax(1) \
      + rsize(1) + rcount(8) + inp(0x02) + END
assert desc(kbd) is None, "a keyboard was taken for a game pad"
mouse = page(1) + usage(0x02) + coll(1) + usage(0x30) + usage(0x31) + lmin(-127) + lmax(127) \
        + rsize(8) + rcount(2) + inp(0x06) + END
assert desc(mouse) is None, "a mouse (X/Y outside a game pad collection) was taken for a game pad"
print("   a keyboard and a mouse are not game pads                   OK")

# ---- 3. the jog law against the PC app's -------------------------------------------------
print("\n3. the jog law, against pc_app's own functions:")

class _Dz:
    _deadzone = DEFAULT_DEADZONE

def pc_model(frames):
    """pc_app: joystick.poll() then command_dispatcher._tick(), for one stick stream."""
    s = [0.0] * 4; last = [0] * 4; last_zero = True; out = []
    for lx, ly, rx, ry in frames:
        dz = lambda v: JoystickHandler._apply_deadzone(_Dz, v)
        raw = [dz(rx), -dz(ry), dz(lx), -dz(ly)]
        s = [_SMOOTH_ALPHA * r + (1 - _SMOOTH_ALPHA) * o for r, o in zip(raw, s)]
        ints = [int(v * 1000) for v in s]
        changed = ints != last
        last = ints
        p, t = circ_to_sq(ints[0], ints[1])
        sl, z = circ_to_sq(ints[2], ints[3])
        is_zero = p == 0 and t == 0 and sl == 0 and z == 0
        send = not (not changed and is_zero and last_zero)
        last_zero = is_zero
        clamp = lambda v: max(-1000, min(1000, v))
        out.append(f"send {clamp(p)} {clamp(t)} {clamp(sl)} {clamp(z)}" if send else "hold")
    return out

rng = random.Random(20261122)
frames = []
for _ in range(60):                       # bursts of movement and rest
    if rng.random() < 0.35:
        frames += [(0.0, 0.0, 0.0, 0.0)] * rng.randint(1, 20)
    else:
        a = [rng.uniform(-1, 1) for _ in range(4)]
        for _ in range(rng.randint(1, 15)):
            a = [max(-1.0, min(1.0, v + rng.uniform(-0.15, 0.15))) for v in a]
            frames.append(tuple(round(v, 4) for v in a))
frames += [(0.05, -0.07, 0.0, 0.079)] * 5 + [(0.0, 0.0, 0.0, 0.0)] * 15   # inside the deadzone
frames += [(1.0, -1.0, 1.0, 1.0)] * 10 + [(0.0, 0.0, 0.0, 0.0)] * 15      # a square-gate corner
got = run(["map_reset"] + [f"map {f[0]} {f[1]} {f[2]} {f[3]}" for f in frames])[1:]
want = pc_model(frames)
bad = [(i, frames[i], g, w) for i, (g, w) in enumerate(zip(got, want)) if g != w]
assert not bad, f"{len(bad)} of {len(frames)} ticks differ from the PC app; first: {bad[0]}"
print(f"   {len(frames)} ticks, every jog and every silence the PC app's     OK")

sends = [g for g in got if g.startswith("send")]
assert any("1000 -1000" in g or "1000 1000" in g for g in sends), "the corner case never reached full"
assert all(abs(int(v)) <= 1000 for g in sends for v in g.split()[1:]), "a jog went past 1000"
print("   a square-gate corner gives 1000, never 1414                 OK")

# The two rules an operator relies on.
out = run(["map_reset"] + ["map 0 0 0.6 0"] * 30 + ["map 0 0 0 0"] * 30)[1:]
moving, stopping = out[:30], out[30:]
assert all(l.startswith("send") for l in moving), \
    "a held stick did not send every tick: the mount's 500 ms jog watchdog would stop it"
first_zero = next(i for i, l in enumerate(stopping) if l == "send 0 0 0 0")
assert all(l == "hold" for l in stopping[first_zero + 1:]), \
    "after the stick came back, more than one zero jog was sent"
assert all(l.startswith("send") and l != "send 0 0 0 0" for l in stopping[:first_zero]), \
    "the smoothing tail was cut short or interrupted"
print(f"   held: a jog every tick; let go: {first_zero} ticks of tail, one zero  OK")

print("\nALL CHECKS PASSED")
