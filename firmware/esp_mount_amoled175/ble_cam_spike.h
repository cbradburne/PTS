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

class BcScanCb : public BLEAdvertisedDeviceCallbacks {
    void onResult(BLEAdvertisedDevice dev) override {
        if (!dev.haveServiceUUID()) return;
        if (!dev.isAdvertisingService(BLEUUID(BLECAM_SERVICE))) return;
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
        if (_bc_found) delete _bc_found;
        _bc_found = new BLEAdvertisedDevice(dev);
        BLEDevice::getScan()->stop();
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

static void ble_cam_spike_setup() {
    Serial.println("[BLECAM] SPIKE BUILD — measuring BLE/ESP-NOW coexistence");
    BLEDevice::init("PTS-Mount");
    BLEDevice::setSecurityCallbacks(new BcSecCb());
    // bonding + MITM + secure connections: the camera shows a passkey and
    // expects it entered, which is MITM protection with keyboard capability.
    BLESecurity::setAuthenticationMode(true, true, true);
    BLESecurity::setCapability(ESP_IO_CAP_KBDISP);

    BLEScan *scan = BLEDevice::getScan();
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
        Serial.printf("[BLECAM] connecting to %s\n",
                      _bc_found->getAddress().toString().c_str());
        if (!_bc_client) {
            _bc_client = BLEDevice::createClient();
            _bc_client->setClientCallbacks(new BcClientCb());
        }
        // Two attempts, because a wrong address type fails silently at this end
        // and never reaches the camera.  The advertised type first, then the
        // other one — cheap, and it removes a whole class of cause.
        uint8_t at = _bc_found->getAddressType();
        bool ok = _bc_client->connect(_bc_found->getAddress(), at, 8000);
        if (!ok) {
            uint8_t alt = at ? 0 : 1;
            Serial.printf("[BLECAM] connect failed as addrtype %u — retrying as %u\n",
                          (unsigned)at, (unsigned)alt);
            ok = _bc_client->connect(_bc_found->getAddress(), alt, 8000);
        }
        if (!ok) {
            Serial.println("[BLECAM] connect failed both address types.");
            if (!_bc_found->isConnectable())
                Serial.println("[BLECAM]   advertisement is NOT connectable — the camera "
                               "is broadcasting, not accepting. Nothing this end can fix.");
            else
                Serial.println("[BLECAM]   it says it is connectable, so something is "
                               "refusing us: another central still bonded/connected "
                               "(close the Blackmagic app, BT off on that device, "
                               "power-cycle the camera) is much the most likely.");
            delete _bc_found; _bc_found = nullptr;   // rescan, the address may be rotating
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
