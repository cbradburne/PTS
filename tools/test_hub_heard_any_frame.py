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

The stamp, the route and the gate are lifted out of the .ino and run here.

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

print("\nALL CHECKS PASSED")
