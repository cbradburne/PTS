"""A satellite's client link follows the hub through a restart.

Each satellite keeps two links to the hub: the relay link (its mounts' traffic
and its own health) and the client link (port 7777, for the phones on its WiFi).
2026-10-19 22:41 the hub was flashed.  Both relay links came straight back; the
client links never did, and the hub counted one TCP client, the PC, for good.

The client link only READS unless a phone sends.  A rebooting hub sends no
reset, so nothing ever told the satellite its link was gone: connected() stayed
true and client_link_service() never retried.  A phone on that satellite saw
every camera grey until someone tapped, and the tap was lost on the dead link.
Every hub restart did it — flashes, power cuts, the weekly maintenance restart —
which is why the hub's display had read "TCP: 1" for weeks where 3 belonged.

The relay link does find out (it is written to all the time), so its reconnect
now closes the client link, and client_link_service() opens a fresh one later
in the same pass.  Lifted from the .ino and run here against a fake hub:

  1. a hub restart       the client link is back on the new hub in the same
                         pass as the relay link, and stays there
  2. a hub switched off  no client-link connects while it is away (each one can
                         block the loop for 2 s; the relay link already tries),
                         then both back in one pass when it returns
  3. relay link alone    if only the relay link dropped, the client link is
                         reopened too — closed first, so the hub never holds two

And the code before, run the same way, must fail 1.

Run directly, or via tools/run_tests.sh with the rest.
"""
import pathlib
import re
import shutil
import subprocess
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
SAT = (REPO / "firmware/esp32_satellite/esp32_satellite.ino").read_text()
LINK = (REPO / "firmware/shared/sat_link.h").read_text()

def block(src: str, sig: str) -> str:
    """The function that starts at `sig`, braces balanced — skipping any match
    that reaches a ';' before its '{', which is a declaration."""
    i = src.index(sig)
    while ";" in src[i:src.index("{", i)]:
        i = src.index(sig, i + 1)
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        depth += {"{": 1, "}": -1}.get(src[k], 0)
        if depth == 0:
            return src[i:k + 1]
    raise AssertionError(f"unbalanced braces after {sig!r}")


def define(src: str, name: str) -> str:
    m = re.search(rf"^#define {name}\b.*$", src, re.M)
    assert m, f"{name} is not defined"
    return m.group(0)


# ---- 1. where it is wired ------------------------------------------------------
# Only that it is there: where, and to what effect, the run below decides.
print("1. the satellite's wiring:")
up = block(SAT, "static void uplink_service(uint32_t now)")
code = "\n".join(l.split("//")[0] for l in up.splitlines())
assert "_client_link" in code, \
    "the relay link's reconnect never touches the client link — after a hub\n" \
    "    restart it stays on the old hub, and the phones on this satellite with it"
print("   uplink_service() deals with the client link                       OK")

# ---- 2. run -------------------------------------------------------------------
if not shutil.which("c++"):
    print("\n2. no C++ compiler — skipping the behavioural check.")
    print("\nALL CHECKS PASSED")
    raise SystemExit(0)

loop = block(SAT, "\nvoid loop()")
calls = sorted((loop.index(c), c) for c in ("uplink_service(now);", "client_link_service(now);"))
one_pass = ""
for _, c in calls:              # the two in the order loop() runs them
    one_pass += f"    {c}\n"
    if c.startswith("uplink"):  # the mounts' traffic goes up right after it
        one_pass += "    if (_uplink.connected()) _uplink.sent();\n"

RIG = r"""
#include <cstdint>
#include <cstdio>
#include <string>
@DEFS@
#define HUB_PORT     SAT_LINK_PORT
#define HUB_HOST_MAX 40

static uint32_t _millis;
static uint32_t millis() { return _millis; }

// The hub, as far as the satellite can tell.  A restart forgets every link.
struct Hub {
    int  epoch = 1;          // which boot of the hub this is
    bool up = true;
    int  open[2] = {0, 0};   // links it holds from us: [0] client, [1] relay
    int  most[2] = {0, 0};   // the most it held at once
    int  tries[2] = {0, 0};  // connects that reached for it
    void restart() { epoch++; open[0] = open[1] = 0; }
} hub;
static int kind(uint16_t port) { return port == SAT_HUB_CLIENT_PORT ? 0 : 1; }

struct IPAddress {
    std::string toString() const { return "10.11.40.19"; }
};

class WiFiClient {
    int  epoch_ = 0, kind_ = -1;
    bool reset_ = false;
    bool open_to(uint16_t port) {
        hub.tries[kind(port)]++;
        if (!hub.up) return false;
        epoch_ = hub.epoch; kind_ = kind(port); reset_ = false;
        if (++hub.open[kind_] > hub.most[kind_]) hub.most[kind_] = hub.open[kind_];
        return true;
    }
public:
    bool connect(IPAddress, uint16_t port, int) { return open_to(port); }
    bool connect(const char *, uint16_t port, int) { return open_to(port); }
    // lwIP's view: connected until a reset or a close has come back.
    bool connected() const { return epoch_ != 0 && !reset_; }
    // Something goes out.  A hub that is up and does not know this link
    // answers with a reset; a hub that is away answers nothing, and TCP
    // retries meanwhile with connected() still true.
    void sent() { if (epoch_ && hub.up && epoch_ != hub.epoch) reset_ = true; }
    // TCP gives up on data the far end never acknowledged.
    void gave_up() { if (epoch_) reset_ = true; }
    // The hub closes this one link itself.
    void closed_by_hub() {
        if (epoch_ == hub.epoch && !reset_) { hub.open[kind_]--; reset_ = true; }
    }
    void stop() {
        if (epoch_ == hub.epoch && !reset_ && hub.up) hub.open[kind_]--;   // the hub frees it
        epoch_ = 0; reset_ = false; kind_ = -1;
    }
    bool on_hub() const { return connected() && epoch_ == hub.epoch; }
    void setNoDelay(bool) {}
    IPAddress remoteIP() const { return IPAddress(); }
};

static struct { template <class... A> void printf(const char *, A...) {} } Serial;
static bool eth_is_up() { return true; }
static void uplink_send_hello() {}
bool _skip_pass_max = false;

static WiFiClient _uplink, _client_link;
static uint32_t   _uplink_next_try_ms = 0, _uplink_backoff_ms = 1000;
static uint32_t   _cl_next_try_ms = 0, _cl_backoff_ms = 1000;
static uint16_t   _tcp_len = 0, _cl_len = 0, _up_agg_len = 0, _up_agg_frames = 0;
static char       _hub_host[HUB_HOST_MAX] = "pts-hub.local";
static IPAddress  _hub_ip;
static bool       _hub_ip_valid = false;
static uint8_t    _hub_ip_fails = 0;

@HUB_CONNECT@
@CLIENT_LINK@
@UPLINK@

static void one_pass(uint32_t now) {
@PASS@
}
static void run_ms(uint32_t ms) {
    for (uint32_t t = 0; t < ms; t += 10) { _millis += 10; one_pass(_millis); }
}
// Pass by pass until the relay link is on the hub: is the client link on it
// at the end of that same pass?
static int same_pass() {
    for (int i = 0; i < 6000; i++) {
        _millis += 10; one_pass(_millis);
        if (_uplink.on_hub()) return _client_link.on_hub();
    }
    return -1;
}
int main() {
    _millis = 5000;
    run_ms(1000);
    printf("boot %d %d\n", _uplink.on_hub(), _client_link.on_hub());
    run_ms(10000);

    // 1. a flash: the hub away 3 s, then back as a new boot
    hub.restart(); hub.up = false;
    run_ms(3000);
    hub.up = true;
    int same = same_pass();
    run_ms(60000);
    printf("restart %d %d %d\n", same, _client_link.on_hub(), hub.open[0]);

    // 2. switched off for 15 min; the relay link's unacknowledged traffic
    //    makes TCP give up on it after ~11 min, and it retries from then on
    hub.restart(); hub.up = false;
    int before = hub.tries[0], relay_before = hub.tries[1];
    run_ms(660000);
    _uplink.gave_up();
    run_ms(240000);
    printf("away %d %d\n", hub.tries[0] - before, hub.tries[1] - relay_before);
    hub.up = true;
    same = same_pass();
    run_ms(10000);
    printf("back %d %d %d\n", same, _client_link.on_hub(), hub.open[0]);

    // 3. the hub closes only the relay link
    hub.most[0] = hub.open[0];
    _uplink.closed_by_hub();
    same = same_pass();
    run_ms(10000);
    printf("relay %d %d %d %d\n", same, _client_link.on_hub(), hub.open[0], hub.most[0]);
    return 0;
}
"""


def run(uplink: str) -> dict:
    src = (RIG.replace("@DEFS@", "\n".join([define(LINK, "SAT_LINK_PORT"),
                                            define(LINK, "SAT_HUB_CLIENT_PORT"),
                                            define(SAT, "HUB_IP_STRIKES"),
                                            define(SAT, "UPLINK_BACKOFF_MAX_MS")]))
              .replace("@HUB_CONNECT@", block(SAT, "static bool hub_connect(WiFiClient &c, uint16_t port)"))
              .replace("@CLIENT_LINK@", block(SAT, "static void client_link_service(uint32_t now)"))
              .replace("@UPLINK@", uplink)
              .replace("@PASS@", one_pass))
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "r.cpp").write_text(src)
    r = subprocess.run(["c++", "-std=c++17", "-Wall", "-o", str(d / "r"), str(d / "r.cpp")],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"the lifted satellite code did not compile:\n{r.stderr[:1500]}"
    out = subprocess.run([str(d / "r")], capture_output=True, text=True, timeout=60).stdout
    return dict(l.split(" ", 1) for l in out.strip().splitlines())


out = run(up)
print("\n2. the relay and client links, run against a hub that restarts:")
assert out["boot"] == "1 1", f"the two links were not both up a second after boot: {out['boot']}"
same, held_on, held = out["restart"].split()
assert held_on == "1" and held == "1", \
    "after a hub restart the client link never came back to the new hub\n" \
    f"    (on the hub {held_on}, client links the hub holds {held}) — the phones on\n" \
    "    this satellite get nothing until someone taps"
assert same == "1", \
    "the client link was not back by the end of the pass in which the relay link\n" \
    "    reconnected: loop() must service the relay link first, or the comment in\n" \
    "    uplink_service() is wrong"
print("   a hub restart: back on the new hub in the relay link's own pass   OK")

cl_tries, relay_tries = map(int, out["away"].split())
assert relay_tries > 0, "the fake hub was never retried — scenario 2 tests nothing"
assert cl_tries == 0, \
    f"the client link made {cl_tries} connect attempt(s) while the hub was away —\n" \
    "    each can block the loop for 2 s, and the relay link is already retrying"
assert out["back"] == "1 1 1", \
    f"after the hub came back the client link was not back with the relay link: {out['back']}"
print(f"   switched off 15 min: 0 client-link connects while away ({relay_tries} by the")
print("   relay link), both back in one pass                                OK")

assert out["relay"] == "1 1 1 1", \
    "after the relay link alone dropped, the hub did not hold exactly one client\n" \
    "    link from this satellite, and never two (same pass, on hub, held, most\n" \
    f"    held = {out['relay']})"
print("   relay link alone dropped: one client link, never two              OK")

# ---- 3. the code before must fail it -------------------------------------------
old = run(re.sub(r"^.*_client_link\.stop\(\);.*\n", "", up, flags=re.M))
print("\n3. the same run with the relay link not closing the client link:")
assert old["restart"].split()[1:] == ["0", "0"], \
    f"the code before passes the restart run — it tells nothing apart: {old['restart']}"
print("   the client link stays on the old hub for good                     OK (fails, as it must)")

print("\nALL CHECKS PASSED")
