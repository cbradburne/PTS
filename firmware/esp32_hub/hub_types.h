#pragma once
/*
 * hub_types.h — private types for esp32_hub that must be defined before
 *               Arduino's auto-generated function prototypes are inserted.
 *
 * Arduino IDE injects generated prototypes immediately after the last
 * #include in the .ino file.  Any struct used as a parameter type in a
 * sketched function must therefore be defined in an included header, not
 * in the .ino body.
 */

#include "../shared/protocol.h"

struct RelayMsg {
    uint8_t  data[PKT_BUF_SIZE + 4];
    uint16_t len;
    int8_t   rssi;
    uint8_t  src_idx;     // bound slot 0-4, or 0xFF if the sender MAC is unbound
    uint8_t  src_mac[6];  // sender MAC — used by the pairing rules in loop()
};

// Raw bytes received from a WebSocket client — queued from the AsyncTCP task
// and processed in the main loop so all WS state access is single-task.
struct WsRxMsg {
    uint8_t  data[PKT_BUF_SIZE];
    uint16_t len;
};
