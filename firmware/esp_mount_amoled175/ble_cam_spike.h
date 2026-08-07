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
//   1. Flash one mount normally.  Let it settle, then take 30 minutes of
//      NODE HEALTH from comms.log — that is the BLE-off baseline.  Mount 1 is
//      the best subject: direct to the hub, and its numbers are clean.
//   2. Reflash the same mount with:
//        BLE_CAM=1 BLE_CAM_PIN=<6 digits from the camera> tools/build.sh flash amoled
//   3. Confirm from the mount's serial that it reaches CONNECTED and that
//      [BLECAM] keeps reporting notifications.  A link that silently failed to
//      connect would show no impact and would look like good news.
//   4. Take another 30 minutes of NODE HEALTH and compare txfail/min.
//
//   Compare like with like: same mount, same position, same rig activity.
//   txfail is per-minute, so equal window lengths matter less than equal
//   conditions.
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

// The camera shows a six-digit code on its screen when pairing.  Passed in at
// build time because the spike has no UI and does not deserve one.
#ifndef BLE_CAM_PIN
#define BLE_CAM_PIN 000000
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

class BcScanCb : public BLEAdvertisedDeviceCallbacks {
    void onResult(BLEAdvertisedDevice dev) override {
        if (!dev.haveServiceUUID()) return;
        if (!dev.isAdvertisingService(BLEUUID(BLECAM_SERVICE))) return;
        Serial.printf("[BLECAM] found \"%s\" %s (%d dBm)\n",
                      dev.getName().c_str(), dev.getAddress().toString().c_str(),
                      dev.getRSSI());
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
    uint32_t onPassKeyRequest() override {
        Serial.printf("[BLECAM] passkey requested — sending %06lu\n",
                      (unsigned long)BLE_CAM_PIN);
        return BLE_CAM_PIN;
    }
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
        if (!_bc_client->connect(_bc_found)) {
            Serial.println("[BLECAM] connect failed");
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
