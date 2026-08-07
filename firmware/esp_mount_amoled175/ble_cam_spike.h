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
// Default OFF, and a no-op when off — nothing here links into a normal build.
// ---------------------------------------------------------------------------
#ifndef BLE_CAM_SPIKE
#define BLE_CAM_SPIKE 0
#endif

#if !BLE_CAM_SPIKE

static inline void ble_cam_spike_setup() {}
static inline void ble_cam_spike_poll()  {}

#else

#include <esp_wifi.h>
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
        String nm = dev.getName();
        bool svc = dev.haveServiceUUID() &&
                   dev.isAdvertisingService(BLEUUID(BLECAM_SERVICE));
        bool named = nm.length() && (nm.indexOf(BLECAM_NAME) >= 0);
        Serial.printf("[BLECAM]  seen \"%s\" %s %d dBm%s%s\n",
                      nm.c_str(), dev.getAddress().toString().c_str(),
                      dev.getRSSI(), svc ? " [svc]" : "", named ? " [NAME MATCH]" : "");
        if (!named && !svc) return;
        // A name match outranks a service match — see above.
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
    Serial.println("[BLECAM] SPIKE BUILD — measuring BLE/ESP-NOW coexistence");
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

static void ble_cam_spike_poll() {
    uint32_t now = millis();

    if (!_bc_connected && (now - _bc_retry_ms) > BLECAM_RETRY_MS) {
        _bc_retry_ms = now;
        if (!_bc_found) {
            // Blocking, but only in a spike build, and only while disconnected.
            // A normal build never reaches here.
            BLEDevice::getScan()->start(3, false);
            return;
        }
        if (_bc_best_rssi < BLECAM_WEAK_RSSI)
            Serial.printf("[BLECAM] NOTE %d dBm is weak for a camera on this mount — "
                          "check %s is the right one if others are in range\n",
                          _bc_best_rssi, _bc_found->getAddress().toString().c_str());
        Serial.printf("[BLECAM] connecting to %s (%d dBm, strongest of the scan)\n",
                      _bc_found->getAddress().toString().c_str(), _bc_best_rssi);
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
        bool ok = _bc_client->connect(_bc_found->getAddress());
        if (!ok) {
            Serial.println("[BLECAM] connect failed.");
            if (!_bc_found->isConnectable())
                Serial.println("[BLECAM]   advertisement is NOT connectable — the camera "
                               "is broadcasting, not accepting. Nothing this end can fix.");
            else
                Serial.println("[BLECAM]   it says it is connectable, so something is "
                               "refusing us: another central still bonded/connected "
                               "(close the Blackmagic app, BT off on that device, "
                               "power-cycle the camera) is much the most likely.");
            delete _bc_found; _bc_found = nullptr; _bc_best_rssi = -999;
            return;
        }
        BLERemoteService *svc = _bc_client->getService(BLEUUID(BLECAM_SERVICE));
        if (!svc) {
            Serial.println("[BLECAM] camera service missing — wrong device?");
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
