// tx_pacer.h — one ESP-NOW send in flight at a time, the rest waiting in turn.
//
// espressif/esp-idf#18682, still open on 2026-10-09: ESP-NOW stops finishing
// sends — no send-complete callback ever comes — until the queue fills and every
// send is refused NO_MEM.  It is reproduced there on ESP-IDF 5.5.5 (the core
// this rig builds with) and on 6.1-dev, and no core release fixes it.  The
// reporter stopped it by pacing: never call esp_now_send() while a send is
// still out, and let its callback release the next.
//
// The mount half did this.  Command replies and its routine reports waited for
// the one in flight, but the camera's status forward (the NimBLE task, on core
// 0) and the Teensy's forwarded frames went out at once, whatever was out, from
// two cores.  Both stalls the radio history caught — cam1 on 2026-10-08, cam3
// on 10-09 — were first refused on a CAM_STATUS.  Through this, every send
// waits its turn.
//
// Pure C++, no Arduino: tools/test_tx_pacer.py compiles it here.  The sketch
// holds its lock around every call.
#pragma once
#include <stdint.h>
#include <string.h>

template <unsigned DEPTH, unsigned MAXLEN>
struct TxPacer {
    uint8_t  data[DEPTH][MAXLEN];
    uint16_t len[DEPTH] = {};
    unsigned head = 0, count = 0;
    // Since the last take_stats(): sends that had to wait, the most waiting at
    // once, and sends turned away because the queue was full.
    uint16_t waited = 0, deepest = 0, dropped = 0;

    enum Verdict { SEND_NOW, WAITING, DROPPED };

    // A frame to send.  `out`: a send is in flight and has not finished.  It may
    // go straight out only when nothing is out AND nothing is waiting, so the
    // order is kept; otherwise it waits, or is dropped if there is no room.
    Verdict offer(const uint8_t *d, uint16_t n, bool out) {
        if (!out && count == 0) return SEND_NOW;
        if (count >= DEPTH || n > MAXLEN) {
            if (dropped < 0xFFFF) dropped++;
            return DROPPED;
        }
        unsigned tail = (head + count) % DEPTH;
        memcpy(data[tail], d, n);
        len[tail] = n;
        count++;
        if (waited < 0xFFFF) waited++;
        if (count > deepest) deepest = (uint16_t)count;
        return WAITING;
    }

    // The frame whose turn it is, once nothing is out.
    bool next(bool out, uint8_t *d, uint16_t *n) {
        if (out || count == 0) return false;
        memcpy(d, data[head], len[head]);
        *n = len[head];
        head = (head + 1) % DEPTH;
        count--;
        return true;
    }

    // The byte at `off` in the frame at the front (its command, for a report),
    // or 0 with nothing waiting.
    uint8_t front_byte(unsigned off) const {
        return (count && len[head] > off) ? data[head][off] : 0;
    }

    void take_stats(uint16_t *w, uint16_t *d, uint16_t *x) {
        *w = waited; *d = deepest; *x = dropped;
        waited = deepest = dropped = 0;
    }
};

// Has the radio stopped finishing sends?  Something is waiting, and for
// `limit` ms nothing has moved: no send has finished, and none has gone out.
// While the link is merely bad, callbacks keep coming — each a failure — so
// this stays false and the old detectors judge it.  Only a radio that has
// stopped answering for its own sends makes it true.
static inline bool tx_pacer_stuck(bool waiting, uint32_t since_callback_ms,
                                  uint32_t since_sent_ms, uint32_t limit_ms) {
    return waiting && since_callback_ms > limit_ms && since_sent_ms > limit_ms;
}
