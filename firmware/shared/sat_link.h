#pragma once
// ---------------------------------------------------------------------------
// Satellite <-> hub link framing (Ethernet/TCP, port SAT_LINK_PORT).
// ---------------------------------------------------------------------------
// Only ever seen on the wired link between a satellite and the hub.  Nothing on
// the air, and no client, ever sees this.
//
// UPLINK (satellite -> hub) is enveloped:
//
//     [0xA5][0x5A][MAC 6][RSSI 1][LEN 1][protocol frame, LEN bytes]
//
// A satellite would otherwise be a pure byte pipe, but the hub's pairing rules
// are MAC-based (mount_table_observe) and it drops anything it cannot bind:
//
//     if (msg.rssi != 0) { ...pairing... }
//     if (msg.src_idx >= NUM_MOUNTS) continue;   // dropped
//
// So a relayed frame carrying no sender MAC would be silently discarded unless
// the mount happened to be bound already — and could never pair in the first
// place.  The envelope hands the hub exactly what its own ESP-NOW receive
// callback gets, so a satellite-attached mount behaves identically to a local
// one, conflicts and all.
//
// RSSI is the mount's signal AT THE SATELLITE, which is the useful number: it
// says whether that mount is well placed for the satellite serving it.  It also
// keeps the hub's ghost guard working, which treats rssi==0 as a phantom frame.
//
// DOWNLINK (hub -> satellite) is raw protocol frames, unwrapped.  The satellite
// reads the destination from mount_id at byte [3] and needs nothing else, so
// there is no reason to pay for a second envelope.  The two directions are
// deliberately asymmetric and each end says so.

#include <stdint.h>
#include <string.h>
#include "protocol.h"

// Not 7777: the hub treats a client on that port as an OBSERVER, whereas a
// satellite OWNS mounts and must be told apart.  Using the port for that keeps
// the distinction out of the wire protocol entirely.
#define SAT_LINK_PORT     7778

// The hub's ordinary client port — the one the PC app and the web app speak on.
// A satellite that serves the web app connects here as a normal client, in
// addition to its satellite link above, so both ends must agree on it.
#define SAT_HUB_CLIENT_PORT  7777

// The name the hub answers to, and the name a satellite dials.  Both ends read
// it from here because a mismatch is invisible from either side: the hub logs a
// healthy listener, the satellite logs a healthy AP, and the two never meet.
// The address itself comes from DHCP and is expected to move — a spare board
// swapped in after a failure gets a different one again — which is exactly why
// the satellite resolves a name instead of holding an IP.
#define SAT_HUB_HOSTNAME  "pts-hub"
#define SAT_HUB_MDNS_NAME SAT_HUB_HOSTNAME ".local"

#define SAT_ENV_MAGIC_1   0xA5
#define SAT_ENV_MAGIC_2   0x5A
#define SAT_ENV_HDR_LEN   10        // magic(2) + mac(6) + rssi(1) + len(1)
#define SAT_ENV_MAX       (SAT_ENV_HDR_LEN + 255)

// Build an uplink envelope.  Returns bytes written, or 0 if the frame is too
// long to describe in the single length byte.
static inline uint16_t sat_env_build(uint8_t *out, const uint8_t *mac,
                                     int8_t rssi, const uint8_t *frame,
                                     uint16_t frame_len) {
    if (frame_len == 0 || frame_len > 255) return 0;
    out[0] = SAT_ENV_MAGIC_1;
    out[1] = SAT_ENV_MAGIC_2;
    memcpy(out + 2, mac, 6);
    out[8] = (uint8_t)rssi;
    out[9] = (uint8_t)frame_len;
    memcpy(out + SAT_ENV_HDR_LEN, frame, frame_len);
    return SAT_ENV_HDR_LEN + frame_len;
}

// Incremental parser, so a TCP read split anywhere still resolves.  Same shape
// as the protocol's own parser: feed bytes, act when it returns true.
typedef struct {
    uint8_t  buf[SAT_ENV_MAX];
    uint16_t len;
} SatEnvParser;

typedef struct {
    uint8_t  mac[6];
    int8_t   rssi;
    uint8_t  frame_len;
    const uint8_t *frame;
} SatEnv;

static inline void sat_env_init(SatEnvParser *p) { p->len = 0; }

static inline bool sat_env_feed(SatEnvParser *p, uint8_t b, SatEnv *out) {
    // Hunt for the magic rather than trusting alignment: a satellite that
    // reboots mid-envelope must not desynchronise the hub permanently.
    if (p->len == 0 && b != SAT_ENV_MAGIC_1) return false;
    if (p->len == 1 && b != SAT_ENV_MAGIC_2) { p->len = 0; return false; }
    if (p->len >= SAT_ENV_MAX) { p->len = 0; return false; }
    p->buf[p->len++] = b;
    if (p->len < SAT_ENV_HDR_LEN) return false;

    uint16_t want = SAT_ENV_HDR_LEN + p->buf[9];
    if (p->buf[9] == 0) { p->len = 0; return false; }      // malformed
    if (p->len < want) return false;

    memcpy(out->mac, p->buf + 2, 6);
    out->rssi      = (int8_t)p->buf[8];
    out->frame_len = p->buf[9];
    out->frame     = p->buf + SAT_ENV_HDR_LEN;
    p->len = 0;
    return true;
}
