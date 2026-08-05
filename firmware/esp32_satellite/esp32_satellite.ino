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
#include <lwip/sockets.h>   // raw non-blocking send() for both TCP links
#include <ESPAsyncWebServer.h>

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
#include "../esp32_hub/web_app.h"   // the page itself — shared, never copied

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
// per unit; mounts find it by scanning, so nothing else needs telling — a
// channel change needs no re-pairing, because a mount matches its hub by BSSID
// and updates the stored channel when it moves.
//
// 11 rather than 6 on the strength of the boot survey, which counted 49 APs in
// this venue: 17 on ch1, 20 on ch6, 12 on ch11.  Note the survey ALSO showed
// this is at best a partial explanation — the hub sits on ch1 with 17
// neighbours and its mounts lose nothing at all, so three fewer APs cannot be
// the whole of a 1% send-failure rate.  Taken because it is the quietest of the
// three and free, not because it is known to be the cause.
#ifndef AP_CHANNEL
#define AP_CHANNEL      11
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
// Web app bridge  —  phones on this satellite's AP reach the hub through here
// ---------------------------------------------------------------------------
// An operator standing by a satellite is, by definition, a long way from the
// hub — so the hub's own AP is out of reach exactly where the web app is most
// wanted.  Bridging the AP onto the wired network would fix that and is not
// allowed here, and rightly: it would put an untrusted WiFi network on the
// Dante LAN.
//
// Instead the satellite serves the page itself and opens a SECOND connection to
// the hub, on its ordinary client port, as a perfectly ordinary client.  Phones
// never touch the wired network; the hub needs no change at all, because it
// already accepts clients speaking the raw protocol there.
//
//   [phone] --WiFi/WS--> [satellite] --TCP 7777--> [hub]
//
// The page comes from esp32_hub/web_app.h, not a copy, so the two surfaces
// cannot drift.  The URL is http://192.168.4.1 either way — a SoftAP defaults
// to that address, hub or satellite — so nothing in the documentation changes.
//
// This does make the satellite less of a dumb pipe than its header claims, and
// that is a deliberate trade for a real constraint rather than a drift.  It is
// also isolated: every failure path here leaves the ESP-NOW relay untouched,
// because a satellite that stops relaying is a mount off the air, while a
// satellite that stops serving a web page is an inconvenience.
static AsyncWebServer _http(80);
static AsyncWebSocket _ws("/ws");

// The second link, to the hub's client port, carrying the phones' traffic.
static WiFiClient _client_link;

// Hub address, cached from the first successful connection.
//
// "pts-hub.local" is resolved by an mDNS multicast query, and connect() runs
// that query INSIDE the call: the 2000 ms timeout argument bounds the TCP
// handshake, not the lookup.  The rig showed the lookup taking 9.1 seconds with
// the main loop stopped dead inside it — nothing relayed, nothing drained, no
// WebSocket serviced.  At boot that is merely slow.  On a reconnect it is an
// outage, and reconnects are exactly when it happens.
//
// So pay for the name once.  Reconnects use the address and are bounded by the
// timeout; the name is consulted again only if the address stops working, which
// is precisely when the hub has actually moved.
static IPAddress _hub_ip;
static bool      _hub_ip_valid = false;

static bool hub_connect(WiFiClient &c, uint16_t port) {
    bool ok = _hub_ip_valid ? c.connect(_hub_ip,   port, 2000)
                            : c.connect(_hub_host, port, 2000);
    if (ok && !_hub_ip_valid) {
        _hub_ip = c.remoteIP(); _hub_ip_valid = true;
        Serial.printf("[NET] %s is %s — cached, reconnects skip the lookup\n",
                      _hub_host, _hub_ip.toString().c_str());
    } else if (!ok && _hub_ip_valid) {
        _hub_ip_valid = false;        // stale — fall back to the name next try
    }
    return ok;
}
static uint32_t   _cl_next_try_ms = 0;
static uint32_t   _cl_backoff_ms  = 1000;
static uint8_t    _cl_buf[PACKET_MAX_PAYLOAD + 16];
static uint16_t   _cl_len = 0;

// WS receive queue.  on_ws_event runs in the AsyncTCP task; touching the socket
// from there would be a cross-task use of WiFiClient, so it only enqueues and
// loop() does the work — the same rule the ESP-NOW receive path follows.
struct WsRx { uint16_t len; uint8_t data[PACKET_MAX_PAYLOAD + 16]; };
static QueueHandle_t _ws_rx_q = nullptr;

static void on_ws_event(AsyncWebSocket *, AsyncWebSocketClient *client,
                        AwsEventType type, void *arg, uint8_t *data, size_t len) {
    if (type == WS_EVT_CONNECT) {
        // Drop frames rather than close a client whose queue is full: updates
        // are superseded every 100 ms, and a phone that hiccups should not be
        // disconnected mid-show.
        client->setCloseClientOnQueueFull(false);
        ws_cli_open(client->id());
        Serial.printf("[WEB] client %u connected from %s\n",
                      client->id(), client->remoteIP().toString().c_str());
    } else if (type == WS_EVT_DISCONNECT) {
        ws_cli_close(client->id());
    } else if (type == WS_EVT_DATA) {
        AwsFrameInfo *info = (AwsFrameInfo *)arg;
        if (info->final && info->index == 0 && info->len == len
                        && info->opcode == WS_BINARY
                        && len <= sizeof(((WsRx *)0)->data) && _ws_rx_q) {
            WsRx m;
            m.len = (uint16_t)len;
            memcpy(m.data, data, len);
            xQueueSend(_ws_rx_q, &m, 0);      // non-blocking; drop if full
        }
    }
}

// Connect (and reconnect) the client link.  Same backoff shape as the satellite
// link.  The backoff keeps a DOWN hub from being retried tightly, but note that
// connect() itself blocks for up to the 2000 ms timeout — and for far longer
// than that if it has to resolve the name, which is why hub_connect() caches
// the address.
static void client_link_service(uint32_t now) {
    if (_client_link.connected()) return;
    if (_cl_len) _cl_len = 0;                  // stale half-frame from the drop
    if (!eth_is_up() || now < _cl_next_try_ms) return;

    if (hub_connect(_client_link, SAT_HUB_CLIENT_PORT)) {
        _client_link.setNoDelay(true);
        _cl_backoff_ms = 1000;
        Serial.printf("[WEB] hub client link up (%s:%d)\n",
                      _hub_host, SAT_HUB_CLIENT_PORT);
    } else {
        _cl_next_try_ms = now + _cl_backoff_ms;
        if (_cl_backoff_ms < UPLINK_BACKOFF_MAX_MS) _cl_backoff_ms *= 2;
    }
}

// phones -> hub.  Raw non-blocking send for the reason documented on the uplink:
// NetworkClient::write() cannot be bounded and will happily block for a minute.
static void drain_ws_to_hub() {
    if (!_ws_rx_q) return;
    WsRx m;
    while (xQueueReceive(_ws_rx_q, &m, 0) == pdTRUE) {
        if (!_client_link.connected()) continue;       // drop; nothing to queue for
        int fd = _client_link.fd();
        if (fd >= 0) ::send(fd, m.data, m.len, MSG_DONTWAIT);
    }
}

// Per-mount STATUS throttle for the WebSocket, mirroring the hub's.  Its own
// comment explains why, and it is not a performance nicety:
//
//   "rate-limit to 5 Hz when idle.  This prevents TCP ACK starvation that
//    previously caused lwIP to close the connection."
//
// The hub found that and fixed it before the web app shipped.  Forwarding the
// raw TCP stream to binaryAll() here reproduced it exactly: a phone that
// connected, was buried in STATUS at full rate from every mount, and had its
// socket closed under it within a second — over and over, with the page showing
// "reconnecting" and the client IDs climbing.  The frames were not the phone's
// to refuse; lwIP dropped the connection.
//
// STATUS is the firehose and the only thing throttled.  Everything else -
// acks, state reports, subject lists, pairing, hub telemetry - passes straight
// through, because none of it repeats at 10 Hz per mount.
#define WS_STATUS_INTERVAL_MS  200          // 5 Hz, same as the hub
static uint32_t _ws_status_ms[NUM_MOUNTS] = {};

// ---- WebSocket diagnostics -------------------------------------------------
// Phones were connecting and vanishing inside a second, and the two obvious
// explanations are indistinguishable from the outside: the send queue backing
// up (our fault, fix the rate) or the AP dropping the station (RF, fix the
// placement or channel).  Both produce "client N connected / client N
// disconnected" and nothing else.  These separate them.
//
// _ws_full counts frames we declined to queue because the queue was already
// full.  If that stays at 0 while clients still drop, queue pressure is NOT
// the cause and the answer is at the WiFi layer — see the AP station events.
static uint32_t _ws_sent = 0, _ws_full = 0;

struct WsCli { uint32_t id, at, sent; };
static WsCli _ws_cli[4] = {};

static void ws_cli_open(uint32_t id) {
    for (auto &c : _ws_cli)
        if (c.id == 0) { c = { id, millis(), _ws_sent }; return; }
    _ws_cli[0] = { id, millis(), _ws_sent };      // table full — reuse
}

// Lifetime and how much we actually pushed at it, which is the number that says
// whether it drowned or was cut off while idle.
static void ws_cli_close(uint32_t id) {
    for (auto &c : _ws_cli)
        if (c.id == id) {
            Serial.printf("[WEB] client %u disconnected after %lu ms, "
                          "%lu frames sent\n", id,
                          (unsigned long)(millis() - c.at),
                          (unsigned long)(_ws_sent - c.sent));
            c.id = 0; return;
        }
    Serial.printf("[WEB] client %u disconnected\n", id);
}

// True if this frame should reach the phones now.
static bool ws_should_send(const uint8_t *f, uint16_t len) {
    if (len < 7) return false;
    uint8_t mid = f[3];
    uint8_t cmd = f[6];
    if (cmd != CMD_STATUS) return true;                 // not the firehose
    if (mid < 1 || mid > NUM_MOUNTS) return true;
    uint32_t t = millis();
    if (t - _ws_status_ms[mid - 1] < WS_STATUS_INTERVAL_MS) return false;
    _ws_status_ms[mid - 1] = t;
    return true;
}

// hub -> phones.  The hub writes a byte stream; the web app expects one packet
// per WebSocket frame, so it is reframed here rather than forwarded raw.
static void drain_hub_to_ws() {
    while (_client_link.available()) {
        int c = _client_link.read();
        if (c < 0) break;
        if (_cl_len == 0 && c != PKT_START_1) continue;
        if (_cl_len == 1 && c != PKT_START_2) { _cl_len = 0; continue; }
        _cl_buf[_cl_len++] = (uint8_t)c;
        if (_cl_len >= 3) {
            uint16_t want = 3 + _cl_buf[2] + 2;        // hdr + LEN body + CRC
            if (want > sizeof(_cl_buf)) { _cl_len = 0; continue; }
            if (_cl_len == want) {
                if (_ws.count() && ws_should_send(_cl_buf, _cl_len)) {
                    // Skip rather than queue when full.  The client is set to
                    // drop-not-close, so this only changes where the frame is
                    // discarded — but it makes the pressure countable.
                    if (_ws.availableForWriteAll()) {
                        _ws.binaryAll(_cl_buf, _cl_len); _ws_sent++;
                    } else _ws_full++;
                }
                _cl_len = 0;
            }
        }
        if (_cl_len >= sizeof(_cl_buf)) _cl_len = 0;
    }
}

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

// Same 11b / 1 Mbps long-preamble rate the hub and the mounts already use for
// their peers - roughly 6-10 dB of link budget over the default.  The satellite
// was the only node on the rig NOT doing this, so its downlink was the least
// robust hop in a chain where every other one had been deliberately hardened.
// An oversight from writing this as a "dumb pipe": the pipe still has a radio.
// Must be called for each peer, after esp_now_add_peer(), and again after any
// reinit - the rate config does not survive esp_now_deinit().
static void espnow_peer_long_range(const uint8_t *mac) {
    esp_now_rate_config_t rate = {};
    rate.phymode = WIFI_PHY_MODE_11B;
    rate.rate    = WIFI_PHY_RATE_1M_L;
    rate.ersu    = false;
    rate.dcm     = false;
    esp_now_set_peer_rate_config(mac, &rate);
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
        espnow_peer_long_range(mac);
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
static uint32_t _up_dropped   = 0;   // uplink frames the hub would not take

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
                // A single non-blocking send on the raw socket, NOT
                // NetworkClient::write().  That function cannot be bounded from
                // outside: its send() uses MSG_DONTWAIT, so SO_SNDTIMEO is never
                // consulted (setting it, as this code did, achieves nothing) and
                // the waiting happens in a select() of 1 s, up to 10 retries —
                // with a PARTIAL write resetting the retry count.  A socket that
                // drains slowly therefore loops indefinitely: seven rounds of
                // partial progress is where the measured 70-second loop pass
                // came from.
                //
                // One send, no loop.  If it will not all go now it does not go:
                // this is telemetry, superseded every 100 ms, and the box
                // staying responsive is worth more than any single frame.
                int sfd = _uplink.fd();
                int  w  = (sfd >= 0) ? ::send(sfd, env, n, MSG_DONTWAIT) : -1;
                _up_writes++;
                // A partial send leaves a truncated envelope on the wire; the
                // hub's parser hunts for the 0xA5/0x5A sync and picks up at the
                // next frame, so it costs one frame rather than the stream.
                if (w != (int)n) _up_dropped++;
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
        espnow_peer_long_range(_peer[i].mac);
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
                  "uplink %lu writes, %lu dropped | ws %lu sent, %lu queue-full\n",
                  (unsigned long)(_loop_count / (secs ? secs : 1)),
                  (unsigned long)_loop_max_us, (unsigned long)_dn_nomem,
                  (unsigned long)_up_writes, (unsigned long)_up_dropped,
                  (unsigned long)_ws_sent, (unsigned long)_ws_full);
    _loop_count = _loop_max_us = _dn_nomem = _up_writes = _up_dropped = 0;
    _ws_sent = _ws_full = 0;

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

    if (hub_connect(_uplink, HUB_PORT)) {
        _uplink.setNoDelay(true);               // jog latency matters more than packing
        // NOTE: setting SO_SNDTIMEO here would do nothing.  NetworkClient's
        // send() passes MSG_DONTWAIT, so the option is never consulted — the
        // uplink is bounded by sending on the raw fd instead, in
        // drain_espnow_to_uplink().
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

    // Station events, because a phone leaving the AP and a phone whose
    // WebSocket died look identical from the application's side.  The reason
    // code distinguishes them and is the difference between "fix the code" and
    // "move the satellite": 8 is the station leaving of its own accord, 4 is an
    // inactivity timeout, 15 a 4-way handshake failure, 2 an auth expiry.  The
    // last three are RF, not software.
    WiFi.onEvent([](arduino_event_id_t, arduino_event_info_t info) {
        const uint8_t *m = info.wifi_ap_stadisconnected.mac;
        Serial.printf("[AP] station %02X:%02X:%02X:%02X:%02X:%02X left "
                      "(reason %u)\n", m[0], m[1], m[2], m[3], m[4], m[5],
                      (unsigned)info.wifi_ap_stadisconnected.reason);
    }, ARDUINO_EVENT_WIFI_AP_STADISCONNECTED);

    WiFi.mode(WIFI_AP);
    WiFi.softAP(AP_SSID, AP_PASSWORD, AP_CHANNEL);
    esp_wifi_set_ps(WIFI_PS_NONE);              // latency over power
    Serial.printf("AP  SSID : %s  (channel %d)\n", AP_SSID, AP_CHANNEL);
    Serial.printf("AP  MAC  : %s\n", WiFi.softAPmacAddress().c_str());

    // One-shot channel survey.  A mount talking to this satellite was failing
    // ~10 sends a minute while two mounts on the hub's channel failed none at
    // all, and the obvious difference is which channel each cell sits on - but
    // "channel 6 is usually busy" is folklore, not a measurement.  So count
    // what is actually on the air here and let the number choose.
    //
    // Done once, at boot, before any mount has attached: a scan takes the radio
    // off-channel for a second or two, which is free now and would not be later.
    {
        int n = WiFi.scanNetworks(false /*blocking*/, false /*no hidden*/);
        if (n > 0) {
            uint8_t per_ch[14] = {};
            int8_t  best_rssi[14];
            for (int c = 0; c < 14; c++) best_rssi[c] = -128;
            for (int i = 0; i < n; i++) {
                int c = WiFi.channel(i);
                if (c < 1 || c > 13) continue;
                per_ch[c]++;
                if ((int8_t)WiFi.RSSI(i) > best_rssi[c]) best_rssi[c] = (int8_t)WiFi.RSSI(i);
            }
            // Report the three non-overlapping channels plus our own, since
            // those are the only realistic choices.
            Serial.printf("[SURVEY] %d APs seen | ch1: %d (max %d dBm) | "
                          "ch6: %d (max %d dBm) | ch11: %d (max %d dBm)\n",
                          n, per_ch[1], best_rssi[1], per_ch[6], best_rssi[6],
                          per_ch[11], best_rssi[11]);
            Serial.printf("[SURVEY] we are on channel %d with %d other AP(s) "
                          "on it%s\n", AP_CHANNEL, per_ch[AP_CHANNEL],
                          per_ch[AP_CHANNEL] ? " — try a quieter one if sends keep failing" : "");
        } else {
            Serial.println("[SURVEY] no other APs visible — channel choice is unlikely to matter");
        }
        WiFi.scanDelete();
        // The scan leaves the radio wherever it finished; put it back.
        esp_wifi_set_channel(AP_CHANNEL, WIFI_SECOND_CHAN_NONE);
    }

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

    // ---- Web app for phones on this satellite's AP ----
    // Last in setup() deliberately: everything the relay needs is already
    // running by here, so if any of this misbehaves the satellite still does
    // its actual job.
    _ws_rx_q = xQueueCreate(16, sizeof(WsRx));
    _ws.onEvent(on_ws_event);
    _http.addHandler(&_ws);
    _http.on("/", HTTP_GET, [](AsyncWebServerRequest *req) {
        // The (const uint8_t*, len) overload, NOT the const char* one — see the
        // long note at the matching handler in esp32_hub_eth.ino.  The char*
        // form copies the whole 130 KB page into one contiguous heap block,
        // which fails on a chip already holding WiFi, ESP-NOW and AsyncTCP and
        // ships a blank 200 instead of an error.
        req->send(200, "text/html", (const uint8_t *)WEB_APP_HTML,
                  sizeof(WEB_APP_HTML) - 1);
    });
    _http.begin();
    Serial.printf("Web app   : http://%s/  (join WiFi \"%s\")\n",
                  WiFi.softAPIP().toString().c_str(), AP_SSID);
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

    // Web app bridge.  After the relay work, never before it: a phone refreshing
    // a page must not delay a mount's commands.
    client_link_service(now);
    drain_ws_to_hub();
    if (_client_link.connected()) drain_hub_to_ws();
    // Required every loop by the mathieucarbou fork, and omitting it is not
    // subtle: closed clients are never freed, DEFAULT_MAX_WS_CLIENTS (8) fills
    // after a handful of reconnects, and every new connection is then closed
    // the moment it opens.  A phone shows "reconnecting" on a loop while the
    // client IDs climb — which is precisely what it did.
    _ws.cleanupClients();

    downlink_report(now);

    _loop_count++;
    uint32_t _dt = micros() - _t0;
    if (_dt > _loop_max_us) _loop_max_us = _dt;
}
