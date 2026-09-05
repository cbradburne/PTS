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
};

static Cfg _cfg = { 0, {0,0,0,0,0,0}, 50, 32, 0, 0, 0, 0, 1, 1, 0 };

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

static uint32_t _max_in_flight  = 0;
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

static void on_sent(const wifi_tx_info_t *, esp_now_send_status_t s) {
    if (s == ESP_NOW_SEND_SUCCESS) _cb_ok  = _cb_ok  + 1;
    else                           _cb_fail = _cb_fail + 1;
}

static void on_recv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
    (void)info; (void)data;
    _rx_count = _rx_count + 1;
    _rx_bytes = _rx_bytes + (uint32_t)len;
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

    if (_cfg.role == 1) {
        esp_now_peer_info_t p = {};
        memcpy(p.peer_addr, _cfg.peer, 6);
        p.channel = _cfg.channel;
        p.ifidx   = WIFI_IF_STA;
        p.encrypt = false;
        esp_now_add_peer(&p);
        if (_cfg.phy_lr) peer_rate_long(p.peer_addr);
    }
}

// ---------------------------------------------------------------------------
// Reporting — one CSV line a second, so a run can be logged and plotted
// ---------------------------------------------------------------------------

static void report(uint32_t now) {
    uint32_t inf = in_flight();
    if (inf > _max_in_flight) _max_in_flight = inf;
    Serial.printf(
        "t=%lus issued=%lu cb_ok=%lu cb_fail=%lu refused=%lu nomem=%lu "
        "in_flight=%lu floor=%lu max=%lu rx=%lu heap=%lu err=0x%lX\n",
        (unsigned long)((now - _t_start_ms) / 1000UL),
        (unsigned long)_issued, (unsigned long)_cb_ok, (unsigned long)_cb_fail,
        (unsigned long)_refused, (unsigned long)_nomem,
        (unsigned long)inf, (unsigned long)_in_flight_floor,
        (unsigned long)_max_in_flight, (unsigned long)_rx_count,
        (unsigned long)ESP.getFreeHeap(), (unsigned long)_last_err);
}

static void counters_reset() {
    // Assigned one at a time: chaining through a volatile reads back what was
    // just written, which C++20 deprecates and which is meaningless here anyway.
    _issued = 0; _cb_ok = 0; _cb_fail = 0;
    _refused = 0; _nomem = 0; _last_err = 0;
    _rx_count = 0; _rx_bytes = 0;
    _max_in_flight = _in_flight_floor = _consec_refused = 0;
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
      "   peer AA:BB:...       who TX sends to (persists)\n"
      "   chan <1-13>          WiFi channel\n"
      "   rate <Hz>            sends per second\n"
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
        Serial.printf("[bench] role %s — rebooting to apply cleanly\n", a1);
        delay(100);
        ESP.restart();
    }
    if (!strcmp(cmd, "peer") && a1) {
        if (!parse_mac(a1, _cfg.peer)) { Serial.println("[bench] bad MAC"); return; }
        cfg_save();
        mac_str(_cfg.peer, buf);
        Serial.printf("[bench] peer %s — reboot or 'go' to apply\n", buf);
        return;
    }
    if (!strcmp(cmd, "chan") && a1) { _cfg.channel = atoi(a1); cfg_save();
        Serial.printf("[bench] channel %u (reboot to apply)\n", _cfg.channel); return; }
    if (!strcmp(cmd, "rate") && a1) { _cfg.rate_hz = atoi(a1); cfg_save();
        Serial.printf("[bench] rate %u Hz\n", _cfg.rate_hz); return; }
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
    if (!strcmp(cmd, "go"))    { counters_reset(); _cfg.running = 1; cfg_save();
        Serial.println("[bench] running"); return; }
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
        Serial.printf("[bench] peer=%s rate=%uHz size=%u cap=%u load=%u/%ums "
                      "scan=%us phy=%s\n",
                      buf, _cfg.rate_hz, _cfg.size, _cfg.cap,
                      _cfg.load_ms, _cfg.load_period_ms, _cfg.scan_period_s,
                      _cfg.phy_lr ? "1M-LR" : "default");
    }
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

    // ---- the send stream ---------------------------------------------------
    if (_cfg.running && _cfg.role == 1 && _cfg.rate_hz) {
        static uint32_t last_tx_us = 0;
        uint32_t period_us = 1000000UL / _cfg.rate_hz;
        uint32_t nowu = micros();
        if ((uint32_t)(nowu - last_tx_us) >= period_us) {
            last_tx_us = nowu;
            // The cap IS the experiment on the rig's behalf: the mount sends
            // with no regard for how many are outstanding. If holding off here
            // stops the leak, that is the fix and it is three lines.
            if (!_cfg.cap || in_flight() < _cfg.cap) {
                static uint8_t pay[240];
                pay[0] = (uint8_t)(_issued >> 24); pay[1] = (uint8_t)(_issued >> 16);
                pay[2] = (uint8_t)(_issued >> 8);  pay[3] = (uint8_t)_issued;
                esp_err_t e = esp_now_send(_cfg.peer, pay, _cfg.size);
                if (e == ESP_OK) {
                    _issued = _issued + 1;
                    _consec_refused = 0;
                } else {
                    _refused = _refused + 1;
                    _last_err = (uint32_t)e;
                    if (e == ESP_ERR_ESPNOW_NO_MEM) _nomem = _nomem + 1;
                    if (!_t_first_ref_ms) {
                        _t_first_ref_ms = now;
                        Serial.printf("[bench] FIRST REFUSAL at t=%lus err=0x%X "
                                      "in_flight=%lu\n",
                                      (unsigned long)((now - _t_start_ms) / 1000UL),
                                      (int)e, (unsigned long)in_flight());
                    }
                    // Continuous refusals with no callback in between is the
                    // wedge itself, as opposed to a momentary full queue.
                    _consec_refused = _consec_refused + 1;
                    if (_consec_refused == 50 && !_t_wedge_ms) {
                        _t_wedge_ms = now;
                        Serial.printf("[bench] *** WEDGED at t=%lus — 50 refusals "
                                      "in a row, in_flight=%lu, cb since start "
                                      "ok=%lu fail=%lu\n",
                                      (unsigned long)((now - _t_start_ms) / 1000UL),
                                      (unsigned long)in_flight(),
                                      (unsigned long)_cb_ok, (unsigned long)_cb_fail);
                    }
                }
            }
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
    if (now - floor_win_ms >= 5000UL) {
        floor_win_ms = now;
        if (floor_min != 0xFFFFFFFF && floor_min > _in_flight_floor)
            _in_flight_floor = floor_min;
        floor_min = 0xFFFFFFFF;
    }

    // ---- one line a second -------------------------------------------------
    static uint32_t last_report = 0;
    if (now - last_report >= 1000UL) {
        last_report = now;
        if (_cfg.role != 0) report(now);
    }
}
