// ws_status_gate.h — a mount's STATUS to the web app: at most one per window,
// and the newest never lost.
//
// The hub throttles STATUS to WebSocket clients to one per mount per 200 ms,
// and the satellite copies it, because at the old 10 Hz per mount lwIP choked
// on the ACKs and closed the phone's connection.  The throttle DROPPED the rest.
// That was safe while the mount streamed STATUS: a dropped one was replaced
// 100 ms later.  Since TEENSY_FWD_FILTER the mount's bridge sends STATUS only
// when something changes, plus a 5 s refresh, so a dropped STATUS is a dropped
// CHANGE, and nothing replaces it for 5 s.
//
// Seen 2026-10-14: SET a position on the iPad and its border stayed red (stored,
// not here) for 5 s before turning green, where the PC app, which is not
// throttled, went green at once.  The store's own STATUS still carried the old
// at-position bits; the corrected one, 100 ms later, fell inside the window and
// was dropped.  The same drop could hold the end of a jog or of a goto.
//
// Now a STATUS that cannot go yet is held, the newest replacing any older one,
// and goes the moment its window ends.  The same rate, and nothing lost.
//
// Pure C++, no Arduino: tools/test_ws_status_gate.py compiles it here.  Used
// from one task (loop()) on both boards, so it takes no lock.
#pragma once
#include <stdint.h>
#include <string.h>
#include "clock_age.h"

template <unsigned MOUNTS, unsigned MAXLEN>
struct WsStatusGate {
    uint32_t window_ms;
    uint32_t sent_ms[MOUNTS]  = {};
    uint16_t held_len[MOUNTS] = {};
    uint8_t  held[MOUNTS][MAXLEN];

    explicit WsStatusGate(uint32_t window) : window_ms(window) {}

    // A STATUS from mount m (0-based) has arrived.  True: send it now, and
    // anything held for m is out of date.  False: keep it with hold().  `hush`
    // holds everything: the hub's quiet while a jog has the radio.
    bool admit(unsigned m, uint32_t now, bool hush) {
        if (m >= MOUNTS) return true;
        if (hush || clock_age_ms(now, sent_ms[m]) < window_ms) return false;
        sent_ms[m]  = now;
        held_len[m] = 0;
        return true;
    }

    // Keep this STATUS for m, replacing any older one.  False if it cannot be
    // kept (too long, or no such mount): send it anyway rather than lose it.
    bool hold(unsigned m, const uint8_t *f, uint16_t n) {
        if (m >= MOUNTS || n == 0 || n > MAXLEN) return false;
        memcpy(held[m], f, n);
        held_len[m] = n;
        return true;
    }

    // The STATUS held for m, once its window has passed and the hush is over.
    bool due(unsigned m, uint32_t now, bool hush, uint8_t *out, uint16_t *n) {
        if (m >= MOUNTS || !held_len[m] || hush) return false;
        if (clock_age_ms(now, sent_ms[m]) < window_ms) return false;
        memcpy(out, held[m], held_len[m]);
        *n          = held_len[m];
        held_len[m] = 0;
        sent_ms[m]  = now;
        return true;
    }
};
