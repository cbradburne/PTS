/*
 * espnow_bench — a rig for one fault, and nothing else.
 *
 * THE FAULT
 * ---------
 * A mount's ESP-NOW transmit path stops.  esp_now_send() begins returning
 * ESP_ERR_ESPNOW_NO_MEM ("the stack's TX queue was full"), the send-complete
 * callback stops firing, and nothing recovers it but a chip reboot.  On the rig
 * cam1 did it twice in 44 hours — once after 31.1 h up, once after 11.4 h.
 *
 * espressif/esp-idf#18682 describes the mechanism: esp_now_send() takes a buffer
 * from a small pool and the send-complete callback returns it.  A callback that
 * does not fire never returns its buffer.  Enough of those and the pool is
 * empty, and every send from then on is refused.
 *
 * So the question is not "why does the pool run out" — that is known.  It is
 * WHY DOES THE CALLBACK STOP.  That is what this bench exists to answer.
 *
 * WHY A BENCH AND NOT THE RIG
 * ---------------------------
 * On the rig the fault takes 11 to 31 hours, the mounts are in enclosures on a
 * truss with no reachable serial port, and every counter that would explain it
 * is reset by the restart that ends it.  Here: two boards on a desk, USB serial
 * on both, and every experiment is a typed line rather than a reflash.
 *
 * THE INSTRUMENT THAT MATTERS
 * ---------------------------
 *     in_flight = issued - (cb_ok + cb_fail)
 *
 * That is the leaked-buffer count, and it is the one number the rig has never
 * had.  A refusal count cannot warn — it only moves once the pool is ALREADY
 * empty, and by then the radio cannot report it.  in_flight starts climbing at
 * the FIRST lost buffer.  If it ratchets up and never comes back down, the leak
 * is happening and you are watching it happen.
 *
 * WHAT TO VARY  (all at runtime — no reflash between experiments)
 * --------------------------------------------------------------
 *   rate   how fast we send.  The rig's wedge is rate-dependent: cutting a
 *          satellite's traffic 88% took it from 36 restarts/17h to zero.
 *   poll   the RX sends COMMANDS back, which the TX must answer.  This is the
 *          one that makes the bench look like the rig — see below.
 *   cap    max sends in flight.  0 = fire and forget, which is what the mount
 *          does today.  1 = the textbook ESP-NOW discipline.  If a cap makes
 *          the leak stop, the fix is a cap.
 *   load   block the loop, imitating an LVGL flush.  The mount renders 37 KB
 *          draw buffers out of PSRAM on the same chip.  If starving the WiFi
 *          task leaks buffers, that is the cause, and cam1 wedging on the
 *          STRONGEST link (-33 dBm) while cam4 at -68 dBm never did points
 *          this way rather than at the radio.
 *   scan   periodic WiFi scan while sending.  A scan retunes the radio.
 *   phy    1 Mbps long preamble (what the rig uses, for range) vs default.
 *   size   payload bytes.
 *
 * WHY `poll` IS THE INTERESTING ONE
 * ---------------------------------
 * A metronome never has two sends outstanding.  Measured: 542,067 sends at
 * 50 Hz over three hours, and in_flight never once exceeded 1.  Frames go out
 * 20 ms apart and each clears in about a millisecond, so they never touch.
 *
 * The rig is not shaped like that.  The bridge runs periodic reports on timers
 * (STATUS 5 s, HEALTH 10 s, RF 10 s) AND answers every non-JOG command the
 * instant it arrives (esp_mount_amoled175.ino:1357).  That reply is issued on
 * ARRIVAL, uncorrelated with the timers by construction, so a command landing
 * mid-report puts a second buffer in the pool on top of the first.  The mount
 * does this all day; the bench, until now, never has.
 *
 * It also fits the one hard positive clue.  Overlap scales worse than linearly
 * with traffic, so an 88% cut removes far more than 88% of it — which is how
 * "rate matters" and "half a million evenly spaced sends did nothing" can both
 * be true.
 *
 * `poll 0` is exactly the old behaviour, so two runs differ in one variable.
 *
 * USING IT
 * --------
 *   Flash the same binary to two boards.  On one:  role rx
 *   It prints its MAC.  On the other:              role tx
 *                                                  peer AA:BB:CC:DD:EE:FF
 *                                                  rate 50
 *                                                  go
 *   Both settings persist in NVS, so a power cycle resumes the experiment.
 *   `help` lists everything.  One CSV line per second on the TX side.
 *
 * Deliberately no display, no LVGL, no Teensy link, no protocol.  Every one of
 * those is a variable, and the point of a bench is to have none you did not
 * choose.
 */

#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <Preferences.h>

// ---------------------------------------------------------------------------
// Settings — all live in NVS so an experiment survives a power cycle
// ---------------------------------------------------------------------------

static Preferences _prefs;

struct Cfg {
    uint8_t  role;          // 0 = idle, 1 = tx, 2 = rx
    uint8_t  peer[6];
    uint16_t rate_hz;       // sends per second
    uint16_t size;          // payload bytes
    uint16_t cap;           // max sends in flight; 0 = unbounded
    uint16_t load_ms;       // block the loop this long...
    uint16_t load_period_ms;// ...every this often; 0 = no synthetic load
    uint16_t scan_period_s; // periodic WiFi scan; 0 = none
    uint8_t  phy_lr;        // 1 = 1 Mbps long preamble (as the rig runs)
    uint8_t  channel;
    uint8_t  running;
    // Appended, so a blob saved by the previous build still loads: getBytes
    // fills what it has and anything past it keeps the initialiser below.
    uint16_t poll_hz;       // RX only: commands per second sent AT the TX
    uint8_t  ap_iface;      // 0 = station (the default), 1 = SoftAP, as the hub
};

static Cfg _cfg = { 0, {0,0,0,0,0,0}, 50, 32, 0, 0, 0, 0, 1, 1, 0, 0, 0 };

// Which 802.11 interface this board uses for everything: the peer entry, the
// MAC it reports, the protocol bits. One accessor, because getting it right in
// four places and wrong in the fifth is how a run measures the interface it was
// not testing.
static inline wifi_interface_t bench_if() {
    return _cfg.ap_iface ? WIFI_IF_AP : WIFI_IF_STA;
}
static inline const char *bench_if_name() {
    return _cfg.ap_iface ? "AP" : "STA";
}

// The bench's own access point, and deliberately NOT the rig's. It exists to
// put the radio in the mode the hub transmits from; it is not meant to be
// joined by anything that matters. The literal below is why: it must never be
// changed to the real one, which lives in an untracked header precisely so it
// is never committed.
#define BENCH_AP_SSID "PTS-Bench"
#define BENCH_AP_PASS "benchbench"

static void cfg_save() {
    _prefs.begin("bench", false);
    _prefs.putBytes("cfg", &_cfg, sizeof(_cfg));
    _prefs.end();
}

static void cfg_load() {
    _prefs.begin("bench", true);
    _prefs.getBytes("cfg", &_cfg, sizeof(_cfg));
    _prefs.end();
    if (_cfg.rate_hz == 0 || _cfg.rate_hz > 2000) _cfg.rate_hz = 50;
    if (_cfg.size < 8 || _cfg.size > 240)         _cfg.size    = 32;
    if (_cfg.channel < 1 || _cfg.channel > 13)    _cfg.channel = 1;
    if (_cfg.role > 2)                            _cfg.role    = 0;
    if (_cfg.poll_hz > 2000)                      _cfg.poll_hz = 0;
    if (_cfg.ap_iface > 1)                        _cfg.ap_iface = 0;
}

// ---------------------------------------------------------------------------
// Counters
// ---------------------------------------------------------------------------
// issued/cb_ok/cb_fail are the whole experiment.  Everything else is context.

static volatile uint32_t _issued   = 0;   // esp_now_send() returned ESP_OK
static volatile uint32_t _cb_ok    = 0;   // send callback, SUCCESS
static volatile uint32_t _cb_fail  = 0;   // send callback, FAIL
static volatile uint32_t _refused  = 0;   // esp_now_send() returned an error
static volatile uint32_t _nomem    = 0;   // ...and that error was NO_MEM
static volatile uint32_t _last_err = 0;
static volatile uint32_t _rx_count = 0;
static volatile uint32_t _rx_bytes = 0;

// Command/reply traffic. Two counters with ONE writer each rather than a single
// count incremented from both sides: the receive callback runs in the WiFi task
// and the reply goes out from loop(), on the other core, so a read-modify-write
// shared between them would quietly lose counts.
static volatile uint32_t _cmd_rx    = 0;   // command frames in   (callback only)
static uint32_t          _cmd_acked = 0;   // replies attempted   (loop only)

static uint32_t _max_in_flight  = 0;
static uint32_t _overlaps       = 0;   // times in_flight went above 1
static uint32_t _t_start_ms     = 0;
static uint32_t _t_first_ref_ms = 0;   // when the FIRST refusal happened
static uint32_t _t_wedge_ms     = 0;   // when refusals became continuous
static uint32_t _consec_refused = 0;

// ── Did the frame actually go out? ───────────────────────────────────────────
//
// The whole point of this pair of counters. When a callback never arrives, two
// completely different things could have happened, and nothing measured so far
// tells them apart:
//
//   the frame WAS transmitted and only the completion notification was lost
//       -> the fault is in the driver's callback bookkeeping
//   the frame was never transmitted at all, despite esp_now_send() saying OK
//       -> the fault is in the TX path itself
//
// They want opposite investigations. TX already stamps its _issued counter into
// payload bytes 0-3; the RX side threw it away and only counted frames. Now it
// tracks the sequence and reports GAPS.
//
// The combination is what decides it:
//
//   callback FAIL + a gap          ordinary loss on air. Accounted for, boring.
//   callback FAIL + NO gap         the frame arrived; only its ACK was lost.
//   no callback   + a gap          the frame never went out.
//   no callback   + NO gap         it went out; the callback was lost.
//
// The second row was added after measuring it, and it replaces the premise this
// probe was written on. That premise was "cb_fail has been 0 over 53 h, so the
// link loses nothing on air" — which made a FAIL mean a lost frame. On
// 2026-09-11 at t=18906s cb_fail went 0 -> 18 over three and a half minutes and
// RX gaps stayed at 0: eighteen sends reported failure and all eighteen
// sequence numbers arrived. A FAIL here means the MAC-layer acknowledgement was
// lost, NOT the frame.
//
// So do not write a gap off as ordinary air loss just because a FAIL sits next
// to it. The gap counter below is independent of cb_fail and is the ground
// truth; cb_fail is not.
static uint32_t _rx_seq_next  = 0;      // sequence expected next
static bool     _rx_seq_armed = false;  // seen a first frame to sync from
static uint32_t _rx_gaps      = 0;      // sequences that never arrived
#define RX_GAP_LOG 32
static uint32_t _rx_gap_seq[RX_GAP_LOG] = {};
static uint8_t  _rx_gap_n = 0;
// Set only by the ceiling probe: lifts the cap for one deliberate burst so the
// driver's real limit can be found. bench_send() stays the only send site.
static bool _probe_uncapped = false;

// A leak is in_flight that never comes back down.  Tracked as the floor it has
// not returned below, because the instantaneous value bounces with every send.
static uint32_t _in_flight_floor = 0;

static inline uint32_t in_flight() {
    uint32_t i = _issued, o = _cb_ok, f = _cb_fail;
    return (i >= o + f) ? (i - o - f) : 0;
}

// ---------------------------------------------------------------------------
// ESP-NOW
// ---------------------------------------------------------------------------

// Byte 4 of every payload says what it is. Bytes 0-3 are the sequence, and the
// minimum `size` is 8, so byte 4 is always there to read.
#define FRAME_DATA  0   // TX -> RX, the periodic stream, `size` bytes
#define FRAME_CMD   1   // RX -> TX, a command, which the TX must answer
#define FRAME_ACK   2   // TX -> RX, the answer
#define CTRL_SIZE  16   // command and ack frames — the rig's ACK is 11 bytes

static void on_sent(const wifi_tx_info_t *, esp_now_send_status_t s) {
    if (s == ESP_NOW_SEND_SUCCESS) _cb_ok  = _cb_ok  + 1;
    else                           _cb_fail = _cb_fail + 1;
}

// The RX side registers no peer, and does not need to.
//
// ESP-NOW requires a peer only to SEND. An unencrypted frame reaches the
// receive callback whatever the sender is, and the 802.11 ACK that decides the
// sender's callback status is generated by the WiFi hardware for any frame
// addressed to this MAC — before ESP-NOW is involved at all. So the receiver is
// entirely passive. What it does have to match is the CHANNEL.
//
// Which means a silent receiver has exactly three causes, and the first frame
// tells you which: nothing arriving at all (wrong channel, or the TX has the
// wrong peer MAC), or frames arriving from a MAC you did not expect.
static volatile uint32_t _rx_first_ms = 0;
static uint8_t _rx_peer[6] = {};
static bool    _rx_peer_ready = false;   // ...and registered, so `poll` can send

static void on_recv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
    if (!_rx_first_ms && info) {
        // MAC first, THEN the flag that says it is there. The loop reads this
        // address as soon as it sees the flag — to register the sender as a
        // peer for `poll` — so publishing the flag first is a window where it
        // can read six bytes of zeros.
        memcpy(_rx_peer, info->src_addr, 6);
        _rx_first_ms = millis() ? millis() : 1;
    }
    // Counted here, answered from loop(). The bridge does exactly this — the
    // receive callback queues and returns (esp_mount_amoled175.ino:1143) and
    // the ACK goes out from the main loop. Sending from the WiFi task would be
    // testing a bug the rig does not have.
    if (len > 4 && data && data[4] == FRAME_CMD) _cmd_rx = _cmd_rx + 1;
    // Sequence from bytes 0-3, big-endian, as the TX side stamps it. Only
    // DATA frames carry a meaningful one — ACK and CMD are the other
    // direction's traffic and would corrupt the sequence if counted.
    if (len > 4 && data && data[4] == FRAME_DATA) {
        uint32_t seq = ((uint32_t)data[0] << 24) | ((uint32_t)data[1] << 16) |
                       ((uint32_t)data[2] <<  8) |  (uint32_t)data[3];
        if (!_rx_seq_armed || seq < _rx_seq_next) {
            _rx_seq_armed = true;        // first frame, or TX counters were reset
        } else if (seq > _rx_seq_next) {
            uint32_t missing = seq - _rx_seq_next;
            _rx_gaps += missing;
            // Keep the first few so they can be matched against the TX log by
            // number rather than by eye.
            for (uint32_t m = _rx_seq_next; m < seq && _rx_gap_n < RX_GAP_LOG; m++)
                _rx_gap_seq[_rx_gap_n++] = m;
        }
        _rx_seq_next = seq + 1;
    }
    _rx_count = _rx_count + 1;
    _rx_bytes = _rx_bytes + (uint32_t)len;
}

// esp_now.h numbers these from ESP_ERR_ESPNOW_BASE (0x3064). Printing the hex
// makes the operator go and look it up, which is the opposite of what a bench
// is for — and the two that matter here read completely differently:
// NOT_FOUND is a setup mistake, NO_MEM is the fault under study.
static const char *espnow_err_name(esp_err_t e) {
    switch (e) {
        case ESP_OK:                    return "OK";
        case ESP_ERR_ESPNOW_NOT_INIT:   return "NOT_INIT (esp_now_init not done)";
        case ESP_ERR_ESPNOW_ARG:        return "ARG (bad argument)";
        case ESP_ERR_ESPNOW_NO_MEM:     return "NO_MEM (the buffer pool is empty — THE WEDGE)";
        case ESP_ERR_ESPNOW_FULL:       return "FULL (peer list full)";
        case ESP_ERR_ESPNOW_NOT_FOUND:  return "NOT_FOUND (peer not registered — check 'peer')";
        case ESP_ERR_ESPNOW_INTERNAL:   return "INTERNAL";
        case ESP_ERR_ESPNOW_EXIST:      return "EXIST (peer already added)";
        case ESP_ERR_ESPNOW_IF:         return "IF (wrong interface)";
        default:                        return "unknown";
    }
}

// Registering the peer is what `peer` and `go` both have to do, so it lives in
// one place. Remove first: add on an existing peer returns EXIST and changes
// nothing, which is how a corrected MAC silently kept the old one.
static bool peer_add(const uint8_t *mac) {
    esp_now_del_peer(mac);
    esp_now_peer_info_t p = {};
    memcpy(p.peer_addr, mac, 6);
    p.channel = _cfg.channel;
    p.ifidx   = bench_if();
    p.encrypt = false;
    esp_err_t e = esp_now_add_peer(&p);
    if (e != ESP_OK) {
        Serial.printf("[bench] add_peer failed: %s\n", espnow_err_name(e));
        return false;
    }
    return true;
}

static bool peer_register() {
    if (_cfg.role != 1) return false;
    return peer_add(_cfg.peer);
}

static void peer_rate_long(const uint8_t *mac) {
    esp_now_rate_config_t r = {};
    r.phymode = WIFI_PHY_MODE_11B;
    r.rate    = WIFI_PHY_RATE_1M_L;     // 1 Mbps, long preamble — as the rig runs
    r.ersu    = false;
    r.dcm     = false;
    esp_now_set_peer_rate_config(mac, &r);
}

// The hub does not transmit the way this bench has been measuring.
//
// Every result so far — 53 h clean at cap 2, the ceiling of 32, the peer-drop
// null — was taken with this board in station mode, peers on WIFI_IF_STA, no
// access point and nothing associated. The hub runs WiFi.softAP() and registers
// its mounts on WIFI_IF_AP. That is a different transmit path in the driver: an
// AP beacons on a fixed interval whatever else it is doing, and it buffers
// frames for any associated station that goes to sleep, out of the same pool
// the sends come from.
//
// That difference is worth a switch rather than an argument, because it would
// produce the hub's exact signature and this bench cannot currently make it:
// frames that SIT rather than FAIL leave txfail flat while callbacks stop, and
// txfail flat at 83 through a fourteen-hour outage is what the hub logged.
//
// `iface sta` is the default and is byte-for-byte what every run so far used,
// so the baselines stay comparable.
static void radio_start() {
    if (_cfg.ap_iface) {
        WiFi.mode(WIFI_AP);
        // Channel here as well as below: softAP() sets its own, and a peer
        // registered on one channel while the radio sits on another is a
        // silent link that looks like a wiring fault.
        WiFi.softAP(BENCH_AP_SSID, BENCH_AP_PASS, _cfg.channel);
    } else {
        WiFi.mode(WIFI_STA);
        WiFi.disconnect();
    }
    esp_wifi_set_ps(WIFI_PS_NONE);
    esp_wifi_set_max_tx_power(84);
    esp_wifi_set_protocol(bench_if(),
        WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N);
    esp_wifi_set_channel(_cfg.channel, WIFI_SECOND_CHAN_NONE);

    if (esp_now_init() != ESP_OK) {
        Serial.println("[bench] esp_now_init FAILED");
        return;
    }
    esp_now_register_send_cb(on_sent);
    esp_now_register_recv_cb(on_recv);

    if (_cfg.role == 1 && peer_register() && _cfg.phy_lr)
        peer_rate_long(_cfg.peer);
}

// ---------------------------------------------------------------------------
// One send path
// ---------------------------------------------------------------------------
// The periodic stream and the reactive replies take buffers from the SAME pool,
// so they have to be counted by the same code. Two send sites with their own
// accounting is how you end up measuring one stream and leaking through the
// other.
//
// Returns false if the cap held it back or the stack refused it. No caller
// retries: the rig does not either — a refused ACK on the mount is simply lost
// (mount_manager.py: nothing to await and nothing to retry against), and a
// bench that retries where the rig does not is measuring its own behaviour.
static bool bench_send(const uint8_t *peer, uint8_t type, uint16_t len,
                       uint32_t now) {
    // The ceiling probe is the one caller allowed past the cap, and it says so
    // by name. The cap clause below is otherwise untouched: it must apply to
    // every stream, or `cap 1` bounds one and not the others.
    if (!_probe_uncapped)
        if (_cfg.cap && in_flight() >= _cfg.cap) return false;

    static uint8_t pay[240];
    pay[0] = (uint8_t)(_issued >> 24); pay[1] = (uint8_t)(_issued >> 16);
    pay[2] = (uint8_t)(_issued >> 8);  pay[3] = (uint8_t)_issued;
    pay[4] = type;

    esp_err_t e = esp_now_send(peer, pay, len);
    if (e == ESP_OK) {
        _issued = _issued + 1;
        _consec_refused = 0;
        return true;
    }

    _refused  = _refused + 1;
    _last_err = (uint32_t)e;
    if (e == ESP_ERR_ESPNOW_NO_MEM) _nomem = _nomem + 1;
    if (!_t_first_ref_ms) {
        _t_first_ref_ms = now;
        Serial.printf("[bench] FIRST REFUSAL at t=%lus err=%s in_flight=%lu\n",
                      (unsigned long)((now - _t_start_ms) / 1000UL),
                      espnow_err_name(e), (unsigned long)in_flight());
    }
    _consec_refused = _consec_refused + 1;

    // ONLY NO_MEM is the wedge.
    //
    // This used to call 50 refusals of any kind a wedge, and an unregistered
    // peer duly produced "*** WEDGED at t=0s" with zero sends ever made. A
    // bench that cries wolf on a setup mistake is worse than one that says
    // nothing: the run looks like a reproduction and the data is worthless.
    if (e == ESP_ERR_ESPNOW_NO_MEM) {
        if (_consec_refused >= 50 && !_t_wedge_ms) {
            _t_wedge_ms = now;
            Serial.printf("[bench] *** WEDGED at t=%lus — 50 NO_MEM in a row, "
                          "in_flight=%lu, cb since start ok=%lu fail=%lu\n",
                          (unsigned long)((now - _t_start_ms) / 1000UL),
                          (unsigned long)in_flight(),
                          (unsigned long)_cb_ok, (unsigned long)_cb_fail);
        }
    } else if (_consec_refused == 50) {
        // Anything else repeating is the bench being held wrong, and it should
        // say so instead of banking a result.
        Serial.printf("[bench] NOT THE FAULT — 50 refusals of %s in a row.\n"
                      "        Nothing has been sent. This is a setup problem, "
                      "not the wedge.\n", espnow_err_name(e));
    }
    return false;
}

// ── ceiling: how many sends can be outstanding at once ───────────────────────
//
// The hub's leak floor stepped to exactly 32 at a stall, twice. Two candidates
// for a round repeatable number — a buffer pool, or something structural — and
// the pool was ruled out from sdkconfig (STATIC_TX_BUFFER_NUM is 8, not 32).
// This measures the real ceiling instead of inferring it.
//
// Fires back to back with NO cap and NO pacing until esp_now_send() refuses,
// which is the only way to find where the driver actually stops. Then it waits
// and reports how many callbacks come back, because that is the question the
// step never answered: a ceiling that drains is a queue, and a ceiling that
// does not is a leak.
//
// Bounded at 512 so a board that never refuses cannot hang the console, and it
// leaves the run stopped so the numbers are not immediately overwritten.
//
// The fill and the drain are shared with the peer-drop probe below. They stay
// out of the cap's way by the same flag and by no other means, so there is
// still exactly one place a frame can leave this board.
static uint32_t probe_fill(uint32_t limit, uint32_t *peak_out) {
    uint32_t accepted = 0, peak = 0, now = millis();
    _probe_uncapped = true;
    for (uint32_t i = 0; i < limit; i++) {
        if (!bench_send(_cfg.peer, FRAME_DATA, _cfg.size, now)) break;
        accepted++;
        uint32_t f = in_flight();
        if (f > peak) peak = f;
    }
    _probe_uncapped = false;
    if (peak_out) *peak_out = peak;
    return accepted;
}

static uint32_t probe_drain(uint32_t ms) {
    uint32_t waited = 0;
    while (in_flight() > 0 && waited < ms) { delay(10); waited += 10; }
    return waited;
}

static void cmd_ceiling() {
    if (_cfg.role != 1) {            // 1 = tx, see Cfg.role
        Serial.println("[ceiling] this is the TX side's probe — run it there");
        return;
    }
    if (!esp_now_is_peer_exist(_cfg.peer)) {
        Serial.println("[ceiling] no peer — set one with 'peer <mac>' first");
        return;
    }
    _cfg.running = 0;                      // no background traffic in the way

    uint32_t before_issued = _issued, before_cb = _cb_ok + _cb_fail;
    uint32_t peak = 0;

    Serial.println("[ceiling] firing with no cap until the driver refuses...");
    uint32_t accepted = probe_fill(512, &peak);
    Serial.printf("[ceiling] accepted %lu before %s, peak in_flight %lu\n",
                  (unsigned long)accepted, espnow_err_name((esp_err_t)_last_err),
                  (unsigned long)peak);

    // Now the part that matters: do they come back?
    uint32_t waited = probe_drain(3000);
    uint32_t returned = (_cb_ok + _cb_fail) - before_cb;
    Serial.printf("[ceiling] after %lu ms: %lu of %lu callbacks returned, "
                  "in_flight %lu\n",
                  (unsigned long)waited, (unsigned long)returned,
                  (unsigned long)(_issued - before_issued),
                  (unsigned long)in_flight());
    if (in_flight() == 0)
        Serial.println("[ceiling] VERDICT: every buffer came back — that ceiling "
                       "is a queue depth, not a leak.");
    else
        Serial.printf("[ceiling] VERDICT: %lu never came back — those are LEAKED.\n",
                      (unsigned long)in_flight());
    Serial.println("[ceiling] run stopped. 'reset' then 'go' to resume.");
}

// ── peerdrop: does the hub's own recovery eat the queue? ─────────────────────
//
// The hub deletes and re-adds a peer after ESPNOW_MAX_CONSEC_FAILS consecutive
// send failures, and on a client connecting it refreshes every bound mount
// unconditionally before querying them. Both were written when a failure was
// believed to mean a lost frame.
//
// It does not. The gap counter above measured eighteen failures against zero
// missing sequence numbers — the frames arrived and the acknowledgements were
// what went missing. The refreshes therefore fire on links that are working,
// and they fire during exactly the episodes when the queue is deepest: in that
// same three minutes the depth went from 2 to 24 of the 32 the driver allows,
// because a frame awaiting retries holds its descriptor longer.
//
// So the question is what a delete does to the frames already outstanding for
// that peer. Two things could be lost and they are not the same:
//
//   the callback        in_flight never comes back down and the board reports
//                       a leak floor it does not have
//   the descriptor      the queue is permanently shallower, and enough of them
//                       leaves the send path with nothing to allocate
//
// The first makes the instrument lie. The second is a fault that ends in a
// wedge. This measures the queue depth before and after, which is the only way
// to tell them apart: a returned buffer refills the queue whether or not
// anyone was told about it.
static void cmd_peerdrop(uint32_t n) {
    if (_cfg.role != 1) {
        Serial.println("[peerdrop] this is the TX side's probe — run it there");
        return;
    }
    if (!esp_now_is_peer_exist(_cfg.peer)) {
        Serial.println("[peerdrop] no peer — set one with 'peer <mac>' first");
        return;
    }
    _cfg.running = 0;

    probe_drain(3000);
    if (in_flight() > 0) {
        Serial.printf("[peerdrop] %lu already outstanding before we start — this "
                      "board has leaked, and every number below would be measured "
                      "from it. Reboot both boards and run this first.\n",
                      (unsigned long)in_flight());
        return;
    }

    // 1. the queue as it stands, and proof it is clean
    uint32_t before = probe_fill(512, nullptr);
    uint32_t waited = probe_drain(3000);
    if (in_flight() > 0) {
        Serial.printf("[peerdrop] baseline is dirty: %lu of %lu never came back "
                      "with the peer untouched. Nothing below would mean "
                      "anything.\n",
                      (unsigned long)in_flight(), (unsigned long)before);
        return;
    }
    Serial.printf("[peerdrop] queue before: %lu accepted, all back in %lu ms\n",
                  (unsigned long)before, (unsigned long)waited);

    // 2. delete and re-add with sends still outstanding — the hub's own action
    if (n < 1) n = 1;
    if (n > before) n = before;
    uint32_t cb_before = _cb_ok + _cb_fail;
    uint32_t fired = probe_fill(n, nullptr);
    uint32_t at_drop = in_flight();
    peer_add(_cfg.peer);                 // del_peer + add_peer, as the hub does
    if (_cfg.phy_lr) peer_rate_long(_cfg.peer);
    waited = probe_drain(3000);
    uint32_t returned = (_cb_ok + _cb_fail) - cb_before;
    uint32_t orphaned = (fired > returned) ? fired - returned : 0;
    Serial.printf("[peerdrop] dropped the peer with %lu in flight: %lu of %lu "
                  "callbacks back in %lu ms, %lu orphaned\n",
                  (unsigned long)at_drop, (unsigned long)returned,
                  (unsigned long)fired, (unsigned long)waited,
                  (unsigned long)orphaned);

    // 3. the queue afterwards. This is the measurement.
    uint32_t after = probe_fill(512, nullptr);
    probe_drain(3000);
    Serial.printf("[peerdrop] queue after:  %lu accepted (was %lu)\n",
                  (unsigned long)after, (unsigned long)before);

    if (orphaned == 0 && after >= before) {
        Serial.println("[peerdrop] VERDICT: harmless. Every callback came back "
                       "and the queue is as deep as it was. The hub's peer "
                       "refresh is not what makes its leak floor.");
    } else if (orphaned > 0 && after + orphaned <= before) {
        Serial.printf("[peerdrop] VERDICT: the refresh CONSUMED %lu descriptor(s) "
                      "— no callback, and the queue came back %lu shallower. "
                      "Enough of these and the send path has nothing left to "
                      "allocate, which is a wedge. This is the fault.\n",
                      (unsigned long)orphaned,
                      (unsigned long)(before - after));
    } else if (orphaned > 0) {
        Serial.printf("[peerdrop] VERDICT: %lu callback(s) never arrived, but the "
                      "queue is still %lu deep — the buffers returned and only "
                      "the notification was lost. The hub's leak floor is an "
                      "artefact of its own recovery, not a leak.\n",
                      (unsigned long)orphaned, (unsigned long)after);
    } else {
        Serial.printf("[peerdrop] VERDICT: every callback returned but the queue "
                      "went %lu -> %lu. Unexpected — run it again before "
                      "believing it.\n",
                      (unsigned long)before, (unsigned long)after);
    }
    Serial.println("[peerdrop] run stopped. Reboot both boards before any timed "
                   "run — the counters carry this probe's damage.");
}

// ---------------------------------------------------------------------------
// Reporting — one CSV line a second, so a run can be logged and plotted
// ---------------------------------------------------------------------------

static void report(uint32_t now) {
    uint32_t inf = in_flight();   // max/overlap are tracked in loop(), at loop rate
    Serial.printf(
        // sta: associated stations, and 0 in station mode. An AP holds frames
        // for a client that goes to sleep, out of the same pool these sends
        // come from — so when a run in AP mode behaves differently, the first
        // question is whether anything was attached, and a column is the only
        // way to answer it hours later.
        "t=%lus issued=%lu cb_ok=%lu cb_fail=%lu refused=%lu nomem=%lu "
        "in_flight=%lu floor=%lu max=%lu rx=%lu heap=%lu err=0x%lX "
        "cmd=%lu ack=%lu ovl=%lu gaps=%lu sta=%lu\n",
        (unsigned long)((now - _t_start_ms) / 1000UL),
        (unsigned long)_issued, (unsigned long)_cb_ok, (unsigned long)_cb_fail,
        (unsigned long)_refused, (unsigned long)_nomem,
        (unsigned long)inf, (unsigned long)_in_flight_floor,
        (unsigned long)_max_in_flight, (unsigned long)_rx_count,
        (unsigned long)ESP.getFreeHeap(), (unsigned long)_last_err,
        (unsigned long)_cmd_rx, (unsigned long)_cmd_acked,
        (unsigned long)_overlaps, (unsigned long)_rx_gaps,
        (unsigned long)(_cfg.ap_iface ? WiFi.softAPgetStationNum() : 0));
}

static void counters_reset() {
    // Let the sends in flight land before zeroing, or the counters start the
    // run already wrong — and wrong in the direction that hides the fault.
    //
    // `_issued` and `_cb_ok` were zeroed together while sends were still
    // outstanding. Their callbacks arrived a moment later and incremented
    // _cb_ok with no matching _issued, leaving cb_ok PERMANENTLY ahead:
    //
    //     t=0   issued=132   cb_ok=134
    //     t=24  issued=3492  cb_ok=3494
    //
    // in_flight() clamps negatives to zero, so it then reads 0 for the whole
    // run whatever happens, and the first two leaked buffers are invisible.
    // The run that found this was a cap test against a leak of three buffers
    // in twenty hours — it could have come back clean while leaking.
    //
    // Sending is off here (go sets running AFTER this), so the stack drains on
    // its own; we only have to wait. The callbacks come from the WiFi task, not
    // from loop(), so a plain delay is enough.
    for (int i = 0; i < 40 && in_flight(); i++) delay(5);
    if (in_flight())
        Serial.printf("[bench] %lu send(s) still unacknowledged after 200ms — "
                      "counters may start\n        skewed. `stop`, wait, then "
                      "`go` again if the first line shows cb_ok\n        ahead "
                      "of issued.\n", (unsigned long)in_flight());

    // A leak survives `go`, and nothing but a reboot clears it.
    //
    // `go` zeroes these counters; it does not hand the stack back the buffers
    // it lost. Start a second leak run on the same power cycle and it begins
    // however many buffers down the last one ended, with `floor` measuring from
    // a false zero — the run after the first reproduction did exactly that and
    // sat at in_flight=3 from its first sample to its last, which was the
    // PREVIOUS run's damage showing through.
    //
    // "Only a full esp_restart() clears it" has been the one fixed point of
    // this fault since August. So say it here, where the mistake is made.
    if (_in_flight_floor) {
        Serial.printf("\n[bench] *** THIS BOARD HAS ALREADY LEAKED %lu BUFFER(S).\n"
                      "        `go` zeroes the counters, not the radio — they are "
                      "still gone, and\n        floor will start from a false "
                      "zero. REBOOT BOTH BOARDS (power cycle,\n        or `role "
                      "tx` / `role rx`) before a run meant to measure a leak.\n\n",
                      (unsigned long)_in_flight_floor);
    }

    // Assigned one at a time: chaining through a volatile reads back what was
    // just written, which C++20 deprecates and which is meaningless here anyway.
    _issued = 0; _cb_ok = 0; _cb_fail = 0;
    _refused = 0; _nomem = 0; _last_err = 0;
    _rx_count = 0; _rx_bytes = 0;
    // Together, and only from here: the reply loop runs while (_cmd_acked !=
    // _cmd_rx), so zeroing one without the other spins out a burst of ACKs for
    // commands that arrived before the run started.
    _cmd_rx = 0; _cmd_acked = 0;
    _max_in_flight = _in_flight_floor = _consec_refused = _overlaps = 0;
    _rx_gaps = _rx_gap_n = 0;
    _rx_seq_armed = false;   // TX sequence restarts too — resync rather than
                             // counting the whole old stream as one huge gap
    _t_start_ms = millis();
    _t_first_ref_ms = _t_wedge_ms = 0;
}

// ---------------------------------------------------------------------------
// Serial control
// ---------------------------------------------------------------------------

static void mac_str(const uint8_t *m, char *out) {
    sprintf(out, "%02X:%02X:%02X:%02X:%02X:%02X", m[0], m[1], m[2], m[3], m[4], m[5]);
}

static void print_help() {
    Serial.println(
      "\n espnow_bench — one fault, nothing else\n"
      "   role tx|rx|idle      what this board is (persists)\n"
      "   peer AA:BB:...       the RECEIVER's MAC — who this board sends TO.\n"
      "                        Read it off the RX board's boot line. (persists)\n"
      "   chan <1-13>          WiFi channel\n"
      "   rate <Hz>            sends per second (TX)\n"
      "   poll <Hz>            RX ONLY: send commands at the TX, which must\n"
      "                        answer each one. That reply lands on top of the\n"
      "                        TX's own stream — the overlap the rig has and a\n"
      "                        metronome does not. 0 = passive, as before.\n"
      "   size <8-240>         payload bytes\n"
      "   cap <n>              max sends IN FLIGHT; 0 = fire and forget (the rig)\n"
      "   load <ms> <period>   block the loop <ms> every <period> ms (fake LVGL)\n"
      "   scan <s>             WiFi scan every <s> seconds; 0 = never\n"
      "   phy lr|def           1 Mbps long preamble (the rig) or default\n"
      "   iface sta|ap         which interface to send from. sta is the default\n"
      "                        and every run so far; ap runs a SoftAP and puts\n"
      "                        peers on it, as the HUB does. Changes this\n"
      "                        board's MAC — reboots, comes back stopped.\n"
      "   go | stop            start / stop sending\n"
      "   reset                zero the counters\n"
      "   stats                print one line now\n"
      "   mac                  this board's MAC\n"
      "   gaps                 RX: which sequences never arrived\n"
      "   ceiling              fire uncapped until refused; how many, and do\n"
      "                        they come back? (TX side, stops the run)\n"
      "   peerdrop [n]         measure the queue, delete and re-add the peer\n"
      "                        with n sends outstanding (default 16), then\n"
      "                        measure it again. Does the hub's own recovery\n"
      "                        cost buffers? (TX side, stops the run)\n"
      "\n in_flight = issued - callbacks. It is the leaked-buffer count.\n"
      " If it ratchets up and never returns, you are watching the leak.\n");
}

static bool parse_mac(const char *s, uint8_t *out) {
    int v[6];
    if (sscanf(s, "%x:%x:%x:%x:%x:%x", &v[0],&v[1],&v[2],&v[3],&v[4],&v[5]) != 6)
        return false;
    for (int i = 0; i < 6; i++) out[i] = (uint8_t)v[i];
    return true;
}

static void handle_line(char *line) {
    char *cmd = strtok(line, " \t");
    if (!cmd) return;
    char *a1 = strtok(nullptr, " \t");
    char *a2 = strtok(nullptr, " \t");
    char buf[20];

    if (!strcmp(cmd, "help") || !strcmp(cmd, "?")) { print_help(); return; }
    if (!strcmp(cmd, "ceiling")) { cmd_ceiling(); return; }
    if (!strcmp(cmd, "peerdrop")) {
        int want = a1 ? atoi(a1) : 16;
        cmd_peerdrop(want > 0 ? (uint32_t)want : 16);
        return;
    }
    if (!strcmp(cmd, "gaps")) {
        Serial.printf("[gaps] %lu sequence(s) never arrived; next expected %lu\n",
                      (unsigned long)_rx_gaps, (unsigned long)_rx_seq_next);
        if (!_rx_gap_n) Serial.println("[gaps] none recorded — the stream is complete");
        for (uint8_t i = 0; i < _rx_gap_n; i++)
            Serial.printf("[gaps]   missing seq %lu\n", (unsigned long)_rx_gap_seq[i]);
        if (_rx_gap_n >= RX_GAP_LOG)
            Serial.println("[gaps] (list full — count above is still exact)");
        return;
    }
    if (!strcmp(cmd, "mac")) {
        uint8_t m[6]; esp_wifi_get_mac(bench_if(), m); mac_str(m, buf);
        Serial.printf("[bench] my MAC %s  channel %u  iface %s\n",
                      buf, _cfg.channel, bench_if_name());
        return;
    }
    if (!strcmp(cmd, "iface") && a1) {
        uint8_t want = !strcmp(a1, "ap") ? 1 : !strcmp(a1, "sta") ? 0 : 255;
        if (want == 255) { Serial.println("[bench] iface ap|sta"); return; }
        _cfg.ap_iface = want;
        _cfg.running  = 0;
        cfg_save();
        // Same shape as `role`: reboot to apply, and come back stopped. The
        // extra warning is the MAC — this board's address changes with the
        // interface, so the OTHER board's peer is now wrong and every send
        // will go nowhere in the way that looks exactly like a dead radio.
        Serial.printf("[bench] iface %s — rebooting to apply cleanly. It comes "
                      "back STOPPED.\n        MY MAC CHANGES WITH THE "
                      "INTERFACE: read the new one off the boot line and set it "
                      "as\n        the peer on the other board before `go`.\n",
                      a1);
        delay(100);
        ESP.restart();
    }
    if (!strcmp(cmd, "role") && a1) {
        _cfg.role = !strcmp(a1, "tx") ? 1 : !strcmp(a1, "rx") ? 2 : 0;
        _cfg.running = 0;
        cfg_save();
        // running=0 above is deliberate — a board whose role just changed must
        // not come back transmitting. But it is the step that gets forgotten:
        // the reboot looks like the whole job, the board comes up configured
        // and linked and silent, and a run sits at issued=0 until someone reads
        // the counters. So say both halves.
        Serial.printf("[bench] role %s — rebooting to apply cleanly. It comes "
                      "back STOPPED:\n        `go` on this board when you are "
                      "ready.\n", a1);
        delay(100);
        ESP.restart();
    }
    if (!strcmp(cmd, "peer") && a1) {
        uint8_t want[6];
        if (!parse_mac(a1, want)) { Serial.println("[bench] bad MAC"); return; }
        // Your own MAC is the one mistake worth catching, because it is the
        // easy one to make: `peer` is who this board SENDS TO, so on the TX it
        // is the RECEIVER's MAC. Point it at yourself and every send goes
        // nowhere, in a way that looks exactly like a dead radio.
        uint8_t mine[6];
        esp_wifi_get_mac(bench_if(), mine);
        if (!memcmp(want, mine, 6)) {
            Serial.println("[bench] that is MY OWN MAC. 'peer' is who this board "
                           "sends TO —\n        on the TX board that is the RX "
                           "board's MAC, the one it\n        prints on boot. Not "
                           "set.");
            return;
        }
        memcpy(_cfg.peer, want, 6);
        cfg_save();
        mac_str(_cfg.peer, buf);
        // Applied HERE, not "on reboot or go". It used to say that and neither
        // did it: the MAC reached NVS and never reached the ESP-NOW stack, so
        // every send came back NOT_FOUND and read as a dead link.
        bool ok = peer_register();
        if (ok && _cfg.phy_lr) peer_rate_long(_cfg.peer);
        Serial.printf("[bench] peer %s — I will send TO that board. %s\n",
                      buf, ok ? "registered, ready for 'go'"
                              : "NOT registered — see the error above");
        return;
    }
    if (!strcmp(cmd, "chan") && a1) { _cfg.channel = atoi(a1); cfg_save();
        Serial.printf("[bench] channel %u (reboot to apply)\n", _cfg.channel); return; }
    if (!strcmp(cmd, "rate") && a1) { _cfg.rate_hz = atoi(a1); cfg_save();
        Serial.printf("[bench] rate %u Hz\n", _cfg.rate_hz); return; }
    if (!strcmp(cmd, "poll") && a1) {
        _cfg.poll_hz = atoi(a1); cfg_save();
        if (_cfg.role != 2)
            Serial.println("[bench] note: poll only does anything in role rx — "
                           "it is the RECEIVER\n        that sends commands at "
                           "the TX board. Saved anyway.");
        Serial.printf("[bench] poll %u Hz%s\n", _cfg.poll_hz,
            _cfg.poll_hz ? " — commands the TX must answer, on top of its own stream"
                         : " (off — the RX is passive, exactly as it was before)");
        if (_cfg.poll_hz && _cfg.role == 2) {
            if (!_rx_peer_ready)
                Serial.println("        waiting for the first frame to learn the "
                               "TX board's MAC.");
            if (!_cfg.running)
                Serial.println("        'go' on THIS board to start polling — it "
                               "zeroes the counters too.");
        }
        return; }
    if (!strcmp(cmd, "size") && a1) { _cfg.size = constrain(atoi(a1), 8, 240);
        cfg_save(); Serial.printf("[bench] size %u\n", _cfg.size); return; }
    if (!strcmp(cmd, "cap") && a1)  { _cfg.cap = atoi(a1); cfg_save();
        Serial.printf("[bench] cap %u in flight%s\n", _cfg.cap,
                      _cfg.cap ? "" : " (fire and forget — what the rig does)");
        return; }
    if (!strcmp(cmd, "load") && a1 && a2) {
        _cfg.load_ms = atoi(a1); _cfg.load_period_ms = atoi(a2); cfg_save();
        Serial.printf("[bench] load %u ms every %u ms\n",
                      _cfg.load_ms, _cfg.load_period_ms);
        return; }
    if (!strcmp(cmd, "scan") && a1) { _cfg.scan_period_s = atoi(a1); cfg_save();
        Serial.printf("[bench] scan every %u s\n", _cfg.scan_period_s); return; }
    if (!strcmp(cmd, "phy") && a1)  { _cfg.phy_lr = !strcmp(a1, "lr"); cfg_save();
        Serial.printf("[bench] phy %s (reboot to apply)\n",
                      _cfg.phy_lr ? "1Mbps long preamble" : "default"); return; }
    if (!strcmp(cmd, "go")) {
        if (_cfg.role == 1) {
            // Defensive: cheap, and the alternative is a run that produces
            // nothing but refusals and looks like the fault being hunted.
            if (!esp_now_is_peer_exist(_cfg.peer)) {
                Serial.println("[bench] peer was not registered — doing it now");
                if (peer_register() && _cfg.phy_lr) peer_rate_long(_cfg.peer);
            }
            uint8_t z[6] = {};
            if (!memcmp(_cfg.peer, z, 6)) {
                Serial.println("[bench] no peer set. 'peer <the RX board's MAC>' "
                               "first — it prints its own on boot.");
                return;
            }
        }
        counters_reset(); _cfg.running = 1; cfg_save();
        // The whole configuration, every time, into whatever is recording.
        //
        // The run file carried only what was typed THROUGH the logger, so a
        // setting that came from NVS, or was set before the logger started,
        // appeared nowhere — and the file's own name was the only claim about
        // what the run tested. A tag is a claim; this is evidence.
        mac_str(_cfg.role == 2 ? _rx_peer : _cfg.peer, buf);
        Serial.printf("[bench] running | role=%s peer=%s rate=%uHz poll=%uHz "
                      "size=%u cap=%u load=%u/%ums scan=%us phy=%s chan=%u\n",
                      _cfg.role == 2 ? "RX" : "TX",
                      buf, _cfg.rate_hz, _cfg.poll_hz, _cfg.size, _cfg.cap,
                      _cfg.load_ms, _cfg.load_period_ms, _cfg.scan_period_s,
                      _cfg.phy_lr ? "1M-LR" : "default", _cfg.channel);
        if (_cfg.role == 2 && _cfg.poll_hz && !_rx_peer_ready)
            Serial.println("[bench] ...but no frame has arrived yet, so I do not "
                           "know who to poll.\n        Start the TX board; I "
                           "learn its MAC from its first frame.");
        return;
    }
    if (!strcmp(cmd, "stop"))  { _cfg.running = 0; cfg_save();
        Serial.println("[bench] stopped"); return; }
    if (!strcmp(cmd, "reset")) { counters_reset(); Serial.println("[bench] zeroed"); return; }
    if (!strcmp(cmd, "stats")) { report(millis()); return; }
    Serial.println("[bench] ? — try 'help'");
}

static void poll_serial() {
    static char line[96];
    static uint8_t n = 0;
    while (Serial.available()) {
        char c = Serial.read();
        if (c == '\r') continue;
        if (c == '\n') { line[n] = 0; n = 0; if (line[0]) handle_line(line); continue; }
        if (n < sizeof(line) - 1) line[n++] = c;
    }
}

// ---------------------------------------------------------------------------

void setup() {
    Serial.begin(115200);
    delay(400);
    cfg_load();
    radio_start();

    uint8_t m[6]; esp_wifi_get_mac(bench_if(), m);
    char buf[20]; mac_str(m, buf);
    // The interface is printed beside the MAC because switching it CHANGES the
    // MAC — the AP and station addresses of one chip differ — and the other
    // board is still holding the old one as its peer. A silent link after an
    // `iface` is that, every time.
    Serial.printf("\n[bench] up. role=%s iface=%s MAC=%s chan=%u\n",
                  _cfg.role == 1 ? "TX" : _cfg.role == 2 ? "RX" : "idle",
                  bench_if_name(), buf, _cfg.channel);
    if (_cfg.role == 1) {
        mac_str(_cfg.peer, buf);
        Serial.printf("[bench] sending TO %s | rate=%uHz size=%u cap=%u "
                      "load=%u/%ums scan=%us phy=%s\n",
                      buf, _cfg.rate_hz, _cfg.size, _cfg.cap,
                      _cfg.load_ms, _cfg.load_period_ms, _cfg.scan_period_s,
                      _cfg.phy_lr ? "1M-LR" : "default");
    } else if (_cfg.role == 2) {
        Serial.printf("[bench] RX needs no peer and no MAC of yours — ESP-NOW\n"
                      "        registers peers only to SEND. Give the MAC above "
                      "to the TX\n        board ('peer %s') and put both on "
                      "channel %u.\n", buf, _cfg.channel);
        if (_cfg.poll_hz)
            Serial.printf("[bench] poll=%uHz — once the TX board's first frame "
                          "arrives I learn its\n        MAC and start sending "
                          "commands back. 'go' on this board to run.\n",
                          _cfg.poll_hz);
    }
    // A configured, linked, silent board looks identical to a working one until
    // someone reads issued=0 off the log. `role` clears the run flag on
    // purpose, so this is the normal state after a reboot, not a fault — but it
    // has to be said or the reboot looks like the whole job.
    if (_cfg.role != 0 && !_cfg.running)
        Serial.println("[bench] STOPPED — nothing will be sent until you type "
                       "`go` on this board.");

    print_help();
    counters_reset();
}

void loop() {
    poll_serial();
    uint32_t now = millis();

    // ---- synthetic load: imitate an LVGL flush hogging the core ------------
    // The mount renders 37 KB draw buffers out of PSRAM on this same chip. If
    // blocking here leaks buffers, the display is the cause and the radio is
    // innocent — which is where cam1 wedging at -33 dBm while cam4 never did
    // at -68 dBm already points.
    static uint32_t last_load = 0;
    if (_cfg.load_ms && _cfg.load_period_ms &&
            (now - last_load) >= _cfg.load_period_ms) {
        last_load = now;
        uint32_t until = millis() + _cfg.load_ms;
        while ((int32_t)(millis() - until) < 0) { __asm__ __volatile__("nop"); }
    }

    // ---- periodic scan: retunes the radio under a live send stream ---------
    static uint32_t last_scan = 0;
    if (_cfg.scan_period_s &&
            (now - last_scan) >= (uint32_t)_cfg.scan_period_s * 1000UL) {
        last_scan = now;
        Serial.println("[bench] scan");
        WiFi.scanNetworks(true, false);      // async, so it overlaps the sends
    }

    // ---- the periodic stream -----------------------------------------------
    // The cap IS the experiment on the rig's behalf: the mount sends with no
    // regard for how many are outstanding. If holding off stops the leak, that
    // is the fix and it is three lines. (The cap lives in bench_send.)
    if (_cfg.running && _cfg.role == 1 && _cfg.rate_hz) {
        static uint32_t last_tx_us = 0;
        uint32_t period_us = 1000000UL / _cfg.rate_hz;
        uint32_t nowu = micros();
        if ((uint32_t)(nowu - last_tx_us) >= period_us) {
            last_tx_us = nowu;
            bench_send(_cfg.peer, FRAME_DATA, _cfg.size, now);
        }
    }

    // ---- the reactive replies ----------------------------------------------
    // The whole point of `poll`. These are issued on ARRIVAL, so they land on
    // top of whatever the metronome above already has in flight — which is the
    // one thing 542,067 evenly spaced sends never managed.
    //
    // No rate limit and no batching: the bridge answers each command as it
    // comes, and if several arrive together it fires several back to back.
    // Attempted once each whether or not the send is accepted, because the rig
    // does not retry an ACK either.
    if (_cfg.running && _cfg.role == 1) {
        // Bounded per pass, and this is CONSERVATIVE next to the mount: the
        // bridge drains its receive queue unbounded (amoled .ino:3525) and
        // answers every non-JOG packet, so a blocked loop lets commands pile up
        // and the resumed loop fires up to ESPNOW_RX_DEPTH=16 ACKs back to
        // back. The block manufactures the burst. Bounded here anyway, because
        // a drain that can stall the loop behind a fast `poll` measures the
        // bench rather than the fault.
        uint8_t budget = 8;
        while (_cmd_acked != _cmd_rx && budget--) {
            _cmd_acked = _cmd_acked + 1;
            bench_send(_cfg.peer, FRAME_ACK, CTRL_SIZE, now);
        }
    }

    // ---- the RX's commands -------------------------------------------------
    // Sent to the MAC the TX board revealed by talking to us, so `poll` needs
    // no MAC typed at it and cannot be pointed at the wrong board.
    if (_cfg.running && _cfg.role == 2 && _cfg.poll_hz && _rx_peer_ready) {
        static uint32_t last_poll_us = 0;
        uint32_t period_us = 1000000UL / _cfg.poll_hz;
        uint32_t nowu = micros();
        if ((uint32_t)(nowu - last_poll_us) >= period_us) {
            last_poll_us = nowu;
            bench_send(_rx_peer, FRAME_CMD, CTRL_SIZE, now);
        }
    }

    // ---- the leak floor ----------------------------------------------------
    // in_flight bounces by one or two with every send; what matters is whether
    // it ever comes back DOWN. The floor only ever rises, so a rising floor is
    // a leak and a flat one is not.
    static uint32_t floor_win_ms = 0;
    static uint32_t floor_min    = 0xFFFFFFFF;
    uint32_t inf = in_flight();
    if (inf < floor_min) floor_min = inf;

    // The high water mark and the overlap count belong HERE, at loop rate, not
    // in report().
    //
    // report() runs once a second and an overlap lasts about a millisecond, so
    // sampling there sees almost none of them. It said so itself: a board
    // sending 20 frames a second reported max=0 — never one in flight, which
    // cannot be true. Every "no overlap" verdict taken that way was measuring
    // the sampler.
    //
    // Counted on the transition rather than per sample: a 1 ms overlap seen by
    // a loop running tens of thousands of times a second would otherwise score
    // dozens.
    //
    // Measured ABOVE THE FLOOR, not above 1. Once buffers start leaking,
    // in_flight never returns below the floor, so a fixed threshold of 1 stops
    // counting transitions altogether — the first leak run froze `ovl` at
    // 437,307 for its last thirteen hours and read as "overlap stopped" when
    // what had happened was the floor reaching 2. The number has to mean the
    // same thing before and after a leak or it cannot be compared across runs.
    static uint32_t prev_inf = 0;
    uint32_t base = _in_flight_floor;
    if (inf > _max_in_flight) _max_in_flight = inf;
    if (inf > base + 1 && prev_inf <= base + 1) _overlaps = _overlaps + 1;
    prev_inf = inf;
    if (now - floor_win_ms >= 5000UL) {
        floor_win_ms = now;
        if (floor_min != 0xFFFFFFFF && floor_min > _in_flight_floor) {
            // THE headline event. Said out loud, with the heap beside it,
            // because the two together are the whole proof: a buffer that
            // never came back, and the memory it took with it. The first
            // reproduction stepped 0->1->2->3 and dropped exactly 208 bytes
            // each time.
            // issued and the callback totals are printed EXACTLY, because the
            // sequence window of the lost frames is issued-minus-callbacks and
            // it has to be computable against the RX board's gap list later.
            // Without them this line says a buffer went missing and gives no
            // way to find out which one.
            Serial.printf("[bench] *** LEAK — floor %lu -> %lu at t=%lus, "
                          "heap %lu, issued %lu, cb_ok %lu, cb_fail %lu, "
                          "outstanding seq %lu..%lu\n",
                          (unsigned long)_in_flight_floor,
                          (unsigned long)floor_min,
                          (unsigned long)((now - _t_start_ms) / 1000UL),
                          (unsigned long)ESP.getFreeHeap(),
                          (unsigned long)_issued,
                          (unsigned long)_cb_ok, (unsigned long)_cb_fail,
                          (unsigned long)(_cb_ok + _cb_fail),
                          (unsigned long)(_issued ? _issued - 1 : 0));
            _in_flight_floor = floor_min;
        }
        floor_min = 0xFFFFFFFF;
    }

    // ---- the first frame, announced once -----------------------------------
    // From the loop rather than the callback: Serial from the WiFi task is a
    // way to get a crash instead of a diagnostic.
    static bool announced = false;
    if (!announced && _rx_first_ms) {
        announced = true;
        Serial.printf("[bench] first frame from %02X:%02X:%02X:%02X:%02X:%02X "
                      "— the link is up\n",
                      _rx_peer[0], _rx_peer[1], _rx_peer[2],
                      _rx_peer[3], _rx_peer[4], _rx_peer[5]);
        // Whoever is sending to us is who `poll` sends commands back to, so
        // register them now and there is no second MAC to type or get wrong.
        // From the loop, not the receive callback, for the same reason the
        // replies are.
        if (_cfg.role == 2) {
            _rx_peer_ready = peer_add(_rx_peer);
            if (_rx_peer_ready && _cfg.phy_lr) peer_rate_long(_rx_peer);
            Serial.printf("[bench] %s — 'poll <Hz>' sends commands back to it, "
                          "which it must answer\n",
                          _rx_peer_ready ? "registered as my peer"
                                         : "COULD NOT register it — poll will not work");
        }
    }
    // Nothing at all after a while, in the role whose whole job is to receive.
    static uint32_t nagged = 0;
    if (_cfg.role == 2 && !_rx_first_ms && now > 15000UL &&
            (now - nagged) > 30000UL) {
        nagged = now;
        uint8_t m[6]; esp_wifi_get_mac(bench_if(), m);
        Serial.printf("[bench] nothing received in %lus. I am "
                      "%02X:%02X:%02X:%02X:%02X:%02X on channel %u — check the TX "
                      "board has that MAC as its peer, and the same channel.\n",
                      (unsigned long)(now / 1000UL),
                      m[0], m[1], m[2], m[3], m[4], m[5], _cfg.channel);
    }

    // ---- did the experiment actually do anything? --------------------------
    // `poll` exists to get two buffers out of the pool at once. Whether it
    // managed that is a thirty-second question, and finding out from a clean
    // twelve-hour log that the answer was no is the waste worth preventing.
    static bool overlap_said = false;
    if (!overlap_said && _overlaps) {
        overlap_said = true;
        Serial.printf("[bench] OVERLAP — two sends outstanding at once, %lus in. "
                      "The metronome\n        alone never managed it in three "
                      "hours. Watch 'ovl' from here: if\n        the leak tracks "
                      "it, that is the mechanism.\n",
                      (unsigned long)((now - _t_start_ms) / 1000UL));
    }
    // Rate, not just the fact. Tuning `poll` needs to know whether overlap is
    // happening twice an hour or twice a second, and the answer decides whether
    // an overnight run is worth starting.
    static uint32_t overlap_said_ms = 0;
    if (overlap_said && (now - overlap_said_ms) >= 300000UL) {
        overlap_said_ms = now;
        uint32_t secs = (now - _t_start_ms) / 1000UL;
        if (secs) Serial.printf("[bench] overlaps %lu in %lus — %lu per 1000 "
                                "sends\n", (unsigned long)_overlaps,
                                (unsigned long)secs,
                                (unsigned long)(_issued ? _overlaps * 1000UL / _issued : 0));
    }
    static bool overlap_warned = false;
    if (!overlap_warned && !overlap_said && _cfg.running && _cfg.role == 1 &&
            _cmd_rx > 100 && (now - _t_start_ms) > 60000UL) {
        overlap_warned = true;
        Serial.printf("[bench] NO OVERLAP after 60s — %lu commands answered and "
                      "in_flight has\n        never exceeded 1, so this is still "
                      "a metronome. Raise the RX's\n        'poll' (or 'rate') "
                      "until it does, or the run tests nothing new.\n",
                      (unsigned long)_cmd_acked);
    }

    // ---- one line a second -------------------------------------------------
    static uint32_t last_report = 0;
    if (now - last_report >= 1000UL) {
        last_report = now;
        if (_cfg.role != 0) report(now);
    }
}
