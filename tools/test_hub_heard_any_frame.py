"""The hub sends to a mount it has heard ANY frame from — not only a STATUS.

2026-09-27, the bench: opening the PC app USB-reset the hub under a running
cam1.  After a restart the hub sends a mount nothing until it knows the mount
is there, and "there" meant a STATUS had registered.  None did — why is not
known — so for half an hour the hub heard cam1's health every 10 s and sent it
nothing, not even its 2 s PING.  cam1's own recovery read the one-way silence as
the hub's fault and waited; four PC-ordered hub restarts only reset the hub's
state again; a power cycle of the mount ended it.

The operator's rule, from the original system: any signal from a mount means
it is alive.  So:

  any frame counts           the relay loop stamps _mount_heard_ms for every
                             frame from a bound mount, before a command is even
                             looked at; the send gate reads it
  ghosts still do not        a frame with rssi 0 counts only if the ghost guard
                             is switched off, exactly as for the display
  the route follows it       where commands go (_mount_sat) moves with the same
                             frames: a satellite mount heard first through its
                             satellite is sent to through it, not directly
  the display is unchanged   _mount_last_seen is still the last STATUS, still
                             set only in process_status_for_display — the tiles,
                             outage accounting and connect transition need what
                             a STATUS says
  a new binding starts cold  every place that forgets last-seen on a
                             (re)binding forgets heard too
  30 s of nothing is absent  a mount switched off is still not transmitted to

And the hub's own recovery goes by the same frames.  2026-09-27 22:54, the
first boot of the change above: cam1 was heard for 31 s before its first STATUS
counted, every send to it failing.  The hub's ladder (reinit at 6 s) and its
peer refresh both asked for a STATUS, so neither could act; the PC's detector
reinit'd the radio and cam1 answered 0.3 s later.  With no PC, nothing would.

  heard, not STATUS          the wedge detector, its "another mount is fine"
                             proof and the peer refresh all ask
                             mount_heard_recently(): any frame within 5 s
  switched off is not stuck  the window is shorter than the first rung, so a
                             mount switched off drops out of it before its
                             failed sends can age to a reinit — at the 16 s
                             STATUS window it did not
  fresh clock, zero = never  aged against millis(), not a caller's `now`; a
                             mount never heard is not heard at boot
  STATUS where STATUS counts the maintenance restart's "is anything moving"
                             still needs a STATUS: it reads the state in one

The stamp, the route, the gate and the recovery checks are lifted out of the
.ino and run here — and run again with the old STATUS rule, which must fail.

Run directly, or via tools/run_tests.sh with the rest.
"""
import pathlib
import re
import shutil
import subprocess
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
HUB = (REPO / "firmware/esp32_hub_eth/esp32_hub_eth.ino").read_text()


def block(sig: str) -> str:
    """The function or block that starts at `sig`, braces balanced — skipping
    any match that reaches a ';' before its '{', which is a declaration."""
    i = HUB.index(sig)
    while ";" in HUB[i:HUB.index("{", i)]:
        i = HUB.index(sig, i + 1)
    j = HUB.index("{", i)
    depth = 0
    for k in range(j, len(HUB)):
        depth += {"{": 1, "}": -1}.get(HUB[k], 0)
        if depth == 0:
            return HUB[i:k + 1]
    raise AssertionError(f"unbalanced braces after {sig!r}")


# ---- 1. where it is wired -----------------------------------------------------
print("1. the hub's wiring:")
relay = HUB[HUB.index("while (xQueueReceive(_relay_queue, &msg, 0) == pdTRUE) {"):]
relay = relay[:relay.index("broadcast_to_all(msg.data, msg.len);")]
assert "if (msg.src_idx >= NUM_MOUNTS) continue;" in relay, relay
assert "mount_heard(msg);" in relay, \
    "the relay loop never stamps heard — the hub only sends to a mount once a STATUS registers"
assert relay.index("if (msg.src_idx >= NUM_MOUNTS) continue;") < relay.index("mount_heard(msg);"), \
    "heard is stamped before the pairing rules have said which mount the frame is from"
assert "if (msg.rssi != 0 || !HUB_DROP_GHOST_RSSI0) mount_heard(msg);" in relay, \
    "a ghost frame (rssi 0) counts as hearing the mount"
assert "pkt.cmd" not in relay, "liveness is decided by which command arrived — any frame is meant to count"
gate = block("static void espnow_send_if_present(")
assert "_mount_heard_ms[idx]" in gate and "_mount_last_seen" not in gate, \
    "the send gate still needs a STATUS — the hub goes silent on a mount it can hear"
psd = block("static void process_status_for_display(")
assert "_mount_sat[" not in psd, \
    "the route is still moved by STATUS alone — heard through a satellite, sent to directly"
assert HUB.count("_mount_last_seen[msg.src_idx] = millis();") == 1 and \
    "_mount_last_seen[msg.src_idx] = millis();" in psd, \
    "last-seen (the display's STATUS time) is stamped somewhere other than the STATUS handler"
resets_seen = re.findall(r"_mount_last_seen\[(cur|slot)\]\s*= 0;", HUB)
resets_heard = re.findall(r"_mount_heard_ms\[(cur|slot)\]\s*= 0;", HUB)
assert len(resets_seen) == 4 and sorted(resets_seen) == sorted(resets_heard), \
    f"a (re)binding forgets last-seen {resets_seen} but not heard {resets_heard} — " \
    "the new mount in that slot would inherit the old one's presence"
for m in re.finditer(r"_mount_last_seen\[(cur|slot)\]\s*= 0;", HUB):
    nxt = HUB[m.end():m.end() + 120]
    assert f"_mount_heard_ms[{m.group(1)}]" in nxt, \
        f"heard is not forgotten beside last-seen at {HUB[:m.start()].count(chr(10)) + 1}"
print("   stamped for every bound frame, ghosts aside; the gate reads it; the\n"
      "   route moved with it; last-seen still the STATUS; bindings start cold   OK")


def code_only(text: str) -> str:
    return "\n".join(l.split("//")[0] for l in text.splitlines())


def braced(i: int) -> str:
    """From HUB[i] to the brace that closes the first one opened after it."""
    j = HUB.index("{", i)
    depth = 0
    for k in range(j, len(HUB)):
        depth += {"{": 1, "}": -1}.get(HUB[k], 0)
        if depth == 0:
            return HUB[i:k + 1]
    raise AssertionError("unbalanced braces")


# ---- 1b. the hub's own recovery goes by the same frames -----------------------
print("\n1b. the recovery path:")
helper = block("static inline bool mount_heard_recently(int i)")
hcode = code_only(helper)
assert "_mount_heard_ms[i]" in hcode and "_mount_last_seen" not in hcode, \
    "mount_heard_recently() does not read the any-frame stamp"
assert "millis() - heard" in hcode, \
    "mount_heard_recently() ages the stamp against something other than a fresh\n" \
    "    millis() — the relay loop can stamp after a caller read its `now`, and\n" \
    "    unsigned, now - stamp is then 49 days of silence"
rec = block("static void check_self_recovery(uint32_t now)")
rcode = code_only(rec)
assert "bool alive   = mount_heard_recently(i);" in rcode, \
    "the wedge detector still needs a STATUS to call a mount alive — the 22:54\n" \
    "    case (heard, no STATUS, every send failing) never arms the ladder"
assert "bool k_alive = mount_heard_recently(k);" in rcode, \
    "'another mount is acknowledging' still needs that mount's STATUS, so a\n" \
    "    mount heard and answering before its STATUS registers proves nothing"
assert "mount_is_active(" not in rcode, \
    "the self-recovery ladder still has a STATUS-only presence test in it"
assert "STATUS still arriving" not in code_only(HUB), \
    "the wedge line still says STATUS is arriving — it is judged on any frame now"
ref_at = HUB.index("        if (!_espnow_need_refresh[i]) continue;")
ref_at = HUB.rindex("    for (int i = 0; i < NUM_MOUNTS; i++) {", 0, ref_at)
refresh = braced(ref_at)
assert "bool alive = mount_heard_recently(i);" in code_only(refresh) and \
       "mount_is_active(" not in code_only(refresh), \
    "the peer refresh still needs a STATUS — a mount heard but failing every\n" \
    "    send is exactly the one whose peer entry may need re-adding"
maint = code_only(block("static void check_maintenance_restart("))
assert "bool connected = mount_is_active(i, now);" in maint, \
    "the maintenance restart no longer asks for a STATUS before it reads a\n" \
    "    mount's state — a mount heard but not yet reporting has no state to read"
print("   the wedge detector, its proof and the peer refresh ask for any frame\n"
      "   within the window; the maintenance restart still asks for a STATUS   OK")

# ---- 2. the stamp, the route and the gate, run -------------------------------
if not shutil.which("c++"):
    print("\n2. no C++ compiler — skipping the behavioural check.")
    print("\nALL CHECKS PASSED")
    raise SystemExit(0)

heard_fn = block("static void mount_heard(const RelayMsg &msg)")
present = re.search(r"#define MOUNT_PRESENT_MS\s+(\d+)UL", HUB)
assert present, "MOUNT_PRESENT_MS is not defined"
gate_lines = gate[gate.index("uint32_t heard ="):]
gate_lines = gate_lines[:gate_lines.index("\n", gate_lines.index("return;")) + 1]
src = r"""
#include <cstdint>
#include <cstdio>
#define NUM_MOUNTS 5
#define MOUNT_PRESENT_MS @PRESENT@UL
struct RelayMsg { uint8_t src_idx; int8_t rssi; int8_t via_sat; };
static uint32_t _millis;
static uint32_t millis() { return _millis; }
static uint32_t _mount_heard_ms[NUM_MOUNTS];
static int8_t   _mount_sat[NUM_MOUNTS] = {-1, -1, -1, -1, -1};
static int      route_msgs;
static void send_mount_route() { route_msgs++; }
static struct { template <class... A> void printf(const char *, A...) {} } Serial;
@HEARD@
static bool open_;
static void gate(int idx) {
    open_ = false;
@GATE@
    open_ = true;
}
int main() {
    _millis = 100000;
    gate(0);                                   printf("never %d\n", open_);
    RelayMsg health = {0, -48, -1};            // cam1's health, heard directly
    mount_heard(health);                       gate(0);
    printf("heard %d %d %d\n", open_, _mount_sat[0], route_msgs);
    _millis += MOUNT_PRESENT_MS;               gate(0);  printf("edge %d\n", open_);
    _millis += 1;                              gate(0);  printf("gone %d\n", open_);
    RelayMsg viasat = {3, -61, 0};             // cam4, relayed by satellite 1
    mount_heard(viasat);                       gate(3);
    printf("sat %d %d %d\n", open_, _mount_sat[3], route_msgs);
    mount_heard(viasat);                       printf("same %d\n", route_msgs);
    RelayMsg back = {3, -70, -1};              // cam4 back on the hub's radio
    mount_heard(back);                         printf("back %d %d\n", _mount_sat[3], route_msgs);
    _millis = 0;                               // the tick millis() reads 0 on
    mount_heard(health);
    _millis = 1;                               gate(0);
    printf("zero %d %u\n", open_, _mount_heard_ms[0]);
    return 0;
}
""".replace("@PRESENT@", present.group(1)).replace("@HEARD@", heard_fn).replace("@GATE@", gate_lines)
d = pathlib.Path(tempfile.mkdtemp())
(d / "h.cpp").write_text(src)
r = subprocess.run(["c++", "-std=c++17", "-Wall", "-o", str(d / "h"), str(d / "h.cpp")],
                   capture_output=True, text=True)
assert r.returncode == 0, f"the lifted hub code did not compile:\n{r.stderr[:1500]}"
out = dict(l.split(" ", 1) for l in subprocess.run([str(d / "h")], capture_output=True,
                                                    text=True).stdout.strip().splitlines())
print("\n2. the stamp, the route and the gate, run:")
assert out["never"] == "0", "the hub sends to a mount it has never heard from"
assert out["heard"] == "1 -1 0", \
    f"a health frame did not open the gate (open, route, route messages = {out['heard']})"
assert out["edge"] == "1" and out["gone"] == "0", \
    f"the gate is not open to {present.group(1)} ms of silence and shut after it: " \
    f"{out['edge']} / {out['gone']}"
assert out["sat"] == "1 0 1", \
    f"a frame through satellite 1 did not route that mount through it: {out['sat']}"
assert out["same"] == "1", "an unchanged route is re-announced to every client on every frame"
assert out["back"] == "-1 2", f"a mount back on the hub's radio is still routed via the satellite: {out['back']}"
assert out["zero"] == "1 1", \
    f"a frame heard at millis() 0 was stored as 0, 'never heard', and lost: {out['zero']}"
print("   any frame opens it, for 30 s; the route follows, announced once per\n"
      "   change; millis() 0 still counts   OK")

# ---- 3. the recovery checks, run ---------------------------------------------
# The detector's per-mount loop, the tx_proven_ok loop and the peer refresh,
# lifted verbatim, with the real helper and window.  Then the same code again
# with the helper swapped for the rule before (STATUS within 16 s): it has to
# fail these cases, or they tell nothing apart.
defs = "\n".join(re.search(rf"^#define {n}\b.*$", HUB, re.M).group(0)
                 for n in ("SELF_WEDGE_MIN_FAILS", "SELF_REINIT_AFTER_MS", "SELF_WEDGE_HEARD_MS"))
detect = rec[rec.index("uint32_t worst_age = 0;"):
             rec.index("// Hub-wide, and checked before the per-mount verdict below.")]
proof = rec[rec.index("bool tx_proven_ok = false;"):rec.index("if (tx_proven_ok) {")]
PROT = (REPO / "firmware/shared/protocol.h").read_text()
status_refresh = int(re.search(r"#define MOUNT_STATUS_REFRESH_MS\s+(\d+)UL", PROT).group(1))
old_rule = ("static inline bool mount_heard_recently(int i) {   // the rule before: a STATUS\n"
            f"    return _mount_last_seen[i] && (millis() - _mount_last_seen[i]) < "
            f"{3 * status_refresh + 1000}UL;\n}}")
rig = r"""
#include <cstdint>
#include <cstdio>
#define NUM_MOUNTS 5
#define ESPNOW_MAX_CONSEC_FAILS 4
@DEFS@
static uint32_t _millis;
static uint32_t millis() { return _millis; }
static uint32_t _mount_heard_ms[NUM_MOUNTS], _mount_last_seen[NUM_MOUNTS];
static int8_t   _mount_sat[NUM_MOUNTS];
static uint8_t  _espnow_fail_run[NUM_MOUNTS];
static uint32_t _tx_wedge_since_ms[NUM_MOUNTS], _last_wedge_ms;
static bool     _restart_block_logged, _cb_stall_active, _espnow_need_refresh[NUM_MOUNTS];
static int      refreshed[NUM_MOUNTS];
static void refresh_espnow_peer(int i) { refreshed[i]++; }
static struct { template <class... A> void printf(const char *, A...) {} } Serial;
@HELPER@
static uint32_t age_out; static int who_out;
static void detect_pass(uint32_t now) {
@DETECT@
    age_out = worst_age; who_out = worst_i;
}
static bool proven(int worst_i) {
@PROOF@
    return tx_proven_ok;
}
static void refresh_pass() {
@REFRESH@
}
static void reset() {
    for (int i = 0; i < NUM_MOUNTS; i++) {
        _mount_heard_ms[i] = _mount_last_seen[i] = _tx_wedge_since_ms[i] = 0;
        _mount_sat[i] = -1; _espnow_fail_run[i] = 0;
        _espnow_need_refresh[i] = false; refreshed[i] = 0;
    }
    _cb_stall_active = false;
}
// The 500 ms self-check over [base, base + ms], cam1's sends failing all along;
// cam1 heard every 100 ms while `heard_too`.  Returns the oldest wedge age seen.
static uint32_t run(uint32_t base, uint32_t ms, bool heard_too) {
    uint32_t oldest = 0;
    for (uint32_t t = 0; t <= ms; t += 100) {
        _millis = base + t;
        if (heard_too) _mount_heard_ms[0] = _millis;
        _espnow_fail_run[0] = 10;
        if (t % 500 == 0) {
            detect_pass(_millis);
            if (who_out == 0 && age_out > oldest) oldest = age_out;
        }
    }
    return oldest;
}
int main() {
    printf("window %lu %lu\n", (unsigned long)SELF_WEDGE_HEARD_MS,
           (unsigned long)SELF_REINIT_AFTER_MS);
    // A. 22:54 - heard ten times a second, no STATUS, every send failing
    reset();
    printf("deaf %lu\n", (unsigned long)run(100000, 8000, true));
    // B. switched off - healthy for 10 s (STATUS every 5 s, the last as it
    //    goes), then nothing heard and every send failing from that instant
    reset();
    for (uint32_t t = 0; t <= 10000; t += 100) {
        _millis = 200000 + t;
        _mount_heard_ms[0] = _millis;
        if (t % 5000 == 0) _mount_last_seen[0] = _millis;
    }
    uint32_t off = run(_millis, 30000, false);
    printf("off %lu %lu\n", (unsigned long)off, (unsigned long)_tx_wedge_since_ms[0]);
    // C. a mount relayed by a satellite, deaf to this radio by design
    reset(); _mount_sat[0] = 0;
    printf("sat %lu %d\n", (unsigned long)run(300000, 8000, true), who_out);
    // D. cam1 failing; cam2 heard 0.2 s ago, answering, its STATUS not in yet -
    //    and then cam2 last heard 6 s ago
    reset(); _millis = 400000;
    _mount_heard_ms[1] = _millis - 200;
    int p1 = proven(0);
    _mount_heard_ms[1] = _millis - 6000;
    printf("proof %d %d\n", p1, (int)proven(0));
    // E. four sends in a row failed: cam1 heard 0.1 s ago (no STATUS), cam3
    //    switched off 6 s ago
    reset(); _millis = 500000;
    _mount_heard_ms[0] = _millis - 100;  _espnow_need_refresh[0] = true;
    _mount_heard_ms[2] = _millis - 6000; _espnow_need_refresh[2] = true;
    refresh_pass();
    printf("refresh %d %d %d\n", refreshed[0], refreshed[2], (int)_espnow_need_refresh[2]);
    // F. one second after boot, a mount never heard
    reset(); _millis = 1000;
    printf("boot %d\n", (int)mount_heard_recently(3));
    return 0;
}
"""


def run_rig(helper_src: str) -> dict:
    src = (rig.replace("@DEFS@", defs).replace("@HELPER@", helper_src)
              .replace("@DETECT@", detect).replace("@PROOF@", proof)
              .replace("@REFRESH@", refresh))
    (d / "r.cpp").write_text(src)
    c = subprocess.run(["c++", "-std=c++17", "-Wall", "-o", str(d / "r"), str(d / "r.cpp")],
                       capture_output=True, text=True)
    assert c.returncode == 0, f"the lifted recovery code did not compile:\n{c.stderr[:1500]}"
    lines = subprocess.run([str(d / "r")], capture_output=True, text=True).stdout.strip()
    return dict(l.split(" ", 1) for l in lines.splitlines())


print("\n3. the recovery checks, run:")
got = run_rig(helper)
window, reinit = (int(x) for x in got["window"].split())
deaf = int(got["deaf"])
assert deaf >= reinit, \
    f"the 22:54 case — cam1 heard, no STATUS, every send failing — ages only to " \
    f"{deaf} ms, short of the {reinit} ms reinit: the hub cannot rescue it alone"
off, since = (int(x) for x in got["off"].split())
assert off < reinit, \
    f"a mount switched off ages to {off} ms of 'wedge', and the reinit is due at " \
    f"{reinit} ms — the hub would reinit its radio, and at 14 s bounce its WiFi, " \
    "because someone turned a mount off"
assert since == 0, "a mount switched off 30 s ago still has the wedge clock running"
assert window < reinit, \
    f"the heard window ({window} ms) is not shorter than the first rung ({reinit} ms)"
assert got["sat"] == "0 -1", \
    f"a satellite-relayed mount armed the wedge detector: {got['sat']}"
assert got["proof"] == "1 0", \
    f"'another mount is answering' reads {got['proof']} (heard 0.2 s ago, heard " \
    "6 s ago): a mount heard and acknowledging must prove this radio fine, one " \
    "gone quiet must not"
assert got["refresh"] == "1 0 0", \
    f"peer refresh (heard mount, switched-off mount, flag left set) = {got['refresh']}"
assert got["boot"] == "0", "a mount never heard reads as heard in the first seconds after boot"
print(f"   heard with no STATUS: the reinit comes due ({deaf} ms of failing)\n"
      f"   switched off: never past {off} ms, then cleared — the rung is {reinit} ms\n"
      f"   a satellite mount never arms it; a mount heard and answering proves the\n"
      f"   radio, one quiet for 6 s does not; the refresh goes to the one heard   OK")

was = run_rig(old_rule)
assert int(was["deaf"]) < reinit and int(was["off"].split()[0]) >= reinit and \
       was["proof"] == "0 0" and was["refresh"] == "0 0 0", \
    f"the rule before this change passes these cases too, so they prove nothing: {was}"
print(f"   and the old STATUS rule fails them: never arms at 22:54, and ages a\n"
      f"   switched-off mount to {was['off'].split()[0]} ms   OK")

print("\nALL CHECKS PASSED")
