#pragma once
// ---------------------------------------------------------------------------
// Ethernet for the Waveshare ESP32-S3-ETH (W5500 over SPI, PoE variant).
// ---------------------------------------------------------------------------
// Both the hub and the satellites run this board, so the bring-up lives here.
//
// >> THE PIN MAP BELOW IS UNVERIFIED. <<
// It has not been checked against a board or Waveshare's schematic.  Confirm
// every line before the first flash.  Getting it wrong does not damage
// anything — the W5500 simply never answers — and eth_begin() prints the exact
// pins it used at boot, so a mismatch is one glance at the serial log:
//
//   [ETH] W5500 on SPI  cs=14 int=13 rst=9  sck=12 miso=11 mosi=10
//   [ETH] link down after 5000 ms — check the pin map in shared/board_eth.h
//
// The Arduino ESP32 core (3.x) drives the W5500 natively via ETH.begin(), so
// there is no third-party Ethernet library to pin a version of.

#include <ETH.h>
#include <SPI.h>

#ifndef ETH_SPI_CS
#define ETH_SPI_CS    14
#define ETH_SPI_INT   13
#define ETH_SPI_RST    9
#define ETH_SPI_SCK   12
#define ETH_SPI_MISO  11
#define ETH_SPI_MOSI  10
#endif

// W5500 has no factory MAC, so one is derived from the ESP32's own — stable
// across reboots, unique per board, and no risk of two units colliding on the
// LAN the way a hard-coded literal would.
#ifndef ETH_PHY_ADDR
#define ETH_PHY_ADDR   1
#endif

static bool _eth_up = false;

inline bool eth_is_up() { return _eth_up; }

inline void eth_on_event(arduino_event_id_t event) {
    switch (event) {
        case ARDUINO_EVENT_ETH_CONNECTED:
            Serial.println("[ETH] link up");
            break;
        case ARDUINO_EVENT_ETH_GOT_IP:
            _eth_up = true;
            Serial.printf("[ETH] %s  gw %s\n",
                          ETH.localIP().toString().c_str(),
                          ETH.gatewayIP().toString().c_str());
            break;
        case ARDUINO_EVENT_ETH_LOST_IP:
        case ARDUINO_EVENT_ETH_DISCONNECTED:
        case ARDUINO_EVENT_ETH_STOP:
            if (_eth_up) Serial.println("[ETH] link down");
            _eth_up = false;
            break;
        default:
            break;
    }
}

// Bring the link up.  Non-blocking: DHCP completes via the event above, so the
// caller carries on and checks eth_is_up() before using the network.  A hub or
// satellite must stay useful with the cable out — ESP-NOW does not care — so
// nothing here is allowed to spin waiting for a lease.
inline bool eth_begin() {
    Network.onEvent(eth_on_event);
    SPI.begin(ETH_SPI_SCK, ETH_SPI_MISO, ETH_SPI_MOSI);
    Serial.printf("[ETH] W5500 on SPI  cs=%d int=%d rst=%d  sck=%d miso=%d mosi=%d\n",
                  ETH_SPI_CS, ETH_SPI_INT, ETH_SPI_RST,
                  ETH_SPI_SCK, ETH_SPI_MISO, ETH_SPI_MOSI);
    bool ok = ETH.begin(ETH_PHY_W5500, ETH_PHY_ADDR,
                        ETH_SPI_CS, ETH_SPI_INT, ETH_SPI_RST, SPI);
    if (!ok) {
        Serial.println("[ETH] ETH.begin() failed — SPI wiring or pin map wrong "
                       "(see shared/board_eth.h)");
    }
    return ok;
}

// Call periodically.  Only produces a one-shot warning: the link genuinely can
// be down for a while (patching, switch reboot) without anything being wrong,
// and a repeating error would bury the log the way the ANOMALY spam did.
inline void eth_report_once_if_down(uint32_t since_boot_ms, uint32_t warn_after_ms) {
    static bool warned = false;
    if (_eth_up) { warned = false; return; }
    if (!warned && since_boot_ms > warn_after_ms) {
        warned = true;
        Serial.printf("[ETH] no link/IP after %lu ms — check the cable, the switch "
                      "port, and the pin map in shared/board_eth.h\n",
                      (unsigned long)since_boot_ms);
    }
}
