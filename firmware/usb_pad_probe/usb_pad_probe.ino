/*
 * usb_pad_probe.ino — the USB controller test on the 7" display, with nothing
 * else running: no screen, no hub link.
 *
 * The question and the output are described in shared/usb_pad_probe.h.  This
 * build answers the first half — does a controller get power and enumerate on
 * the display's "USB" port, and what does it send.  The memory half needs the
 * screen running too:
 *
 *   USB_PAD_PROBE=1 tools/build.sh flash display <port>
 *
 * Use this one if that build will not boot, or shows nothing: if a controller
 * works here and not there, the display firmware is in the way, not the board.
 *
 * The screen stays dark — nothing here drives it.
 *
 *   tools/build.sh flash usbprobe <port>     (same board settings as the display)
 *
 * Wiring: computer to the "USB to UART" Type-C (power, console at 115200, and
 * flashing — it has the auto-reset circuit), controller to the "USB" Type-C.
 * Switch 15 on "USB to UART".  A USB-C controller needs a C-to-C cable, a
 * USB-A one a USB-C OTG adapter.
 */

#include "../shared/usb_pad_probe.h"

void setup() {
    Serial0.setTxBufferSize(4096);    // a descriptor dump must not stall the USB task
    Serial0.begin(115200);
    delay(200);
    usb_pad_probe_begin(Serial0);
}

void loop() {
    delay(1000);
}
