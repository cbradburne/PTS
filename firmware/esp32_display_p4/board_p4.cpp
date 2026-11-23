/*
 * board_p4.cpp — panel, backlight and touch of the ESP32-P4-WIFI6-Touch-LCD-7B.
 *
 * The panel bring-up is Waveshare's own (BSP 3.0.1, bsp_display_new_with_handles):
 *   1. the DSI PHY's supply: LDO channel 3 at 2500 mV
 *   2. the DSI bus: 2 data lanes at 1000 Mbps
 *   3. a DBI channel over it, for the panel's commands
 *   4. the DPI (video) side: 1024 × 600 at 52 MHz, RGB565, one frame buffer in
 *      PSRAM, DMA2D to copy into it
 *   5. the EK79007 driver: reset on GPIO33, then its init commands
 */
#include <Arduino.h>
#include <Wire.h>
#include "esp_ldo_regulator.h"
#include "esp_lcd_mipi_dsi.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_ek79007.h"
#include "board_p4.h"

#define PIN_LCD_BACKLIGHT  32
#define PIN_LCD_RESET      33
#define PIN_TOUCH_SDA       7
#define PIN_TOUCH_SCL       8

#define DSI_PHY_LDO_CHAN    3
#define DSI_PHY_LDO_MV      2500
#define DSI_LANES           2
#define DSI_LANE_MBPS       1000

static esp_lcd_panel_handle_t _panel = nullptr;
static board_draw_done_fn     _done = nullptr;
static void                  *_done_ctx = nullptr;

static bool IRAM_ATTR on_color_trans_done(esp_lcd_panel_handle_t, esp_lcd_dpi_panel_event_data_t *, void *) {
    if (_done) _done(_done_ctx);
    return false;
}

#define CHECK(call, what) do { esp_err_t _e = (call); if (_e != ESP_OK) { \
    Serial.printf("[BOARD] %s failed: %s\n", what, esp_err_to_name(_e)); return false; } } while (0)

bool board_display_init(board_draw_done_fn done, void *ctx) {
    _done = done;
    _done_ctx = ctx;

    // Backlight off until there is a picture.  Off is the pin HIGH: see
    // board_backlight().
    ledcAttach(PIN_LCD_BACKLIGHT, 5000, 10);
    ledcWrite(PIN_LCD_BACKLIGHT, 1023);

    static esp_ldo_channel_handle_t phy_ldo = nullptr;
    esp_ldo_channel_config_t ldo_cfg = {};
    ldo_cfg.chan_id = DSI_PHY_LDO_CHAN;
    ldo_cfg.voltage_mv = DSI_PHY_LDO_MV;
    CHECK(esp_ldo_acquire_channel(&ldo_cfg, &phy_ldo), "DSI PHY supply (LDO 3)");

    esp_lcd_dsi_bus_handle_t bus = nullptr;
    esp_lcd_dsi_bus_config_t bus_cfg = {};
    bus_cfg.bus_id = 0;
    bus_cfg.num_data_lanes = DSI_LANES;
    // The DSI PHY's PLL reference, which is not the same on the two chip
    // revisions: v3 silicon takes XTAL (or APLL/CPLL/SPLL/MPLL), earlier chips
    // PLL_F20M (or RC_FAST/PLL_F25M), and each driver aborts on the other's.
    // The core's own MIPI_DSI_PHY_CLK_SRC_DEFAULT is the EARLIER chips' default
    // in both builds — which is what the first v3.2 board died on, every boot,
    // inside esp_lcd_new_dsi_bus() (2026-11-23).
#if CONFIG_ESP32P4_SELECTS_REV_LESS_V3
    bus_cfg.phy_clk_src = MIPI_DSI_PHY_PLLREF_CLK_SRC_PLL_F20M;
#else
    bus_cfg.phy_clk_src = MIPI_DSI_PHY_PLLREF_CLK_SRC_DEFAULT;      // XTAL
#endif
    bus_cfg.lane_bit_rate_mbps = DSI_LANE_MBPS;
    CHECK(esp_lcd_new_dsi_bus(&bus_cfg, &bus), "DSI bus");

    esp_lcd_panel_io_handle_t io = nullptr;
    esp_lcd_dbi_io_config_t dbi_cfg = {};
    dbi_cfg.virtual_channel = 0;
    dbi_cfg.lcd_cmd_bits = 8;
    dbi_cfg.lcd_param_bits = 8;
    CHECK(esp_lcd_new_panel_io_dbi(bus, &dbi_cfg, &io), "DSI command channel");

    esp_lcd_dpi_panel_config_t dpi_cfg = EK79007_1024_600_PANEL_60HZ_CONFIG(LCD_COLOR_PIXEL_FORMAT_RGB565);
    dpi_cfg.num_fbs = 1;

    ek79007_vendor_config_t vendor = {};
    vendor.mipi_config.dsi_bus = bus;
    vendor.mipi_config.dpi_config = &dpi_cfg;
    vendor.mipi_config.lane_num = DSI_LANES;

    esp_lcd_panel_dev_config_t dev_cfg = {};
    dev_cfg.reset_gpio_num = PIN_LCD_RESET;
    dev_cfg.rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB;
    dev_cfg.bits_per_pixel = 16;
    dev_cfg.vendor_config = &vendor;
    CHECK(esp_lcd_new_panel_ek79007(io, &dev_cfg, &_panel), "EK79007 panel");
    CHECK(esp_lcd_panel_reset(_panel), "panel reset");
    CHECK(esp_lcd_panel_init(_panel), "panel init");

    esp_lcd_dpi_panel_event_callbacks_t cbs = {};
    cbs.on_color_trans_done = on_color_trans_done;
    CHECK(esp_lcd_dpi_panel_register_event_callbacks(_panel, &cbs, nullptr), "panel callbacks");
    Serial.printf("[BOARD] panel up: %d x %d%s\n", BOARD_LCD_W, BOARD_LCD_H, DISPLAY_FLIP ? ", flipped" : "");
    return true;
}

// DISPLAY_FLIP turns the picture 180° here, in software.  The panel cannot: the
// EK79007's own mirror (MADCTL, which esp_lcd_panel_mirror() sends) is ignored
// in DSI video mode — tried on the first board, 2026-11-23: the touch turned
// and the picture did not.  For 180° it is cheap: a band reversed end to end
// IS the band turned 180°, so its pixels are swapped in place and it goes to
// the mirrored rectangle.  About a millisecond for a whole screen at 400 MHz.
// LVGL is done with the band once it has handed it over, so it is ours to turn.
void board_display_draw(int x1, int y1, int x2, int y2, void *pixels) {
#if DISPLAY_FLIP
    uint16_t *p = (uint16_t *)pixels;
    size_t n = (size_t)(x2 - x1 + 1) * (size_t)(y2 - y1 + 1);
    for (size_t i = 0, j = n - 1; i < j; i++, j--) {
        uint16_t t = p[i]; p[i] = p[j]; p[j] = t;
    }
    int fx1 = BOARD_LCD_W - 1 - x2, fx2 = BOARD_LCD_W - 1 - x1;
    int fy1 = BOARD_LCD_H - 1 - y2, fy2 = BOARD_LCD_H - 1 - y1;
    x1 = fx1; x2 = fx2; y1 = fy1; y2 = fy2;
#endif
    esp_lcd_panel_draw_bitmap(_panel, x1, y1, x2 + 1, y2 + 1, pixels);
}

// The backlight is lit by the pin going LOW: Waveshare's own driver sets
// output_invert on this channel.  So full brightness is duty 0 and off is
// 1023.  Driven the obvious way, "100%" held the pin high and the first board
// (2026-11-23) booted with a perfectly good picture and no light behind it.
void board_backlight(uint8_t percent) {
    if (percent > 100) percent = 100;
    ledcWrite(PIN_LCD_BACKLIGHT, 1023u - (1023u * percent) / 100u);
}

// ---- touch: GT911 -------------------------------------------------------------
// Not TAMC_GT911: that library drives the chip's reset and interrupt lines,
// and here neither is on a GPIO.  What it does over I2C is all this needs:
// status at 0x814E (bit 7 = new data, low bits = points), points from 0x814F,
// 8 bytes each, then a zero written back to the status.

static uint8_t _gt_addr = 0;
static int     _gt_n = 0;
static int16_t _gt_x[5], _gt_y[5];

static bool gt_read(uint16_t reg, uint8_t *buf, int n) {
    Wire.beginTransmission(_gt_addr);
    Wire.write((uint8_t)(reg >> 8));
    Wire.write((uint8_t)reg);
    if (Wire.endTransmission(false) != 0) return false;
    if (Wire.requestFrom((int)_gt_addr, n) != n) return false;
    for (int i = 0; i < n; i++) buf[i] = Wire.read();
    return true;
}

static void gt_write8(uint16_t reg, uint8_t v) {
    Wire.beginTransmission(_gt_addr);
    Wire.write((uint8_t)(reg >> 8));
    Wire.write((uint8_t)reg);
    Wire.write(v);
    Wire.endTransmission();
}

bool board_touch_init() {
    Wire.begin(PIN_TOUCH_SDA, PIN_TOUCH_SCL, 400000);
    // With its address line not driven at power-up the GT911 picks one of two.
    static const uint8_t ADDRS[2] = { 0x5D, 0x14 };
    for (uint8_t a : ADDRS) {
        Wire.beginTransmission(a);
        if (Wire.endTransmission() == 0) { _gt_addr = a; break; }
    }
    if (!_gt_addr) {
        Serial.println("[BOARD] touch: no GT911 at 0x5D or 0x14");
        return false;
    }
    uint8_t id[4] = {};
    gt_read(0x8140, id, 4);
    Serial.printf("[BOARD] touch: GT911 at 0x%02X (product id \"%.4s\")\n", _gt_addr, (const char *)id);
    return true;
}

int board_touch_read(int16_t *xs, int16_t *ys, int max) {
    if (_gt_addr) {
        uint8_t st = 0;
        if (gt_read(0x814E, &st, 1) && (st & 0x80)) {
            int n = st & 0x0F;
            if (n > 5) n = 5;
            uint8_t pts[40];
            if (n == 0 || gt_read(0x814F, pts, n * 8)) {
                _gt_n = n;
                for (int i = 0; i < n; i++) {
                    int x = pts[i * 8 + 1] | (pts[i * 8 + 2] << 8);
                    int y = pts[i * 8 + 3] | (pts[i * 8 + 4] << 8);
                    // The panel flip and the touch's own mirroring cancel out.
                    if (TOUCH_MIRROR_XY ^ DISPLAY_FLIP) { x = BOARD_LCD_W - 1 - x; y = BOARD_LCD_H - 1 - y; }
                    _gt_x[i] = (int16_t)constrain(x, 0, BOARD_LCD_W - 1);
                    _gt_y[i] = (int16_t)constrain(y, 0, BOARD_LCD_H - 1);
                }
            }
            gt_write8(0x814E, 0);
        }
    }
    int n = _gt_n < max ? _gt_n : max;
    for (int i = 0; i < n; i++) { xs[i] = _gt_x[i]; ys[i] = _gt_y[i]; }
    return n;
}
