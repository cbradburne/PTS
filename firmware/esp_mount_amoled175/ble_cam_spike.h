#pragma once
// ---------------------------------------------------------------------------
// BLE camera control — SPIKE, not a feature
// ---------------------------------------------------------------------------
// This exists to answer ONE question and then be deleted or grown up:
//
//     What does holding a BLE link to the camera cost the ESP-NOW link?
//
// The ESP32-S3 has a single radio shared between WiFi and Bluetooth.  ESP-NOW
// and BLE coexist by time-slicing, and these mounts already sit at 5-7
// txfail/min with the occasional 11-second dropout.  Whether adding BLE costs
// 5% or 50% of that budget decides whether camera control belongs on this chip
// at all — and it is not answerable by reasoning, only by measuring.
//
// So this connects to a camera, subscribes to its status, and otherwise does
// NOTHING.  No camera commands, no protocol work, no UI.  Those are the easy
// parts and there is no point building them before the radio question is
// settled.
//
//   HOW TO RUN THE MEASUREMENT
//   Use whichever mount actually carries a camera — that constraint wins over
//   any preference about which link is cleanest.
//
//   1. With a NORMAL build on that mount, in the position it will stay in, take
//      ~30 minutes of NODE HEALTH from comms.log.  That is the BLE-off baseline.
//   2. Reflash the SAME mount, in the SAME position, with:
//        BLE_CAM=1 tools/build.sh flash amoled
//      Then open a serial monitor.  When the mount finds the camera it will ask
//      for the passkey; the camera shows six digits at that moment — type them
//      in and press Enter.  Pairing is remembered, so this is once per mount.
//   3. Confirm from the mount's serial that it reaches CONNECTED and that
//      [BLECAM] keeps reporting a rising notification count.  A link that
//      silently failed to connect shows no impact and looks like good news.
//   4. Take another ~30 minutes and compare txfail/min.
//
//   The baseline must be FRESH.  txfail depends on where the mount is and
//   whether it reaches the hub directly or through a satellite, so a figure
//   from before a move — or from when it was on a different path — is not a
//   baseline, it is a different experiment.  A mount on a satellite is a
//   perfectly good subject; its txfail simply describes the mount-to-satellite
//   hop rather than mount-to-hub.
//
//   Same mount, same position, same path, same rig activity.  Equal window
//   lengths matter less than equal conditions, since txfail is per-minute.
//
//   WHY IT DOES NOT CONNECT — FOUND, AND NOT FIXABLE FROM HERE
//
//   With CORE_DEBUG_LEVEL up, the rig finally said it:
//
//     BLEClient.cpp:1167  BLEClient: Connected event. Handle: 1
//     BLEClient.cpp:1173  MTU exchange error
//     BLEClient.cpp:1015  Connection failed; status=2
//                         "Operation already in progress or completed."
//
//   The connection SUCCEEDS.  What fails is the next line of the core's own
//   BLE_GAP_EVENT_CONNECT handler:
//
//     rc = ble_gattc_exchange_mtu(client->m_conn_id, nullptr, nullptr);
//     if (rc != 0) { log_e(...); break; }        // <- tears the link down
//
//   status=2 is BLE_HS_EALREADY: the MTU exchange has ALREADY happened,
//   because this camera initiates it itself the instant a central connects.
//   A peer being quick is not an error, but the library treats any non-zero
//   return as fatal and drops the connection — and because the teardown
//   happens before the security block a few lines below, pairing never starts
//   and the camera never shows a passkey.  Every symptom follows from that.
//
//   None of it is reachable from a sketch: the call is inside the core's
//   BLEClient event handler.  The fix is to vendor NimBLE-Arduino into
//   libraries/ (as lvgl, GFX_Library_for_Arduino and SensorLib already are)
//   and drive it directly, which also gets a smaller stack than this wrapper.
//
//   Five theories were spent on this before the log was simply turned up:
//   security config, address types, WiFi coexistence, the wrong device, a
//   half-open connection.  All wrong.  The two things that found real faults
//   were reading a crash dump and reading the library's source.
//
// Default OFF, and a no-op when off — nothing here links into a normal build.
// ---------------------------------------------------------------------------
#ifndef BLE_CAM_SPIKE
#define BLE_CAM_SPIKE 0
#endif

#if !BLE_CAM_SPIKE

// One line, on purpose.  A build without the flag is silent and behaves
// perfectly normally, so "no [BLECAM] output" and "the spike is not in this
// binary" look identical from a serial monitor — that ambiguity has now cost
// two flash-and-test rounds.  Cheaper to say so than to work it out again.
static inline void ble_cam_spike_setup() {
    Serial.println("[BLECAM] spike NOT compiled in (build with BLE_CAM=1 or 2)");
}
static inline void ble_cam_spike_poll()  {}

#else

#include <esp_wifi.h>
#include <esp_task_wdt.h>
#include <BLEDevice.h>
#include <BLEUtils.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>
#include <BLESecurity.h>

// Published by Blackmagic in the camera's "Developer Information" manual
// section.  Same Camera Control Protocol the SDI path carries, so if this
// spike passes, the command work is mostly mapping rather than inventing.
#define BLECAM_SERVICE   "291d567a-6d75-11e6-8b77-86f30ca893d3"
#define BLECAM_OUTGOING  "5dd3465f-1aee-4299-8493-d2eca2f8e1bb"  // camera -> us
#define BLECAM_INCOMING  "b864e140-76a0-416a-bf30-5876504537d9"  // us -> camera
#define BLECAM_STATUS    "7fe8691d-95dc-4fc5-8abd-ca74339b51b9"

// The passkey is typed into the SERIAL MONITOR while pairing is in progress.
//
// It cannot be a build flag, which is what this first tried.  The camera only
// displays a code once something attempts to pair with it, so the code does not
// exist until after the flash — and BLE passkey pairing generates fresh random
// digits every attempt, so there is nothing to carry forward between flashes
// either.  Compile-time was circular twice over.
//
// So onPassKeyRequest() waits for the number, which is also what a real
// implementation would do — from the mount's own touchscreen rather than a
// serial monitor.
#define BLE_CAM_PIN_WAIT_MS  90000UL

// Blocking, deliberately.  It runs on the BLE host task during pairing, which
// is exactly the moment there is nothing else for that task to do, and the
// alternative — failing the pairing and retrying — cannot work when the camera
// picks new digits each time.
static uint32_t bc_prompt_passkey() {
    Serial.println();
    Serial.println("[BLECAM] ============================================");
    Serial.println("[BLECAM] The camera is now showing a 6-digit code.");
    Serial.println("[BLECAM] Type it here and press Enter.");
    Serial.println("[BLECAM] ============================================");
    char buf[8]; uint8_t n = 0;
    uint32_t deadline = millis() + BLE_CAM_PIN_WAIT_MS;
    while ((int32_t)(millis() - deadline) < 0) {
        while (Serial.available()) {
            int c = Serial.read();
            if (c == '\r' || c == '\n') {
                if (n == 0) continue;                 // ignore a bare newline
                buf[n] = 0;
                uint32_t k = (uint32_t)strtoul(buf, nullptr, 10);
                Serial.printf("[BLECAM] using %06lu\n", (unsigned long)k);
                return k;
            }
            if (c >= '0' && c <= '9' && n < 6) buf[n++] = (char)c;
        }
        delay(10);
    }
    Serial.println("[BLECAM] no code entered — pairing will fail, it will retry");
    return 0;
}

// BLE_VERBOSE=1 turns the BLE stack's own logging up.  Four rounds of guessing
// at why connect() returns false have cost more than reading the error would
// have: the stack knows exactly why and simply is not asked.
#ifndef BLECAM_VERBOSE
#define BLECAM_VERBOSE 0
#endif

#define BLECAM_REPORT_MS   30000UL
#define BLECAM_RETRY_MS    10000UL

static BLEClient            *_bc_client = nullptr;
static BLEAdvertisedDevice  *_bc_found  = nullptr;
static volatile bool         _bc_connected = false;
static volatile uint32_t     _bc_notifies  = 0;
static uint32_t              _bc_report_ms = 0;
static uint32_t              _bc_retry_ms  = 0;
static uint32_t              _bc_since_ms  = 0;
static int                   _bc_best_rssi = -999;
static bool                  _bc_by_name   = false;
static BLEAddress            _bc_pick_addr;

// Every device the scan turned up, so a human can pick one.
//
// Auto-matching was guessing — first by service UUID, which found a nameless
// device that was not the camera at all, then by name, which only works if the
// name is what I assumed.  A numbered list needs no assumption: the operator
// reads the camera's Bluetooth screen and picks the matching line.  An exact
// name match still auto-selects, so once it is known to work this stops
// needing a human.
#define BC_MAX_CAND 12
// Address kept as TEXT.  getNative() is little-endian, so printing those bytes
// in order reverses the address: the scan line said 90:fd:9f:b4:50:df and the
// menu said DF:50:B4:9F:FD:90 for the same device.  toString() is the form
// everything else in the system uses, and BLEAddress can be rebuilt from it.
struct BcCand { char addr[20]; char name[26]; int16_t rssi; bool svc; bool named; };
static BcCand  _bc_cand[BC_MAX_CAND];
static uint8_t _bc_ncand = 0;
// The name from the camera's Bluetooth menu.  Substring, so "BMPCC" matches
// "Colin BMPCC".  Override at build time if yours is named otherwise.
#ifndef BLECAM_NAME
#define BLECAM_NAME  "BMPCC"
#endif
// A camera on this mount should be strong.  Weaker than this is worth SAYING
// on a rig where several mounts each carry one — connecting to a neighbour's
// camera would look like success — but it is only a warning, not a veto: a
// metal-bodied camera at arm's length can read -66, and refusing to try would
// have blocked the only camera in the room on a bench with one.
#define BLECAM_WEAK_RSSI  (-65)

class BcScanCb : public BLEAdvertisedDeviceCallbacks {
    void onResult(BLEAdvertisedDevice dev) override {
        // EVERY device, named or not.  The service-UUID filter alone was
        // matching a nameless device that is not the camera at all — the camera
        // advertises the name set in its Bluetooth menu ("Colin BMPCC"), and
        // that name is the only thing here that identifies it beyond doubt.
        // Chasing the wrong device is why the camera never showed a pairing
        // code: nothing was ever talking to it.
        // getName() returns only the COMPLETE local name (AD type 0x09).  This
        // camera advertises a SHORTENED one (0x08), so getName() was empty and
        // the name looked absent — it was there the whole time:
        //   1E 08 436F6C696E20424D504343  ->  "Colin BMPCC"
        String nm = dev.getName();
        if (!nm.length()) {
            uint8_t *pl = dev.getPayload();
            size_t   pn = dev.getPayloadLength();
            for (size_t i = 0; i + 1 < pn; ) {
                uint8_t fl = pl[i];
                if (!fl || i + fl >= pn + 1) break;
                uint8_t ty = pl[i + 1];
                if ((ty == 0x08 || ty == 0x09) && fl > 1) {
                    char t[27]; uint8_t n = fl - 1;
                    if (n > sizeof(t) - 1) n = sizeof(t) - 1;
                    memcpy(t, pl + i + 2, n); t[n] = 0;
                    nm = String(t);
                    break;
                }
                i += fl + 1;
            }
        }
        bool svc = dev.haveServiceUUID() &&
                   dev.isAdvertisingService(BLEUUID(BLECAM_SERVICE));
        bool named = nm.length() && (nm.indexOf(BLECAM_NAME) >= 0);
        Serial.printf("[BLECAM]  seen \"%s\" %s %d dBm%s%s\n",
                      nm.c_str(), dev.getAddress().toString().c_str(),
                      dev.getRSSI(), svc ? " [svc]" : "", named ? " [NAME MATCH]" : "");
        // Remembered whether or not it looks like a camera: the whole point is
        // that our idea of "looks like a camera" has been wrong twice.
        String as = dev.getAddress().toString();
        bool dup = false;
        for (uint8_t i = 0; i < _bc_ncand; i++)
            if (as == _bc_cand[i].addr) { _bc_cand[i].rssi = dev.getRSSI(); dup = true; break; }
        if (!dup && _bc_ncand < BC_MAX_CAND) {
            BcCand &c = _bc_cand[_bc_ncand++];
            snprintf(c.addr, sizeof(c.addr), "%s", as.c_str());
            snprintf(c.name, sizeof(c.name), "%s", nm.c_str());
            c.rssi = dev.getRSSI(); c.svc = svc; c.named = named;
        }
        if (!named && !svc) return;
        if (_bc_found && _bc_by_name && !named) return;
        if (named && !_bc_by_name) { _bc_best_rssi = -999; _bc_by_name = true; }
        // "connect failed" on its own is useless — it was, on the rig.  These
        // four fields separate the causes that look identical from outside:
        //   connectable=0  the camera is broadcasting, not accepting.  Nothing
        //                  we do on this side will help; it needs putting into
        //                  a state where it accepts a central.
        //   addrtype 1     a random address.  connect() must be told, or it
        //                  tries the wrong type and fails without reaching the
        //                  camera — which matches "no sign of it at the camera".
        Serial.printf("[BLECAM] found \"%s\" %s | rssi %d | addrtype %u | "
                      "advtype %u | connectable %d | payload %u B\n",
                      dev.getName().c_str(), dev.getAddress().toString().c_str(),
                      dev.getRSSI(), (unsigned)dev.getAddressType(),
                      (unsigned)dev.getAdvType(), (int)dev.isConnectable(),
                      (unsigned)dev.getPayloadLength());
        // Keep the STRONGEST, do not stop at the first.
        //
        // A rig has a Blackmagic camera on several mounts, and they all
        // advertise this service.  Stopping at the first match meant a mount
        // trying to connect to a camera across the building — seen at -81 dBm,
        // when the one bolted to this mount is centimetres away and should be
        // -30 to -50.  Signal strength is the only thing that distinguishes
        // "mine" from "someone else's" here, and it distinguishes it easily.
        // Dump the raw advertisement once per device.  The name is empty, which a
        // BMPCC4K should not be, so the filter matching is worth confirming
        // rather than trusting: this prints what actually came off the air.
        static uint8_t seen[6] = {};
        if (memcmp(seen, dev.getAddress().getNative(), 6) != 0) {
            memcpy(seen, dev.getAddress().getNative(), 6);
            uint8_t *pl = dev.getPayload();
            size_t   n  = dev.getPayloadLength();
            Serial.print("[BLECAM] raw adv: ");
            for (size_t i = 0; i < n && i < 62; i++) Serial.printf("%02X", pl[i]);
            Serial.println();
            Serial.printf("[BLECAM] svc uuid: %s | count %d\n",
                          dev.getServiceUUID().toString().c_str(),
                          (int)dev.getServiceDataCount());
        }
        if (!_bc_found || dev.getRSSI() > _bc_best_rssi) {
            if (_bc_found) delete _bc_found;
            _bc_found = new BLEAdvertisedDevice(dev);
            _bc_best_rssi = dev.getRSSI();
        }
    }
};

class BcClientCb : public BLEClientCallbacks {
    void onConnect(BLEClient *) override {
        _bc_connected = true; _bc_since_ms = millis();
        Serial.println("[BLECAM] CONNECTED");
    }
    void onDisconnect(BLEClient *) override {
        _bc_connected = false;
        Serial.println("[BLECAM] disconnected");
    }
};

class BcSecCb : public BLESecurityCallbacks {
    uint32_t onPassKeyRequest() override { return bc_prompt_passkey(); }
    void onPassKeyNotify(uint32_t pass) override {
        Serial.printf("[BLECAM] camera shows %06lu\n", (unsigned long)pass);
    }
    bool onSecurityRequest() override { return true; }
    bool onConfirmPIN(uint32_t pin) override {
        Serial.printf("[BLECAM] confirm %06lu\n", (unsigned long)pin);
        return true;
    }
    // onAuthenticationComplete() is Bluedroid-only and this core builds the BLE
    // library on NimBLE, so pairing success is judged by whether the service
    // actually resolves below rather than by a callback that never fires.
    bool onAuthorizationRequest(uint16_t, uint16_t, bool) override { return true; }
};

// Counted, not decoded.  A rising count is proof the link is carrying traffic
// during the measurement window, which is all this spike needs to establish.
static void bc_notify(BLERemoteCharacteristic *, uint8_t *, size_t, bool) {
    _bc_notifies++;
}

// BLE_CAM=2 — PAIR-ONLY mode.  WiFi is stopped before BLE starts.
//
// With WiFi running, the camera was found every time and the connection never
// completed.  BlueMagic32, which works against this camera, runs on boards
// doing nothing else; a mount runs WiFi STA and ESP-NOW on the same radio, and
// on the S3 WiFi wins coexistence arbitration by default.  Establishing a BLE
// connection needs sustained radio time that it may simply never get.
//
// Pairing is the expensive part; reconnecting to a BONDED peer is far cheaper.
// So pair once with WiFi stopped, then reflash with BLE_CAM=1 and see whether
// the bonded reconnect survives alongside ESP-NOW.  That splits one unanswerable
// question into two answerable ones:
//
//   pair-only connects   -> coexistence blocks CONNECTION SETUP specifically
//   pair-only also fails -> the fault is not coexistence, look elsewhere
//                           (power, camera state, bond)
//
// This mode cannot relay anything and must never be flashed to a working rig.
static void ble_cam_spike_setup() {
#if BLE_CAM_SPIKE == 2
    Serial.println("[BLECAM] PAIR-ONLY BUILD — stopping WiFi so BLE has the radio.");
    Serial.println("[BLECAM] This mount will NOT talk to the hub. Pair, then reflash BLE_CAM=1.");
    esp_wifi_stop();
    delay(200);
#endif
    // The task watchdog is switched OFF for the whole spike build.
    //
    // Releasing loopTask from it was not enough: connect() then starved the
    // IDLE1 task instead and the watchdog fired on that —
    //   task 'IDLE1' faulted ... Task watchdog got triggered
    // which is the same crash wearing a different name.  Whatever the BLE
    // connect does to this core, it does not leave the idle task enough room.
    //
    // A spike build is a bench diagnostic — BLE_CAM=2 already refuses to talk
    // to a hub at all — so the watchdog is protecting nothing here, and it is
    // the only thing standing between us and reading the connect's actual
    // error.  It stays exactly as it was in every normal build.
    esp_task_wdt_deinit();
    Serial.println("[BLECAM] SPIKE BUILD — task watchdog OFF for this build");
    BLEDevice::init("PTS-Mount");
    BLEDevice::setPower(ESP_PWR_LVL_P9);          // as BlueMagic32 does
    BLEDevice::setSecurityCallbacks(new BcSecCb());

    // Taken from schoolpost/BlueMagic32, which is proven against the BMPCC4K,
    // after my own guess at this failed to connect at all.
    //
    // IO capability decides the pairing association model, and it is the whole
    // difference.  KEYBOARD_ONLY means "the peer displays, I type" — Passkey
    // Entry, which is this camera's flow.  KEYBOARD_DISPLAY, which I had,
    // claims both and negotiates Numeric Comparison instead; the camera will
    // not do that, and because encryption is required at connect time the link
    // is dropped before pairing ever appears on the camera's screen.  That is
    // exactly what the rig showed: "connect failed", camera showing nothing.
    //
    // MITM is NOT requested for the same reason BlueMagic32 does not request
    // it — SC + bonding with keyboard-only already yields passkey entry.
    BLESecurity::setAuthenticationMode(ESP_LE_AUTH_REQ_SC_BOND);
    BLESecurity::setCapability(ESP_IO_CAP_IN);
    BLESecurity::setRespEncryptionKey(ESP_BLE_ENC_KEY_MASK | ESP_BLE_ID_KEY_MASK);

    BLEScan *scan = BLEDevice::getScan();
    scan->clearResults();
    scan->setAdvertisedDeviceCallbacks(new BcScanCb());
    scan->setActiveScan(true);
    scan->setInterval(100);
    scan->setWindow(80);
}

// Print what was found and let the operator pick.  Returns true once _bc_found
// holds a choice.  Auto-selects a single name match without asking, so this
// stops needing a human as soon as the name is known to be right.
static bool _bc_chosen = false;

static bool bc_choose() {
    if (!_bc_ncand) { Serial.println("[BLECAM] scan found nothing at all"); return false; }

    // Match on the SERVICE.  The name is now recovered from the raw payload as
    // well and shown in the list, but the service UUID is the reliable
    // identifier: a camera could be renamed to anything, and 0x1800 is what
    // getServiceUUID() returns for this one anyway.
    int only_named = -1, n_named = 0;
    for (uint8_t i = 0; i < _bc_ncand; i++)
        if (_bc_cand[i].svc) { only_named = i; n_named++; }

    Serial.println("\n[BLECAM] ---- devices in range ----");
    for (uint8_t i = 0; i < _bc_ncand; i++) {
        BcCand &c = _bc_cand[i];
        Serial.printf("[BLECAM]  %2u) %-26s %-18s %4d dBm%s\n",
                      i + 1, c.name[0] ? c.name : "(no name)", c.addr, c.rssi,
                      c.svc ? "  <-- BLACKMAGIC CAMERA" : "");
    }

    int pick = -1;
    if (n_named == 1) {
        pick = only_named;
        Serial.printf("[BLECAM] one Blackmagic camera in range — using %u\n", pick + 1);
    } else {
        Serial.printf("[BLECAM] type 1-%u and Enter (15 s, else rescan): ", _bc_ncand);
        uint32_t deadline = millis() + 15000UL;
        int v = 0; bool any = false;
        while ((int32_t)(millis() - deadline) < 0) {
            while (Serial.available()) {
                int ch = Serial.read();
                if (ch == '\r' || ch == '\n') { if (any) { deadline = 0; break; } continue; }
                if (ch >= '0' && ch <= '9') { v = v * 10 + (ch - '0'); any = true; Serial.write(ch); }
            }
            if (!deadline) break;
            delay(10);
        }
        Serial.println();
        if (!any || v < 1 || v > _bc_ncand) { Serial.println("[BLECAM] no valid choice — rescanning"); return false; }
        pick = v - 1;
    }

    if (_bc_found) { delete _bc_found; _bc_found = nullptr; }
    _bc_pick_addr  = BLEAddress(String(_bc_cand[pick].addr));
    _bc_best_rssi  = _bc_cand[pick].rssi;
    _bc_chosen     = true;
    Serial.printf("[BLECAM] chose %s (%d dBm)\n",
                  _bc_pick_addr.toString().c_str(), _bc_best_rssi);
    return true;
}

static void ble_cam_spike_poll() {
    uint32_t now = millis();

    if (!_bc_connected && (now - _bc_retry_ms) > BLECAM_RETRY_MS) {
        _bc_retry_ms = now;
        if (!_bc_chosen) {
            // Blocking, but only in a spike build, and only while disconnected.
            // A normal build never reaches here.
            _bc_ncand = 0;
            BLEDevice::getScan()->start(5, false);
            if (!bc_choose()) return;
        }
        Serial.printf("[BLECAM] connecting to %s (%d dBm)\n",
                      _bc_pick_addr.toString().c_str(), _bc_best_rssi);
        // A fresh client per attempt, as BlueMagic32 does.  Reusing one across a
        // failed connect can leave it in a state that never succeeds again,
        // which would turn a first failure into a permanent one.
        if (!_bc_client) {
            _bc_client = BLEDevice::createClient();
            _bc_client->setClientCallbacks(new BcClientCb());
        }
        // Plain address, as BlueMagic32 does.  The address-type retry added
        // earlier was chasing the wrong fault — the addresses here are public
        // (addrtype 0) and the failure was the security negotiation, not
        // addressing.
        // connect() blocks for longer than the task watchdog allows, and this runs
        // on loopTask, which is subscribed to it.  Every attempt was ending in
        //   "Task watchdog got triggered ... loopTask (CPU 1)" -> reboot
        // which surfaced as a connect failure and looked like the camera
        // refusing us.  It was this end crashing before the camera ever
        // answered.  Leave the watchdog for the duration and rejoin after.
        // NimBLE refuses a connection while discovery is active (BLE_HS_EBUSY).
        // The blocking scan should have ended by itself, but "should have" is
        // not worth a round trip to the rig — BlueMagic32 stops it explicitly
        // in its scan callback, and this is one line.
        BLEDevice::getScan()->stop();
        delay(50);

        esp_task_wdt_delete(NULL);
        bool ok = _bc_client->connect(_bc_pick_addr);
        esp_task_wdt_add(NULL);
        if (!ok) {
            // Tear down whatever got part-way.  After a failed attempt the
            // camera disappeared from every following scan, which is what a
            // peripheral does when it believes it is connected — so the attempt
            // is reaching it and half-succeeding, and leaving that hanging
            // would explain why retrying never finds it again.
            _bc_client->disconnect();
            delay(200);
            Serial.println("[BLECAM] connect failed — picking again from a fresh scan.");
#if !BLECAM_VERBOSE
            Serial.println("[BLECAM]   no reason available at this log level. Rebuild with");
            Serial.println("[BLECAM]   BLE_CAM=2 BLE_VERBOSE=1 to make the BLE stack print");
            Serial.println("[BLECAM]   the GAP error it is actually returning.");
#endif
            _bc_chosen = false;
            return;
        }
        BLERemoteService *svc = _bc_client->getService(BLEUUID(BLECAM_SERVICE));
        if (!svc) {
            _bc_chosen = false;    // wrong device — offer the list again
            Serial.println("[BLECAM] connected, but no Blackmagic service — wrong device");
            _bc_client->disconnect();
            return;
        }
        // Subscribing is what makes this a realistic load: an idle connected
        // link is cheap, a link actually carrying notifications is the thing
        // camera control would really do.
        for (const char *u : { BLECAM_OUTGOING, BLECAM_STATUS }) {
            BLERemoteCharacteristic *ch = svc->getCharacteristic(BLEUUID(u));
            if (ch && ch->canNotify()) ch->registerForNotify(bc_notify);
        }
        Serial.println("[BLECAM] subscribed — leave it running and take the numbers");
    }

    if ((now - _bc_report_ms) >= BLECAM_REPORT_MS) {
        _bc_report_ms = now;
        // Printed even when disconnected, on purpose: "no ESP-NOW impact"
        // means nothing if the link was down for the window.
        Serial.printf("[BLECAM] %s | up %lus | %lu notifications\n",
                      _bc_connected ? "CONNECTED" : "not connected",
                      (unsigned long)(_bc_connected ? (now - _bc_since_ms) / 1000UL : 0),
                      (unsigned long)_bc_notifies);
    }
}

#endif  // BLE_CAM_SPIKE
