#pragma once
// ---------------------------------------------------------------------------
// Camera and position names, held by the hub and shared by every client.
// ---------------------------------------------------------------------------
// Until 2026-09-26 each device kept its own: the PC app in JSON files in
// Documents/PTS, each phone in its own browser storage.  Two devices on one rig
// could call the same shot two different things.  The hub is the one node every
// client talks to and the one that is always on, so the names live here and
// every client shows what the hub says.
//
// Keyed by CAMERA NUMBER, 1..NAMES_MAX_CAMS, never by a row on somebody's
// screen: one device may show cameras 1-5 and another 6-10, and each asks for
// the cameras it shows.  NAMES_MAX_CAMS is deliberately more than NUM_MOUNTS.
//
// Plain C++ with no Arduino in it.  The hub wraps it with NVS and the broadcast
// (esp32_hub_eth.ino); tools/test_hub_names.py compiles this file on a laptop
// and drives it with the bytes the PC app and the web app build.
//
#include <stdint.h>
#include <string.h>
#include "protocol.h"   // NAMES_MAX_CAMS, NAMES_SLOTS, NAME_MAX_BYTES, NAME_SLOT_CAMERA, ...

static_assert(NAMES_MAX_CAMS >= 1 && NAMES_MAX_CAMS <= 32,
              "cameras are tracked in 32-bit masks");
// 255 - 4, not PACKET_MAX_PAYLOAD: the packet's length byte counts the mount
// id, seq and cmd as well, so 251 is the most a payload can ever be.
static_assert(2 + (1 + NAMES_SLOTS) * (1 + NAME_MAX_BYTES) <= 255 - 4,
              "one camera's names must fit in one packet");

struct NameText {
    uint8_t len;                  // 0 = no name: the client shows its default
    char    txt[NAME_MAX_BYTES];  // UTF-8, not terminated
};
// n[0] is the camera's own name, n[1 + s] the name of position s.
struct CamNames { NameText n[1 + NAMES_SLOTS]; };

struct NameStore {
    CamNames cam[NAMES_MAX_CAMS];  // camera c at cam[c - 1]
    uint32_t rev;                  // moves whenever any name does
    uint32_t pending;              // camera bit: its CMD_NAMES is owed to the clients
    uint32_t dirty;                // camera bit: changed since it was last saved
};

static inline uint32_t names_bit(uint8_t cam)   { return 1u << (cam - 1); }
static inline bool     names_cam_ok(uint8_t cam) { return cam >= 1 && cam <= NAMES_MAX_CAMS; }

// Tidy a name the way every client should already have done: control
// characters out, spaces trimmed off both ends, and no more than NAME_MAX_BYTES
// — cut back to a whole UTF-8 character rather than through the middle of one,
// which would leave the display a replacement character to draw.
static uint8_t names_clean(const uint8_t *src, uint8_t n, char out[NAME_MAX_BYTES]) {
    uint8_t tmp[255];
    uint8_t m = 0;
    for (uint8_t i = 0; i < n; i++)
        if (src[i] >= 0x20 && src[i] != 0x7F) tmp[m++] = src[i];
    uint8_t a = 0;
    while (a < m && tmp[a] == ' ') a++;
    while (m > a && tmp[m - 1] == ' ') m--;
    uint8_t len = (uint8_t)(m - a);
    if (len > NAME_MAX_BYTES) {
        len = NAME_MAX_BYTES;
        // tmp[a + len] is the first byte cut off.  If it continues a character,
        // that character straddles the cut: drop the part that fitted.
        while (len > 0 && (tmp[a + len] & 0xC0) == 0x80) len--;
        while (len > 0 && tmp[a + len - 1] == ' ') len--;
    }
    memcpy(out, tmp + a, len);
    return len;
}

// Set one name.  slot 0..NAMES_SLOTS-1 is a position, NAME_SLOT_CAMERA the
// camera.  len 0 clears it.  Returns true if anything changed — and only then
// does the rev move and the camera fall due to be sent and saved.
static bool names_set(NameStore *s, uint8_t cam, uint8_t slot,
                      const uint8_t *txt, uint8_t len) {
    if (!names_cam_ok(cam)) return false;
    uint8_t idx;
    if (slot == NAME_SLOT_CAMERA)  idx = 0;
    else if (slot < NAMES_SLOTS)   idx = (uint8_t)(1 + slot);
    else                           return false;
    char clean[NAME_MAX_BYTES];
    uint8_t n = txt ? names_clean(txt, len, clean) : 0;
    NameText &t = s->cam[cam - 1].n[idx];
    if (t.len == n && memcmp(t.txt, clean, n) == 0) return false;
    t.len = n;
    memcpy(t.txt, clean, n);
    s->rev++;
    s->pending |= names_bit(cam);
    s->dirty   |= names_bit(cam);
    return true;
}

// CMD_NAME_SET: {cam, slot, len, UTF-8 × len}, repeated.  An entry for a camera
// or slot this hub does not keep is skipped, not fatal; one that runs off the
// end of the payload ends it.  Returns how many names changed.
static int names_apply_set(NameStore *s, const uint8_t *p, uint16_t n) {
    int changed = 0;
    uint16_t i = 0;
    while (i + 3 <= n) {
        uint8_t cam = p[i], slot = p[i + 1], len = p[i + 2];
        if (i + 3 + len > n) break;
        if (names_set(s, cam, slot, p + i + 3, len)) changed++;
        i = (uint16_t)(i + 3 + len);
    }
    return changed;
}

// A position was cleared: its name goes with it, whichever device cleared it,
// as the PC app has always done for its own clears.  Otherwise the next shot
// stored in that slot would inherit a name that described the last one.
static bool names_on_clear_pos(NameStore *s, uint8_t cam, uint8_t slot) {
    if (!names_cam_ok(cam) || slot >= NAMES_SLOTS) return false;
    return names_set(s, cam, slot, nullptr, 0);
}

static bool names_any(const CamNames &c) {
    for (int k = 0; k < 1 + NAMES_SLOTS; k++)
        if (c.n[k].len) return true;
    return false;
}

// Cameras with at least one name.
static uint32_t names_named_mask(const NameStore *s) {
    uint32_t m = 0;
    for (uint8_t c = 1; c <= NAMES_MAX_CAMS; c++)
        if (names_any(s->cam[c - 1])) m |= names_bit(c);
    return m;
}

// CMD_NAMES_GET: the cameras asked for fall due, named or not — a camera with
// no names is an answer too, and the only way a client that missed a clear
// finds out.  An empty request means every camera that has a name.
static void names_request(NameStore *s, const uint8_t *p, uint16_t n) {
    if (n == 0) {
        s->pending |= names_named_mask(s);
        return;
    }
    for (uint16_t i = 0; i < n; i++)
        if (names_cam_ok(p[i])) s->pending |= names_bit(p[i]);
}

// The next camera owed to the clients, lowest first, or 0 for none.
static uint8_t names_next_pending(NameStore *s) {
    for (uint8_t c = 1; c <= NAMES_MAX_CAMS; c++)
        if (s->pending & names_bit(c)) {
            s->pending &= ~names_bit(c);
            return c;
        }
    return 0;
}

// One camera as CMD_NAMES carries it, and as the hub saves it:
//   cam, n_slots, then 1 + n_slots × {len, UTF-8 × len}, the camera's own first.
static uint16_t names_pack(const NameStore *s, uint8_t cam, uint8_t *out) {
    const CamNames &c = s->cam[cam - 1];
    uint16_t j = 0;
    out[j++] = cam;
    out[j++] = NAMES_SLOTS;
    for (int k = 0; k < 1 + NAMES_SLOTS; k++) {
        out[j++] = c.n[k].len;
        memcpy(out + j, c.n[k].txt, c.n[k].len);
        j = (uint16_t)(j + c.n[k].len);
    }
    return j;
}

// The reverse, for a record read back from storage.  A record written by a
// build with more or fewer slots still loads the slots both have.  Returns
// false, and changes nothing, if the record is malformed.  Neither the rev nor
// the pending and dirty bits move: this is what the hub already had.
static bool names_unpack(NameStore *s, const uint8_t *p, uint16_t n) {
    if (n < 2 || !names_cam_ok(p[0])) return false;
    uint8_t  cam = p[0], slots = p[1];
    CamNames tmp;
    memset(&tmp, 0, sizeof(tmp));
    uint16_t i = 2;
    for (uint16_t k = 0; k < 1u + slots; k++) {
        if (i >= n) return false;
        uint8_t len = p[i++];
        if (i + len > n) return false;
        if (k < 1 + NAMES_SLOTS)
            tmp.n[k].len = names_clean(p + i, len, tmp.n[k].txt);
        i = (uint16_t)(i + len);
    }
    s->cam[cam - 1] = tmp;
    return true;
}

// CMD_NAMES_REV: rev (big-endian, as every multi-byte field on this link), how
// many cameras have a name, and this hub's limits.
static uint16_t names_rev_payload(const NameStore *s, uint8_t *out) {
    out[0] = (uint8_t)(s->rev >> 24);
    out[1] = (uint8_t)(s->rev >> 16);
    out[2] = (uint8_t)(s->rev >> 8);
    out[3] = (uint8_t)(s->rev);
    out[4] = (uint8_t)__builtin_popcount(names_named_mask(s));
    out[5] = NAMES_MAX_CAMS;
    out[6] = NAMES_SLOTS;
    out[7] = NAME_MAX_BYTES;
    return NAMES_REV_PAYLOAD_LEN;
}
