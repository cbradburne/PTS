// net_rx_count.h — every frame the hub's network chip hands it, by who it was
// addressed to, and the two busiest senders.
//
// From 16:42:15 on 2026-10-12 the hub ran its loop at a third of the speed it
// had held for two weeks, and everything arriving over the wire came late: the
// PC's commands, both satellites' reports.  Everything it sent left on time.  A
// reflash cleared it for forty seconds.  The network is the venue's, not the
// rig's, so a change on it would not have been announced.
//
// The W5500 passes every frame it accepts across SPI, one at a time, to the
// processor that runs the rig: the hub's own traffic, and every broadcast and
// multicast on the segment.  Nothing counted them, so nothing could say whether
// the wire had got busier.  This counts them as they arrive, and keeps the
// senders in a small table so the busiest can be named — by MAC and address,
// which is what the network's owners can look up.
//
// The table is space-saving: when it is full the lowest-ranked sender makes way
// and the newcomer is ranked as if it had that sender's count, so one that
// arrives late and then dominates still rises to the top.  The frames reported
// are only those actually counted from it, never more than it sent: a sender
// that dominates the window is counted exactly.
//
// Pure C++, no Arduino: tools/test_hub_net.py compiles it here.  The sketch
// calls note() from the Ethernet driver's task and take() from loop(), under
// one spinlock.
#pragma once
#include <stdint.h>
#include <string.h>
#include "protocol.h"

static_assert(HUB_NET_PAYLOAD_LEN == HUB_NET_TOP_OFF + 2 * HUB_NET_TOP_LEN,
              "CMD_HUB_NET carries exactly two senders after the counts");

struct NetRxSender {
    uint8_t  mac[6];
    uint8_t  ip[4];      // IPv4 as on the wire; all zero when it sent none
    uint8_t  to;         // HUB_NET_TO_ of its last frame
    uint8_t  proto;      // HUB_NET_P_ of its last frame
    uint16_t port;       // see HUB_NET_P_ in protocol.h
    uint32_t frames;     // 0 = no such sender
};

struct NetRxCounts {
    uint32_t to_hub, bcast, mcast, other, bytes;
    uint16_t refused;
};

struct NetRxWindow {
    NetRxCounts c;
    NetRxSender top[2];   // busiest first
};

// What one frame's headers say, as far as the readable bytes go.
struct NetRxFrame {
    uint8_t        to;      // HUB_NET_TO_
    uint8_t        proto;   // HUB_NET_P_
    uint16_t       port;
    uint8_t        ip[4];
    const uint8_t *src;     // source MAC, inside the frame
};

static inline uint16_t net_rx_be16(const uint8_t *p) {
    return (uint16_t)((p[0] << 8) | p[1]);
}

// `n` is how many bytes at `f` can be read.  False when there is not even a
// source address: that is not a frame anyone could be blamed for.
static inline bool net_rx_parse(const uint8_t *f, uint16_t n, const uint8_t own[6],
                                NetRxFrame *o) {
    if (n < 12) return false;
    static const uint8_t all[6] = { 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF };
    if (memcmp(f, all, 6) == 0)       o->to = HUB_NET_TO_ALL;
    else if (f[0] & 0x01)             o->to = HUB_NET_TO_GROUP;
    else if (memcmp(f, own, 6) == 0)  o->to = HUB_NET_TO_HUB;
    else                              o->to = HUB_NET_TO_OTHER;
    o->src   = f + 6;
    o->proto = HUB_NET_P_NONE;
    o->port  = 0;
    memset(o->ip, 0, 4);
    if (n < 14) return true;

    uint16_t et  = net_rx_be16(f + 12);
    unsigned off = 14;
    if (et == 0x8100) {                         // 802.1Q: the real type follows the tag
        if (n < 18) return true;
        et  = net_rx_be16(f + 16);
        off = 18;
    }
    const uint8_t *l3 = f + off;
    unsigned       m  = n - off;                // readable bytes from the L3 header on

    if (et == 0x0806) {                         // ARP: the operation, and who is asking
        o->proto = HUB_NET_P_ARP;
        if (m >= 8) o->port = net_rx_be16(l3 + 6);
        if (m >= 18 && l3[4] == 6 && l3[5] == 4) memcpy(o->ip, l3 + 14, 4);
    } else if (et == 0x0800) {                  // IPv4
        o->proto = HUB_NET_P_IP4;
        if (m < 20 || (l3[0] >> 4) != 4) return true;
        memcpy(o->ip, l3 + 12, 4);
        unsigned ihl = (l3[0] & 0x0F) * 4u;
        uint8_t  pr  = l3[9];
        o->port = pr;
        // A later fragment carries no UDP or TCP header, only the middle of one.
        bool first = (net_rx_be16(l3 + 6) & 0x1FFF) == 0;
        if ((pr == 17 || pr == 6) && first && ihl >= 20 && m >= ihl + 4) {
            o->proto = (pr == 17) ? HUB_NET_P_UDP4 : HUB_NET_P_TCP4;
            o->port  = net_rx_be16(l3 + ihl + 2);
        }
    } else if (et == 0x86DD) {                  // IPv6: no room for its address, so MAC only
        o->proto = HUB_NET_P_IP6;
        if (m < 40) return true;
        uint8_t nh = l3[6];
        o->port = nh;
        if (nh == 17 && m >= 44) {
            o->proto = HUB_NET_P_UDP6;
            o->port  = net_rx_be16(l3 + 42);
        }
    } else {
        o->proto = HUB_NET_P_OTHER;
        o->port  = et;
    }
    return true;
}

template <unsigned SLOTS>
struct NetRx {
    static_assert(SLOTS >= 2, "two senders are reported");
    uint8_t     own[6] = {};
    NetRxCounts c      = {};
    NetRxSender s[SLOTS] = {};
    uint32_t    rank[SLOTS] = {};   // its frames, plus the count it inherited
    unsigned    used   = 0;

    void set_own(const uint8_t m[6]) { memcpy(own, m, 6); }

    // One frame as it arrives.  `n` is what is readable at `f` (the first
    // buffer of it); `total` is the whole frame.
    void note(const uint8_t *f, uint16_t n, uint32_t total) {
        NetRxFrame fr;
        if (!net_rx_parse(f, n, own, &fr)) return;
        switch (fr.to) {
            case HUB_NET_TO_HUB:   c.to_hub++; break;
            case HUB_NET_TO_ALL:   c.bcast++;  break;
            case HUB_NET_TO_GROUP: c.mcast++;  break;
            default:               c.other++;  break;
        }
        c.bytes += total;

        unsigned k = 0;
        while (k < used && memcmp(s[k].mac, fr.src, 6) != 0) k++;
        if (k == used) {
            if (used < SLOTS) {
                used++;
                rank[k] = 0;
            } else {
                k = 0;                          // the lowest makes way; its rank stays
                for (unsigned i = 1; i < SLOTS; i++)
                    if (rank[i] < rank[k]) k = i;
            }
            memcpy(s[k].mac, fr.src, 6);
            memset(s[k].ip, 0, 4);
            s[k].frames = 0;
        }
        NetRxSender *e = &s[k];
        rank[k]++;
        e->frames++;
        e->to    = fr.to;
        e->proto = fr.proto;
        e->port  = fr.port;
        if (fr.ip[0] | fr.ip[1] | fr.ip[2] | fr.ip[3]) memcpy(e->ip, fr.ip, 4);
    }

    // A frame the stack would not take.
    void refused_one() { if (c.refused < 0xFFFF) c.refused++; }

    // The window that just ended, with its two busiest senders.  Starts the next.
    NetRxWindow take() {
        NetRxWindow w = {};
        w.c = c;
        int a = -1, b = -1;
        for (unsigned i = 0; i < used; i++) {
            if (a < 0 || rank[i] > rank[a]) { b = a; a = (int)i; }
            else if (b < 0 || rank[i] > rank[b]) b = (int)i;
        }
        if (a >= 0) w.top[0] = s[a];
        if (b >= 0) w.top[1] = s[b];
        c    = NetRxCounts{};
        used = 0;
        return w;
    }
};

// The connections the hub holds, for the same report.
struct NetTcpStats {
    uint8_t now, ws, opened, dropped, refused;
    uint8_t last_ip[4];
};

static inline void net_rx_put32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)(v >> 24); p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);  p[3] = (uint8_t)v;
}

// The CMD_HUB_NET payload.  Layout in shared/protocol.h.
static inline void hub_net_encode(uint8_t *out, const NetRxWindow &w, uint32_t window_ms,
                                  uint8_t link, const NetTcpStats &t) {
    memset(out, 0, HUB_NET_PAYLOAD_LEN);
    net_rx_put32(out + 0,  window_ms);
    net_rx_put32(out + 4,  w.c.to_hub);
    net_rx_put32(out + 8,  w.c.bcast);
    net_rx_put32(out + 12, w.c.mcast);
    net_rx_put32(out + 16, w.c.other);
    net_rx_put32(out + 20, w.c.bytes);
    out[24] = (uint8_t)(w.c.refused >> 8);
    out[25] = (uint8_t)w.c.refused;
    out[26] = link;
    out[27] = t.now;
    out[28] = t.ws;
    out[29] = t.opened;
    out[30] = t.dropped;
    out[31] = t.refused;
    memcpy(out + 32, t.last_ip, 4);
    for (int k = 0; k < 2; k++) {
        uint8_t           *e = out + HUB_NET_TOP_OFF + k * HUB_NET_TOP_LEN;
        const NetRxSender &x = w.top[k];
        memcpy(e, x.mac, 6);
        memcpy(e + 6, x.ip, 4);
        e[10] = x.to;
        e[11] = x.proto;
        e[12] = (uint8_t)(x.port >> 8);
        e[13] = (uint8_t)x.port;
        net_rx_put32(e + 14, x.frames);
    }
}
