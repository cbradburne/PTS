#pragma once
/*
 * usb_pad.h — one game controller on the board's USB-A port.
 *
 * The USB-A socket is the ESP32-P4's high-speed USB OTG port, run here as a
 * host.  Its 5 V is always on (the board has no switch for it).  The library
 * runs on core 0, away from the LVGL task on core 1, and the UI reads the
 * controller through usb_pad_get(), which may be called from any task.
 *
 * One controller at a time: the first one plugged in that it can read.  A
 * second is left alone and named in the log.
 */
#include <stdint.h>
#include "pad_reports.h"

struct PadSnapshot {
    bool     connected = false;
    PadKind  kind = PadKind::NONE;
    uint16_t vid = 0, pid = 0;
    char     name[40] = "";        // its own product string, else its kind
    PadInput in;
    uint32_t reports = 0;          // input reports since it was plugged in
    char     refused[48] = "";     // the last device plugged in that is not a pad it reads
};

void usb_pad_begin();
void usb_pad_get(PadSnapshot *out);

// The controller's own lights, where it has them: the DualSense's and
// DualShock 4's light bar in (r, g, b), and player lights showing `player`
// (1-5) on a DualSense, Xbox 360 type or Switch Pro.  Kept and re-sent when a
// controller is plugged in.
void usb_pad_set_light(uint8_t r, uint8_t g, uint8_t b, uint8_t player);
