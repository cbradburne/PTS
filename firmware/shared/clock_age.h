// clock_age.h — how long ago a millis() stamp was, safe against a stamp that is
// a little AHEAD of the clock it is compared with.
//
// The trap, caught by cam1's own report on 2026-10-07: loop() reads the clock,
// and a moment later the WiFi task, on the other core, stamps _last_hub_rx_ms
// with a fresh millis().  `now - stamp` is then -1, which unsigned is
// 4294967295 ms: the mount took its base for 49 days silent and scanned every
// channel for another, deaf and mute for seven seconds, two or three times a
// day on every mount.  3d92492 fixed the same trap for the heartbeat by ageing
// against a fresh clock; this is the general form, for every age taken against
// a stamp another task writes, wherever the clock was read.
//
// Pure C++, no Arduino: tools/test_clock_age.py compiles it here.
#pragma once
#include <stdint.h>

// A stamp up to this far ahead of `now` is a race, not a wrap, and reads as
// just now.  A minute is far more than any race and far less than the 49.7
// days a real wrap would take.
#define CLOCK_AGE_AHEAD_MS 60000UL

static inline uint32_t clock_age_ms(uint32_t now, uint32_t stamp) {
    uint32_t d = now - stamp;
    return d > (0xFFFFFFFFUL - CLOCK_AGE_AHEAD_MS) ? 0 : d;
}

// Has nothing been heard for more than `limit_ms`?  The base-silence test that
// started the scans, in one place so it cannot be written the unsafe way again.
static inline bool clock_silent(uint32_t now, uint32_t heard, uint32_t limit_ms) {
    return clock_age_ms(now, heard) > limit_ms;
}
