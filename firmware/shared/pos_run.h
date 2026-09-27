#pragma once
// ---------------------------------------------------------------------------
// A position run: a mount cycling its stored positions, owned by the mount.
// ---------------------------------------------------------------------------
// The PC app used to drive this: watch each STATUS, and when the mount reported
// it was at the target, send the goto for the next.  That made a run only as
// alive as the app driving it — a phone driving one stops the moment its screen
// locks — and runs started on two devices never saw each other.  The look-at
// run moved onto the mount for the same reasons; this is the other half.
//
// Same behaviour as the PC app's run, decision for decision:
//   - start at the first stored slot and go through them in slot order,
//     round and round, at the mount's own active speed presets;
//   - on to the next the moment the mount reports it is AT the target;
//   - a position stored or cleared mid-run joins or leaves the cycle;
//   - fewer than two stored positions and there is nothing to run between;
//   - the operator jogging takes over and ends it.
// One addition the PC never needed: a goto the mount did not act on (idle, not
// there, POS_RUN_STALL_MS after it was sent) ends the run rather than leaving
// every device showing a run that is going nowhere.
//
// Pure logic.  The bridge (esp_mount_amoled175.ino) feeds it the Teensy's
// STATUS and does what it says; tools/test_mount_run.py compiles it on a laptop.
#include <stdint.h>
#include "protocol.h"

// A STATUS older than the goto still describes the mount before it: ignore
// STATUS for this long after sending one.  The PC skipped three STATUS ticks.
#define POS_RUN_SETTLE_MS    300UL
#define POS_RUN_STALL_MS    3000UL

struct PosRun {
    bool     active;
    uint8_t  target;      // the slot being travelled to
    uint32_t sent_ms;     // when its goto went
};

// What to do after a STATUS.  The stops are kept apart so the log can say
// which: a run that ended for no visible reason is a question someone asks.
enum PosRunAction : uint8_t {
    PR_NONE = 0,
    PR_GOTO,             // send the goto for *slot
    PR_STOP_JOGGED,      // the operator took over
    PR_STOP_TOO_FEW,     // fewer than two positions left to run between
    PR_STOP_STALLED,     // a goto the mount did not act on
};

static inline int pos_run_stored(uint16_t occ) {
    return __builtin_popcount(occ & ((1u << NUM_POSITIONS) - 1));
}

// The next stored slot after `after`, wrapping round — or the first stored
// slot when `after` is not a slot.  0xFF if fewer than two are stored.
static uint8_t pos_run_next(uint16_t occ, uint8_t after) {
    if (pos_run_stored(occ) < 2) return 0xFF;
    uint8_t from = (after < NUM_POSITIONS) ? after : (uint8_t)(NUM_POSITIONS - 1);
    for (uint8_t k = 1; k <= NUM_POSITIONS; k++) {
        uint8_t s = (uint8_t)((from + k) % NUM_POSITIONS);
        if (occ & (1u << s)) return s;
    }
    return 0xFF;
}

// Start, at the first stored slot, as the PC app did.  Returns the slot to go
// to — or 0xFF, and no run, when fewer than two positions are stored.
static uint8_t pos_run_start(PosRun *r, uint16_t occ, uint32_t now) {
    uint8_t first = pos_run_next(occ, 0xFF);
    r->active = (first != 0xFF);
    if (!r->active) return 0xFF;
    r->target  = first;
    r->sent_ms = now;
    return first;
}

static inline void pos_run_stop(PosRun *r) { r->active = false; }

// Whether a CMD_JOG payload moves anything: the operator taking the camera,
// which ends either kind of run.  A zero jog does not — the PC sends one to
// announce a speed preset, and a screen sends one on its way out as a safety
// stop, and neither is anyone taking over.  Read as the Teensy reads it: four
// big-endian int16 velocities, and byte 10's axis mask, where only 0x03 (pan
// and tilt) and 0x0C (slider and zoom) narrow it and anything else means all
// four.  Under 8 bytes the Teensy ignores it, and so does this.
static inline bool jog_moves(const uint8_t *p, uint16_t len) {
    if (len < 8) return false;
    uint8_t mask = (len >= 11) ? p[10] : 0x0F;
    if (mask != 0x03 && mask != 0x0C) mask = 0x0F;
    for (int a = 0; a < 4; a++)
        if ((mask & (1u << a)) && (p[2 * a] | p[2 * a + 1])) return true;
    return false;
}

// Each STATUS from the Teensy.  PR_GOTO sets *slot; a PR_STOP_* means the run
// has ended, and says why.
static PosRunAction pos_run_on_status(PosRun *r, uint8_t state, uint16_t occ,
                                      uint16_t at, uint32_t now, uint8_t *slot) {
    if (!r->active) return PR_NONE;
    if (now - r->sent_ms < POS_RUN_SETTLE_MS) return PR_NONE;
    if (state == STATE_JOGGING) {
        r->active = false;
        return PR_STOP_JOGGED;
    }
    bool gone    = !(occ & (1u << r->target));    // cleared mid-run
    bool arrived = !gone && (at & (1u << r->target));
    if (gone || arrived) {
        uint8_t next = pos_run_next(occ, r->target);
        if (next == 0xFF) {
            r->active = false;
            return PR_STOP_TOO_FEW;
        }
        r->target  = next;
        r->sent_ms = now;
        *slot = next;
        return PR_GOTO;
    }
    if (state == STATE_IDLE && now - r->sent_ms >= POS_RUN_STALL_MS) {
        r->active = false;
        return PR_STOP_STALLED;
    }
    return PR_NONE;
}
