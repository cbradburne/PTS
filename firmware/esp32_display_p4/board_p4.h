#pragma once
/*
 * board_p4.h — the hardware of the Waveshare ESP32-P4-WIFI6-Touch-LCD-7B.
 *
 *   panel     1024 × 600, EK79007, MIPI-DSI 2 lanes at 1 Gbps, 52 MHz pixel clock
 *   backlight GPIO32, PWM (10-bit, 5 kHz)       panel reset  GPIO33
 *   DSI PHY   powered from the P4's LDO channel 3 at 2.5 V
 *   touch     GT911 on I2C SDA 7 / SCL 8; its reset and interrupt are not on GPIOs
 *
 * All of it as Waveshare's own board support package (BSP 3.0.1) sets it up.
 * The LVGL side lives in hub_display.cpp; nothing here knows about LVGL.
 */
#include <stdint.h>
#include <stdbool.h>

#define BOARD_LCD_W  1024
#define BOARD_LCD_H  600

// Turn the picture upside down, for a display mounted the other way up.  Done
// by the panel itself (its scan direction), so it costs nothing.  The touch
// coordinates follow.
#ifndef DISPLAY_FLIP
#define DISPLAY_FLIP 0
#endif

// The GT911 on this board reports from the opposite corner to the panel's
// scan: Waveshare's BSP mirrors both of its axes.  If taps land in the
// diagonally opposite corner of where they should, build with 0.
#ifndef TOUCH_MIRROR_XY
#define TOUCH_MIRROR_XY 1
#endif

// Called when the panel has taken the pixels handed to board_display_draw()
// and the buffer may be drawn into again.  From an interrupt.
typedef void (*board_draw_done_fn)(void *ctx);

bool board_display_init(board_draw_done_fn done, void *ctx);

// Copies a rectangle (x2/y2 inclusive) of RGB565 pixels to the panel's frame
// buffer, by DMA.  Returns at once; `done` fires when the copy has finished.
void board_display_draw(int x1, int y1, int x2, int y2, const void *pixels);

void board_backlight(uint8_t percent);

bool board_touch_init();

// Up to `max` touch points, in screen coordinates.  Returns how many.  Each
// call is one I2C read; between the GT911's own updates it repeats the last.
int board_touch_read(int16_t *xs, int16_t *ys, int max);
