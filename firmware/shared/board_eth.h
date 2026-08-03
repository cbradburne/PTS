#pragma once
// ---------------------------------------------------------------------------
// Ethernet for the Waveshare ESP32-S3-ETH (W5500 over SPI, PoE variant).
// ---------------------------------------------------------------------------
// Both the hub and the satellites run this board, so the bring-up lives here.
//
// Pin map confirmed against Waveshare's ESP32-S3-ETH pinout diagram.
// eth_begin() still prints what it used at boot, so a board revision that moves
// something shows up as one line in the log rather than a silent failure:
//
//   [ETH] W5500 on SPI  cs=14 int=10 rst=9  sck=13 miso=12 mosi=11
//
// ALSO SPOKEN FOR on this board, do not reuse:
//   GPIO 4,5,6,7   microSD slot (CS, MISO, MOSI, CLK)
//   GPIO 19,20     USB D-/D+ — the PC app rides that link
//   GPIO 43,44     UART0, used for the 7" display (confirmed broken out, free)
//
// The Arduino ESP32 core (3.x) drives the W5500 natively via ETH.begin(), so
// there is no third-party Ethernet library to pin a version of.

#include <ETH.h>
#include <SPI.h>

#ifndef ETH_SPI_CS
#define ETH_SPI_CS    14   // ETH_CS
#define ETH_SPI_INT   10   // ETH_INT
#define ETH_SPI_RST    9   // ETH_RST
#define ETH_SPI_SCK   13   // ETH_CLK
#define ETH_SPI_MISO  12   // ETH_MISO
#define ETH_SPI_MOSI  11   // ETH_MOSI
#endif

// W5500 has no factory MAC, so one is derived from the ESP32's own — stable
// across reboots, unique per board, and no risk of two units colliding on the
// LAN the way a hard-coded literal would.
#ifndef ETH_PHY_ADDR
#define ETH_PHY_ADDR   1
#endif

// Name this node answers to, and how it appears in the DHCP server's lease
// list.  Set per sketch before including this header.
#ifndef ETH_HOSTNAME
#define ETH_HOSTNAME "pts-node"
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
        return false;
    }

    // Order matters both ways.  setHostname() needs the netif ETH.begin() has
    // just created — called earlier it silently returns false — but it must
    // land before the W5500 finishes negotiating, or the DHCP DISCOVER goes out
    // without it and the lease list shows an anonymous espressif device.
    ETH.setHostname(ETH_HOSTNAME);

    // The MAC is printed HERE rather than alongside the IP, because a network
    // that only serves addresses to known MACs gives an unlisted board no lease
    // at all — waiting for an IP to reveal the MAC you need in order to be
    // granted one is a circle that never closes.  This is also NOT the MAC
    // printed as "AP MAC": the W5500 gets its own address derived from the
    // ESP32's, and it is the only one that ever appears on the wire.
    Serial.printf("[ETH] mac %s  host %s  (this is the address to register, "
                  "not the AP MAC)\n",
                  ETH.macAddress().c_str(), ETH_HOSTNAME);
    return true;
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
