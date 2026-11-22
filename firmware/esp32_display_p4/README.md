# Hub display on the ESP32-P4 7B

The hub display firmware for the **Waveshare ESP32-P4-WIFI6-Touch-LCD-7B**
(1024 × 600, MIPI-DSI), with a game controller in its USB-A port.

It is a separate firmware from `firmware/esp32_display` (the S3 board, 800 × 480),
which it leaves untouched.  If the P4 board doesn't work out, delete this folder.

**Status:** compiles for both chip revisions; the screens and the controller code
are tested on the Mac (below).  Nothing has run on the board yet.

## Wiring to the hub

Three wires, on the 12-pin header **P1** along the bottom edge:

| P1 pin | P4         | hub                        |
|--------|------------|----------------------------|
| 6      | GPIO47, TX | GPIO44, RX (`DISP_RX_PIN`) |
| 7      | GPIO46, RX | GPIO43, TX (`DISP_TX_PIN`) |
| 8      | GND        | GND                        |

Don't use the 4-pin "UART" connector: it is GPIO37/38, the P4's own console,
shared with the USB-serial chip.  Don't use IO34 or IO36 either: they are
strapping pins.

The hub needs no change: it speaks the same UART protocol as to the S3 display.

## Build and flash

```bash
firmware/esp32_display_p4/build_p4.sh flash /dev/cu.usbmodemXXXX
```

On Windows: `firmware\esp32_display_p4\build_p4.bat flash COM5`.

- Flash and watch the log through the **"UART" Type-C** (CH343P), at 115200 baud.
- The settings are pinned in the scripts: ESP32P4 Dev Module, PSRAM on, 32 MB
  flash, the 32 MB partition table, and **USB mode "Hardware CDC and JTAG"**.
  TinyUSB would take the USB-A port the controller needs.
- **Chip revision.** Waveshare ships pre-v3 chips (360 MHz), and that is the
  default build.  If the boot log reports v3.00 or newer, build with `CHIP=v3`
  (`set CHIP=v3` on Windows).

### Options

| Option              | Default | What it does                                                         |
|---------------------|---------|----------------------------------------------------------------------|
| `DISPLAY_FLIP=1`    | 0       | The picture upside down, for a display mounted that way. The panel does it itself; touch follows. |
| `TOUCH_MIRROR_XY=0` | 1       | Use this if taps land in the diagonally opposite corner (see the first power-up). |
| `PAD_USB=0`         | 1       | Builds without the USB controller.                                   |

On the Mac these are environment variables for `build_p4.sh`.  On Windows,
change the defaults in `board_p4.h` (or the `.ino` for `PAD_USB`).

## First power-up: what the log should say

```
[BOARD] panel up: 1024 x 600
[BOARD] touch: GT911 at 0x5D (product id "911")
[DISP] draw bands: 2 x 80 KB in internal RAM
[PAD] USB host running: plug a controller into the USB-A port
```

- **Touch orientation.** Tap near the **top-left** corner.  The log prints
  `[TOUCH] press at (x, y)` for the first five presses.
  - Small numbers mean it is right.
  - About (1000, 590) means it is mirrored: build with `TOUCH_MIRROR_XY=0`.
  - The default follows Waveshare's own support package, which mirrors this
    panel's touch on both axes.  It is the one setting I could not confirm
    without the board.
- **Controller.** Plug one in.  The log should show
  `[PAD] plugged in: 054c:0ce6 ...` and then `[PAD] reading DualSense ...`.
  Then tap **PAD** in the top bar: the bars move with the sticks.

## The layout

These are the S3's screens, drawn at the same physical size on the same 7"
glass, at 1.28 × the pixels.  Text, buttons and touch targets are the size they
were; everything is sharper.

The layout is not redrawn by hand.  `hub_display.cpp` is the S3's file with a
**layout scale** block at the top.  It routes this file's LVGL position, size
and padding calls through ×1.28 (x) and ×1.25 (y), and maps each font one size
up.  Every coordinate in the file is still in the S3's 800 × 480 units.

That keeps the two files in step.  Diff them, and the only differences are the
scale block, the hardware layer and the controller, each marked **(P4)**.  To
port a change to the S3 UI, copy the changed function across.

## The controller

Plug a controller into the USB-A port.  It drives a camera the way the PC app's
joystick does, with the same deadzone, smoothing and 20 Hz jogs:

| Control          | Does                                                    |
|------------------|---------------------------------------------------------|
| Right stick      | pan / tilt (stick up is tilt up)                        |
| Left stick       | slider (left/right), zoom (up/down; stick up is zoom in) |
| L1 / R1          | previous / next connected camera                        |
| D-pad up/down    | pan/tilt speed dial                                     |
| D-pad left/right | slider/zoom speed dial                                  |

**Which camera it drives:**
- On a camera's detail screen, that camera.  L1/R1 switch the screen, as the
  CAM tabs do.
- Anywhere else, the camera it last drove.  It is shown three ways:
  - **PAD n** in the top bar;
  - a USB mark on that camera's tile;
  - the colour of a DualSense or DualShock 4's light bar.

**While it is driving:**
- A finger on the touch joystick or a slider has the camera; the controller
  waits until the finger lifts.
- Unplugging it mid-move sends that camera its zero jog.
- The **PAD** panel shows the controller and the jog it is sending, and has an
  **On** switch to take it out of play.

**Supported controllers:**
- DualSense, DualShock 4, Xbox 360-type, Xbox One / Series, Switch Pro;
- any USB controller that describes itself as a HID game pad.

One controller at a time, wired.  No Bluetooth: the board's radio chip is
left off.

## Tests on the Mac

```bash
firmware/esp32_display_p4/host/render.sh
```

Builds the real `hub_display.cpp` against the repo's LVGL and writes every screen
at 1024 × 600 to `.build/p4-host/shots/`.  It reaches each screen the way a tap
does, through the UI's own task loop.  Use it to check a layout change without
the board.

```bash
python3 firmware/esp32_display_p4/host/test_pad.py
```

Tests the controller code:
- every family's report, byte by byte;
- the HID descriptor reader and its traps;
- the jog law against the PC app's own `circ_to_sq()`, deadzone and smoothing,
  matching to the count.

## Not yet confirmed on the board

- The touch orientation default (above).
- The light bar and player lights: DualSense, DualShock 4, Xbox 360-type,
  Switch Pro.  The packets are laid out as Linux's drivers send them.  If they
  are wrong, the light stays as it was; nothing else is affected.
- The Switch Pro's "full reports" request.  Without it, its sticks read nothing.
- Generic HID pads: their button numbering varies by maker.  L1/R1 are assumed
  to be buttons 5 and 6.
- Whether the USB-A port's 5 V holds up under a controller that is charging.

## Files

| File | What it is |
|------|------------|
| `esp32_display_p4.ino` | Setup, the hub UART, the message dispatcher (the S3's, on P4 pins) |
| `hub_display.cpp/.h` | The UI: the S3's, scaled, with the controller added |
| `board_p4.cpp/.h` | Panel (MIPI-DSI, EK79007), backlight, GT911 touch |
| `usb_pad.cpp/.h` | USB host: finds the controller and reads it, on core 0 |
| `pad_reports.h` | Report layouts, per controller family (pure C++) |
| `pad_map.h` | Sticks to jog, the PC app's law (pure C++) |
| `esp_lcd_ek79007.c/.h`, `esp_lcd_panel_interface.h` | Espressif's panel driver and its interface header (Apache-2.0), vendored |
| `build_p4.sh`, `build_p4.bat` | Build and flash |
| `host/` | The Mac render harness and the controller tests |
