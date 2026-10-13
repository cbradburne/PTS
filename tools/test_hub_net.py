"""The hub says what reaches it over the wire, and who is connected.

From 16:42:15 on 2026-10-12 the concert hub ran its loop at a third of the
speed it had held for two weeks (3,400 passes a second to 1,230), and
everything arriving over the wire came late: the PC's commands, both
satellites' reports.  Everything it sent left on time.  A reflash cleared it
for forty seconds.  The hub's W5500 passes every frame it accepts across SPI to
the processor that runs the rig, and the network is the venue's, not the rig's,
so a change on it would not have been announced.  Nothing counted those frames, so
nothing could say whether the wire had got busier.

  1. shared/net_rx_count.h, compiled here under the address sanitizer: what a
     frame was addressed to and what it carried, never reading past its end;
     the counts and their reset; the busiest sender found among many, and a
     late one that dominates still found;
  2. its CMD_HUB_NET payload, decoded by the app field for field;
  3. the hub sketch: every frame still reaches the stack, a refused one is
     counted, the hook goes in safely and in the wired build only, the TCP
     connections are counted where they open, end and are turned away, and the
     report goes out with the loop report;
  4. the app's lines: the busiest sender named with the rig's own names, a busy
     wire or a refused frame as a warning, the link as negotiated, and the
     connections said when they change.

Run directly, or via tools/run_tests.sh.
"""
import io, logging, os, pathlib, re, shutil, struct, subprocess, sys, tempfile, types
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
SHARED = REPO / "firmware" / "shared"
sys.path.insert(0, str(REPO / "pc_app"))
INO = (REPO / "firmware/esp32_hub_eth/esp32_hub_eth.ino").read_text()
FLAT = re.sub(r"\s+", " ", INO)

from comms import protocol as P
from comms.protocol import Cmd, decode_hub_net

DRIVER = r"""
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <vector>
#include "net_rx_count.h"

static std::vector<uint8_t> unhex(const char *s) {
    std::vector<uint8_t> v;
    if (!strcmp(s, "-")) return v;
    for (size_t i = 0; s[i] && s[i + 1]; i += 2) {
        unsigned b = 0;
        sscanf(s + i, "%2x", &b);
        v.push_back((uint8_t)b);
    }
    return v;
}

// Exactly the readable bytes, on the heap: one byte read past them is caught.
static uint8_t *exact(const std::vector<uint8_t> &f) {
    uint8_t *b = (uint8_t *)malloc(f.size() ? f.size() : 1);
    if (f.size()) memcpy(b, f.data(), f.size());
    return b;
}

static void sender(const NetRxSender &x) {
    printf("%02x:%02x:%02x:%02x:%02x:%02x %u.%u.%u.%u %u %u %u %u\n",
           x.mac[0], x.mac[1], x.mac[2], x.mac[3], x.mac[4], x.mac[5],
           x.ip[0], x.ip[1], x.ip[2], x.ip[3], x.to, x.proto, x.port, (unsigned)x.frames);
}

int main() {
    static NetRx<8> rx;
    static char cmd[32], arg[8192];
    while (scanf("%31s", cmd) == 1) {
        if (!strcmp(cmd, "own")) {
            if (scanf("%8191s", arg) != 1) return 1;
            std::vector<uint8_t> m = unhex(arg);
            rx.set_own(m.data());
        } else if (!strcmp(cmd, "frame")) {           // frame <hex> <total>
            unsigned total;
            if (scanf("%8191s %u", arg, &total) != 2) return 1;
            std::vector<uint8_t> f = unhex(arg);
            uint8_t *b = exact(f);
            rx.note(b, (uint16_t)f.size(), total);
            free(b);
        } else if (!strcmp(cmd, "parse")) {           // parse <hex>
            if (scanf("%8191s", arg) != 1) return 1;
            std::vector<uint8_t> f = unhex(arg);
            uint8_t *b = exact(f);
            NetRxFrame o;
            if (!net_rx_parse(b, (uint16_t)f.size(), rx.own, &o)) printf("none\n");
            else printf("%u %u %u %u.%u.%u.%u %02x:%02x:%02x:%02x:%02x:%02x\n",
                        o.to, o.proto, o.port, o.ip[0], o.ip[1], o.ip[2], o.ip[3],
                        o.src[0], o.src[1], o.src[2], o.src[3], o.src[4], o.src[5]);
            free(b);
        } else if (!strcmp(cmd, "refuse")) {          // refuse <n>
            unsigned k;
            if (scanf("%u", &k) != 1) return 1;
            while (k--) rx.refused_one();
        } else if (!strcmp(cmd, "take")) {
            NetRxWindow w = rx.take();
            printf("%u %u %u %u %u %u\n", (unsigned)w.c.to_hub, (unsigned)w.c.bcast,
                   (unsigned)w.c.mcast, (unsigned)w.c.other, (unsigned)w.c.bytes,
                   (unsigned)w.c.refused);
            sender(w.top[0]);
            sender(w.top[1]);
        } else if (!strcmp(cmd, "encode")) {
            // encode <window_ms> <link> <now> <ws> <opened> <dropped> <refused> <ip>
            unsigned ms, link, now, ws, op, dr, rf;
            if (scanf("%u %u %u %u %u %u %u %8191s", &ms, &link, &now, &ws, &op, &dr,
                      &rf, arg) != 8) return 1;
            std::vector<uint8_t> ip = unhex(arg);
            NetTcpStats t = {};
            t.now = (uint8_t)now; t.ws = (uint8_t)ws; t.opened = (uint8_t)op;
            t.dropped = (uint8_t)dr; t.refused = (uint8_t)rf;
            memcpy(t.last_ip, ip.data(), 4);
            uint8_t out[HUB_NET_PAYLOAD_LEN];
            hub_net_encode(out, rx.take(), ms, (uint8_t)link, t);
            for (unsigned i = 0; i < HUB_NET_PAYLOAD_LEN; i++) printf("%02x", out[i]);
            printf("\n");
        } else {
            return 2;
        }
    }
    return 0;
}
"""

tmp = pathlib.Path(tempfile.mkdtemp())
(tmp / "d.cpp").write_text(DRIVER)
cxx = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
assert cxx, "no C++ compiler on this machine"
base = [cxx, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(SHARED),
        "-o", str(tmp / "d"), str(tmp / "d.cpp")]
SAN = ["-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-g"]
r = subprocess.run(base[:1] + SAN + base[1:], capture_output=True, text=True)
sanitized = r.returncode == 0
if not sanitized:
    r = subprocess.run(base, capture_output=True, text=True)
assert r.returncode == 0, "shared/net_rx_count.h does not compile:\n" + r.stderr


def run(script: str) -> list[str]:
    p = subprocess.run([str(tmp / "d")], input=script, capture_output=True, text=True,
                       timeout=60)
    assert p.returncode == 0 and "Sanitizer" not in p.stderr, \
        f"the counter crashed or read where it should not:\n{p.stderr[-2000:]}"
    return p.stdout.split("\n")[:-1]


# ---- frames, built the way they arrive ---------------------------------------
def mac(s: str) -> bytes:
    return bytes.fromhex(s.replace(":", ""))


def ipb(s: str) -> bytes:
    return bytes(int(x) for x in s.split("."))


def eth(dst: str, src: str, etype: int, body: bytes) -> bytes:
    return mac(dst) + mac(src) + struct.pack(">H", etype) + body


def vlan(dst: str, src: str, etype: int, body: bytes) -> bytes:
    return mac(dst) + mac(src) + struct.pack(">HHH", 0x8100, 40, etype) + body


def ip4(src: str, dst: str, proto: int, l4: bytes, ihl: int = 5, frag: int = 0,
        version: int = 4) -> bytes:
    h = bytearray(ihl * 4)
    h[0] = (version << 4) | ihl
    h[2:4] = struct.pack(">H", len(h) + len(l4))
    h[6:8] = struct.pack(">H", frag)
    h[8], h[9] = 64, proto
    h[12:16], h[16:20] = ipb(src), ipb(dst)
    return bytes(h) + l4


def udp(sport: int, dport: int) -> bytes:
    return struct.pack(">HHHH", sport, dport, 16, 0) + b"\x00" * 8


def tcp(sport: int, dport: int) -> bytes:
    return struct.pack(">HHIIBBHHH", sport, dport, 1, 1, 0x50, 0x10, 4096, 0, 0)


def arp(op: int, sha: str, spa: str, tpa: str) -> bytes:
    return (struct.pack(">HHBBH", 1, 0x0800, 6, 4, op) + mac(sha) + ipb(spa)
            + bytes(6) + ipb(tpa))


def ip6(nh: int, l4: bytes) -> bytes:
    return (bytes([0x60, 0, 0, 0]) + struct.pack(">H", len(l4)) + bytes([nh, 1])
            + bytes(16) + bytes(16) + l4)


HUB = "02:11:22:33:44:55"
PCM = "a4:bb:6d:00:00:13"
FOY = "48:27:e2:00:00:20"
FLOOD = "00:1b:21:0a:0b:0c"
ALL = "ff:ff:ff:ff:ff:ff"

F = {
    "arp":      eth(ALL, FLOOD, 0x0806, arp(1, FLOOD, "10.11.40.77", "10.11.40.1")),
    "artnet":   eth(ALL, FLOOD, 0x0800, ip4("10.11.40.77", "10.11.40.255", 17, udp(49200, 6454))),
    "to_hub":   eth(HUB, FOY, 0x0800, ip4("10.11.40.20", "10.11.40.19", 6, tcp(50000, 7778))),
    "igmp":     eth("01:00:5e:00:00:16", PCM, 0x0800,
                    ip4("10.11.40.13", "224.0.0.22", 2, b"\x22" + bytes(7))),
    "frag":     eth(ALL, FLOOD, 0x0800, ip4("10.11.40.77", "10.11.40.255", 17, b"\x12\x34" * 8,
                                            frag=185)),
    "first":    eth(ALL, FLOOD, 0x0800, ip4("10.11.40.77", "10.11.40.255", 17, udp(1, 6454),
                                            frag=0x2000)),
    "options":  eth(ALL, PCM, 0x0800, ip4("10.11.40.13", "10.11.40.255", 17, udp(1, 1900),
                                          ihl=6)),
    "mdns6":    eth("33:33:00:00:00:fb", PCM, 0x86DD, ip6(17, udp(49201, 5353))),
    "mld":      eth("33:33:00:00:00:16", PCM, 0x86DD, ip6(0, bytes(8))),
    "vlan":     vlan("01:00:5e:7f:00:01", FLOOD, 0x0800,
                     ip4("10.11.40.77", "239.255.0.1", 17, udp(1, 5568))),
    "lldp":     eth("01:80:c2:00:00:0e", FLOOD, 0x88CC, bytes(30)),
    "other":    eth("00:00:5e:00:01:01", PCM, 0x0800, ip4("10.11.40.13", "10.11.40.1", 6, tcp(1, 443))),
    "badver":   eth(ALL, FLOOD, 0x0800, ip4("10.11.40.77", "10.11.40.255", 17, udp(1, 2), version=6)),
}


def parse(frame: bytes) -> str:
    return run(f"own {HUB.replace(':', '')}\nparse {frame.hex() or '-'}\n")[0]


# ---- 1. the counter -----------------------------------------------------------
print("1. shared/net_rx_count.h, compiled here"
      + (" under the address sanitizer:" if sanitized else
         " (NO sanitizer on this machine: the bounds sweep proves less):"))
T, G, O = P.HUB_NET_TO_ALL, P.HUB_NET_TO_GROUP, P.HUB_NET_TO_OTHER
EXPECT = {
    "arp":     f"{T} {P.HUB_NET_P_ARP} 1 10.11.40.77 {FLOOD}",
    "artnet":  f"{T} {P.HUB_NET_P_UDP4} 6454 10.11.40.77 {FLOOD}",
    "to_hub":  f"{P.HUB_NET_TO_HUB} {P.HUB_NET_P_TCP4} 7778 10.11.40.20 {FOY}",
    "igmp":    f"{G} {P.HUB_NET_P_IP4} 2 10.11.40.13 {PCM}",
    "frag":    f"{T} {P.HUB_NET_P_IP4} 17 10.11.40.77 {FLOOD}",
    "first":   f"{T} {P.HUB_NET_P_UDP4} 6454 10.11.40.77 {FLOOD}",
    "options": f"{T} {P.HUB_NET_P_UDP4} 1900 10.11.40.13 {PCM}",
    "mdns6":   f"{G} {P.HUB_NET_P_UDP6} 5353 0.0.0.0 {PCM}",
    "mld":     f"{G} {P.HUB_NET_P_IP6} 0 0.0.0.0 {PCM}",
    "vlan":    f"{G} {P.HUB_NET_P_UDP4} 5568 10.11.40.77 {FLOOD}",
    "lldp":    f"{G} {P.HUB_NET_P_OTHER} {0x88CC} 0.0.0.0 {FLOOD}",
    "other":   f"{O} {P.HUB_NET_P_TCP4} 443 10.11.40.13 {PCM}",
    "badver":  f"{T} {P.HUB_NET_P_IP4} 0 0.0.0.0 {FLOOD}",
}
for name, want in EXPECT.items():
    got = parse(F[name])
    assert got == want, f"{name}: read as\n      {got}\n    expected\n      {want}"
print(f"   {len(EXPECT)} kinds of frame read right: who it was for, what it carried,"
      " from where")

assert parse(F["artnet"][:11]) == "none", \
    "a frame too short to hold a source address was counted; nobody can be named for it"
assert parse(F["artnet"][:13]) == f"{T} {P.HUB_NET_P_NONE} 0 0.0.0.0 {FLOOD}", \
    "a frame with a source but no type should count by address and say nothing more"
assert parse(F["vlan"][:16]).split()[1] == str(P.HUB_NET_P_NONE), \
    "a VLAN tag with nothing after it was read as if a type followed"
assert parse(F["artnet"][:14 + 19]) == f"{T} {P.HUB_NET_P_IP4} 0 0.0.0.0 {FLOOD}", \
    "an IPv4 header cut short was read anyway"
assert parse(F["artnet"][:14 + 20 + 3]).split()[1] == str(P.HUB_NET_P_IP4), \
    "a UDP port was read from a header cut short before it"
print("   short frames read only as far as they go")

# Every prefix of every frame, through both the parser and the counter.  Under
# the sanitizer one byte read past the end is a crash; without it this still
# proves nothing goes wrong on a frame that stops anywhere.
sweep = [f"own {HUB.replace(':', '')}"]
for fr in F.values():
    for n in range(len(fr) + 1):
        sweep.append(f"parse {fr[:n].hex() or '-'}")
        sweep.append(f"frame {fr[:n].hex() or '-'} {n}")
sweep.append("take")
out = run("\n".join(sweep) + "\n")
print(f"   {sum(len(f) + 1 for f in F.values())} truncations of {len(F)} frames,"
      " none read past the end")

# Counting, and the window starting again.
script = [f"own {HUB.replace(':', '')}"]
for name, k in (("to_hub", 3), ("artnet", 5), ("igmp", 2), ("other", 1)):
    script += [f"frame {F[name].hex()} {len(F[name]) + 4}"] * k
script += ["refuse 4", "take", "take"]
out = run("\n".join(script) + "\n")
want_bytes = sum((len(F[n]) + 4) * k for n, k in
                 (("to_hub", 3), ("artnet", 5), ("igmp", 2), ("other", 1)))
assert out[0] == f"3 5 2 1 {want_bytes} 4", f"window counts wrong: {out[0]}"
assert out[3] == "0 0 0 0 0 0" and out[4].endswith(" 0") and out[5].endswith(" 0"), \
    "take() did not start a fresh window: the next one carried the last one's counts"
print("   to the hub 3, broadcast 5, multicast 2, other 1, the bytes, 4 refused;"
      " then a fresh window")

script = ["refuse 70000", "take"]
assert run("\n".join(script) + "\n")[0].split()[5] == "65535", \
    "the refused count wrapped instead of stopping at 65535"
print("   the refused count stops at 65535 rather than wrapping")

# The busiest sender among many: 1,000 flood frames with twenty other senders
# talking in between.  The table holds eight, so the twenty churn through it.
script = [f"own {HUB.replace(':', '')}"]
for i in range(1000):
    script.append(f"frame {F['artnet'].hex()} 64")
    if i % 10 == 0:
        src = f"52:54:00:00:00:{(i // 10) % 20:02x}"
        script.append(f"frame {eth(ALL, src, 0x0806, arp(1, src, '10.11.40.5', '10.11.40.1')).hex()} 60")
script.append("take")
out = run("\n".join(script) + "\n")
top0, top1 = out[1].split(), out[2].split()
assert top0[0] == FLOOD and top0[5] == "1000", \
    f"the flood was not named, or not counted exactly: {out[1]}"
assert top0[1] == "10.11.40.77" and top0[3] == str(P.HUB_NET_P_UDP4) and top0[4] == "6454", \
    f"the busiest sender's address or what it sent is wrong: {out[1]}"
assert top1[0].startswith("52:54:00") and 1 <= int(top1[5]) <= 5, \
    f"the runner-up should be one of the twenty, never credited with more than its 5: {out[2]}"
print(f"   1,000 frames among twenty other senders: {FLOOD} named, counted exactly")

# A sender that dominates after the table has filled with busy ones still wins,
# with a stream of new senders arriving between its frames.  Each newcomer takes
# the lowest slot; ranked from nothing, the late one would always be that slot.
script = [f"own {HUB.replace(':', '')}"]
for rep in range(50):
    for k in range(12):
        src = f"52:54:00:00:01:{k:02x}"
        script.append(f"frame {eth(HUB, src, 0x0800, ip4('10.11.40.9', '10.11.40.19', 6, tcp(1, 7777))).hex()} 60")
for i in range(2000):
    script.append(f"frame {F['lldp'].hex()} 64")
    src = f"52:54:00:00:02:{i % 30:02x}"
    script.append(f"frame {eth(ALL, src, 0x0806, arp(1, src, '10.11.40.6', '10.11.40.1')).hex()} 60")
script.append("take")
out = run("\n".join(script) + "\n")
late = out[1].split()
assert late[0] == FLOOD and late[5] == "2000", \
    f"a sender that arrived late and dominated was not named, or was miscounted: {out[1]}"
print("   arriving after twelve busy senders filled the table: still named, and"
      " counted exactly")

# Its address is kept from its last IPv4 frame when a later frame has none.
script = [f"own {HUB.replace(':', '')}", f"frame {F['options'].hex()} 60",
          f"frame {F['mdns6'].hex()} 80", "take"]
kept = run("\n".join(script) + "\n")[1].split()
assert kept[1] == "10.11.40.13" and kept[3] == str(P.HUB_NET_P_UDP6), \
    f"an IPv6 frame wiped the sender's IPv4 address: {kept}"
print("   a sender's IPv4 address survives its IPv6 frames")

# ---- 2. the wire form, read by the app ----------------------------------------
print("\n2. CMD_HUB_NET, encoded there and decoded here:")
script = [f"own {HUB.replace(':', '')}"]
script += [f"frame {F['artnet'].hex()} 100"] * 7 + [f"frame {F['to_hub'].hex()} 70"] * 3
script += [f"frame {F['mdns6'].hex()} 90", f"frame {F['other'].hex()} 60", "refuse 2",
           "encode 30123 194 3 1 2 1 4 0a0b2814"]
hexpay = run("\n".join(script) + "\n")[0]
pay = bytes.fromhex(hexpay)
assert len(pay) == P.HUB_NET_PAYLOAD_LEN
n = decode_hub_net(pay)
assert (n["window_ms"], n["to_hub"], n["broadcast"], n["multicast"], n["not_hub"]) == \
    (30123, 3, 7, 1, 1), f"the counts came back wrong: {n}"
assert n["bytes"] == 7 * 100 + 3 * 70 + 90 + 60 and n["refused"] == 2, \
    f"the bytes or the refused count came back wrong: {n}"
assert n["link"] == 194 and (n["tcp_now"], n["ws_now"], n["tcp_opened"],
                             n["tcp_dropped"], n["tcp_refused"]) == (3, 1, 2, 1, 4), \
    f"the link or the connection counts came back wrong: {n}"
assert n["tcp_last_ip"] == "10.11.40.20", f"the last connection's address came back wrong: {n}"
assert [t["mac"] for t in n["top"]] == [FLOOD, FOY], \
    f"the two senders did not both come through, busiest first: {n['top']}"
assert n["top"][0] == {"mac": FLOOD, "ip": "10.11.40.77", "to": P.HUB_NET_TO_ALL,
                       "proto": P.HUB_NET_P_UDP4, "port": 6454, "frames": 7}, \
    f"the busiest sender came back wrong: {n['top'][0]}"
assert n["top"][1]["frames"] == 3 and n["top"][1]["port"] == 7778, \
    f"the second sender came back wrong: {n['top'][1]}"
print("   counts, bytes, refused, link, connections, last address, both senders:"
      " every field the same")
empty = decode_hub_net(bytes.fromhex(run("encode 0 0 0 0 0 0 0 00000000\n")[0]))
assert empty["top"] == [] and empty["tcp_last_ip"] == "", \
    "an empty window should decode to no senders and no address, not zeros dressed up"
print("   an empty window decodes to no senders and no address")

# ---- 3. the hub sketch ----------------------------------------------------------
print("\n3. the hub:")
m = re.search(r"static err_t netrx_input\(struct pbuf \*p, struct netif \*inp\) \{(.*?)\n\}",
              INO, re.S)
assert m, "netrx_input() is gone: nothing sits between the W5500 and the stack"
body = m.group(1)
assert re.search(r"err_t e = _netrx_next\(p, inp\);", body) and "return e;" in body, \
    "a frame must still reach the stack, and its answer must go back to the driver,\n" \
    "    which frees the frame when the answer is not ERR_OK"
assert re.search(r"if \(e != ERR_OK\) \{.*?_netrx\.refused_one\(\);", body, re.S), \
    "a frame the stack refused is not counted: the lateness has no number"
assert body.count("portENTER_CRITICAL(&_netrx_mux)") == 2 and \
    body.count("portEXIT_CRITICAL(&_netrx_mux)") == 2, \
    "the driver task writes the counts while loop() reads them: both under the lock"
assert re.search(r"_netrx\.note\(\(const uint8_t \*\)p->payload, p->len, p->tot_len\)", body), \
    "the counter must be given the readable bytes (p->len), not the whole frame's length"
print("   every frame still goes on to the stack; a refused one is counted, under the lock")

m = re.search(r"static void netrx_hook\(\) \{(.*?)\n\}", INO, re.S)
assert m, "netrx_hook() is gone"
hook = m.group(1)
i_next, i_bar, i_swap = (hook.find("_netrx_next = n->input;"),
                         hook.find("__sync_synchronize();"),
                         hook.find("n->input = netrx_input;"))
assert -1 not in (i_next, i_bar, i_swap) and i_next < i_bar < i_swap, \
    "the stack's input must be saved, then a barrier, then ours swapped in: the\n" \
    "    driver task can take a frame the instant the pointer changes"
assert hook.find("if (n->input == netrx_input) return;") != -1 and \
    hook.find("if (n->input == netrx_input) return;") < i_next, \
    "hooking twice would save OUR input as the next one, and every frame would\n" \
    "    loop back into the counter for ever"
assert "_netrx.set_own(n->hwaddr);" in hook, \
    "'to the hub' must mean the interface's own MAC"
assert re.search(r"#if HUB_WIRED && HUB_NET_COUNT\s*\nstatic netif_input_fn _netrx_next", INO), \
    "the hook belongs to the wired build: the XIAO has no Ethernet interface"
assert re.search(r"if \(now - _netrx_hook_ms >= 1000UL\) \{ _netrx_hook_ms = now; netrx_hook\(\); \}",
                 FLAT), \
    "the hook must be retried once a second: the interface exists only after\n" \
    "    ETH.begin() returns, and a rebuilt one comes with the stack's input back"
assert re.search(r"#if HUB_NET_COUNT static uint32_t _netrx_hook_ms = 0;", FLAT), \
    "with the counter switched off nothing may hook in"
print("   the hook: saved, barrier, swapped; never twice; wired build only; retried each second")

assert re.search(r"#ifndef HUB_NET_COUNT\s*\n#define HUB_NET_COUNT 1\s*\n#endif", INO), \
    "the counter must be on by default; HUB_NET_COUNT=0 is the way back"
BUILD = (REPO / "tools/build.sh").read_text()
assert '[ -n "${HUB_NET_COUNT:-}" ] && _f="$_f -DHUB_NET_COUNT=${HUB_NET_COUNT}"' in BUILD, \
    "build.sh must pass HUB_NET_COUNT through, or the switch cannot be thrown"
assert re.search(r"#if HUB_NET_COUNT\s+.*?struct netif \*nif = netrx_if\(\);\s+"
                 r"if \(nif && nif->input == netrx_input\) link \|= HUB_NET_LINK_COUNTED;",
                 INO, re.S), \
    "the report must say whether the counter was in, from the interface itself:\n" \
    "    a rebuilt interface has the stack's input back, and its counts are not counts"
print("   on by default, HUB_NET_COUNT=0 through build.sh; the report says whether it counted")

assert re.search(r"send_loop_report\(\); send_net_report\(\);", FLAT), \
    "the network report must go out with the loop report, every LOOP_REPORT_MS"
m = re.search(r"static void send_net_report\(\) \{(.*?)\n\}", INO, re.S)
assert m, "send_net_report() is gone"
rep = m.group(1)
assert re.search(r"NetRxWindow w = _netrx\.take\(\);", rep) and \
    rep.find("portENTER_CRITICAL(&_netrx_mux)") < rep.find("_netrx.take()") < \
    rep.find("portEXIT_CRITICAL(&_netrx_mux)"), "take() must run under the lock"
assert "build_packet(raw, 0xFE, ++_pair_seq, CMD_HUB_NET, buf, sizeof(buf))" in rep
assert re.search(r"if \(ETH\.linkUp\(\)\) \{ link = HUB_NET_LINK_UP; if \(ETH\.fullDuplex\(\)\) "
                 r"link \|= HUB_NET_LINK_FULL;", re.sub(r"\s+", " ", rep)), \
    "the link byte must say up and duplex from what the W5500 negotiated"
assert re.search(r"_tcp_opened = _tcp_dropped = _tcp_turned_away = 0;", rep) and \
    rep.find("t.opened  = _tcp_opened;") < rep.find("_tcp_opened = _tcp_dropped"), \
    "the connection counts must be copied out, then cleared, once per report"
print("   sent with the loop report; take() under the lock; link from the W5500; counts cleared")

acc = INO[INO.find("WiFiClient incoming = _tcp_server.accept();"):
          INO.find("MARK(SEC_ACCEPT);")]
assert re.search(r"bool ended = _slots\[i\]\.active && !_slots\[i\]\.client\.connected\(\);", acc) \
    and re.search(r"if \(ended\) tcp_count\(_tcp_dropped\);", acc), \
    "a slot reused because its client had gone must count that client as dropped"
assert re.search(r"_slots\[i\]\.active = true; placed = true;\s*tcp_count\(_tcp_opened\);", acc) \
    and "_tcp_last_ip[k] = from[k];" in acc, \
    "an accepted connection must be counted, with the address it came from"
assert re.search(r"if \(!placed\) \{\s*incoming\.stop\(\);\s*tcp_count\(_tcp_turned_away\);", acc), \
    "a connection turned away for want of a slot must be counted"
tcp_sec = INO[INO.find("// ---- TCP clients → mounts ----"):INO.find("MARK(SEC_TCP);")]
assert re.search(r"_slots\[i\]\.active = false;\s*tcp_count\(_tcp_dropped\);", tcp_sec), \
    "a connection that ends must be counted as dropped"
print("   connections counted where they open (with the address), end, and are turned away")

# ---- 4. the app's lines --------------------------------------------------------
print("\n4. the app:")
from comms.bridge import Bridge
import comms.bridge as bridgemod

src = (REPO / "pc_app/comms/bridge.py").read_text()
disp = src[src.find("def _dispatch("):src.find("for cb in self._callbacks:")]
assert "self._note_hub_net(pkt)" in disp, "the packet is decoded but never handed to the logger"

BUF = io.StringIO()
_h = logging.StreamHandler(BUF)
_h.setFormatter(logging.Formatter("%(levelname)s|%(message)s"))
_lg = logging.getLogger("comms.bridge")
_lg.handlers, _lg.propagate = [_h], False
_lg.setLevel(logging.INFO)


def payload(win=30000, to_hub=0, bc=0, mc=0, other=0, nbytes=0, refused=0, link=0xE2,
            now=1, ws=0, opened=0, dropped=0, turned=0, last="0.0.0.0", tops=()):
    b = bytearray(P.HUB_NET_PAYLOAD_LEN)
    struct.pack_into(">IIIIIIH", b, 0, win, to_hub, bc, mc, other, nbytes, refused)
    b[26:32] = bytes([link, now, ws, opened, dropped, turned])
    b[32:36] = ipb(last)
    for k, (mc_, ip_, to_, pr_, port_, fr_) in enumerate(tops):
        e = P.HUB_NET_TOP_OFF + k * P.HUB_NET_TOP_LEN
        b[e:e + 6], b[e + 6:e + 10] = mac(mc_), ipb(ip_)
        b[e + 10], b[e + 11] = to_, pr_
        struct.pack_into(">HI", b, e + 12, port_, fr_)
    return bytes(b)


br = Bridge.__new__(Bridge)
br._transport = types.SimpleNamespace(local_ip="10.11.40.13")
br._sat_names, br._sat_ips = {2: "Foyer"}, {2: "10.11.40.20"}


def say(pl: bytes) -> list[str]:
    BUF.truncate(0); BUF.seek(0)
    br._note_hub_net(types.SimpleNamespace(cmd=Cmd.HUB_NET, payload=pl, mount_id=0xFE))
    return BUF.getvalue().strip().splitlines()


lines = say(payload(to_hub=1500, bc=58500, nbytes=7_500_000, last="10.11.40.13",
                    tops=[(FLOOD, "10.11.40.77", P.HUB_NET_TO_ALL, P.HUB_NET_P_UDP4, 6454, 58500),
                          (PCM, "10.11.40.13", P.HUB_NET_TO_HUB, P.HUB_NET_P_TCP4, 7777, 750)]))
net = lines[0]
assert net.startswith("WARNING|HUB NET: 2000 frames/s arriving, 2.00 Mbit/s"), \
    f"the total, the bit rate or the warning is wrong: {net}"
assert "50 to the hub, 1950 broadcast, 0 multicast, 0 for other devices" in net, \
    f"the per-kind rates are wrong: {net}"
assert f"busiest: 10.11.40.77 [{FLOOD}] 1950/s broadcast UDP 6454 (Art-Net)" in net, \
    f"the busiest sender is not named with its address, MAC, rate and what it sent: {net}"
assert f"next: 10.11.40.13 (this PC) [{PCM}] 25/s to the hub TCP 7777 (the hub's client port)" \
    in net, f"this PC is not named as this PC: {net}"
assert net.endswith("| link 100 Mbit full duplex"), f"the link reads wrong: {net}"
print("   " + net.split("|", 1)[1][:150] + "…")
assert lines[1] == ("INFO|HUB CLIENTS: 1 TCP connected (the PC app and each satellite's web "
                    "link), 0 web-app page(s) | the last one opened came from 10.11.40.13 "
                    "(this PC)"), lines[1]
print("   " + lines[1].split("|", 1)[1])

edge = say(payload(bc=15000))[0]
under = say(payload(bc=14970))[0]
assert edge.startswith("WARNING|") and under.startswith("INFO|"), \
    f"{Bridge.HUB_NET_BUSY_PER_S}/s of traffic the hub did not ask for is the warning line, " \
    f"one less is not:\n      {edge[:60]}\n      {under[:60]}"
assert say(payload(to_hub=60000))[0].startswith("INFO|"), \
    "traffic addressed TO the hub is its own work, not a busy network"
print(f"   {Bridge.HUB_NET_BUSY_PER_S}/s not addressed to the hub warns, 499 does not;"
      " its own traffic never does")

ref = say(payload(to_hub=900, refused=12))[0]
assert ref.startswith("WARNING|") and "12 frames TURNED AWAY by the hub's network stack" in ref, \
    f"frames the stack turned away must make a warning, with the count: {ref}"
print("   a refused frame is a warning, with the count")

for link, words in ((0xA2, "link 100 Mbit HALF duplex"), (0xE1, "link 10 Mbit full duplex"),
                    (0xA0, "link unknown speed HALF duplex"), (0x20, "no wired link")):
    got = say(payload(to_hub=900, link=link))[0]
    assert got.endswith(words), f"link 0x{link:02X} should read '{words}': {got}"
print("   the link as negotiated: 100/10 Mbit, full or HALF duplex, or none")

# Not counted is not zero.  A hub built with HUB_NET_COUNT=0, or one whose hook
# has not gone in, sends empty counts: said once, as what it is.
def net_lines(pl: bytes) -> list[str]:
    return [l for l in say(pl) if "|HUB NET:" in l]


off = net_lines(payload(link=0xC2))
assert off == ["INFO|HUB NET: nothing counted — the counter is not in front of the hub's "
               "network port (built with HUB_NET_COUNT=0, or not hooked in yet) | link 100 "
               "Mbit full duplex"], f"uncounted reports must say so, not show zeros: {off}"
assert net_lines(payload(link=0xC2)) == [], \
    "'nothing counted' must be said once, not every 30 s"
assert net_lines(payload(to_hub=900))[0].startswith("INFO|HUB NET: 30 frames/s"), \
    "once it counts again, the counts must come back"
assert net_lines(payload(link=0x00)) == ["INFO|HUB NET: nothing counted — no wired link"], \
    "a hub with no wire (the XIAO) must say that, once"
assert net_lines(payload(link=0x00)) == [], "'no wired link' must be said once, not every 30 s"
print("   not counted is said once as such, never as zeros; no wire is said once")

slow = say(payload(win=30000, to_hub=15, tops=[(FOY, "10.11.40.20", P.HUB_NET_TO_HUB,
                                                 P.HUB_NET_P_TCP4, 7778, 15)]))[0]
assert "0.5 to the hub" in slow, f"a rate under 10 lost its decimal: {slow}"
assert f"busiest: 10.11.40.20 (Foyer satellite) [{FOY}] 0.5/s to the hub TCP 7778 " \
    "(the satellite link)" in slow, f"the satellite is not named as the Foyer satellite: {slow}"
assert "0 frames/s" not in slow and "0.00 Mbit/s" in slow, f"slow totals read wrong: {slow}"
print("   a satellite named as the rig knows it; slow rates keep a decimal")

assert say(payload(to_hub=900)) and len(say(payload(to_hub=900))) == 1, \
    "the connections line repeated with nothing changed: every 30 s for ever"
moved = say(payload(to_hub=900, opened=2, dropped=2, last="10.11.40.20"))
assert len(moved) == 2 and moved[1] == (
    "INFO|HUB CLIENTS: 1 TCP connected (the PC app and each satellite's web "
    "link), 0 web-app page(s) | last 30 s: 2 opened, 2 dropped, 0 turned away "
    "(every slot full) | the last one opened came from 10.11.40.20 "
    "(Foyer satellite)"), f"connections that came and went were not said as they were: {moved}"
print("   " + moved[1].split("|", 1)[1][:120] + "…")
assert len(say(payload(to_hub=900, now=3))) == 2, \
    "a change in how many are connected must be said even when none came or went this window"
turned = say(payload(to_hub=900, now=3, opened=255, turned=255))[1]
assert turned.startswith("WARNING|"), f"a connection turned away must warn: {turned}"
assert "255+ opened" in turned and "255+ turned away" in turned, \
    f"a count that stopped at 255 must say so: {turned}"
print("   said on a change; a turned-away connection warns; a full count reads 255+")

BUF.truncate(0); BUF.seek(0)
br._note_hub_net(types.SimpleNamespace(cmd=Cmd.HUB_NET, payload=bytes(71), mount_id=0xFE))
assert BUF.getvalue() == "", "a short payload must be ignored, not half-read"
print("   a short payload is ignored")

print("\nALL CHECKS PASSED")
