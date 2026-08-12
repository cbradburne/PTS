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
#include <errno.h>          // why a non-blocking send refused, not just that it did
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
// ---------------------------------------------------------------------------
// Diagnostic telemetry (PTS_DIAG)
// ---------------------------------------------------------------------------
// Scaffolding, not instrumentation the firmware needs to run.  These lines were
// added to answer specific questions — why phones lost their WebSocket, whether
// a client was drowning or being dropped — and they answered them.  They print
// unconditionally on a port someone may be watching for something else, so they
// are compiled out unless asked for:
//
//     DIAG=1 ./tools/build.sh sat
//
// OFF by default: a shipped rig should not be narrating itself, and these lines
// are noise to anyone watching the port for something else.  Turn them back on
// for a debugging session with DIAG=1.
//
// What makes that safe is that FAULT reports are never gated — [DOWN], ESP-NOW
// errors, link up/down all still print.  A quiet build still says when
// something breaks; it just stops saying when nothing does.
#ifndef PTS_DIAG
#define PTS_DIAG 0
#endif
#if PTS_DIAG
  #define DIAG_PRINTF(...)  Serial.printf(__VA_ARGS__)
#else
  #define DIAG_PRINTF(...)  ((void)0)
#endif

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
        DIAG_PRINTF("[WEB] client %u connected from %s\n",
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
static uint32_t _ws_sent = 0, _ws_full = 0, _ws_pkts = 0;

// Free-running, never zeroed.  The per-client totals below are differences
// taken across a connection, and _ws_sent is reset at every [RATE] report — so
// differencing it reported only as far back as the last reset.  A 35-second
// session that really sent 234 frames was logged as 4.
static uint32_t _ws_frames_total = 0;

// Coalescing buffer, hub -> phones.
//
// Measured on the rig: 44-70 WebSocket messages per second at one phone, each
// its own TCP segment and its own 802.11 frame with its own ACK, on a channel
// shared with twelve other APs and with this board's own ESP-NOW.  The send
// queue backed up (45-64 queue-full per 30 s) and the main loop went from a
// 19 ms worst pass to 64 ms the moment a phone connected.
//
// The hub never had this problem because it forwards a whole relay message at a
// time, several packets to a frame.  Splitting that stream into one frame per
// packet — which this did, on the false premise that the web app needed it —
// multiplied the frame count for no gain.  onPkt() in web_app.h walks the whole
// buffer and always has:
//
//     "The hub may concatenate multiple Teensy packets into one WebSocket
//      frame ... Walk the entire buffer so every packet is processed."
//
// So batch them again.  40 ms adds nothing a viewer can perceive to a status
// display, and control travels the other way, untouched by this.
#define WS_COALESCE_MS   40
#define WS_COALESCE_MAX  1024

static uint8_t  _ws_agg[WS_COALESCE_MAX];
static uint16_t _ws_agg_len   = 0;
static uint32_t _ws_agg_since = 0;

static void ws_flush() {
    if (!_ws_agg_len) return;
    if (_ws.count()) {
        // Skip rather than queue when full: the client is set to drop-not-close,
        // so this only decides where the frame is discarded — and makes the
        // pressure countable.
        if (_ws.availableForWriteAll()) { _ws.binaryAll(_ws_agg, _ws_agg_len);
                                          _ws_sent++; _ws_frames_total++; }
        else _ws_full++;
    }
    _ws_agg_len = 0;
}

static void ws_queue(const uint8_t *f, uint16_t len) {
    if (len > WS_COALESCE_MAX) return;                 // cannot batch; drop
    if (_ws_agg_len + len > WS_COALESCE_MAX) ws_flush();
    if (_ws_agg_len == 0) _ws_agg_since = millis();
    memcpy(_ws_agg + _ws_agg_len, f, len);
    _ws_agg_len += len;
    _ws_pkts++;
}

struct WsCli { uint32_t id, at, sent; };
static WsCli _ws_cli[4] = {};

static void ws_cli_open(uint32_t id) {
    for (auto &c : _ws_cli)
        if (c.id == 0) { c = { id, millis(), _ws_frames_total }; return; }
    _ws_cli[0] = { id, millis(), _ws_frames_total };   // table full — reuse
}

// Lifetime and how much we actually pushed at it, which is the number that says
// whether it drowned or was cut off while idle.
static void ws_cli_close(uint32_t id) {
    for (auto &c : _ws_cli)
        if (c.id == id) {
            DIAG_PRINTF("[WEB] client %u disconnected after %lu ms, "
                          "%lu frames sent\n", id,
                          (unsigned long)(millis() - c.at),
                          (unsigned long)(_ws_frames_total - c.sent));
            c.id = 0; return;
        }
    DIAG_PRINTF("[WEB] client %u disconnected\n", id);
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

// hub -> phones.  The hub writes a byte stream, so packet boundaries are
// recovered here — but they are then re-batched by ws_queue() rather than sent
// one frame per packet.  The web app parses several packets from one frame and
// always could; sending them individually was pure overhead.
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
                if (_ws.count() && ws_should_send(_cl_buf, _cl_len))
                    ws_queue(_cl_buf, _cl_len);
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
// starved by drain_espnow_to_uplink().  That used to do a TCP write per uplink
// frame while a mount streams STATUS at ~10 Hz; it now batches them, so this
// candidate is far weaker than when the note was written.
static uint32_t _loop_count   = 0;   // passes since the last report
static uint32_t _loop_max_us  = 0;   // slowest single pass
// Set when a pass is dominated by a known blocking call that has already
// reported its own cost, so it is not double-counted as a stall.
static bool     _skip_pass_max = false;
static uint32_t _dn_nomem     = 0;   // esp_now_send() refusals
// Cumulative twins of the counters above, kept for the health record.  Every
// counter in this block is WINDOWED — downlink_report() zeroes them each
// DN_REPORT_MS so the [RATE] line reads as a rate — and a health line built
// from those would sawtooth back to zero every window.  That is precisely the
// shape a fault looks like when it clears itself, so it would get read as one.
static uint32_t _sat_nomem_total   = 0;   // send refusals since boot
// What the relay was ASKED to do, against what the radio took.  Only refusals
// were ever counted, and a refusal count alone cannot be read: this box
// restarted itself 36 times in 17 hours with ~1100 refusals before each, and
// 1100 out of 1150 offered is a radio that stopped while 1100 out of a million
// is a relay being flooded.  Opposite diagnoses, opposite fixes, and nothing in
// the log could separate them.
static uint32_t _sat_dn_offered    = 0;   // frames the hub gave us to relay
static uint32_t _sat_dn_attempts   = 0;   // esp_now_send() calls — see below
static uint32_t _sat_dn_sent_total = 0;   // ...of which returned ESP_OK
static uint32_t _sat_unacked_total = 0;   // downlink sends no mount acked, since boot
static uint32_t _sat_loopmax_ms    = 0;   // worst single pass since boot
static uint32_t _up_writes    = 0;   // actual send() calls (batches, not frames)
static uint32_t _up_frames    = 0;   // envelopes queued — comparable to the old count
static uint32_t _up_dropped   = 0;   // uplink frames the hub would not take
static uint32_t _up_drop_full = 0;   // ...refused outright (send() < 0)
static uint32_t _up_drop_part = 0;   // ...taken in part, truncated on the wire
static int      _up_errno     = 0;   // last refusal reason; EAGAIN(11) = buffer full

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

// Coalesce uplink envelopes into one write instead of one write per frame.
//
// The measurement that forced this: ~1900 envelopes per 30 s (63/s) at ~50
// bytes each is about 3 KB/s — nothing for a W5500 — yet a quarter to a third
// of them came back EAGAIN, every single one refused OUTRIGHT with not one
// partial write in ten windows.  A link refusing whole frames while carrying
// 3 KB/s is not short of bandwidth; it is short of QUEUE.
//
// setNoDelay(true) makes every one of those tiny frames its own TCP segment,
// and lwIP bounds its send queue by segment COUNT, not bytes.  With the hub's
// delayed ACK running to ~200 ms, a dozen or more unacked segments pile up
// against a limit around 8-16, the queue overflows, and it recovers the moment
// an ACK lands — which is exactly the bursty 16-34% pattern that was logged.
//
// Batching attacks the count directly: same bytes, a third of the segments.
// It also mirrors what the WebSocket side already does for the same reason,
// WS_COALESCE_MS.
//
// The cost is up to UPLINK_COALESCE_MS of latency on mount telemetry, which is
// invisible against STATUS being superseded every 100 ms.  The DOWNLINK is
// untouched — those are the hub's sends, and [DOWN] was already clean.
#define UPLINK_COALESCE_MS  20
// One MSS-ish.  Going over just makes lwIP split it again, which is the thing
// being avoided.
#define UPLINK_AGG_MAX      1200

static uint8_t  _up_agg[UPLINK_AGG_MAX + SAT_ENV_MAX];
static uint16_t _up_agg_len    = 0;
static uint16_t _up_agg_frames = 0;   // envelopes in the batch, for the drop count
static uint32_t _up_agg_since  = 0;

static void uplink_flush(uint32_t now) {
    if (!_up_agg_len) return;
    if (!_uplink.connected()) {          // nowhere to send it; do not hoard stale telemetry
        _up_agg_len = _up_agg_frames = 0;
        return;
    }
    int sfd = _uplink.fd();
    int  w  = (sfd >= 0) ? ::send(sfd, _up_agg, _up_agg_len, MSG_DONTWAIT) : -1;
    _up_writes++;
    if (w != (int)_up_agg_len) {
        // Frames, not batches, so this number stays comparable with the
        // per-frame counts from before coalescing.
        _up_dropped += _up_agg_frames;
        if (w < 0) { _up_drop_full++; _up_errno = errno; }
        else         _up_drop_part++;
    }
    _up_agg_len = _up_agg_frames = 0;
    _up_agg_since = now;
}

static void drain_espnow_to_uplink(uint32_t now) {
    RxItem it;
    while (xQueueReceive(_rx_q, &it, 0) == pdTRUE) {
        peer_learn(frame_mount_id(it.data, it.len), it.mac, now);
        if (_uplink.connected()) {
            uint8_t env[SAT_ENV_MAX];
            uint16_t n = sat_env_build(env, it.mac, it.rssi, it.data, it.len);
            if (n) {
                if (_up_agg_len + n > UPLINK_AGG_MAX) uplink_flush(now);
                memcpy(_up_agg + _up_agg_len, env, n);
                _up_agg_len += n;
                _up_agg_frames++;
                _up_frames++;
                if (!_up_agg_since) _up_agg_since = now;
            }
        }
        // Not connected: drop.  Buffering telemetry to replay later would
        // deliver a burst of stale STATUS the moment the hub reappears, and
        // STATUS is superseded every 100 ms anyway.
    }
    // Age the batch out even while nothing new arrives, or the last frames
    // before a lull would sit here until the next one — indefinitely, on an
    // idle rig.  The same trap ws_flush() exists to avoid.
    if (_up_agg_len && (now - _up_agg_since) >= UPLINK_COALESCE_MS) uplink_flush(now);
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
// Recovery attempts are timed SEPARATELY from the last good send.  Stamping
// _dn_last_ok_ms when a rebuild was merely attempted made the rebuild reset its
// own trigger — and the drop timer above it, which shares that variable — so
// the queue could never drain and the interval restarted from zero every time.
// The rig showed the result: "ESP-NOW stalled - reinitialising" every five
// seconds without pause, "sent" frozen at the same value across four
// consecutive reports, and nomem running to 2,254 per 30 s.
static uint32_t _dn_last_recover_ms = 0;
// ...and a rebuild that has not helped after this many tries is not going to.
// Tearing the stack down destroys whatever is in flight, so repeating it
// forever makes a bad radio state worse rather than better.  Past the cap the
// satellite sheds frames and says so, which is honest and lets the mounts age
// out and go looking for another hub instead of waiting on a link that is never
// coming back.  Mirrors ESPNOW_RESTART_MAX on the mount, for the same reason.
#define DN_RECOVER_MAX  5
static uint8_t  _dn_recover_run = 0;

// ...and if the rebuilds do not clear it, RESTART, rather than shed for ever.
//
// Shedding was the old end of the ladder, on the reasoning that the mounts
// would "age out and go looking for another hub".  Watched on a rig, they do
// not: the AP is still beaconing and RECEIVE still works, so a satellite with a
// dead transmit path looks like a perfectly good base.  A mount goes quiet,
// rescans, picks the same satellite as the strongest known base, and lands back
// on the same dead path.  Measured 2026-08-10: sent frozen at 363,019 for ten
// minutes, ~200 nomem per 30 s, uplink still carrying 900 frames a window, and
// two mounts unreachable until the box was power-cycled by hand.
//
// A reboot is the one remedy that reliably clears it — it did, immediately —
// and it costs a few seconds of relay against a link that is otherwise gone
// until somebody notices.  The mount already restarts itself when isolated and
// the hub has its own ladder; the satellite was the one node that could fail
// silently for ever.
// Was 60 s, cut to 15 after watching it happen. The rebuilds take ~30 s to
// exhaust and have never once cleared this, so the grace after them was 60 s of
// known-dead air on top: a 90-second outage every six minutes. 15 s still guards
// against restarting over a brief hiccup, and takes the outage to ~45 s.
#define DN_RESTART_AFTER_MS  (15UL * 1000UL)   // wedged this long past the cap
// Boot-loop guard, same shape as the hub's.  RTC_NOINIT survives a restart but
// is undefined after a power-on, hence the magic.
#define SAT_RST_MAGIC          0x5A7E11E0UL
#define SAT_RESTART_MAX_STREAK 3
// Give the quota back once the box has been UP this long.
//
// 5 minutes, chosen against the observed failure: the stall recurs about every
// six minutes, so a longer window would never be reached and the streak would
// creep to the cap exactly as it did — three restarts last night, then
// permanently shedding, with both mounts unreachable until someone power-cycled
// it.  A box that manages five minutes of real service is not boot-looping; it
// is working badly, and it should keep rescuing itself indefinitely rather than
// give up.  A genuine boot loop never reaches five minutes, so the cap still
// catches that.
//
// Cleared on UPTIME, not on "wedge-free", for the reason the hub already
// records: requiring wedge-free deadlocks a persistently wedged box, because
// the wedge keeps restamping the timer and the streak can never clear.
#define SAT_HEALTHY_CLEAR_MS   (5UL * 60UL * 1000UL)
RTC_NOINIT_ATTR static uint32_t _sat_rst_magic;
RTC_NOINIT_ATTR static uint32_t _sat_rst_streak;
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
    else                              { _dn_unacked++; _sat_unacked_total++; }
}

// Push onto the ring.  Dropping the OLDEST on overflow, not the newest: these
// are control commands, and the most recent one is the operator's latest
// intention — a stale jog is worth less than the stop that followed it.
static void dn_enqueue(uint8_t idx, const uint8_t *frame, uint16_t len) {
    if (len > sizeof(((DnFrame *)0)->data)) return;
    _sat_dn_offered++;
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
        // Counted per CALL, not per frame: a NO_MEM makes the pump hold this
        // frame and try it again next pass, so one stuck frame can raise the
        // refusal count without limit.  attempts vs refusals is what tells the
        // two apart, and it is the whole reason this counter exists separately
        // from _sat_dn_offered.
        _sat_dn_attempts++;
        esp_err_t e = esp_now_send(_peer[f.idx].mac, f.data, f.len);
        if (e == ESP_ERR_ESPNOW_NO_MEM) {
            _dn_nomem++;
            _sat_nomem_total++;
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
            if (now - _dn_last_recover_ms > DN_NOMEM_RECOVER_MS) {
                _dn_last_recover_ms = now;          // paces the attempts only
                if (_dn_recover_run < DN_RECOVER_MAX) {
                    _dn_recover_run++;
                    espnow_recover();
                } else if (_dn_recover_run == DN_RECOVER_MAX) {
                    _dn_recover_run++;              // say this once
                    Serial.printf("[DOWN] %d rebuilds did not clear the stall — "
                                  "restarting in %lus if it does not clear\n",
                                  DN_RECOVER_MAX,
                                  (unsigned long)(DN_RESTART_AFTER_MS / 1000UL));
                } else if (now - _dn_last_ok_ms > DN_RESTART_AFTER_MS) {
                    // Nothing has been accepted by the radio for a full minute
                    // past the rebuilds.  Restart — see DN_RESTART_AFTER_MS.
                    if (_sat_rst_streak < SAT_RESTART_MAX_STREAK) {
                        _sat_rst_streak++;
                        Serial.printf("[DOWN] still stalled — RESTARTING "
                                      "(attempt %lu of %d)\n",
                                      (unsigned long)_sat_rst_streak,
                                      SAT_RESTART_MAX_STREAK);
                        Serial.flush();
                        delay(50);
                        esp_restart();
                    } else {
                        static bool said = false;
                        if (!said) {
                            said = true;
                            Serial.printf("[DOWN] %d restarts did not clear it — "
                                          "staying up and shedding.  This needs a "
                                          "human: check the radio and the channel.\n",
                                          SAT_RESTART_MAX_STREAK);
                        }
                    }
                }
            }
            return;
        }
        _dn_tail = (uint8_t)((_dn_tail + 1) % DN_QUEUE_DEPTH);
        if (e == ESP_OK) {
            _dn_sent++;
            _sat_dn_sent_total++;
            _dn_last_ok_ms = now;
            _dn_recover_run = 0;        // a real send proves the stack is back
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
// Hand the restart quota back after a healthy spell.  Its absence was the bug:
// the constant existed, the reasoning was written down, and nothing ever called
// it — so the streak only counted up, reached the cap of 3, and left the box
// shedding for good with two mounts unreachable.
static void sat_restart_streak_poll(uint32_t now) {
    if (_sat_rst_streak && now >= SAT_HEALTHY_CLEAR_MS) {
        Serial.printf("[SELF] %lu min up — clearing self-restart streak (%lu)\n",
                      (unsigned long)(SAT_HEALTHY_CLEAR_MS / 60000UL),
                      (unsigned long)_sat_rst_streak);
        _sat_rst_streak = 0;
    }
}

static void downlink_report(uint32_t now) {
    if (now - _dn_last_report_ms < DN_REPORT_MS) return;
    _dn_last_report_ms = now;
    // Throughput first: printed unconditionally alongside any downlink trouble,
    // because "how fast can this box actually push frames" is the question the
    // shed count cannot answer on its own.
    uint32_t secs = DN_REPORT_MS / 1000UL;
    DIAG_PRINTF("[RATE] %lu loops/s (slowest pass %lu us) | %lu nomem | "
                  "uplink %lu frames in %lu sends, %lu dropped (%lu refused errno %d, %lu partial) | "
                  "ws %lu pkts in %lu frames, %lu queue-full\n",
                  (unsigned long)(_loop_count / (secs ? secs : 1)),
                  (unsigned long)_loop_max_us, (unsigned long)_dn_nomem,
                  (unsigned long)_up_frames, (unsigned long)_up_writes,
                  (unsigned long)_up_dropped,
                  (unsigned long)_up_drop_full, _up_errno,
                  (unsigned long)_up_drop_part,
                  (unsigned long)_ws_pkts, (unsigned long)_ws_sent,
                  (unsigned long)_ws_full);
    _loop_count = _loop_max_us = _dn_nomem = _up_writes = _up_dropped = 0;
    _up_frames = 0;
    _up_drop_full = _up_drop_part = 0;
    _ws_sent = _ws_full = _ws_pkts = 0;

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

// Tell the hub what this satellite is called, so a client can say "via Foyer"
// rather than "via SAT 2" — the slot number is TCP accept order and means
// nothing to someone standing in the building.
//
// Sent on every connect rather than once at boot: the slot is assigned at
// accept time and a reconnect can land in a different one, so the name has to
// arrive with the connection it describes.  If it is ever lost the hub simply
// shows the slot number, which is what it did before.
//
// The envelope's MAC field is this satellite's own.  It has to be something,
// and its own address is the honest answer — the hub recognises the frame by
// its command and never looks the MAC up.
static void uplink_send_hello() {
    uint8_t name[SAT_HELLO_PAYLOAD_LEN] = {};
    snprintf((char *)name, sizeof(name), "%s", _hub_name);

    uint8_t  frame[PKT_BUF_SIZE + 4];
    uint16_t fn = build_packet(frame, 0, 0, CMD_SAT_HELLO, name, sizeof(name));

    uint8_t  mac[6];
    WiFi.softAPmacAddress(mac);
    uint8_t  env[SAT_ENV_MAX];
    uint16_t en = sat_env_build(env, mac, 0, frame, fn);
    if (en) _uplink.write(env, en);
    Serial.printf("[UPLINK] introduced myself as \"%s\"\n", _hub_name);
}

// The satellite's own health, on the same 10 s cadence and in the same record
// every other node uses.
//
// Until now it sent none.  Every counter this box keeps went to its serial
// port, and its serial port is behind the box, in a foyer, with nobody at it —
// so the node that relays traffic for half the rig, that wedged badly enough to
// need a self-restart ladder, and whose reboot makes every mount rescan, was
// the one node invisible in comms.log.  Its uptime, heap and refusals were all
// tracked and none of them were ever said out loud.
//
// Sent with mount_id 0: the slot is the hub's TCP accept order, so only the hub
// can say which satellite this is, and it stamps SAT_ADDR_BASE + slot on the
// way past — the same trick as the name.
static uint32_t _sat_health_ms  = 0;
static uint16_t _sat_health_seq = 0;

static void sat_send_health(uint32_t now) {
    if (!_uplink.connected()) return;      // nowhere to send it
    if (_sat_health_ms && (now - _sat_health_ms) < HEALTH_INTERVAL_MS) return;
    bool first = !_sat_health_ms;
    _sat_health_ms = now ? now : 1;

    PayloadHealth h = {};
    h.node_type     = HEALTH_NODE_SATELLITE;
    h.reset_reason  = (uint8_t)esp_reset_reason();
    h.uptime_s      = now / 1000UL;
    h.free_heap     = (uint32_t)esp_get_free_heap_size();
    h.min_free_heap = (uint32_t)esp_get_minimum_free_heap_size();
    h.loop_max_ms   = (uint16_t)(_sat_loopmax_ms > 0xFFFF ? 0xFFFF : _sat_loopmax_ms);
    // Downlink sends the mount never acked — the same thing tx_fail means on a
    // mount, so the column compares straight across.
    h.tx_fail       = (uint16_t)(_sat_unacked_total > 0xFFFF ? 0xFFFF
                                                             : _sat_unacked_total);
    // No single RSSI to report: this box talks to several mounts at once, and
    // its own uplink is wired.  0, as the hub does, rather than a number that
    // would invite comparison with a mount's.
    h.rssi          = 0;
    h.flags         = (first || _sat_rst_streak) ? HEALTH_FLAG_ANOMALY : 0;
    // Packed as the mount's is: refusals high, self-restart streak low.  These
    // are the two numbers that explain this box — NO_MEM is how its transmit
    // path wedges, and the streak is how close it is to giving up on fixing
    // itself.  A streak stuck at its cap is the state that left two mounts
    // unreachable with every windowed counter reading zero.
    h.node_u32      = ((_sat_nomem_total & 0xFFFFUL) << 16) |
                      (_sat_rst_streak   & 0xFFFFUL);

    uint8_t  frame[PKT_BUF_SIZE + 4];
    uint16_t fn = build_health(frame, 0, ++_sat_health_seq, &h);

    uint8_t  mac[6];
    WiFi.softAPmacAddress(mac);
    uint8_t  env[SAT_ENV_MAX];
    uint16_t en = sat_env_build(env, mac, 0, frame, fn);
    if (en) _uplink.write(env, en);
}

// The downlink ledger, on the same clock as health.  Cumulative since boot, so
// it can be differenced across any two lines in the log; the [RATE] counters
// next door are windowed and cannot be.
static uint32_t _sat_dn_report_ms = 0;

static void sat_send_downlink(uint32_t now) {
    if (!_uplink.connected()) return;
    // Its own clock, deliberately.  Hung off the end of sat_send_health() this
    // would inherit that function's early returns, which is exactly how the
    // mount's RF report ended up disabled on the one mount that had the fault.
    if (_sat_dn_report_ms && (now - _sat_dn_report_ms) < HEALTH_INTERVAL_MS) return;
    _sat_dn_report_ms = now ? now : 1;
    uint32_t v[4] = { _sat_dn_offered, _sat_dn_attempts,
                      _sat_dn_sent_total, _sat_nomem_total };
    uint8_t p[SAT_DOWNLINK_PAYLOAD_LEN];
    for (int i = 0; i < 4; i++) {
        p[i*4+0] = (uint8_t)(v[i] >> 24); p[i*4+1] = (uint8_t)(v[i] >> 16);
        p[i*4+2] = (uint8_t)(v[i] >>  8); p[i*4+3] = (uint8_t)(v[i]);
    }
    uint8_t  frame[PKT_BUF_SIZE + 4];
    uint16_t fn = build_packet(frame, 0, ++_sat_health_seq,
                               CMD_SAT_DOWNLINK, p, sizeof(p));
    uint8_t  mac[6];
    WiFi.softAPmacAddress(mac);
    uint8_t  env[SAT_ENV_MAX];
    uint16_t en = sat_env_build(env, mac, 0, frame, fn);
    if (en) _uplink.write(env, en);
}

static void uplink_service(uint32_t now) {
    if (_uplink.connected()) return;
    if (_tcp_len) _tcp_len = 0;                 // stale half-frame from the drop
    if (!eth_is_up() || now < _uplink_next_try_ms) return;

    // hub_connect() BLOCKS — up to the 2 s connect timeout, and far longer the
    // first time, when the mDNS name still has to be resolved.  It is measured
    // and reported here for two reasons.
    //
    // It was showing up as the slowest loop pass instead: the first [RATE] line
    // after a restart routinely read seconds, once 9.1 s, and the working
    // knowledge became "ignore the first one".  That is a metric being quietly
    // discounted, which is the same as not having it — a genuine multi-second
    // stall in that window would have been waved through by the same habit.
    //
    // So the connect reports its own cost and then clears the stall figure it
    // caused.  Nothing is hidden: a connect that starts taking 30 s says so on
    // its own line, and the [RATE] slowest-pass goes back to meaning what it
    // says — the worst pass the satellite managed while actually working.
    uint32_t t0 = millis();
    bool ok = hub_connect(_uplink, HUB_PORT);
    uint32_t took = millis() - t0;

    if (ok) {
        _uplink.setNoDelay(true);               // jog latency matters more than packing
        // NOTE: setting SO_SNDTIMEO here would do nothing.  NetworkClient's
        // send() passes MSG_DONTWAIT, so the option is never consulted — the
        // uplink is bounded by sending on the raw fd instead, in
        // drain_espnow_to_uplink().
        _uplink_backoff_ms = 1000;
        _up_agg_len = _up_agg_frames = 0;   // no half-batch from the dead socket
        Serial.printf("[UPLINK] connected to %s:%d (connect blocked %lu ms)\n",
                      _hub_host, HUB_PORT, (unsigned long)took);
        uplink_send_hello();
    } else {
        _uplink_next_try_ms = now + _uplink_backoff_ms;
        if (_uplink_backoff_ms < UPLINK_BACKOFF_MAX_MS) _uplink_backoff_ms *= 2;
        Serial.printf("[UPLINK] %s:%d unreachable after %lu ms — retry in %lu ms\n",
                      _hub_host, HUB_PORT, (unsigned long)took,
                      (unsigned long)_uplink_backoff_ms);
    }
    // Either way the blocking call is accounted for above, so it must not also
    // be reported as a stall.  Failed attempts block too — the 9.1 s pass was
    // one failed resolve plus the retry that succeeded.
    //
    // A FLAG, not a reset of _loop_max_us: this pass has not been measured yet.
    // _loop_max_us is updated at the bottom of loop(), after this returns, so
    // clearing it here would discard earlier honest measurements from the same
    // window and then record the connect anyway — the exact opposite of the
    // intent.
    if (took > 20) _skip_pass_max = true;
}

// ---------------------------------------------------------------------------

void setup() {
    Serial.begin(115200);
    delay(200);
    Serial.println("\n=== PTS satellite ===");
    // Restart-streak guard.  RTC_NOINIT holds whatever was in RAM after a
    // power-on, so it is only trusted behind a magic — otherwise a random value
    // either disables the restarts entirely or spends the quota immediately.
    // A power-on also means a human was here, which resets the count.
    if (_sat_rst_magic != SAT_RST_MAGIC || esp_reset_reason() != ESP_RST_SW) {
        _sat_rst_magic  = SAT_RST_MAGIC;
        _sat_rst_streak = 0;
    }
    if (_sat_rst_streak)
        Serial.printf("Self-restart streak: %lu\n", (unsigned long)_sat_rst_streak);

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
        DIAG_PRINTF("[AP] station %02X:%02X:%02X:%02X:%02X:%02X left "
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

    // Flush a partial batch once it has waited long enough.  Without this, the
    // last packets before a lull would sit in the buffer until the next one
    // arrived — which for a rig sitting idle is indefinitely.
    if (_ws_agg_len && millis() - _ws_agg_since >= WS_COALESCE_MS) ws_flush();
    // Required every loop by the mathieucarbou fork, and omitting it is not
    // subtle: closed clients are never freed, DEFAULT_MAX_WS_CLIENTS (8) fills
    // after a handful of reconnects, and every new connection is then closed
    // the moment it opens.  A phone shows "reconnecting" on a loop while the
    // client IDs climb — which is precisely what it did.
    _ws.cleanupClients();

    downlink_report(now);
    sat_restart_streak_poll(now);
    sat_send_health(now);
    sat_send_downlink(now);

    _loop_count++;
    uint32_t _dt = micros() - _t0;
    if (_skip_pass_max)            _skip_pass_max = false;
    else {
        if (_dt > _loop_max_us)    _loop_max_us   = _dt;
        uint32_t dtms = _dt / 1000UL;
        if (dtms > _sat_loopmax_ms) _sat_loopmax_ms = dtms;
    }
}
