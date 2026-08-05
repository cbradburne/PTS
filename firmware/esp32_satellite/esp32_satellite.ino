/*
 * PTS satellite — an ESP-NOW cell on the end of an Ethernet cable.
 *
 * Waveshare ESP32-S3-ETH, the same board as the hub.  Runs wherever a mount is
 * too far from the hub to hear it: mount 4 sat 50 m away at −85 dBm and spent
 * a week dropping out, where a satellite in the same room puts it at −40.
 *
 *   [mount] ──ESP-NOW──> [satellite] ──Ethernet/TCP──> [hub] ──> PC / web / display
 *
 * AS CLOSE TO A DUMB PIPE AS THE HUB ALLOWS.  It reads exactly one field —
 * mount_id at byte [3] — to know which peer a downlink frame is for, and never
 * looks at a payload.  Uplink frames are wrapped in a small envelope carrying
 * the mount's MAC and RSSI (shared/sat_link.h), because the hub pairs on MAC
 * and drops anything it cannot bind; a pure byte pipe would make a
 * satellite-attached mount unpairable.  Everything else stays in the hub, so a
 * satellite needs no update when the protocol grows.
 *
 * The mount chooses ITS satellite, not the other way round: each one raises a
 * SoftAP called "PTS-<name>", the mount's existing scan picks the strongest of
 * the hubs it knows, and the satellite simply serves whoever turns up.  That is
 * why exactly one device ever transmits to a given mount, and why no
 * duplicate-command protection is needed here.
 *
 * Arduino IDE board settings (Waveshare ESP32-S3-ETH).  These must match
 * FQBN_SAT in tools/build.sh — the scripted build is the one normally flashed,
 * and a Tools-menu difference produces a different binary from the same source:
 *   Board            : ESP32S3 Dev Module
 *   USB CDC On Boot  : Enabled
 *   USB Mode         : Hardware CDC and JTAG
 *   Flash Size       : 16MB (128Mb)
 *   Partition Scheme : 8M with spiffs (3MB APP/1.5MB SPIFFS)
 *   PSRAM            : Disabled          <-- NOT "OPI PSRAM"
 *
 * That last one is the trap.  The mounts and the 7" display are also "ESP32S3
 * Dev Module" but need OPI PSRAM, and the IDE remembers the setting per board
 * type, not per sketch — so opening this straight after flashing a mount leaves
 * PSRAM enabled on a module that has none.  Naming the satellite means editing
 * SAT_NAME below, since the IDE has no way to pass the build flag build.sh uses.
 *
 * The AP also means a satellite's channel is its own: discovery is a WiFi scan
 * across all channels, so neighbouring cells need not share one and distant
 * mounts stop competing for airtime with local ones.
 */

#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <Preferences.h>

#include "../shared/protocol.h"
#define ETH_HOSTNAME "pts-sat"

// This satellite's address on the wired (Dante) network.  Static for the same
// reason the hub's is: 169.254.0.0/16 is the link-local range, which by
// definition has no DHCP server to ask.  Without an address _eth_up never
// becomes true and uplink_service() returns before it even attempts to connect
// — a satellite that looks healthy at both ends and silently relays nothing.
//
// Gateway 0.0.0.0 deliberately: a flat link-local network has no router, and
// the only thing this box talks to over the wire is the hub, on this subnet.
// No DNS server either — "pts-hub.local" is resolved by mDNS multicast, which
// asks the network rather than a server.
//
// >> ONE ADDRESS PER SATELLITE <<  This is a fixed value, so a second unit
// built from this sketch unchanged would claim the same address as the first.
// Two hosts sharing an IP fail intermittently and asymmetrically, which on a
// radio bridge looks exactly like interference — the last thing you would
// suspect.  Give each satellite its own last octet and write them down:
//
//     .22  hub          .23  first satellite          .24, .25, ...
#define ETH_STATIC_IP  "169.254.22.23"
#define ETH_SUBNET     "255.255.0.0"
#define ETH_GATEWAY    "0.0.0.0"

#include "../shared/board_eth.h"
#include "../shared/sat_link.h"

// ---------------------------------------------------------------------------
// Identity
// ---------------------------------------------------------------------------
// Must match HUB_SSID_PREFIX in the mount sketch — that is the scan filter, and
// a mount cannot tell a satellite from a hub (nor should it).
#define AP_SSID_PREFIX  "PTS-"
#define HUB_NAME_MAX    12
static char AP_SSID[5 + HUB_NAME_MAX];      // composed at boot, see SAT_NAME
#define AP_PASSWORD     "camctrl123"

// Where this satellite is, as the operator reads it in the mount's setup list:
// "PTS-Foyer".  Name it after the room — that name is the only thing on screen
// when someone is choosing which cell a mount should join, so "Foyer" beats any
// serial number.  Max 12 characters (the mount keeps 17 per entry, and "PTS-"
// plus a terminator claims five); use _ for spaces.  Over-long names fail the
// build below rather than being cut short out on a pole.
//
//   SAT_NAME="Concert_Hall" tools/build.sh flash sat
//
// Keeping it in the environment leaves no diff behind when you flash three
// satellites in a row.  Uncommenting the line below works too and OVERRIDES the
// build flag — the sketch is the later definition, so it wins and the compiler
// warns about the redefinition.  Use one or the other, not both.
//
// #define SAT_NAME "Foyer"

// Unnamed, a satellite is "PTS-Sat-A3F2" off its own MAC: unique, so two of
// them never collide in a scan list, and honest about having no name yet.
#define HUB_NAME_FALLBACK_PREFIX "Sat-"

// Channel is per-satellite, so cells do not have to share airtime.  Override
// per unit; mounts find it by scanning, so nothing else needs telling.
#ifndef AP_CHANNEL
#define AP_CHANNEL      6
#endif

static Preferences _prefs;
#include "../shared/hub_name.h"

#ifdef SAT_NAME
// A name too long to fit is truncated silently, and a satellite has no screen
// to notice that on — so it fails the build instead of the deployment.
static_assert(sizeof(SAT_NAME) - 1 <= HUB_NAME_MAX,
              "SAT_NAME is longer than 12 characters - a mount would show it cut short");
#endif

// ---------------------------------------------------------------------------
// Uplink to the hub
// ---------------------------------------------------------------------------
// Port and envelope format both come from shared/sat_link.h, so the two ends
// cannot drift apart.
#define HUB_PORT        SAT_LINK_PORT
#define HUB_HOST_MAX    40
static char     _hub_host[HUB_HOST_MAX] = SAT_HUB_MDNS_NAME;
static WiFiClient _uplink;
static uint32_t _uplink_next_try_ms = 0;
static uint32_t _uplink_backoff_ms  = 1000;
#define UPLINK_BACKOFF_MAX_MS  15000

// ---------------------------------------------------------------------------
// Mount peers — learned, never configured
// ---------------------------------------------------------------------------
// A mount announces itself by talking to us.  We remember which MAC each
// mount_id came from so downlink packets can be addressed, and refresh the
// timestamp on every frame so a mount that roams to another satellite ages out
// here rather than being transmitted to forever.
struct MountPeer {
    uint8_t  mac[6];
    uint32_t last_rx_ms;
    bool     used;
};
static MountPeer _peer[NUM_MOUNTS];      // indexed by mount_id - 1
#define PEER_STALE_MS  60000UL

static void peer_forget_stale(uint32_t now) {
    for (int i = 0; i < NUM_MOUNTS; i++) {
        if (!_peer[i].used) continue;
        if (now - _peer[i].last_rx_ms < PEER_STALE_MS) continue;
        esp_now_del_peer(_peer[i].mac);
        _peer[i].used = false;
        // Worth a loud line: from this moment the hub's commands for that mount
        // arrive here and are discarded, while the hub goes on believing we
        // serve it.  If this appears while the mount is still alive, the uplink
        // stopped and the downlink went with it.
        Serial.printf("[PEER] mount %d aged out after %lu ms silent — its downlink "
                      "is now dropped here until it transmits again\n",
                      i + 1, (unsigned long)PEER_STALE_MS);
    }
}

static void peer_learn(uint8_t mount_id, const uint8_t *mac, uint32_t now) {
    if (mount_id < 1 || mount_id > NUM_MOUNTS) return;
    MountPeer &p = _peer[mount_id - 1];
    bool changed = !p.used || memcmp(p.mac, mac, 6) != 0;
    if (changed) {
        if (p.used) esp_now_del_peer(p.mac);
        memcpy(p.mac, mac, 6);
        esp_now_peer_info_t info = {};
        memcpy(info.peer_addr, mac, 6);
        info.channel = AP_CHANNEL;
        info.ifidx   = WIFI_IF_AP;      // we serve mounts on our OWN AP
        info.encrypt = false;
        esp_now_add_peer(&info);
        p.used = true;
        Serial.printf("[PEER] mount %d at %02X:%02X:%02X:%02X:%02X:%02X\n",
                      mount_id, mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    }
    p.last_rx_ms = now;
}

// ---------------------------------------------------------------------------
// Throughput diagnostics
// ---------------------------------------------------------------------------
// The downlink was sustaining ~1.3 frames/s while ~2.1/s arrived, so 40% of the
// commands for a mount were being shed - and a deeper queue would not have
// helped, because a queue absorbs bursts and this is a steady shortfall.
//
// Two candidates, and these separate them.  Either the radio accepts almost
// nothing per pass (nomem climbing, pumped-per-pass ~1), or the loop itself is
// too slow to pump often enough (loop rate low, nomem near zero) - most likely
// starved by drain_espnow_to_uplink(), which does a TCP write per uplink frame
// while a mount streams STATUS at ~10 Hz.
static uint32_t _loop_count   = 0;   // passes since the last report
static uint32_t _loop_max_us  = 0;   // slowest single pass
static uint32_t _dn_nomem     = 0;   // esp_now_send() refusals
static uint32_t _up_writes    = 0;   // TCP writes made for uplink frames
static uint32_t _up_write_us  = 0;   // time spent in them

// ---------------------------------------------------------------------------
// ESP-NOW  ->  uplink
// ---------------------------------------------------------------------------
// The receive callback runs in WiFi task context, so it only queues.  Touching
// the TCP socket from here would be a cross-task use of WiFiClient, and calling
// into the network stack from a radio callback is how the mount bridge's own
// ESP-NOW reinit ended up faulting in ipc1.
struct RxItem { uint8_t len; uint8_t data[PACKET_MAX_PAYLOAD + 16];
                uint8_t mac[6]; int8_t rssi; };
static QueueHandle_t _rx_q = nullptr;

static void on_espnow_recv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
    if (!info || len <= 0 || len > (int)sizeof(((RxItem *)0)->data)) return;
    RxItem it;
    it.len = (uint8_t)len;
    memcpy(it.data, data, len);
    memcpy(it.mac, info->src_addr, 6);
    // The hub pairs on MAC and uses rssi==0 as its ghost guard, so both must
    // survive the trip over Ethernet — see shared/sat_link.h.
    it.rssi = (info->rx_ctrl && info->rx_ctrl->rssi) ? (int8_t)info->rx_ctrl->rssi : -1;
    BaseType_t hp = pdFALSE;
    xQueueSendFromISR(_rx_q, &it, &hp);
    if (hp) portYIELD_FROM_ISR();
}

// mount_id is byte [3] of the frame: [0xAA][0x55][LEN][MOUNT_ID]...
static inline uint8_t frame_mount_id(const uint8_t *d, uint8_t len) {
    return (len >= 4 && d[0] == PKT_START_1 && d[1] == PKT_START_2) ? d[3] : 0;
}

static void drain_espnow_to_uplink(uint32_t now) {
    RxItem it;
    while (xQueueReceive(_rx_q, &it, 0) == pdTRUE) {
        peer_learn(frame_mount_id(it.data, it.len), it.mac, now);
        if (_uplink.connected()) {
            uint8_t env[SAT_ENV_MAX];
            uint16_t n = sat_env_build(env, it.mac, it.rssi, it.data, it.len);
            if (n) {
                uint32_t t0 = micros();
                _uplink.write(env, n);
                _up_write_us += (micros() - t0);
                _up_writes++;
            }
        }
        // Not connected: drop.  Buffering telemetry to replay later would
        // deliver a burst of stale STATUS the moment the hub reappears, and
        // STATUS is superseded every 100 ms anyway.
    }
}

// ---------------------------------------------------------------------------
// Uplink  ->  ESP-NOW
// ---------------------------------------------------------------------------
// Framing is recovered the same way every other node does it, so a partial TCP
// read cannot desynchronise the stream.
static uint8_t  _tcp_buf[PACKET_MAX_PAYLOAD + 16];
static uint16_t _tcp_len = 0;

// Downlink counters.  A satellite that quietly stops delivering looks exactly
// like a mount that stopped listening, and the difference is only visible from
// here: the hub cannot see this side, and on TCP the PC app cannot see even the
// hub's console.  One rig lost 31% of its commands to a mount for an hour with
// nothing anywhere recording a single dropped frame.
static uint32_t _dn_sent = 0, _dn_no_peer = 0, _dn_send_err = 0;
// Accepted by the radio vs actually acknowledged by the mount.
static volatile uint32_t _dn_acked = 0, _dn_unacked = 0;
static uint32_t _dn_last_report_ms = 0;
static uint32_t _dn_last_ok_ms     = 0;   // last accepted send

#define DN_NOMEM_DROP_MS      250UL      // stop holding a stale frame
#define DN_NOMEM_RECOVER_MS  5000UL      // stack is wedged, rebuild it
#define DN_REPORT_MS  30000UL

// ---------------------------------------------------------------------------
// Downlink pacing
// ---------------------------------------------------------------------------
// esp_now_send() was called fire-and-forget, one per frame off the wire, with
// no send callback and nothing watching whether the previous frame had left.
// A mount that is not listening — because it has roamed to the hub, which is on
// a different AP channel — never acknowledges, so each frame occupies the
// driver for its full retry period and the queue backs up.  The result is
// bursts of ESP_ERR_ESPNOW_NO_MEM, and those frames are simply lost: 76 of them
// in one 30-minute window, every one a command the operator issued.
//
// So: a small ring, one frame in flight, released by the send callback.  This
// does not make an absent mount reachable — nothing here can — but it stops a
// burst aimed at one mount from being discarded outright, and it stops the
// driver being hammered while it is already saturated.
#define DN_QUEUE_DEPTH   32
// Holds the mount INDEX, not a copy of its MAC.  A queued frame outlives the
// moment it was queued, and peer_forget_stale() deletes peers 60 s after a
// mount goes quiet — so a MAC captured at enqueue time can name a peer that no
// longer exists by the time it is sent, and esp_now_send() then fails with
// ESP_ERR_ESPNOW_NOT_FOUND.  Resolving the peer at SEND time means the check
// and the send cannot disagree.
struct DnFrame { uint8_t idx; uint16_t len; uint8_t data[PACKET_MAX_PAYLOAD + 16]; };
static DnFrame  _dn_q[DN_QUEUE_DEPTH];
static uint8_t  _dn_head = 0, _dn_tail = 0;
static uint32_t _dn_overflow = 0;

// The status here is the ONLY place delivery is visible.  esp_now_send()
// returning ESP_OK means the radio accepted the frame, nothing more; whether
// the mount acknowledged it at the MAC layer is reported only in this callback.
// Discarding it — as this did — left "0 send errors" being printed while 70% of
// commands were never reaching the mount, which reads as the satellite being
// healthy and the mount being at fault.
static void on_espnow_sent(const wifi_tx_info_t *info, esp_now_send_status_t status) {
    (void)info;
    if (status == ESP_NOW_SEND_SUCCESS) _dn_acked++;
    else                                _dn_unacked++;
}

// Push onto the ring.  Dropping the OLDEST on overflow, not the newest: these
// are control commands, and the most recent one is the operator's latest
// intention — a stale jog is worth less than the stop that followed it.
static void dn_enqueue(uint8_t idx, const uint8_t *frame, uint16_t len) {
    if (len > sizeof(((DnFrame *)0)->data)) return;
    uint8_t next = (uint8_t)((_dn_head + 1) % DN_QUEUE_DEPTH);
    if (next == _dn_tail) {
        _dn_tail = (uint8_t)((_dn_tail + 1) % DN_QUEUE_DEPTH);
        _dn_overflow++;
    }
    memcpy(_dn_q[_dn_head].data, frame, len);
    _dn_q[_dn_head].len = len;
    _dn_q[_dn_head].idx = idx;
    _dn_head = next;
}

// Called from loop().  Sends as many as the radio will take, stopping only when
// it says NO_MEM — which is the backpressure signal, and the right one.
//
// This used to send ONE frame per loop pass and then wait for the send
// callback, while drain_uplink_to_espnow() enqueues a whole burst from the hub
// in that same pass.  Enqueue was unbounded, dequeue was one: a burst simply
// filled the 32-deep ring and everything after it was shed.  That cost 150
// commands in a 30-second window while every frame that DID go out was
// acknowledged by the mount — the radio was never the problem, the pacing was.
//
// The one-in-flight rule was there to avoid NO_MEM, which only ever appeared
// because sends to an absent mount burned their full retry period first.  The
// peer is now checked at send time, so that case is gone and the guard costs
// far more than it saves.
// Rebuild the ESP-NOW stack and re-register the peers we know about.  The
// satellite had no recovery at all: if the driver stopped accepting frames it
// stayed that way, and the only symptom was a downlink that went quiet.
static void espnow_recover() {
    Serial.println("[DOWN] ESP-NOW stalled — reinitialising the stack");
    esp_now_deinit();
    delay(50);
    if (esp_now_init() != ESP_OK) {
        Serial.println("[DOWN] ESP-NOW reinit FAILED — will retry");
        return;
    }
    esp_now_register_recv_cb(on_espnow_recv);
    esp_now_register_send_cb(on_espnow_sent);
    for (int i = 0; i < NUM_MOUNTS; i++) {
        if (!_peer[i].used) continue;
        esp_now_peer_info_t info = {};
        memcpy(info.peer_addr, _peer[i].mac, 6);
        info.channel = AP_CHANNEL;
        info.ifidx   = WIFI_IF_AP;
        info.encrypt = false;
        esp_now_add_peer(&info);
    }
    Serial.println("[DOWN] ESP-NOW reinitialised, peers restored");
}

static void dn_pump(uint32_t now) {
    (void)now;
    while (_dn_head != _dn_tail) {
        DnFrame &f = _dn_q[_dn_tail];
        // The mount may have aged out while this frame waited.  Drop it rather
        // than send to a peer that no longer exists: it cannot be delivered
        // either way, and holding it stalls everything behind it.
        if (f.idx >= NUM_MOUNTS || !_peer[f.idx].used) {
            _dn_tail = (uint8_t)((_dn_tail + 1) % DN_QUEUE_DEPTH);
            _dn_no_peer++;
            continue;
        }
        esp_err_t e = esp_now_send(_peer[f.idx].mac, f.data, f.len);
        if (e == ESP_ERR_ESPNOW_NO_MEM) {
            _dn_nomem++;
            // Saturated.  Holding position is right for a moment - the radio
            // is busy and will drain - but it must not be forever.  Written as
            // a bare "return", it was: the driver stopped accepting frames and
            // the satellite relayed nothing for fifteen minutes, sent frozen,
            // ~52 commands shed every 30 s, and every other counter reading
            // zero.  A silent permanent stall is the worst thing this box can
            // do, so it is now bounded twice over.
            if (now - _dn_last_ok_ms > DN_NOMEM_DROP_MS) {
                // Shift the head so the queue can move even while the radio
                // refuses; a command this old is stale anyway.
                _dn_tail = (uint8_t)((_dn_tail + 1) % DN_QUEUE_DEPTH);
                _dn_send_err++;
            }
            if (now - _dn_last_ok_ms > DN_NOMEM_RECOVER_MS) {
                _dn_last_ok_ms = now;      // one attempt per interval
                espnow_recover();
            }
            return;
        }
        _dn_tail = (uint8_t)((_dn_tail + 1) % DN_QUEUE_DEPTH);
        if (e == ESP_OK) {
            _dn_sent++;
            _dn_last_ok_ms = now;
        } else {
            _dn_send_err++;
            Serial.printf("[DOWN] send failed: %s\n", esp_err_to_name(e));
        }
    }
}

static void forward_frame(const uint8_t *frame, uint16_t len) {
    uint8_t mid = frame_mount_id(frame, len);
    if (mid == MOUNT_BROADCAST) {
        for (int i = 0; i < NUM_MOUNTS; i++)
            if (_peer[i].used) dn_enqueue((uint8_t)i, frame, len);
        return;
    }
    if (mid < 1 || mid > NUM_MOUNTS) return;      // not a mount frame at all

    if (!_peer[mid - 1].used) {
        // A mount we have never heard from is not ours — the hub broadcasts to
        // every satellite, and each one ignores the mounts that live in another
        // room.  But it is ALSO what a mount that has aged out of our table
        // looks like, and that is a fault: the hub still believes we serve it,
        // so its commands arrive here and stop, unacknowledged and unlogged.
        // Counted, and reported below, so the two can be told apart.
        _dn_no_peer++;
        return;
    }
    dn_enqueue((uint8_t)(mid - 1), frame, len);
}

// Called from loop().  Silent while nothing is being dropped, so this cannot
// bury the log the way an unconditional heartbeat would.
static void downlink_report(uint32_t now) {
    if (now - _dn_last_report_ms < DN_REPORT_MS) return;
    _dn_last_report_ms = now;
    // Throughput first: printed unconditionally alongside any downlink trouble,
    // because "how fast can this box actually push frames" is the question the
    // shed count cannot answer on its own.
    uint32_t secs = DN_REPORT_MS / 1000UL;
    Serial.printf("[RATE] %lu loops/s (slowest pass %lu us) | %lu nomem | "
                  "uplink %lu writes, %lu us total\n",
                  (unsigned long)(_loop_count / (secs ? secs : 1)),
                  (unsigned long)_loop_max_us, (unsigned long)_dn_nomem,
                  (unsigned long)_up_writes, (unsigned long)_up_write_us);
    _loop_count = _loop_max_us = _dn_nomem = _up_writes = _up_write_us = 0;

    if (!_dn_no_peer && !_dn_send_err && !_dn_overflow && !_dn_unacked) return;
    Serial.printf("[DOWN] %lu sent (%lu acked by the mount, %lu NOT acked), "
                  "%lu dropped (no peer), %lu send errors, %lu shed (queue full)\n",
                  (unsigned long)_dn_sent, (unsigned long)_dn_acked,
                  (unsigned long)_dn_unacked, (unsigned long)_dn_no_peer,
                  (unsigned long)_dn_send_err, (unsigned long)_dn_overflow);
    _dn_no_peer = _dn_send_err = _dn_overflow = _dn_unacked = 0;
}

// Reads whatever the hub has sent and hands each complete frame to
// forward_frame().  Pumps as it goes: this used to enqueue the ENTIRE burst and
// leave dn_pump() to run afterwards, once, at the end of the loop pass — so a
// hub sending more than DN_QUEUE_DEPTH frames back-to-back overflowed the ring
// before a single one had been transmitted, and the excess was shed.
//
// That was the whole of the shedding.  The instrumentation ruled out both of
// the things I suspected: the loop runs ~12,000 times a second and the radio
// never once refused a frame (0 nomem).  Neither could have been the cause; the
// queue was simply being filled faster than the one drain per pass could empty
// it, inside a single pass.
static void drain_uplink_to_espnow() {
    while (_uplink.available()) {
        int c = _uplink.read();
        if (c < 0) break;
        if (_tcp_len == 0 && c != PKT_START_1) continue;             // hunt for 0xAA
        if (_tcp_len == 1 && c != PKT_START_2) { _tcp_len = 0; continue; }
        _tcp_buf[_tcp_len++] = (uint8_t)c;
        if (_tcp_len >= 3) {
            uint16_t want = 3 + _tcp_buf[2] + 2;      // hdr + LEN body + CRC
            if (want > sizeof(_tcp_buf)) { _tcp_len = 0; continue; }
            if (_tcp_len == want) {
                forward_frame(_tcp_buf, _tcp_len);
                _tcp_len = 0;
                // Drain as we fill.  Cheap — the radio is accepting frames, so
                // this normally sends the one just queued and returns.
                dn_pump(millis());
            }
        }
        if (_tcp_len >= sizeof(_tcp_buf)) _tcp_len = 0;
    }
}

static void uplink_service(uint32_t now) {
    if (_uplink.connected()) return;
    if (_tcp_len) _tcp_len = 0;                 // stale half-frame from the drop
    if (!eth_is_up() || now < _uplink_next_try_ms) return;

    if (_uplink.connect(_hub_host, HUB_PORT, 2000)) {
        _uplink.setNoDelay(true);               // jog latency matters more than packing
        _uplink_backoff_ms = 1000;
        Serial.printf("[UPLINK] connected to %s:%d\n", _hub_host, HUB_PORT);
    } else {
        _uplink_next_try_ms = now + _uplink_backoff_ms;
        if (_uplink_backoff_ms < UPLINK_BACKOFF_MAX_MS) _uplink_backoff_ms *= 2;
        Serial.printf("[UPLINK] %s:%d unreachable — retry in %lu ms\n",
                      _hub_host, HUB_PORT, (unsigned long)_uplink_backoff_ms);
    }
}

// ---------------------------------------------------------------------------

void setup() {
    Serial.begin(115200);
    delay(200);
    Serial.println("\n=== PTS satellite ===");

    _prefs.begin("sat", false);
    _prefs.getString("hubhost", _hub_host, sizeof(_hub_host));
    _prefs.end();
    if (_hub_host[0] == '\0') strncpy(_hub_host, SAT_HUB_MDNS_NAME, sizeof(_hub_host) - 1);

    // The build names a satellite, and NVS is deliberately not consulted for
    // it.  This is the hub's board: a unit demoted from hub to satellite still
    // has that hub's name in storage, and reading it here would quietly bring
    // up a satellite called "Concert Hall" with nothing on any screen to say
    // why.  Leaving the stored name untouched also means it comes back if the
    // box is ever promoted to hub again.
#ifdef SAT_NAME
    hub_name_use(SAT_NAME);
#else
    hub_name_apply();                           // unnamed: Sat-<MAC>
#endif

    WiFi.mode(WIFI_AP);
    WiFi.softAP(AP_SSID, AP_PASSWORD, AP_CHANNEL);
    esp_wifi_set_ps(WIFI_PS_NONE);              // latency over power
    Serial.printf("AP  SSID : %s  (channel %d)\n", AP_SSID, AP_CHANNEL);
    Serial.printf("AP  MAC  : %s\n", WiFi.softAPmacAddress().c_str());

    _rx_q = xQueueCreate(24, sizeof(RxItem));
    if (esp_now_init() != ESP_OK) {
        Serial.println("[ESPNOW] init failed — restarting");
        delay(500);
        ESP.restart();
    }
    esp_now_register_recv_cb(on_espnow_recv);
    esp_now_register_send_cb(on_espnow_sent);   // paces dn_pump()
    // Start the stall clock now.  Left at 0, the first NO_MEM after boot would
    // read as a five-second stall and trigger a pointless stack rebuild.
    _dn_last_ok_ms = millis();

    eth_begin();
    Serial.printf("Hub uplink: %s:%d\n", _hub_host, HUB_PORT);
}

void loop() {
    uint32_t _t0 = micros();
    uint32_t now = millis();
    eth_report_once_if_down(now, 8000);
    uplink_service(now);
    drain_espnow_to_uplink(now);
    if (_uplink.connected()) drain_uplink_to_espnow();
    peer_forget_stale(now);
    dn_pump(now);
    downlink_report(now);

    _loop_count++;
    uint32_t _dt = micros() - _t0;
    if (_dt > _loop_max_us) _loop_max_us = _dt;
}
