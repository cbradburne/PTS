// link_run.h — a run of unanswered sends, from the first failure to the first
// send that gets through.  What it is for, and what every field of the report
// says, is at MOUNT_EVENT_LINK_RUN in protocol.h.
//
// Pure C++, no Arduino: the callers pass in the time and whatever they read off
// the radio, so tools/test_link_run.py compiles this on a laptop and plays runs
// through it.  The mount feeds it from five places, and holds a spinlock
// around every call, because two of them are on the WiFi task:
//   on_result()     the send-complete callback, either status    (WiFi task)
//   on_rx()         the receive callback                         (WiFi task)
//   on_refused()    esp_now_send() refusing a frame outright     (the sender)
//   on_action()     a recovery step, as it runs                  (loop)
//   start_sample()  what only loop() may read — channel, BLE, RAM — owed once
//   take()          just after a run starts, and again when it is reported
#pragma once
#include <stdint.h>
#include "protocol.h"

enum LinkAction : uint8_t {
    LINK_ACT_REFRESH = 0,   // the hub peer deleted and added again
    LINK_ACT_REINIT  = 1,   // ESP-NOW deinit and init
    LINK_ACT_WIFI    = 2,   // the WiFi driver torn down and rebuilt
    LINK_ACT_SCAN    = 3,   // a scan for a base started
};

// What loop() reads off the radio.  Counters are since boot; the run keeps
// the difference.
struct LinkSample {
    uint8_t  chan;           // WiFi primary channel
    uint8_t  ble;            // MOUNT_NOMEM_BLE_* bits
    uint32_t cam_notifies;   // camera notifications received
    uint32_t ble_drops;      // times the camera's BLE link dropped
    uint32_t iram_free;      // internal RAM free
    uint32_t in_flight;      // sends accepted and not yet called back
};

static inline uint16_t link_sat16(uint32_t v) { return v > 0xFFFF ? 0xFFFF : (uint16_t)v; }
static inline uint8_t  link_sat8(uint32_t v)  { return v > 0xFF ? 0xFF : (uint8_t)v; }
// A time in the run.  Capped at 0xFFFE, so a long one never reads as "never".
static inline uint16_t link_t16(uint32_t ms)  { return ms >= 0xFFFE ? 0xFFFE : (uint16_t)ms; }

struct LinkRunTracker {
    // Kept between runs: what "just before" means when one starts.  Zero until
    // the first of each this boot, so the gap reads as time since boot, which
    // is the truth.
    uint32_t last_ok_ms  = 0;    // last send that got through
    uint32_t last_rx_ms  = 0;    // last frame heard
    uint32_t last_res_ms = 0;    // last send result, either way
    int8_t   last_rssi   = 0;
    int8_t   last_noise  = 0;

    // The run in progress.
    bool         open       = false;
    uint32_t     start_ms   = 0;
    bool         need_start = false;   // loop() still owes the start sample
    bool         sampled    = false;   // ...and has paid it, for this run
    uint32_t     fails = 0, refused = 0, rx = 0, max_gap = 0;
    uint32_t     n_refresh = 0, n_reinit = 0;
    uint32_t     notifies0 = 0, drops0 = 0;
    MountLinkRun r = {};

    // A finished run, waiting for loop() to report it.
    bool         due = false;
    bool         done_sampled = false;
    uint32_t     done_notifies0 = 0, done_drops0 = 0;
    uint32_t     done_start_ms = 0;   // its first failure, for the radio tail
    MountLinkRun done = {};
    uint32_t     lost = 0;   // finished while another was still waiting

    void on_result(uint32_t now, bool ok) {
        if (open) {
            uint32_t gap = now - last_res_ms;
            if (gap > max_gap) max_gap = gap;
        }
        last_res_ms = now;
        if (!ok) {
            if (!open) begin(now);
            fails++;
            return;
        }
        last_ok_ms = now;
        if (!open) return;
        open = false;
        need_start = false;      // too late for "just after the first failure"
        if (fails < MOUNT_LINK_RUN_MIN_FAILS) return;    // routine
        if (due) { lost++; return; }                     // keep the one waiting
        r.dur_ms        = now - start_ms;
        r.fails         = link_sat16(fails);
        r.refused       = link_sat16(refused);
        r.rx_during     = link_sat16(rx);
        r.max_cb_gap_ms = link_sat16(max_gap);
        r.n_refresh     = link_sat8(n_refresh);
        r.n_reinit      = link_sat8(n_reinit);
        done           = r;
        done_sampled   = sampled;
        done_notifies0 = notifies0;
        done_drops0    = drops0;
        done_start_ms  = start_ms;
        due            = true;
    }

    void on_rx(uint32_t now, int8_t rssi, int8_t noise) {
        last_rx_ms = now;
        last_rssi  = rssi;
        last_noise = noise;
        if (!open) return;
        if (rx == 0) {
            r.t_first_rx  = link_t16(now - start_ms);
            r.rssi_first  = rssi;
            r.noise_first = noise;
        }
        rx++;
    }

    void on_refused(uint32_t) {
        if (open) refused++;
    }

    void on_action(uint32_t now, LinkAction a) {
        if (!open) return;
        uint16_t t = link_t16(now - start_ms);
        switch (a) {
        case LINK_ACT_REFRESH:
            if (r.t_refresh == MOUNT_LINK_T_NEVER) r.t_refresh = t;
            n_refresh++;
            break;
        case LINK_ACT_REINIT:
            if (r.t_reinit == MOUNT_LINK_T_NEVER) r.t_reinit = t;
            n_reinit++;
            break;
        case LINK_ACT_WIFI:
            if (r.t_wifi == MOUNT_LINK_T_NEVER) r.t_wifi = t;
            break;
        case LINK_ACT_SCAN:
            if (r.t_scan == MOUNT_LINK_T_NEVER) r.t_scan = t;
            break;
        }
    }

    // Just after a run starts: what only loop() can read.  A run that ended
    // before loop() came round keeps 0 for "not read".
    void start_sample(const LinkSample &s, uint8_t chan_hub) {
        if (!need_start) return;
        need_start = false;
        if (!open) return;
        sampled      = true;
        r.chan_start = s.chan;
        r.chan_hub   = chan_hub;
        r.ble_start  = s.ble;
        r.iram_free  = s.iram_free;
        r.in_flight  = link_sat16(s.in_flight);
        notifies0    = s.cam_notifies;
        drops0       = s.ble_drops;
    }

    // A finished run, completed with what the radio says now.  False if none.
    // `start_ms`, if given, gets the run's first failure in uptime ms.
    bool take(const LinkSample &s, uint8_t chan_hub, MountLinkRun *out,
              uint32_t *start_ms = nullptr) {
        if (!due) return false;
        due = false;
        if (start_ms) *start_ms = done_start_ms;
        done.chan_end = s.chan;
        done.ble_end  = s.ble;
        if (!done_sampled) done.chan_hub = chan_hub;
        else {
            done.cam_notifies = link_sat16(s.cam_notifies - done_notifies0);
            done.ble_drops    = link_sat8(s.ble_drops - done_drops0);
        }
        *out = done;
        return true;
    }

private:
    void begin(uint32_t now) {
        open       = true;
        start_ms   = now;
        need_start = true;
        sampled    = false;
        fails = refused = rx = max_gap = n_refresh = n_reinit = 0;
        r = MountLinkRun{};
        r.uptime_s     = now / 1000UL;
        r.since_ok_ms  = link_sat16(now - last_ok_ms);
        r.since_rx_ms  = link_sat16(now - last_rx_ms);
        r.rssi_before  = last_rssi;
        r.noise_before = last_noise;
        r.t_first_rx = r.t_refresh = r.t_reinit = r.t_wifi = r.t_scan = MOUNT_LINK_T_NEVER;
    }
};

// The radio's recent history, for the v2 tail of the run's report.  Kept all
// the time, not only while a run is open: a scan that causes a run starts
// before it.  Fed by the mount from three places, under the same lock:
//   scan_started()   each scan the mount's own code starts, and why  (loop)
//   channel_seen()   the radio's channel, every few tens of ms       (loop)
//   wifi_event()     each event the WiFi driver raises       (the event task)
// Times are uptime ms; 0 means "never", so a time of 0 is stored as 1.
struct RadioLog {
    uint32_t scan_ms = 0;          // the last scan the mount's code started
    uint8_t  scan_why = 0;         // MOUNT_SCAN_*
    int32_t  scan_silence = 0;     // the base silence its decision saw, signed
    uint32_t scans = 0;            // started by the mount's code, since boot
    uint32_t scans_done = 0;       // finished, as the driver reports them
    uint8_t  scan_aps = 0;         // networks the last finished one saw
    bool     off = false;          // the radio is off its base channel now
    uint32_t off_ms = 0;           // when it last left
    uint8_t  off_chan = 0;         // the channel it was seen on then
    uint32_t back_ms = 0;          // when it last came back (0: not since)
    uint32_t offs = 0;             // departures since boot
    uint8_t  n = 0, next = 0;      // the event ring: how many, where the next goes
    uint8_t  ev_code[MOUNT_LINK_WEV_SLOTS] = {};
    uint8_t  ev_raw[MOUNT_LINK_WEV_SLOTS]  = {};
    uint32_t ev_ms[MOUNT_LINK_WEV_SLOTS]   = {};

    static uint32_t stamp(uint32_t now) { return now ? now : 1; }

    void scan_started(uint32_t now, uint8_t why, int32_t silence) {
        scan_ms      = stamp(now);
        scan_why     = why;
        scan_silence = silence;
        scans++;
    }

    // `base` 0 means there is no base to be off (unpaired), and `chan` 0 a
    // reading that failed: neither says anything.
    void channel_seen(uint32_t now, uint8_t chan, uint8_t base) {
        if (!chan || !base) return;
        if (chan != base) {
            if (off) return;
            off      = true;
            off_ms   = stamp(now);
            off_chan = chan;
            back_ms  = 0;
            offs++;
        } else if (off) {
            off     = false;
            back_ms = stamp(now);
        }
    }

    // `aps` is the networks a finished scan saw, or below zero for any other
    // event.
    void wifi_event(uint32_t now, uint8_t code, uint8_t raw, int aps) {
        ev_code[next] = code;
        ev_raw[next]  = raw;
        ev_ms[next]   = stamp(now);
        next = (uint8_t)((next + 1) % MOUNT_LINK_WEV_SLOTS);
        if (n < MOUNT_LINK_WEV_SLOTS) n++;
        if (code == MOUNT_WEV_SCAN_DONE) {
            scans_done++;
            if (aps >= 0) scan_aps = link_sat8((uint32_t)aps);
        }
    }

    // The tail for a run whose first failure was at `first_fail_ms`.
    void fill(MountLinkRadio *t, uint32_t first_fail_ms) const {
        *t = MountLinkRadio{};
        t->first_fail_ms   = first_fail_ms;
        t->scan_ms         = scan_ms;
        t->scan_why        = scan_why;
        t->scan_silence_ms = scan_silence;
        t->off_ms          = off_ms;
        t->off_chan        = off_chan;
        t->back_ms         = back_ms;
        t->scans           = link_sat16(scans);
        t->scans_done      = link_sat16(scans_done);
        t->offs            = link_sat16(offs);
        t->scan_aps        = scan_aps;
        t->n_wev           = n;
        uint8_t first = (uint8_t)((next + MOUNT_LINK_WEV_SLOTS - n) % MOUNT_LINK_WEV_SLOTS);
        for (uint8_t i = 0; i < n; i++) {
            uint8_t k = (uint8_t)((first + i) % MOUNT_LINK_WEV_SLOTS);
            t->wev_code[i] = ev_code[k];
            t->wev_raw[i]  = ev_raw[k];
            t->wev_ms[i]   = ev_ms[k];
        }
    }
};
