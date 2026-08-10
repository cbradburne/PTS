#pragma once
// ---------------------------------------------------------------------------
// Blackmagic camera control over BLE
// ---------------------------------------------------------------------------
// Each mount holds a Bluetooth link to the camera riding on it and relays the
// Blackmagic Camera Control Protocol both ways: commands down from the PC app
// (CMD_CAM_CONTROL), camera-reported state back up (CMD_CAM_STATUS).
//
// This started as a spike to answer one question — what does holding a BLE link
// cost the ESP-NOW link, on a chip with one radio time-slicing between them?
// Measured on hardware 2026-08-07 with a paired, subscribed, actively-used link:
// txfail 0, loop max 9 ms.  Indistinguishable from the BLE-off baseline.  That
// result is why camera control lives on this chip instead of a second ESP32 per
// mount, so it is worth re-measuring if the radio picture ever changes.
//
//   PAIRING — once per mount, from the mount's own screen
//
//   Hold the screen to reach SETUP, hold again for CAMERA PAIRING.  It searches,
//   the camera puts six digits on its own display, and they go into the keypad.
//   FORGET drops every bond this mount holds, for when a camera moves to a
//   different mount.  The screen leaves on its own after two minutes idle.
//
//   No build flag and no serial monitor.  It was both, and that was wrong twice
//   over: it needed two flashes and a laptop per mount, and the mount's USB port
//   is sealed inside the enclosure on a rig — the one place the old flow could
//   not be used was the place it was for.  Whoever types the code has to be able
//   to read the camera's screen, so they are standing at the mount regardless.
//
//   Pairing stops WiFi, so the mount is off the air until it finishes and the
//   hub will see it drop.  Acceptable for a one-time bench job, and the reason
//   the screen times out rather than waiting for ever.  The bond then lives in
//   NVS and reconnects on its own for good.
//
//   THE MTU WORKAROUND — why this bypasses BLEClient
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
//   status=2 is BLE_HS_EALREADY: the MTU exchange has ALREADY happened, because
//   this camera initiates it itself the instant a central connects.  A peer
//   being quick is not an error, but the library treats any non-zero return as
//   fatal and drops the connection — and because the teardown happens before
//   the security block a few lines below, pairing never starts and the camera
//   never shows a passkey.  Every symptom followed from that.
//
//   The wrapper cannot be changed from a sketch — but it does not have to be.
//   <host/ble_gap.h> comes in with the library's own headers, so the NimBLE C
//   API underneath is available directly.  bc_gap_event() below runs the same
//   sequence in the same order, minus the one line that throws the connection
//   away, and calls ble_gap_security_initiate() itself so pairing actually
//   starts.  Vendoring a whole BLE library turned out to be unnecessary.
//
//   TWO THINGS THAT WILL WASTE A DAY IF FORGOTTEN
//
//   * The camera's characteristics are named from the CONTROLLER's point of
//     view.  "Outgoing" is the one we WRITE to; "Incoming" is the one the camera
//     notifies on.  Writing to the obvious-sounding one silently does nothing.
//   * The CCCD wants 0x02 — INDICATIONS, not notifications.  Subscribing with
//     0x01 succeeds, reports success, and delivers nothing.
//
//   Both were found by adding a diagnostic, not by reasoning.  So was every
//   other real fault here; five theories were spent before the log was simply
//   turned up.
// ---------------------------------------------------------------------------


#include <esp_wifi.h>
#include <esp_task_wdt.h>
#include <BLEDevice.h>
#include <BLEUtils.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>
#include <BLESecurity.h>
#include <host/ble_store.h>

// Published by Blackmagic in the camera's "Developer Information" manual
// section.  Same Camera Control Protocol the SDI path carries, so a command
// built here is the one an SDI controller would send, byte for byte.
#define CAM_SERVICE   "291d567a-6d75-11e6-8b77-86f30ca893d3"
// Named from the CONTROLLER's point of view, not the camera's — which is the
// opposite of what the words suggest and cost a round trip on the rig:
// commands were being written to the notify characteristic, so the link was up,
// the write returned success, and the camera did nothing.  Checked against
// schoolpost/BlueMagic32, which writes to Outgoing and subscribes to Incoming.
#define CAM_OUTGOING  "5dd3465f-1aee-4299-8493-d2eca2f8e1bb"  // us -> camera (WRITE)
#define CAM_INCOMING  "b864e140-76a0-416a-bf30-5876504537d9"  // camera -> us (notify)
#define CAM_STATUS    "7fe8691d-95dc-4fc5-8abd-ca74339b51b9"

// The passkey is typed into the SERIAL MONITOR while pairing is in progress.
//
// It cannot be a build flag, which is what this first tried.  The camera only
// displays a code once something attempts to pair with it, so the code does not
// exist until after the flash — and BLE passkey pairing generates fresh random
// digits every attempt, so there is nothing to carry forward between flashes
// either.  Compile-time was circular twice over.
//
// So the code is asked for at the moment the camera shows it, from the mount's
// own touchscreen — see the pairing state machine below.

// PAIRING, as a runtime mode driven by the mount's own screen.
//
// It used to be a separate BUILD that blocked the BLE host task for up to 90 s
// reading six digits off the serial port.  That meant two flashes and a laptop
// per mount, a task watchdog switched off to survive the block, and a serial
// port that on a rig is sealed inside the enclosure.  The digits are on the
// camera's screen, so whoever types them is standing at the mount anyway — and
// the mount has a touchscreen.
//
// Nothing here blocks.  The passkey request parks in BCP_WANT_CODE, the UI puts
// a keypad up, and ble_cam_pair_submit() injects the code from the main loop
// whenever it arrives.  The watchdog stays on throughout.
enum BcPairState : uint8_t {
    BCP_OFF = 0,     // not in pairing mode
    BCP_SEARCHING,   // scanning / connecting
    BCP_NO_CAMERA,   // scan completed with nothing to pair to
    BCP_WANT_CODE,   // camera is showing six digits, waiting for the keypad
    BCP_PAIRED,
    BCP_FAILED,
};
static volatile BcPairState _bc_pair_state = BCP_OFF;
static volatile bool        _bc_pair_mode  = false;
static uint16_t             _bc_pk_conn    = BLE_HS_CONN_HANDLE_NONE;


// BLE_VERBOSE=1 turns the BLE stack's own logging up.  Four rounds of guessing
// at why connect() returns false have cost more than reading the error would
// have: the stack knows exactly why and simply is not asked.
#ifndef CAM_VERBOSE
#define CAM_VERBOSE 0
#endif

#define CAM_REPORT_MS   30000UL
#define CAM_RETRY_MS    10000UL

// A paired mount has exactly ONE camera it should ever talk to, and NimBLE
// already knows which: the bond is in NVS, keyed by the camera's address.
// Preferring it outranks both name and signal strength, and it is what makes a
// five-camera rig work at all — see bc_choose().
static int bc_bonded_count() {
    ble_addr_t peers[8]; int n = 0;
    if (ble_store_util_bonded_peers(peers, &n, 8) != 0) return 0;
    return n;
}
// nat is the 6-byte little-endian address, the same order ble_addr_t.val uses.
static bool bc_is_bonded(const uint8_t *nat) {
    ble_addr_t peers[8]; int n = 0;
    if (ble_store_util_bonded_peers(peers, &n, 8) != 0) return false;
    for (int i = 0; i < n; i++)
        if (memcmp(peers[i].val, nat, 6) == 0) return true;
    return false;
}

// Declared here because bc_gap_event() clears it when it lands on the wrong
// camera, and the scan code that owns it is defined further down.
static bool _bc_chosen = false;

static BLEAdvertisedDevice  *_bc_found  = nullptr;
static volatile bool         _bc_connected = false;
static volatile uint32_t     _bc_notifies  = 0;
static uint32_t              _bc_report_ms = 0;
static uint32_t              _bc_retry_ms  = 0;
static uint32_t              _bc_since_ms  = 0;

// ---------------------------------------------------------------------------
// Connect via NimBLE directly, not through BLEClient
// ---------------------------------------------------------------------------
// BLEClient's own BLE_GAP_EVENT_CONNECT handler does this:
//
//     rc = ble_gattc_exchange_mtu(...);
//     if (rc != 0) { log_e(...); break; }     // tears the connection down
//
// and this camera returns BLE_HS_EALREADY (2) because it initiates the MTU
// exchange itself the instant a central connects.  A peer being quick is not a
// failure, but the wrapper treats every non-zero return as fatal, drops a
// working link, and never reaches the security block a few lines below — so
// pairing never starts and the camera never shows a passkey.
//
// None of that is reachable from a sketch.  But the NimBLE C API underneath it
// is: <host/ble_gap.h> comes in with the library's own headers.  So this drives
// GAP itself, with the same steps in the same order, minus the one line that
// throws the connection away.
static uint16_t _bc_conn = BLE_HS_CONN_HANDLE_NONE;
// Value handle of the camera's incoming-control characteristic, found once per
// connection.  0 = not discovered yet, and a write before then is dropped
// rather than guessed at.
static uint16_t _bc_ctrl_handle = 0;
// Sticky until reported: a write that fails between two health sends must not
// be lost just because the next one succeeded.
static bool     _bc_write_err   = false;
// Notify handle for the camera's status characteristic, and its CCCD.  A
// characteristic is not enough: notifications only start once 0x0001 is written
// to the Client Characteristic Configuration Descriptor, which has to be
// discovered separately.
static uint16_t _bc_notify_handle = 0;
static uint16_t _bc_cccd_handle   = 0;
static uint16_t _bc_svc_start = 0, _bc_svc_end = 0;
// volatile: written by the NimBLE host task, read by loop() to gate retries.
// Does this mount have a camera bond at all?  Cached rather than asked每 loop:
// bc_bonded_count() reads NVS-backed state and this gates a hot path.
//
// It replaces a latch that was set when a camera asked for a passkey we could
// not answer, and that latch was solving the wrong problem.  On a rig with one
// camera and five mounts, every cameraless mount in range CONNECTED to the one
// camera, got as far as the security exchange, and gave up — and a Blackmagic
// camera takes one connection at a time, so each of those attempts stole the
// link from the mount the camera actually belongs to.  Measured: cam4 with no
// camera of its own spent 486 health samples reporting NOT PAIRED, having
// reached for mount 5's.
//
// So the rule is now: only ever connect to a camera we are BONDED to, unless
// the operator has deliberately opened the pairing screen.
static volatile bool _bc_have_bond = false;
static bool     _bc_subscribed = false;

// NimBLE runs ONE GATT procedure per connection at a time.  The first version
// started the control and notify discoveries back to back and then wrote the
// CCCD from inside a discovery callback — three overlapping procedures, of
// which only the first could run.  Autofocus still worked, because that only
// needs the control handle the first discovery found, so everything looked
// fine while notifications had never been subscribed at all.
//
// Each step therefore starts the next one from its BLE_HS_EDONE, which is
// NimBLE's "that procedure is finished" and the only safe moment to begin
// another:
//
//   service -> control chr -> notify chr -> its CCCD -> write 0x0001

// The sketch supplies this; this layer does not know what a hub is.  Keeps the
// relay decision (what to do with camera bytes) out of the BLE layer.
// LAST-KNOWN CAMERA STATE, so a client that was not listening can still see it.
//
// Camera status is volunteered, never asked for: the camera sends an indication
// when a value CHANGES and says nothing otherwise.  Relaying each one live and
// forgetting it means the numbers exist only for whoever happened to be
// connected at that instant — so a PC app started after the mount, or reopened
// later, shows "—" for gain and white balance until somebody physically turns a
// dial on the camera.  It looks exactly like a broken link, and it is the state
// a rig is in every single time it powers up.
//
// So keep the latest frame per (category, parameter) and re-send them on a slow
// tick.  Verbatim, through the same callback a live indication uses, so nothing
// downstream needs to know the difference — and nothing here has to understand
// what the bytes mean, which is the property that let gain and white balance be
// added without touching the mount at all.
// Sized from a measurement, after guessing twice and being wrong twice.
//
// A Pocket Cinema Camera 4K reports 33 distinct (category, parameter) pairs.
// The cap was 8, then 32 — the first threw away 25 of them, the second exactly
// one, and both did it in silence.  Whichever parameter fell off the end simply
// never appeared in a client, which is precisely how gain and white balance
// went missing.
//
// 64 is double what this camera needs.  At 65 bytes an entry that is ~4 KB
// against 8 MB of free heap, so there is no reason to sit close to the line —
// and HEALTH_FLAG_CAM_CACHE_FULL now reports it in comms.log if a camera ever
// does exceed it, rather than leaving it to be inferred from a missing number.
#define CAM_CACHE_MAX   64
// One frame per tick, cycling, rather than the whole cache at once.
//
// A full sweep of 32 frames back-to-back every few seconds is precisely the
// shape of traffic this rig has spent a long time removing: a burst the hub
// reads as a run of send failures, which it calls a TX wedge.  A steady trickle
// costs the same bytes and never bursts — ~3 packets/s against a ~32 packets/s
// baseline, constant no matter how much the camera has to say.
// Overridable so the replay can be switched OFF for a test:
//     CAM_REPLAY_MS=0 tools/build.sh flash amoled
//
// Why that test exists.  At the venue, the one mount with a camera isolated
// itself three times in 90 minutes — ESP-NOW dead for the full two-minute
// timeout, then SW(esp_restart) — while its BLE link stayed PAIRED and
// subscribed right up to the reboot, and the mount beside it on the same
// satellite and channel, without a camera, never faltered.  So it is not the
// air and not the satellite; it is something about carrying a camera.
//
// This replay is the traffic that was ADDED today: 3.3 extra ESP-NOW sends a
// second that a cameraless mount never makes.  Turning it off separates "the
// extra traffic tips a marginal link" from "BLE coexistence starves ESP-NOW",
// which want completely different answers.  The cost while off is only that a
// late-joining client waits for the camera to report something before its gain
// and white balance appear.
#ifndef CAM_REPLAY_MS
#define CAM_REPLAY_MS   300UL
#endif

struct BcCachedStatus { uint8_t len; uint8_t data[CAM_CONTROL_MAX_LEN]; };
static BcCachedStatus _bc_cache[CAM_CACHE_MAX];
static uint8_t        _bc_ncache   = 0;
static uint32_t       _bc_replay_ms = 0;
static uint8_t        _bc_replay_i  = 0;   // next cache entry to re-offer
static bool           _bc_cache_full = false;  // dropped at least one parameter

// BMD framing: [4]=category [5]=parameter identify the value being reported.
static void bc_cache_store(const uint8_t *d, uint16_t n) {
    if (n < 6 || n > CAM_CONTROL_MAX_LEN) return;
    for (uint8_t i = 0; i < _bc_ncache; i++) {
        if (_bc_cache[i].len < 6) continue;
        if (_bc_cache[i].data[4] != d[4] || _bc_cache[i].data[5] != d[5]) continue;
        memcpy(_bc_cache[i].data, d, n);        // same parameter — newest wins
        _bc_cache[i].len = (uint8_t)n;
        return;
    }
    // Full: drop rather than evict, so the replay cannot flap between two
    // parameters fighting for the last slot.  With the cap at 32 this should be
    // unreachable; it is a backstop, not a policy.
    if (_bc_ncache >= CAM_CACHE_MAX) {
        // Should not happen at 32.  If it ever does, the tail is being dropped
        // again and the symptom is a value that never appears — so say it once
        // rather than let it look like a dead camera.
        // Latched, and reported in health: on a rig this serial line is inside
        // the enclosure, so saying it only here would be saying it nowhere.
        _bc_cache_full = true;
        static bool moaned = false;
        if (!moaned) {
            moaned = true;
            Serial.printf("[CAM] status cache FULL at %d — parameter %u/%u dropped "
                          "and will never be reported\n", CAM_CACHE_MAX, d[4], d[5]);
        }
        return;
    }
    memcpy(_bc_cache[_bc_ncache].data, d, n);
    _bc_cache[_bc_ncache].len = (uint8_t)n;
    _bc_ncache++;
}

static void (*_bc_status_cb)(const uint8_t *, uint16_t) = nullptr;
void ble_cam_on_status(void (*cb)(const uint8_t *, uint16_t)) { _bc_status_cb = cb; }

// GATT discovery, run after encryption because this characteristic is not
// readable before it.  Two async steps: find the service, then the
// characteristic inside it.
// Enable notifications by writing 0x0001 to the CCCD, once we have found it.
static int bc_on_dsc(uint16_t conn, const struct ble_gatt_error *err,
                     uint16_t chr_val_handle, const struct ble_gatt_dsc *dsc,
                     void *) {
    // chr_val_handle is checked, not just the UUID.  The discovery window is a
    // handle range, and a range that overruns into the NEXT characteristic
    // would find ITS 0x2902 — writing that reports success and subscribes us to
    // something we never read.  Silence afterwards would look identical.
    if (err->status == 0 && dsc && chr_val_handle == _bc_notify_handle
                                && ble_uuid_u16(&dsc->uuid.u) == 0x2902) {
        _bc_cccd_handle = dsc->handle;      // remembered, written below
        return 0;
    }
    if (err->status == BLE_HS_EDONE && _bc_cccd_handle) {
        // 0x02 = INDICATIONS, not 0x01 notifications.
        //
        // This characteristic indicates; it never notifies.  Writing 0x01
        // enables a thing the camera does not send, and the write SUCCEEDS —
        // so the mount reported "subscribed" and then sat in silence, even
        // while ISO and white balance were being changed on the camera body.
        //
        // BlueMagic32 says so in one argument that is easy to read past:
        //     _incomingCameraControl->registerForNotify(controlNotify, false);
        // and in the library that flag is exactly this byte:
        //     uint8_t val[] = {0x01, 0x00};
        //     if (!notifications) val[0] = 0x02;
        // Its other two subscriptions (timecode, camera status) leave the flag
        // at its default and do use notifications, which is why the difference
        // is deliberate rather than incidental.
        //
        // Indications arrive through the same BLE_GAP_EVENT_NOTIFY_RX; NimBLE
        // sends the ATT confirmation itself, so nothing else changes.
        //
        // Now, and not before: writing from inside the discovery would be a
        // second procedure while the first is still running.
        static const uint8_t on[2] = { 0x02, 0x00 };
        int rc = ble_gattc_write_flat(conn, _bc_cccd_handle, on, sizeof(on),
                                      nullptr, nullptr);
        _bc_subscribed = (rc == 0);
        // Timed from boot because that is the wait people actually experience —
        // a mount restarts and someone stands watching the camera do nothing.
        // Reported so "it feels like a while" can be checked against a number.
        Serial.printf("[CAM] status indications %s — camera usable %lu ms after boot\n",
                      rc ? "FAILED" : "enabled", (unsigned long)millis());
    } else if (err->status == BLE_HS_EDONE) {
        Serial.println("[CAM] no CCCD found — camera will not notify");
    }
    return 0;
}

static int bc_on_chr(uint16_t conn, const struct ble_gatt_error *err,
                     const struct ble_gatt_chr *chr, void *arg) {
    bool is_notify = (arg != nullptr);
    if (err->status == 0 && chr) {
        if (is_notify) {
            _bc_notify_handle = chr->val_handle;
            Serial.printf("[CAM] status characteristic (handle %u)\n",
                          _bc_notify_handle);
        } else {
            _bc_ctrl_handle = chr->val_handle;
            Serial.printf("[CAM] control characteristic ready (handle %u)\n",
                          _bc_ctrl_handle);
        }
        return 0;
    }
    if (err->status == BLE_HS_EDONE) {
        if (!is_notify) {
            // Control characteristic done — now the notify one.
            static ble_uuid_any_t ui;
            ble_uuid_from_str(&ui, CAM_INCOMING);
            ble_gattc_disc_chrs_by_uuid(conn, _bc_svc_start, _bc_svc_end,
                                        &ui.u, bc_on_chr, (void *)1);
        } else if (_bc_notify_handle) {
            // ...and now its CCCD.  It sits just after the characteristic's
            // value handle, so a range of +3 finds it without walking the
            // whole service.
            ble_gattc_disc_all_dscs(conn, _bc_notify_handle,
                                    _bc_notify_handle + 3, bc_on_dsc, nullptr);
        } else {
            Serial.println("[CAM] status characteristic not found");
        }
        return 0;
    }
    Serial.printf("[CAM] characteristic discovery failed, status=%d\n",
                  err->status);
    return 0;
}

static int bc_on_svc(uint16_t conn, const struct ble_gatt_error *err,
                     const struct ble_gatt_svc *svc, void *) {
    if (err->status == 0 && svc) {
        // Remembered so the chained discoveries below know where to look.
        _bc_svc_start = svc->start_handle;
        _bc_svc_end   = svc->end_handle;
        static ble_uuid_any_t uo;
        ble_uuid_from_str(&uo, CAM_OUTGOING);  // we WRITE here; notify next
        ble_gattc_disc_chrs_by_uuid(conn, _bc_svc_start, _bc_svc_end,
                                    &uo.u, bc_on_chr, nullptr);
    } else if (err->status != BLE_HS_EDONE) {
        Serial.printf("[CAM] service discovery failed, status=%d\n", err->status);
    }
    return 0;
}

// Relay a Blackmagic command to the camera, byte for byte.  The mount never
// interprets it — see CMD_CAM_CONTROL in protocol.h for why.
bool ble_cam_send(const uint8_t *cmd, uint16_t len) {
    if (!_bc_connected || !_bc_ctrl_handle) return false;
    if (!len || len > CAM_CONTROL_MAX_LEN) return false;
    int rc = ble_gattc_write_flat(_bc_conn, _bc_ctrl_handle, cmd, len, nullptr, nullptr);
    if (rc) { Serial.printf("[CAM] write rc=%d\n", rc); _bc_write_err = true; }
    return rc == 0;
}

static int bc_gap_event(struct ble_gap_event *ev, void *) {
    switch (ev->type) {

    case BLE_GAP_EVENT_CONNECT:
        if (ev->connect.status != 0) {
            Serial.printf("[CAM] connect failed, status=%d\n", ev->connect.status);
            _bc_conn = BLE_HS_CONN_HANDLE_NONE;
            return 0;
        }
        _bc_conn = ev->connect.conn_handle;
        Serial.printf("[CAM] CONNECTED (handle %u)\n", _bc_conn);
        {
            // Ask, but do not care.  EALREADY means the camera got there first,
            // which is fine — this is the line the wrapper dies on.
            int rc = ble_gattc_exchange_mtu(_bc_conn, nullptr, nullptr);
            if (rc) Serial.printf("[CAM] (MTU exchange rc=%d — ignored)\n", rc);
        }
        // Pairing has to be asked for; the camera will not volunteer it.
        if (int rc = ble_gap_security_initiate(_bc_conn))
            Serial.printf("[CAM] security_initiate rc=%d\n", rc);
        return 0;

    case BLE_GAP_EVENT_PASSKEY_ACTION:
        if (ev->passkey.params.action == BLE_SM_IOACT_INPUT) {
            if (_bc_pair_mode) {
                // Park it.  The camera is displaying the code as of now, and the
                // answer arrives from a touchscreen an unknown number of seconds
                // later — a thing to wait for on the UI task, never on this one.
                _bc_pk_conn    = ev->passkey.conn_handle;
                _bc_pair_state = BCP_WANT_CODE;
                Serial.println("[CAM] camera is showing a code — waiting for the keypad");
            } else if (bc_bonded_count() == 0) {
                // Not in pairing mode and bonded to nothing: no reconnect can
                // create a bond, so stop rather than put a failed-pairing prompt
                // on the camera every 10 s for as long as the rig is powered.
                // Should be unreachable now that an unbonded mount never
                // initiates outside pairing mode.  Kept as a backstop: if it
                // ever fires, something reached a camera it had no business
                // reaching, and saying so beats failing quietly.
                Serial.println("[CAM] passkey asked for with no bond and not in "
                               "pairing mode — declining");
                ble_gap_terminate(ev->passkey.conn_handle, BLE_ERR_REM_USER_CONN_TERM);
            } else {
                // We ARE paired, just not to this camera — a neighbour's, picked
                // while ours was out of range.  Recoverable, so drop the choice
                // and rescan rather than latching a working mount out of service.
                Serial.println("[CAM] that is not our camera (no bond with it) — "
                               "dropping it and rescanning");
                _bc_chosen = false;
                ble_gap_terminate(ev->passkey.conn_handle, BLE_ERR_REM_USER_CONN_TERM);
            }
        } else {
            Serial.printf("[CAM] unexpected passkey action %d\n",
                          ev->passkey.params.action);
        }
        return 0;

    case BLE_GAP_EVENT_ENC_CHANGE:
        Serial.printf("[CAM] encryption %s (status=%d)\n",
                      ev->enc_change.status ? "FAILED" : "ESTABLISHED — PAIRED",
                      ev->enc_change.status);
        if (_bc_pair_mode)
            _bc_pair_state = ev->enc_change.status ? BCP_FAILED : BCP_PAIRED;
        // Encryption established means the bond is now in NVS, so the mount may
        // reconnect on its own from here without the pairing screen.
        if (ev->enc_change.status == 0) _bc_have_bond = true;
        if (ev->enc_change.status == 0) {
            _bc_connected = true;
            _bc_since_ms  = millis();
            // Only now: the control characteristic is not reachable before the
            // link is encrypted.
            ble_uuid_any_t u;
            ble_uuid_from_str(&u, CAM_SERVICE);
            ble_gattc_disc_svc_by_uuid(_bc_conn, &u.u, bc_on_svc, nullptr);
        }
        return 0;

    case BLE_GAP_EVENT_DISCONNECT:
        Serial.printf("[CAM] disconnected (reason %d)\n", ev->disconnect.reason);
        _bc_conn        = BLE_HS_CONN_HANDLE_NONE;
        _bc_connected   = false;
        _bc_ctrl_handle   = 0;    // handles do not survive a connection
        _bc_notify_handle = 0;
        _bc_cccd_handle   = 0;
        _bc_subscribed    = false;
        // Forget the readings too.  Replaying them would keep a dead camera's
        // last gain on screen indefinitely, which is worse than showing nothing:
        // the dash is honest about not knowing, a stale number is not.
        _bc_ncache        = 0;
        _bc_cache_full    = false;
        _bc_replay_i      = 0;
        return 0;

    case BLE_GAP_EVENT_NOTIFY_RX: {
        _bc_notifies++;
        // Hand the bytes up untouched — the mount does not decode camera
        // status any more than it decodes camera commands.
        if (ev->notify_rx.om) {
            uint8_t buf[CAM_CONTROL_MAX_LEN];
            uint16_t n = OS_MBUF_PKTLEN(ev->notify_rx.om);
            if (n && n <= sizeof(buf) &&
                ble_hs_mbuf_to_flat(ev->notify_rx.om, buf, sizeof(buf), &n) == 0) {
                bc_cache_store(buf, n);
                if (_bc_status_cb) _bc_status_cb(buf, n);
            }
        }
        return 0;
    }

    default:
        return 0;
    }
}

// Returns false if the attempt could not even be started.
static bool bc_connect(const char *addr_text) {
    ble_addr_t a = {};
    a.type = BLE_ADDR_PUBLIC;
    unsigned v[6];
    if (sscanf(addr_text, "%x:%x:%x:%x:%x:%x",
               &v[0], &v[1], &v[2], &v[3], &v[4], &v[5]) != 6) return false;
    // ble_addr_t.val is little-endian — the reverse of the printed form.
    for (int i = 0; i < 6; i++) a.val[i] = (uint8_t)v[5 - i];
    int rc = ble_gap_connect(BLE_OWN_ADDR_PUBLIC, &a, 15000, nullptr,
                             bc_gap_event, nullptr);
    if (rc) Serial.printf("[CAM] ble_gap_connect rc=%d\n", rc);
    return rc == 0;
}

static int                   _bc_best_rssi = -999;
static bool                  _bc_by_name   = false;
static char                  _bc_pick_text[20] = {};

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
struct BcCand { char addr[20]; char name[26]; uint8_t nat[6];
                int16_t rssi; bool svc; bool named; };
static BcCand  _bc_cand[BC_MAX_CAND];
static uint8_t _bc_ncand = 0;
// The name from the camera's Bluetooth menu.  Substring, so "BMPCC" matches
// "Colin BMPCC".  Override at build time if yours is named otherwise.
#ifndef CAM_NAME
#define CAM_NAME  "BMPCC"
#endif
// A camera on this mount should be strong.  Weaker than this is worth SAYING
// on a rig where several mounts each carry one — connecting to a neighbour's
// camera would look like success — but it is only a warning, not a veto: a
// metal-bodied camera at arm's length can read -66, and refusing to try would
// have blocked the only camera in the room on a bench with one.
#define CAM_WEAK_RSSI  (-65)

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
                   dev.isAdvertisingService(BLEUUID(CAM_SERVICE));
        bool named = nm.length() && (nm.indexOf(CAM_NAME) >= 0);
        Serial.printf("[CAM]  seen \"%s\" %s %d dBm%s%s\n",
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
            memcpy(c.nat, dev.getAddress().getNative(), 6);
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
        Serial.printf("[CAM] found \"%s\" %s | rssi %d | addrtype %u | "
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
            Serial.print("[CAM] raw adv: ");
            for (size_t i = 0; i < n && i < 62; i++) Serial.printf("%02X", pl[i]);
            Serial.println();
            Serial.printf("[CAM] svc uuid: %s | count %d\n",
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

class BcSecCb : public BLESecurityCallbacks {
    // Never reached: pairing runs through bc_gap_event()'s PASSKEY_ACTION, not
    // the wrapper.  Returning 0 rather than prompting keeps it that way — a
    // second, blocking path into the same procedure is how the watchdog problem
    // started.
    uint32_t onPassKeyRequest() override { return 0; }
    void onPassKeyNotify(uint32_t pass) override {
        Serial.printf("[CAM] camera shows %06lu\n", (unsigned long)pass);
    }
    bool onSecurityRequest() override { return true; }
    bool onConfirmPIN(uint32_t pin) override {
        Serial.printf("[CAM] confirm %06lu\n", (unsigned long)pin);
        return true;
    }
    // onAuthenticationComplete() is Bluedroid-only and this core builds the BLE
    // library on NimBLE, so pairing success is judged by whether the service
    // actually resolves below rather than by a callback that never fires.
    bool onAuthorizationRequest(uint16_t, uint16_t, bool) override { return true; }
};

// Pairing is no longer a build.  It is a runtime mode the operator enters from
// the mount's screen (ble_cam_pair_begin below), which is why the task watchdog
// that this used to switch off now stays on: nothing blocks any more.
//
// CAM_PAIR_KEEP_WIFI=1 is the one build-time knob left, and it exists only to
// retest an assumption.  Pairing stops WiFi because with WiFi running the camera
// was found every time and the connection never completed — read at the time as
// the S3 giving WiFi the radio and starving BLE of the sustained time a pairing
// needs.  That reading predates the discovery that the core's BLEClient was
// tearing down every connection over the MTU exchange, which explains the same
// symptom without invoking coexistence at all.  If pairing completes with the
// radio shared, the stop can go and a mount stays reachable while it pairs.
static void ble_cam_setup() {
    BLEDevice::init("PTS-Mount");
    BLEDevice::setPower(ESP_PWR_LVL_P9);          // as BlueMagic32 does
    BLEDevice::setSecurityCallbacks(new BcSecCb());

    // Read the bond store once, now the host is up.  Everything downstream
    // gates on this rather than re-asking, and a mount with no bond will not
    // touch a camera at all — see _bc_have_bond.
    _bc_have_bond = bc_bonded_count() > 0;
    Serial.printf("[CAM] %s\n", _bc_have_bond
                  ? "bonded to a camera — will reconnect on its own"
                  : "no camera bond — idle until paired from the screen");

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

// Print what was found and pick.  Returns true once a choice is held.

static volatile bool _bc_scanning   = false;
static volatile bool _bc_scan_ready = false;

// Runs on the BLE task, so it does no work beyond saying the scan is over.
// Choosing can wait fifteen seconds for a keystroke, which has no business
// happening on that task.
static void bc_scan_done(BLEScanResults) {
    _bc_scanning   = false;
    _bc_scan_ready = true;
}

static bool bc_choose() {
    if (!_bc_ncand) { Serial.println("[CAM] scan found nothing at all"); return false; }

    // Match on the SERVICE.  The name is now recovered from the raw payload as
    // well and shown in the list, but the service UUID is the reliable
    // identifier: a camera could be renamed to anything, and 0x1800 is what
    // getServiceUUID() returns for this one anyway.
    int only_named = -1, n_named = 0;
    for (uint8_t i = 0; i < _bc_ncand; i++)
        if (_bc_cand[i].svc) { only_named = i; n_named++; }

    // The bonded camera wins outright, however many are in range.
    //
    // Without this, a rig is unusable: the auto-pick below only fires when
    // exactly ONE Blackmagic camera is visible, so with five mounts each
    // carrying one, every mount would fall through to the keystroke prompt —
    // and there is no serial monitor on a rig to type into, so every mount
    // would rescan for ever and none would ever connect.  It worked on the
    // bench only because there was a single camera in the room.
    //
    // Strongest-of-the-bonded rather than first: a mount re-paired to a
    // replacement camera keeps the old bond in NVS, and the one actually
    // bolted to it is the near one.
    int bonded_i = -1, n_bonded = 0;
    for (uint8_t i = 0; i < _bc_ncand; i++)
        if (bc_is_bonded(_bc_cand[i].nat)) {
            n_bonded++;
            if (bonded_i < 0 || _bc_cand[i].rssi > _bc_cand[bonded_i].rssi) bonded_i = i;
        }

    Serial.println("\n[CAM] ---- devices in range ----");
    for (uint8_t i = 0; i < _bc_ncand; i++) {
        BcCand &c = _bc_cand[i];
        Serial.printf("[CAM]  %2u) %-26s %-18s %4d dBm%s\n",
                      i + 1, c.name[0] ? c.name : "(no name)", c.addr, c.rssi,
                      bc_is_bonded(c.nat) ? "  <-- PAIRED WITH THIS MOUNT"
                                          : (c.svc ? "  <-- BLACKMAGIC CAMERA" : ""));
    }

    int pick = -1;
    if (n_bonded) {
        pick = bonded_i;
        Serial.printf("[CAM] paired camera in range — using %u%s\n", pick + 1,
                      n_bonded > 1 ? " (strongest of several bonded)" : "");
    } else if (!_bc_pair_mode) {
        // A bonded mount reaches for ITS camera or for nothing.  Falling
        // through to "one Blackmagic camera in range" would have a mount whose
        // own camera is switched off go and connect to a NEIGHBOUR's — getting
        // as far as the security exchange before being refused, and taking that
        // camera's one connection slot with it on the way.
        //
        // Picking a camera you are not bonded to is only ever right when a
        // human has opened the pairing screen and is standing there to type the
        // code.  Everywhere else it is a mount interfering with another mount.
        Serial.println("[CAM] our camera is not in range — waiting (will not "
                       "reach for another mount's)");
        return false;
    } else if (n_named == 1) {
        pick = only_named;
        Serial.printf("[CAM] one Blackmagic camera in range — using %u\n", pick + 1);
    } else {
        // NON-BLOCKING.  An earlier version waited fifteen seconds here for a
        // keystroke, on the main loop — and on a rig there is no serial monitor
        // attached to type into, so every ambiguous scan cost fifteen seconds
        // of dead motion control.  The rig showed it: loopmax 15002ms.
        //
        // Only wait if somebody is actually typing.  Nothing buffered means
        // nobody is there, so say what was found and rescan instead of
        // stopping the mount to wait for an operator who does not exist.
        if (!Serial.available()) {
            Serial.printf("[CAM] no single Blackmagic camera in range — "
                          "type 1-%u while a scan result is fresh to force one, "
                          "otherwise rescanning\n", _bc_ncand);
            return false;
        }
        int v = 0; bool any = false;
        while (Serial.available()) {
            int ch = Serial.read();
            if (ch == '\r' || ch == '\n') break;
            if (ch >= '0' && ch <= '9') { v = v * 10 + (ch - '0'); any = true; Serial.write(ch); }
        }
        Serial.println();
        if (!any || v < 1 || v > _bc_ncand) {
            Serial.println("[CAM] no valid choice — rescanning");
            return false;
        }
        pick = v - 1;
    }

    if (_bc_found) { delete _bc_found; _bc_found = nullptr; }
    snprintf(_bc_pick_text, sizeof(_bc_pick_text), "%s", _bc_cand[pick].addr);
    _bc_best_rssi  = _bc_cand[pick].rssi;
    _bc_chosen     = true;
    Serial.printf("[CAM] chose %s (%d dBm)\n", _bc_pick_text, _bc_best_rssi);
    return true;
}

// Reported in every CMD_HEALTH, because a mount on a rig has no readable serial
// port — the enclosure is shut and the Teensy owns the USB cable — so [CAM]
// output is invisible exactly where the camera actually is.  These bits land in
// comms.log instead, beside txfail, which is what they get compared with.
// BLE_BUILD is unconditional: the PC app uses its absence to tell "old firmware"
// apart from "camera switched off", which look the same from a dark button.
static uint8_t ble_cam_health_flags() {
    uint8_t f = HEALTH_FLAG_BLE_BUILD | (_bc_connected ? HEALTH_FLAG_BLE_LINK : 0)
              | (_bc_have_bond ? 0 : HEALTH_FLAG_CAM_UNPAIRED)
              | (_bc_cache_full ? HEALTH_FLAG_CAM_CACHE_FULL : 0)
              | (_bc_subscribed ? HEALTH_FLAG_CAM_SUBSCR : 0)
              | (_bc_notifies   ? HEALTH_FLAG_CAM_RX     : 0);
    if (_bc_write_err) { f |= HEALTH_FLAG_CAM_WR_ERR; _bc_write_err = false; }
    return f;
}

static void ble_cam_poll() {
    uint32_t now = millis();

    // ble_gap_connect() is asynchronous: this only starts an attempt, and
    // bc_gap_event() carries it through connect -> passkey -> encrypted.  So
    // there is no long blocking call in loop() any more, and nothing for the
    // task watchdog to trip over — the reason it had to be disabled was the
    // wrapper's blocking connect(), which is gone.
    bool busy = _bc_connected || _bc_conn != BLE_HS_CONN_HANDLE_NONE || _bc_scanning;
    // _bc_retry_ms == 0 means "try now".  Without that case the arithmetic
    // below reads (now - 0) > CAM_RETRY_MS, so a freshly booted mount sits for
    // a full ten seconds before it even begins looking for its camera — dead
    // time on top of the 5 s scan and the connect, which is most of why a
    // reboot feels slow to someone standing at the mount.
    //
    // The same zero is written by ble_cam_forget() and ble_cam_pair_begin(),
    // where "try now" is exactly what is wanted too.
    bool due = (_bc_retry_ms == 0) || ((now - _bc_retry_ms) > CAM_RETRY_MS);
    // No bond and not pairing: do not go looking.  See _bc_have_bond — an
    // unbonded mount that hunts for cameras takes the link away from whichever
    // mount owns the one it finds.
    if (!busy && (_bc_pair_mode || _bc_have_bond) && due) {
        _bc_retry_ms = now ? now : 1;
        if (!_bc_chosen) {
            if (!_bc_scan_ready) {
                // Asynchronous.  The blocking form stopped loop() dead for its
                // whole duration, and the rig showed it plainly:
                //   loopmax 5054ms ... loopmax 20003ms
                // Harmless on a bench; on a rig a camera that is switched off
                // would mean five seconds of no motion control every ten, which
                // is worse than having no camera control at all.
                _bc_ncand    = 0;
                _bc_scanning = true;
                BLEDevice::getScan()->start(5, bc_scan_done, false);
                return;
            }
            _bc_scan_ready = false;
            if (!bc_choose()) {
                // Tell the pairing screen, rather than leaving it saying
                // "searching" at somebody who is standing there with a camera
                // that is switched off.
                if (_bc_pair_mode) _bc_pair_state = BCP_NO_CAMERA;
                return;                    // nothing picked — rescan next time
            }
            if (_bc_pair_mode && _bc_pair_state == BCP_NO_CAMERA)
                _bc_pair_state = BCP_SEARCHING;
        }
        // NimBLE will not start a connection while discovery is running.
        BLEDevice::getScan()->stop();
        delay(50);
        Serial.printf("[CAM] connecting to %s (%d dBm)\n",
                      _bc_pick_text, _bc_best_rssi);
        if (!bc_connect(_bc_pick_text)) {
            Serial.println("[CAM] could not start the attempt — rescanning");
            _bc_chosen = false;
        }
    }

    // Re-offer what the camera has already said.  Only while connected and only
    // what it actually reported, so this invents nothing — it just stops the
    // information being a one-shot that a client had to be present to catch.
    if (CAM_REPLAY_MS && _bc_connected && _bc_ncache &&
            (now - _bc_replay_ms) >= CAM_REPLAY_MS) {
        _bc_replay_ms = now;
        if (_bc_replay_i >= _bc_ncache) _bc_replay_i = 0;
        if (_bc_status_cb)
            _bc_status_cb(_bc_cache[_bc_replay_i].data, _bc_cache[_bc_replay_i].len);
        _bc_replay_i++;
    }

    if ((now - _bc_report_ms) >= CAM_REPORT_MS) {
        _bc_report_ms = now;
        // Printed even when disconnected, on purpose: "no ESP-NOW impact"
        // means nothing if the link was down for the window.
        Serial.printf("[CAM] %s | up %lus | %lu notifications\n",
                      _bc_connected ? "PAIRED" : "not connected",
                      (unsigned long)(_bc_connected ? (now - _bc_since_ms) / 1000UL : 0),
                      (unsigned long)_bc_notifies);
    }
}


// ---------------------------------------------------------------------------
// Pairing, driven by the mount's screen
// ---------------------------------------------------------------------------

#ifndef CAM_PAIR_KEEP_WIFI
#define CAM_PAIR_KEEP_WIFI 0
#endif

BcPairState ble_cam_pair_state() { return _bc_pair_state; }

// Camera state for the mount's OWN screen.  Separate from ble_cam_health_flags()
// because that one latches — it clears _bc_write_err as a side effect — and a
// UI refresh must never consume a fault the PC app has not seen yet.
//
// BCU_NONE for a mount with no camera bond, so the four mounts without one show
// nothing rather than a permanent empty indicator.
enum BcUiState : uint8_t { BCU_NONE = 0, BCU_LINKING, BCU_READY, BCU_UNPAIRED };
BcUiState ble_cam_ui_state() {
    if (_bc_connected && _bc_subscribed) return BCU_READY;
    // Deliberately NOT reporting "unpaired" on the mount's own screen: on a rig
    // with fewer cameras than mounts that is most of them, and a permanent
    // warning about a camera a mount does not have is noise where it stands.
    // The PC app still shows it, which is where someone decides what to pair.
    if (!_bc_have_bond)                  return BCU_NONE;
    if (bc_bonded_count() > 0)           return BCU_LINKING;
    return BCU_NONE;
}

// Enter pairing mode.  Stops WiFi so BLE has the radio (see ble_cam_setup), so
// the mount goes off the air until pairing ends — deliberate and acceptable for
// a one-time bench operation, but it does mean the hub will see this mount drop.
void ble_cam_pair_begin() {
    if (_bc_pair_mode) return;
    _bc_pair_mode  = true;
    _bc_pair_state = BCP_SEARCHING;

    // Clear the giving-up latch and the current choice, so a mount that has
    // already decided it is unpaired will actually go and look again — without
    // this, entering pairing on the mount that most needs it would do nothing.
    _bc_chosen   = false;
    _bc_retry_ms = 0;

#if !CAM_PAIR_KEEP_WIFI
    Serial.println("[CAM] pairing — stopping WiFi, this mount is off the air");
    esp_wifi_stop();
    delay(200);
#else
    Serial.println("[CAM] pairing with WiFi LEFT UP (CAM_PAIR_KEEP_WIFI)");
#endif
}

// Leave pairing mode.  Restarting the chip rather than restarting WiFi by hand:
// the bond is already in NVS, a fresh boot re-runs the base pick and the normal
// camera connect, and it avoids having to unpick WiFi/ESP-NOW state that was
// torn down mid-flight.  setup_apply_save() takes the same view for the same
// reason.
void ble_cam_pair_end() {
    _bc_pair_mode  = false;
    _bc_pair_state = BCP_OFF;
#if !CAM_PAIR_KEEP_WIFI
    Serial.println("[CAM] leaving pairing — restarting");
    delay(300);
    esp_restart();
#endif
}

// The keypad's answer.  Runs on the UI task; ble_sm_inject_io takes the host
// lock itself, so it does not have to be marshalled onto the BLE task.
bool ble_cam_pair_submit(uint32_t code) {
    if (_bc_pair_state != BCP_WANT_CODE) return false;
    struct ble_sm_io io = {};
    io.action  = BLE_SM_IOACT_INPUT;
    io.passkey = code;
    int rc = ble_sm_inject_io(_bc_pk_conn, &io);
    Serial.printf("[CAM] passkey %06lu injected, rc=%d\n", (unsigned long)code, rc);
    // Back to SEARCHING rather than straight to a verdict: the answer is
    // BLE_GAP_EVENT_ENC_CHANGE, a moment later.  Claiming success here would
    // show PAIRED for a code the camera is about to reject.
    _bc_pair_state = rc ? BCP_FAILED : BCP_SEARCHING;
    return rc == 0;
}

// Forget every camera this mount is bonded to.
//
// Needed because cameras move between mounts.  A stale bond is not inert: the
// boot pick PREFERS the bonded camera, so a mount still holding a bond to a
// camera that now lives on another mount will keep reaching for it instead of
// pairing with the one in front of it.
//
// Note this only clears OUR side.  The camera keeps its own bond, and a camera
// that thinks it is still paired may refuse to show a code — its own "forget"
// may be needed too.  Said on serial rather than guessed at silently.
void ble_cam_forget() {
    if (_bc_conn != BLE_HS_CONN_HANDLE_NONE)
        ble_gap_terminate(_bc_conn, BLE_ERR_REM_USER_CONN_TERM);

    ble_addr_t peers[8]; int n = 0;
    int gone = 0;
    if (ble_store_util_bonded_peers(peers, &n, 8) == 0)
        for (int i = 0; i < n; i++)
            if (ble_gap_unpair(&peers[i]) == 0) gone++;

    _bc_ncache     = 0;
    _bc_cache_full = false;
    _bc_have_bond  = false;   // just deleted every bond we had
    _bc_chosen     = false;
    _bc_replay_i   = 0;
    _bc_retry_ms   = 0;
    if (_bc_pair_mode) _bc_pair_state = BCP_SEARCHING;
    Serial.printf("[CAM] forgot %d bond(s). If the camera still thinks it is "
                  "paired, forget this mount on the camera too.\n", gone);
}
