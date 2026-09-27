"""Camera and position names, held by the hub and shared by every client.

Three implementations of one wire format have to agree byte for byte — the
hub's (firmware/shared/names.h, compiled here with the host compiler), the PC
app's (comms/protocol.py) and the web app's (the names-pure block of
web_app.h, run in node).  A name that one of them trims, cuts or packs
differently is a name that changes as it goes round the rig.

Then the behaviour that decides whether names survive the move to the hub:
  - an EMPTY hub gets this PC's names, once — and the hub's empty answer,
    arriving first, must not wipe them before they are sent;
  - a hub that already has names is never overwritten by a PC's old ones;
  - hub firmware from before names leaves the PC working as it always did;
  - an edit made while the link is down is sent when it returns, first;
  - names for cameras a device does not show are kept and not drawn — one
    device may show 1-5 and another 6-10;
  - clearing a position takes its name, whoever cleared it.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))
import json, random, re, shutil, subprocess, tempfile

from comms import protocol as P

SHARED = REPO / "firmware/shared"
HUB = (REPO / "firmware/esp32_hub_eth/esp32_hub_eth.ino").read_text()
WEB = (SHARED / "web_app.h").read_text()
rng = random.Random(20260926)
TMP = pathlib.Path(tempfile.mkdtemp())

NAMES = ["Balcony", "Piano", "  Stage Left  ", "", "Stage Left Wide Shot on Piano",
         "Café crème brûlée très long", "日本語のカメラ名です長い名前", "a\tb\nc\x7fd",
         "x" * 25, "   ", "Drums 🥁 kit and more", "12345678901234567890",
         "1234567890123456789 é", "ends with space      x", "Pulpit"]
NAMES += ["".join(rng.choice("abcdé ü日🥁 -") for _ in range(rng.randint(0, 30)))
          for _ in range(200)]


# ---- 1. the hub's store, compiled here --------------------------------------
cxx = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
assert cxx, "no C++ compiler — the hub's store cannot be checked"
harness = r"""
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>
#include "names.h"
static NameStore S;
static std::vector<uint8_t> unhex(const std::string &h) {
    std::vector<uint8_t> v;
    for (size_t i = 0; i + 1 < h.size(); i += 2) v.push_back((uint8_t)std::stoi(h.substr(i, 2), nullptr, 16));
    return v;
}
static void hex(const uint8_t *p, int n) { for (int i = 0; i < n; i++) printf("%02x", p[i]); }
int main() {
    memset(&S, 0, sizeof(S));
    char cmd[16], arg[1100];
    while (scanf("%15s %1099s", cmd, arg) == 2) {
        std::vector<uint8_t> b = unhex(strcmp(arg, "-") ? arg : "");
        if (!strcmp(cmd, "CLEAN")) {
            char out[NAME_MAX_BYTES];
            uint8_t n = names_clean(b.data(), (uint8_t)b.size(), out);
            hex((const uint8_t *)out, n); printf("\n");
        } else if (!strcmp(cmd, "SET")) {
            uint32_t rev0 = S.rev;
            int c = names_apply_set(&S, b.data(), (uint16_t)b.size());
            printf("%d %u\n", c, S.rev - rev0);
        } else if (!strcmp(cmd, "GET")) {
            S.pending = 0;
            names_request(&S, b.data(), (uint16_t)b.size());
            uint8_t cam, rec[260];
            while ((cam = names_next_pending(&S)) != 0) {
                int n = names_pack(&S, cam, rec); hex(rec, n); printf(" ");
            }
            printf("\n");
        } else if (!strcmp(cmd, "CLEAR")) {
            printf("%d\n", names_on_clear_pos(&S, b[0], b[1]) ? 1 : 0);
        } else if (!strcmp(cmd, "REV")) {
            uint8_t p[NAMES_REV_PAYLOAD_LEN];
            hex(p, names_rev_payload(&S, p)); printf("\n");
        } else if (!strcmp(cmd, "UNPACK")) {
            NameStore T; memset(&T, 0, sizeof(T));
            bool ok = names_unpack(&T, b.data(), (uint16_t)b.size());
            printf("%d", ok ? 1 : 0);
            if (ok) { uint8_t rec[260]; int n = names_pack(&T, b[0], rec); printf(" "); hex(rec, n); }
            printf(" %u %u %u\n", T.rev, T.pending, T.dirty);
        } else if (!strcmp(cmd, "MASKS")) {
            printf("%u %u\n", S.pending, S.dirty);
            S.pending = S.dirty = 0;
        }
        fflush(stdout);
    }
}
"""
(TMP / "h.cpp").write_text(harness)
r = subprocess.run([cxx, "-std=c++17", "-Wall", "-Wno-unused-function", "-I", str(SHARED),
                    str(TMP / "h.cpp"), "-o", str(TMP / "h")], capture_output=True, text=True)
assert r.returncode == 0, "names.h does not compile on the host:\n" + r.stderr[:2000]


def hub(lines):
    r = subprocess.run([str(TMP / "h")], input="\n".join(lines) + "\n",
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines()


h = lambda b: b.hex() if b else "-"

print("1. the hub's store (shared/names.h, compiled here) vs the PC app:")
out = hub([f"CLEAN {h(n.encode())}" for n in NAMES])
for n, got in zip(NAMES, out):
    assert bytes.fromhex(got) == P.fit_name(n), (n, got, P.fit_name(n))
print(f"   {len(NAMES)} names tidied identically — trim, control characters, "
      f"cut at 20 bytes on a whole character")

# A PC edit, byte for byte into the hub, and the hub's record back into the PC
entries = [(1, P.NAME_SLOT_CAMERA, "Balcony"), (1, 0, "Drums"), (1, 2, "Piano"),
           (4, P.NAME_SLOT_CAMERA, "Stage Left"), (4, 9, "Organ"),
           (32, 5, "Last camera"), (33, 0, "no such camera"), (2, 10, "no such slot"),
           (7, P.NAME_SLOT_CAMERA, "Foyer")]
sets = [f"SET {p[7:-2].hex()}" for p in P.pkts_name_set(entries)]
out = hub(sets + ["GET -", "GET 0102030b21"])
changed = sum(int(line.split()[0]) for line in out[:len(sets)])
assert changed == 7, out                    # camera 33 and slot 10 skipped
recs = [bytes.fromhex(x) for x in out[len(sets)].split()]
got = {cam: (name, slots) for cam, name, slots in (P.decode_names(r) for r in recs)}
assert sorted(got) == [1, 4, 7, 32], sorted(got)           # empty GET = named cameras
assert got[1] == ("Balcony", ["Drums", "", "Piano"] + [""] * 7), got[1]
assert got[4][0] == "Stage Left" and got[4][1][9] == "Organ"
assert got[32][1][5] == "Last camera"
asked = [P.decode_names(bytes.fromhex(x))[0] for x in out[len(sets) + 1].split()]
assert asked == [1, 2, 3, 11], asked       # named or not; 33 is no camera
print("   PC edits land; the hub answers every camera asked for, named or not,")
print("   and an empty request with every named camera; camera 33 and slot 10 ignored")

# Every packed record fits a packet, whatever the names
big = [(c, s, "é" * 11) for c in (1, 2) for s in list(range(10)) + [P.NAME_SLOT_CAMERA]]
out = hub([f"SET {p[7:-2].hex()}" for p in P.pkts_name_set(big)] + ["GET 01"])
rec = bytes.fromhex(out[-1].split()[0])
assert len(rec) <= 251 and len(rec) == 2 + 11 * (1 + 20), len(rec)
assert all(len(p) - 9 <= 251 for p in P.pkts_name_set(big))
print(f"   a camera with every name full is {len(rec)} bytes — one packet; "
      f"a whole set splits cleanly")

# Unchanged is not a change; clearing a position takes its name; rev
out = hub(["SET " + P.pkts_name_set([(1, 0, "Drums")])[0][7:-2].hex(),
           "SET " + P.pkts_name_set([(1, 0, "Drums")])[0][7:-2].hex(),
           "MASKS -", "CLEAR 0100", "CLEAR 0100", "CLEAR 0105", "CLEAR 2100",
           "MASKS -", "REV -"])
assert out[0] == "1 1" and out[1] == "0 0", out[:2]
assert out[2] == "1 1", out[2]              # camera 1 owed and unsaved
assert out[3:7] == ["1", "0", "0", "0"], out[3:7]
assert out[7] == "1 1", out[7]
rev = P.decode_names_rev(bytes.fromhex(out[8]))
assert rev["named"] == 0 and rev["max_cams"] == 32 and rev["slots"] == 10 \
    and rev["name_max"] == 20, rev
print("   the same name twice is not a change; a cleared position loses its name")

# What the hub saved reads back exactly — and a damaged save changes nothing
full = bytes([3, 10, 7]) + b"Balcony" + bytes([5]) + b"Drums" + bytes([0] * 9)
more = bytes([3, 12, 1]) + b"A" + bytes([0] * 12)            # a build with 12 slots
out = hub([f"UNPACK {full.hex()}", f"UNPACK {more.hex()}", f"UNPACK {full[:-3].hex()}",
           "UNPACK 2a0a00", f"UNPACK {bytes([3, 10, 25]).hex()}{'41' * 25}{'00' * 10}"])
ok, packed, rev_, pend, dirty = out[0].split()
assert ok == "1" and bytes.fromhex(packed) == full and (rev_, pend, dirty) == ("0", "0", "0")
assert out[1].startswith("1 ") and bytes.fromhex(out[1].split()[1]) == bytes([3, 10, 1]) + b"A" + bytes([0] * 10)
assert out[2].startswith("0") and out[3].startswith("0"), out[2:4]
long_ = bytes.fromhex(out[4].split()[1])
assert long_[2] == 20, long_                 # a too-long saved name is cut, not trusted
print("   saved names read back exactly; damaged ones are refused, not half-loaded")


# ---- 2. the web app's JavaScript, run in node --------------------------------
if shutil.which("node"):
    m = re.search(r"// BEGIN names-pure\n(.*?)// END names-pure", WEB, re.S)
    assert m, "names-pure markers not found in web_app.h"
    consts = "\n".join(re.findall(r"^const (?:NAME_MAX_BYTES|NAMES_SLOTS|NAME_SLOT_CAMERA)\s*=.*$",
                                  WEB, re.M))
    recs = [bytes.fromhex(x) for x in hub(sets + ["GET 0104072003"])[-1].split()]
    js = consts + "\n" + m.group(1) + r"""
const C = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));
console.log(JSON.stringify({
    fit: C.names.map(n => fitName(n)),
    sets: nameSetPayloads(C.entries),
    recs: C.recs.map(r => parseNames(Uint8Array.from(r))),
    rev: parseNamesRev(Uint8Array.from(C.rev)),
}));
"""
    (TMP / "n.js").write_text(js)
    (TMP / "c.json").write_text(json.dumps({
        "names": NAMES, "entries": [list(e) for e in entries + big],
        "recs": [list(r) for r in recs], "rev": [0x12, 0x34, 0x56, 0x78, 3, 32, 10, 20]}))
    r = subprocess.run(["node", str(TMP / "n.js"), str(TMP / "c.json")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:1500]
    J = json.loads(r.stdout)
    print("\n2. web_app.h JavaScript in node vs the PC app and the hub:")
    for n, got in zip(NAMES, J["fit"]):
        assert bytes(got) == P.fit_name(n), (n, got)
    want = [p[7:-2] for p in P.pkts_name_set(entries + big)]
    assert [bytes(x) for x in J["sets"]] == want, "NAME_SET packing differs"
    for rec, got in zip(recs, J["recs"]):
        cam, name, slots = P.decode_names(rec)
        assert got == {"cam": cam, "name": name, "slots": slots}, (got, cam, name, slots)
    assert J["rev"] == {"rev": 0x12345678, "named": 3, "maxCams": 32, "slots": 10,
                        "nameMax": 20}, J["rev"]
    print(f"   {len(NAMES)} names tidied, {len(want)} NAME_SET payloads, "
          f"{len(recs)} hub records and the rev — identical")
else:
    print("\n2. node not installed — the web app's half skipped")


# ---- 3. NameSync: what the PC app does with it all ---------------------------
from PyQt6.QtCore import QObject, pyqtSignal, QCoreApplication
app = QCoreApplication.instance() or QCoreApplication([])
from config import name_store, name_sync
from config.position_store import PositionStore
from config.mount_config import AppConfig

name_store.NAMES_DIR = TMP / "PTS"
name_store.DEFAULT_PATH = name_store.NAMES_DIR / "Default.json"
name_store.TEMP_PATH = name_store.NAMES_DIR / "temp.json"
name_store.CACHE_PATH = name_store.NAMES_DIR / "hub_names.json"


class FakeMM(QObject):
    names_received = pyqtSignal(int, str, list)
    names_rev_received = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.log = []

    def request_names(self, cams):
        self.log.append(("GET", list(cams)))

    def send_names(self, entries):
        self.log.append(("SET", list(entries)))


class Link:
    connected = False


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def fresh(pc_names=None, cache=False, shown=range(1, 6)):
    if name_store.NAMES_DIR.exists():
        shutil.rmtree(name_store.NAMES_DIR)
    name_store.NAMES_DIR.mkdir(parents=True)
    store, config = PositionStore(), AppConfig()
    for (cam, slot), nm in (pc_names or {}).items():
        if slot is None:
            config.mount(cam).label = nm
        else:
            store.set_label(cam, slot, nm)
    if cache:
        name_store.save_to(name_store.CACHE_PATH, store, config)
    mm, link, clock = FakeMM(), Link(), Clock()
    ns = name_sync.NameSync(mm, link, store, config, shown=shown, clock=clock, timer=False)
    changed = []
    ns.changed.connect(lambda: changed.append(1))
    return ns, mm, link, clock, store, config, changed


def rev(n, named):
    return {"rev": n, "named": named, "max_cams": 32, "slots": 10, "name_max": 20}


def deliver(mm, *msgs):
    for kind, *args in msgs:
        (mm.names_received if kind == "names" else mm.names_rev_received).emit(*args)
    app.processEvents()


print("\n3. the PC app's NameSync:")

# 3a. hub firmware from before names: the July model, untouched
ns, mm, link, clock, store, config, _ = fresh({(1, 0): "Drums"})
link.connected = True
ns.tick()
assert mm.log == [("GET", [1, 2, 3, 4, 5])], mm.log
clock.t += 16
ns.tick()
assert ns.mode == "local", ns.mode
ns.set_position_name(1, 1, "Piano")
assert name_store.TEMP_PATH.exists(), "an edit on an old hub must still reach temp.json"
assert not any(k == "SET" for k, _ in mm.log), "nothing sent to a hub that keeps no names"
print("   older hub firmware: 15 s unanswered → names stay on this PC, edits to temp.json")

# 3b. an EMPTY hub gets this PC's names, and its empty answer must not wipe them
ns, mm, link, clock, store, config, changed = fresh({(1, None): "Balcony", (1, 0): "Drums",
                                                     (3, 4): "Lectern"})
link.connected = True
ns.tick()
empty = [""] * 10
deliver(mm, *[("names", c, "", empty) for c in (1, 2, 3, 4, 5)])
assert store.get_label(1, 0) == "Drums" and config.mount(1).label == "Balcony", \
    "the empty hub's answer wiped this PC's names before they could be sent"
deliver(mm, ("rev", rev(7, 0)))
sent = [e for k, v in mm.log if k == "SET" for e in v]
assert (1, P.NAME_SLOT_CAMERA, "Balcony") in sent and (1, 0, "Drums") in sent \
    and (3, 4, "Lectern") in sent, sent
assert (2, P.NAME_SLOT_CAMERA, "") in sent and (1, 5, "") in sent   # defaults go as ""
assert ns.mode == "hub" and ns.migrated
print("   empty hub: this PC's names sent to it — and not wiped by its empty answer first")

# 3c. a hub that has names wins; this PC's old ones are never sent over them
ns, mm, link, clock, store, config, changed = fresh({(1, 0): "Old name"})
link.connected = True
ns.tick()
deliver(mm, ("names", 1, "Balcony", ["Drums"] + [""] * 9), ("rev", rev(9, 1)))
assert not any(k == "SET" for k, _ in mm.log), mm.log
assert store.get_label(1, 0) == "Drums" and config.mount(1).label == "Balcony"
assert changed, "names arrived and nothing was told to redraw"
print("   hub with names: its names shown, this PC's old ones not sent over them")

# 3d. once moved, never again — even to an empty hub
ns, mm, link, clock, store, config, changed = fresh({(1, 0): "Stale"}, cache=True)
link.connected = True
ns.tick()
deliver(mm, ("rev", rev(3, 0)))
assert not any(k == "SET" for k, _ in mm.log), "a PC that has moved must not re-send"
print("   a PC that has already moved never re-sends, even to an emptied hub")

# 3e. edits: to the hub at once; while the link is down, held and sent first
ns, mm, link, clock, store, config, changed = fresh(cache=True)
link.connected = True
ns.tick()
deliver(mm, ("rev", rev(1, 1)))
mm.log.clear()
ns.set_camera_name(2, "  Drone Cam  ")
ns.set_position_name(2, 3, "4")                      # its own number = no name
assert mm.log == [("SET", [(2, P.NAME_SLOT_CAMERA, "Drone Cam")]), ("SET", [(2, 3, "")])], mm.log
link.connected = False
ns.tick()
mm.log.clear()
ns.set_position_name(1, 0, "Offline edit")
assert mm.log == [], "sent into a link that is down"
deliver(mm, ("names", 1, "", ["Old"] + [""] * 9))
assert store.get_label(1, 0) == "Offline edit", "a stale record overwrote an unsent edit"
link.connected = True
ns.tick()
assert mm.log[0] == ("SET", [(1, 0, "Offline edit")]) and mm.log[1][0] == "GET", mm.log
print("   edits go at once; made offline, they are held, protected, and sent first")

# 3f. another device's set of cameras: kept, not drawn; this one's own set asked for
ns, mm, link, clock, store, config, changed = fresh(cache=True, shown=[6, 7, 8])
link.connected = True
ns.tick()
assert mm.log == [("GET", [6, 7, 8])], mm.log
ns2, mm2, *_ = fresh(cache=True)                    # a 1-5 device, same kind of hub
deliver(mm, ("names", 2, "Cam two", [""] * 10), ("rev", rev(4, 2)))
assert 2 in ns.hub_names and config.mount(2).label == "", "a camera not shown was drawn"
print("   a device showing cameras 6-8 asks for 6-8; camera 2's names kept, not drawn")

# 3g. a missed change: the rev moves and it asks again; a lost answer is re-asked
ns, mm, link, clock, store, config, changed = fresh(cache=True)
link.connected = True
ns.tick()
deliver(mm, *[("names", c, "", [""] * 10) for c in range(1, 6)], ("rev", rev(5, 0)))
mm.log.clear()
deliver(mm, ("rev", rev(5, 0)))
assert mm.log == [], "the same rev must not trigger a request"
deliver(mm, ("rev", rev(6, 1)))
assert mm.log == [("GET", [1, 2, 3, 4, 5])], mm.log
deliver(mm, *[("names", c, "", [""] * 10) for c in (1, 2, 4, 5)])   # camera 3 lost
mm.log.clear()
clock.t += 5
ns.tick()
assert mm.log == [("GET", [3])], mm.log
print("   a moved rev fetches again; a camera whose answer was lost is asked for again")


# ---- 4. the hub's glue, and the web app's --------------------------------------
# The definitions, not the forward declarations that come first.
fwd = HUB[HUB.index("static void forward_to_mounts(const ParsedPacket &pkt) {"):]
fwd = fwd[:fwd.index("\n}\n")]
assert fwd.index("handle_names_cmd(pkt)") < fwd.index("demo_consume_cmd"), \
    "names must be handled before the demo or the mounts see them"
assert "names_note_mount_cmd(pkt.mount_id" in fwd, "client clears no longer take the name"
ui = HUB[HUB.index("static void ui_send_to_mount(uint8_t mount_id, CmdType cmd,\n                              const uint8_t *payload, uint8_t plen) {"):]
assert "names_note_mount_cmd(mount_id" in ui[:ui.index("\n}\n")], \
    "a CLEAR from the hub display no longer takes the name"
assert "    names_load();" in HUB and "    names_tick(now);" in HUB
act = HUB[HUB.index("static inline bool cmd_is_client_activity"):]
assert "case CMD_NAMES_GET:" in act[:act.index("\n}\n")], \
    "a names request would count as client activity and hold off the quiet restart"
assert "localStorage.setItem(lblKey" not in WEB and "function lblKey" not in WEB, \
    "the web app still keeps its own position names"
assert "askNames(SHOWN_CAMS)" in WEB, "the web app no longer asks for its own cameras"
print("\n4. hub: names handled before any mount; client AND display clears take the name;")
print("   loaded at boot, ticked in the loop, and a request is not activity.")
print("   web app: no names of its own; asks the hub for the cameras it shows")

print("\nALL CHECKS PASSED")
