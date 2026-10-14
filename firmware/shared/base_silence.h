// base_silence.h — has a mount's base gone quiet, or was the mount not there to hear it?
//
// A mount whose base says nothing for BASE_SILENT_MS (three of the base's
// heartbeats) scans every channel for another base, and scans again every
// REACQ_PERIOD_MS while the silence lasts.  The silence was taken from the last
// frame heard, so a scan's own time off the channel counted as more of it —
// which says nothing about the base: the mount was not listening.
//
// On the bench, 2026-10-14, that locked cam1 out of a hub that was working.
// Each scan ran 8.8 s, 8.2 s of it off the base's channel, so with one every
// 10 s it listened for 1.2 s between them, less than one 2 s heartbeat.  And
// 10 s is exactly five heartbeats, so the hub's PINGs fell at the same point
// of every cycle, all of them while the radio was away.  After each hub flash
// cam1 went round that for minutes and the web app showed no cameras.  Any
// frame landing in those 1.2 s broke it, which is why starting the PC app, with
// traffic of its own, seemed to fix the web app.  Simulated with that scan, the
// old rule left a mount still scanning 5 min after 39% of hub restarts.
//
// So silence counts only while the radio is on the base's channel: from the
// later of the last frame heard and the moment the last scan gave the radio
// back.  After a scan the base gets the same three heartbeats as anywhere
// else.  A base that really has gone is still hunted, one scan per scan length
// plus BASE_SILENT_MS.
//
// Pure C++, no Arduino: tools/test_base_silence.py compiles it here.
#pragma once
#include <stdint.h>
#include "clock_age.h"

// heard  the last frame from the base (0 = none yet)
// back   when the last scan handed the radio back to the base's channel
//        (0 = no scan yet)
static inline bool base_silent(uint32_t now, uint32_t heard, uint32_t back,
                               uint32_t limit_ms) {
    return clock_silent(now, heard, limit_ms) && clock_silent(now, back, limit_ms);
}
