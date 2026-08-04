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
            if (n) _uplink.write(env, n);
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
static uint32_t _dn_last_report_ms = 0;
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
#define DN_QUEUE_DEPTH   16
struct DnFrame { uint8_t mac[6]; uint16_t len; uint8_t data[PACKET_MAX_PAYLOAD + 16]; };
static DnFrame  _dn_q[DN_QUEUE_DEPTH];
static uint8_t  _dn_head = 0, _dn_tail = 0;
static volatile bool _dn_in_flight = false;
static uint32_t _dn_sent_ms = 0;
static uint32_t _dn_overflow = 0;
#define DN_IN_FLIGHT_TIMEOUT_MS  200   // a send that never reports back

static void on_espnow_sent(const wifi_tx_info_t *info, esp_now_send_status_t status) {
    (void)info; (void)status;          // delivery is the mount's business, not ours
    _dn_in_flight = false;
}

// Push onto the ring.  Dropping the OLDEST on overflow, not the newest: these
// are control commands, and the most recent one is the operator's latest
// intention — a stale jog is worth less than the stop that followed it.
static void dn_enqueue(const uint8_t *mac, const uint8_t *frame, uint16_t len) {
    if (len > sizeof(((DnFrame *)0)->data)) return;
    uint8_t next = (uint8_t)((_dn_head + 1) % DN_QUEUE_DEPTH);
    if (next == _dn_tail) {
        _dn_tail = (uint8_t)((_dn_tail + 1) % DN_QUEUE_DEPTH);
        _dn_overflow++;
    }
    memcpy(_dn_q[_dn_head].mac, mac, 6);
    memcpy(_dn_q[_dn_head].data, frame, len);
    _dn_q[_dn_head].len = len;
    _dn_head = next;
}

// Called from loop().  One frame at a time, next only once the driver has
// reported the last one done — or timed out, so a lost callback cannot wedge
// the queue permanently.
static void dn_pump(uint32_t now) {
    if (_dn_head == _dn_tail) return;
    if (_dn_in_flight) {
        if (now - _dn_sent_ms < DN_IN_FLIGHT_TIMEOUT_MS) return;
        _dn_in_flight = false;                 // assume lost, carry on
    }
    DnFrame &f = _dn_q[_dn_tail];
    esp_err_t e = esp_now_send(f.mac, f.data, f.len);
    if (e == ESP_ERR_ESPNOW_NO_MEM) return;    // still saturated — hold position
    _dn_tail = (uint8_t)((_dn_tail + 1) % DN_QUEUE_DEPTH);
    if (e == ESP_OK) {
        _dn_sent++;
        _dn_in_flight = true;
        _dn_sent_ms   = now;
    } else {
        _dn_send_err++;
        Serial.printf("[DOWN] send failed: %s\n", esp_err_to_name(e));
    }
}

static void forward_frame(const uint8_t *frame, uint16_t len) {
    uint8_t mid = frame_mount_id(frame, len);
    if (mid == MOUNT_BROADCAST) {
        for (int i = 0; i < NUM_MOUNTS; i++)
            if (_peer[i].used) dn_enqueue(_peer[i].mac, frame, len);
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
    dn_enqueue(_peer[mid - 1].mac, frame, len);
}

// Called from loop().  Silent while nothing is being dropped, so this cannot
// bury the log the way an unconditional heartbeat would.
static void downlink_report(uint32_t now) {
    if (now - _dn_last_report_ms < DN_REPORT_MS) return;
    _dn_last_report_ms = now;
    if (!_dn_no_peer && !_dn_send_err && !_dn_overflow) return;
    Serial.printf("[DOWN] %lu delivered, %lu dropped (mount not in our peer "
                  "table), %lu send errors, %lu shed from a full queue\n",
                  (unsigned long)_dn_sent, (unsigned long)_dn_no_peer,
                  (unsigned long)_dn_send_err, (unsigned long)_dn_overflow);
    _dn_no_peer = _dn_send_err = _dn_overflow = 0;
}

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
            if (_tcp_len == want) { forward_frame(_tcp_buf, _tcp_len); _tcp_len = 0; }
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

    eth_begin();
    Serial.printf("Hub uplink: %s:%d\n", _hub_host, HUB_PORT);
}

void loop() {
    uint32_t now = millis();
    eth_report_once_if_down(now, 8000);
    uplink_service(now);
    drain_espnow_to_uplink(now);
    if (_uplink.connected()) drain_uplink_to_espnow();
    peer_forget_stale(now);
    dn_pump(now);
    downlink_report(now);
}
