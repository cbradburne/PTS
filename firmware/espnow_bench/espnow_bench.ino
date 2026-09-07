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
};

static Cfg _cfg = { 0, {0,0,0,0,0,0}, 50, 32, 0, 0, 0, 0, 1, 1, 0, 0 };

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
    p.ifidx   = WIFI_IF_STA;
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

static void radio_start() {
    WiFi.mode(WIFI_STA);
    WiFi.disconnect();
    esp_wifi_set_ps(WIFI_PS_NONE);
    esp_wifi_set_max_tx_power(84);
    esp_wifi_set_protocol(WIFI_IF_STA,
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

// ---------------------------------------------------------------------------
// Reporting — one CSV line a second, so a run can be logged and plotted
// ---------------------------------------------------------------------------

static void report(uint32_t now) {
    uint32_t inf = in_flight();   // max/overlap are tracked in loop(), at loop rate
    Serial.printf(
        "t=%lus issued=%lu cb_ok=%lu cb_fail=%lu refused=%lu nomem=%lu "
        "in_flight=%lu floor=%lu max=%lu rx=%lu heap=%lu err=0x%lX "
        "cmd=%lu ack=%lu ovl=%lu\n",
        (unsigned long)((now - _t_start_ms) / 1000UL),
        (unsigned long)_issued, (unsigned long)_cb_ok, (unsigned long)_cb_fail,
        (unsigned long)_refused, (unsigned long)_nomem,
        (unsigned long)inf, (unsigned long)_in_flight_floor,
        (unsigned long)_max_in_flight, (unsigned long)_rx_count,
        (unsigned long)ESP.getFreeHeap(), (unsigned long)_last_err,
        (unsigned long)_cmd_rx, (unsigned long)_cmd_acked,
        (unsigned long)_overlaps);
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
      "   go | stop            start / stop sending\n"
      "   reset                zero the counters\n"
      "   stats                print one line now\n"
      "   mac                  this board's MAC\n"
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
    if (!strcmp(cmd, "mac")) {
        uint8_t m[6]; esp_wifi_get_mac(WIFI_IF_STA, m); mac_str(m, buf);
        Serial.printf("[bench] my MAC %s  channel %u\n", buf, _cfg.channel);
        return;
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
        esp_wifi_get_mac(WIFI_IF_STA, mine);
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

    uint8_t m[6]; esp_wifi_get_mac(WIFI_IF_STA, m);
    char buf[20]; mac_str(m, buf);
    Serial.printf("\n[bench] up. role=%s MAC=%s chan=%u\n",
                  _cfg.role == 1 ? "TX" : _cfg.role == 2 ? "RX" : "idle",
                  buf, _cfg.channel);
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
        // Bounded per pass. The bridge dequeues from a FreeRTOS queue that is
        // finite and drops when full, so an unbounded drain here would be the
        // bench inventing a burst the rig cannot produce — and, if the loop
        // ever fell behind a fast `poll`, would stall it doing so.
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
            Serial.printf("[bench] *** LEAK — floor %lu -> %lu at t=%lus, "
                          "heap %lu, issued %lu\n",
                          (unsigned long)_in_flight_floor,
                          (unsigned long)floor_min,
                          (unsigned long)((now - _t_start_ms) / 1000UL),
                          (unsigned long)ESP.getFreeHeap(),
                          (unsigned long)_issued);
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
        uint8_t m[6]; esp_wifi_get_mac(WIFI_IF_STA, m);
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
