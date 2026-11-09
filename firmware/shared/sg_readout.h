/*
 * sg_readout.h — what one leg of a limit find saw of StallGuard.
 *
 * Tuning a stall threshold is choosing a trip point between two readings: the
 * lowest SG_RESULT while the axis moves freely (trip above that and it stops
 * early) and the reading when it pushes against its end (trip below that and
 * it runs on).  The Teensy samples SG_RESULT while a leg moves and keeps two
 * numbers for the Find Limits dialog: the lowest, and the last.
 *
 * The lowest leaves out the newest samples, SG_READOUT_TAIL of them taken
 * SG_READOUT_SAMPLE_MS apart — the last 150 ms before the leg ended.  That is
 * the approach to the end: the load climbing as the ring meets its stop.
 * Counting it would put a reading from the stop itself into "lowest while
 * moving" and make every good stall look as if it had no headroom.  The last
 * sample is the opposite case and is kept as it is: on a leg that ran into its
 * end without tripping, it is the reading at the end.
 *
 * No Arduino here, so tools/test_limit_find_readings.py compiles and runs it.
 */
#pragma once
#include <stdint.h>

#define SG_READOUT_NONE       0xFFFF   // no sample (SG_RESULT is 10 bits, so never real)
#define SG_READOUT_SAMPLE_MS  50       // one SG_RESULT read per this, while a leg moves
#define SG_READOUT_TAIL       3        // newest samples held out of the lowest: 150 ms

struct SgReadout {
    uint16_t lowest;                    // lowest sample older than the tail
    uint16_t last;                      // newest sample
    uint16_t tail[SG_READOUT_TAIL];     // newest samples, oldest first
    uint8_t  n_tail;
};

static inline void sg_readout_reset(SgReadout *r) {
    r->lowest = SG_READOUT_NONE;
    r->last   = SG_READOUT_NONE;
    r->n_tail = 0;
}

// One sample from a leg that is moving and past its settle time.
static inline void sg_readout_add(SgReadout *r, uint16_t sg) {
    if (r->n_tail == SG_READOUT_TAIL) {
        // The oldest sample leaves the tail: it was not part of the ending.
        if (r->tail[0] < r->lowest) r->lowest = r->tail[0];
        for (int i = 1; i < SG_READOUT_TAIL; i++) r->tail[i - 1] = r->tail[i];
        r->n_tail--;
    }
    r->tail[r->n_tail++] = sg;
    r->last = sg;
}
