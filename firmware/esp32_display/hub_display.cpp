/*
 * hub_display.cpp — LVGL v9 UI for Waveshare ESP32-S3-Touch-LCD-7  (800 × 480)
 *
 * Hardware layer uses ESP-IDF v5 esp_lcd_panel_rgb API directly (compatible
 * with Arduino ESP32 core v3.x / IDF v5).
 */

#include "hub_display.h"
#include "../shared/disp_uart.h"
#include <lvgl.h>
#include "esp_lcd_panel_rgb.h"
#include "esp_lcd_panel_ops.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>
#include <TAMC_GT911.h>
#include <Wire.h>

// ============================================================
//  Hardware pin definitions
// ============================================================

#define LCD_WIDTH   800
#define LCD_HEIGHT  480

#define PIN_DE      5
#define PIN_VSYNC   3
#define PIN_HSYNC  46
#define PIN_PCLK    7       // 7

#define PIN_R0  1
#define PIN_R1  2
#define PIN_R2  42
#define PIN_R3  41
#define PIN_R4  40

#define PIN_G0  39
#define PIN_G1  0
#define PIN_G2  45
#define PIN_G3  48
#define PIN_G4  47
#define PIN_G5  21

#define PIN_B0  14
#define PIN_B1  38
#define PIN_B2  18
#define PIN_B3  17
#define PIN_B4  10

#define PIN_SDA  8
#define PIN_SCL  9

#define PIN_TOUCH_INT  4
#define PIN_TOUCH_RST  -1   // RST not wired to a free GPIO on this board

// Display timing for Waveshare ESP32-S3-Touch-LCD-7 (ST7262, 800×480)
// 16 MHz is this panel's floor — tested at 12 MHz it doesn't just slow down,
// it shows solid primary-colour blocks from boot (out of the ST7262's range).
#define LCD_PCLK_HZ      (16 * 1000 * 1000)
#define LCD_HBPORCH  40
#define LCD_HFPORCH  40
#define LCD_HPULSE   48
// 30, not 23: at 23 the vertical sync margin is borderline for this panel —
// during redraw bursts the image slips lines, so icons visibly "jump" and the
// whole picture can settle vertically displaced.  30 blank lines is stable.
#define LCD_VBPORCH  30
#define LCD_VFPORCH   7
#define LCD_VPULSE    1

// ============================================================
//  Colour palette
// ============================================================
#define C_BG        0x121212
#define C_SURF      0x1E1E1E
#define C_SURF2     0x2A2A2A
#define C_BORDER    0x333333
#define C_TEXT      0xE0E0E0
#define C_DIM       0x777777
#define C_ACCENT    0x1565C0
#define C_ACCENT2   0x2196F3
#define C_GREEN     0x388E3C
#define C_GREEN_LIT 0x4CAF50
#define C_ORANGE    0xE65100
#define C_RED       0xB71C1C
#define C_RED_LIT   0xEF5350

// Per-camera muted background colours — match PC app CAM_COLORS btn_bg hues
static const uint32_t C_CAM_BG[5] = {
    0x1B3A1D,   // cam 1 — dark green
    0x1A2E45,   // cam 2 — dark blue
    0x332B00,   // cam 3 — dark gold
    0x002E2A,   // cam 4 — dark teal
    0x2E1040,   // cam 5 — dark purple
};

// Arc dial colours (match esp_mount_s3 style)
#define C_ARC_ACCENT  0x00AEEF
#define C_ARC_TRACK   0x222222

// Slot circle colours (used for overview dots etc.)
#define C_SLOT_EMPTY  0x252525
#define C_SLOT_OCC    0x761100   // saved position exists
#define C_SLOT_AT     0x2E7D32   // mount is here now
#define C_SLOT_TGT    0xCCCF19   // moving to this slot

// Detail-screen position button border colours
// Background stays dark grey; the border carries the status colour.
#define C_BTN_BORDER_EMPTY  0x555555   // grey  — no position stored
#define C_BTN_BORDER_OCC    0xC62828   // red   — position stored
#define C_BTN_BORDER_TGT    0xCDD619   // yellow — moving to this slot
#define C_BTN_BORDER_AT     0x4CAF50   // green  — mount is at this slot
#define C_BTN_BORDER_W      3          // border width (px)

// ============================================================
//  Hardware objects
// ============================================================

static esp_lcd_panel_handle_t _panel_handle = nullptr;
static TAMC_GT911             _touch(PIN_SDA, PIN_SCL, PIN_TOUCH_INT, PIN_TOUCH_RST,
                                     LCD_WIDTH, LCD_HEIGHT);

static SemaphoreHandle_t  _lvgl_mux         = nullptr;
static SemaphoreHandle_t  _vsync_sem        = nullptr;
static TaskHandle_t       _lvgl_task_handle = nullptr;
static void              *_fb0              = nullptr;
static void              *_fb1              = nullptr;

// Pending navigation action — set by event callbacks, executed by the task loop
// BEFORE the next lv_timer_handler call so all work happens at flat stack depth.
// Creating 60+ objects inside lv_timer_handler (nested in touch-event dispatch)
// causes LVGL to run a post-event layout pass that overflows the 32 KB stack.
enum class NavAction : uint8_t {
    NONE = 0,
    GOTO_POSITIONS,
    GOTO_CONFIG,
    GOTO_STATUS_FROM_POSITIONS, // positions back button
    GOTO_STATUS_FROM_CONFIG,    // config back button / ev_goto_status
    OPEN_DETAIL,                // tap a camera tile on the status screen
    DETAIL_BACK,                // back button on detail screen
    DISCONNECT_RETURN,          // camera disconnected while on detail screen
};
static NavAction              _pending_action = NavAction::NONE;
static uint8_t                _pending_cam    = 0;   // for OPEN_DETAIL

// Set by hub_ui_notify_look_at_status() from loop() (core 0); cleared by the
// LVGL task (core 1) at the top of each tick.  A volatile bool write/read is
// atomic on Xtensa, and xSemaphoreTake inside the LVGL task provides the
// memory barrier needed to see the latest value from core 0.
static volatile bool          _la_refresh_pending = false;

// ---- Pairing (stage 3) state ------------------------------------------------
// Data written from loop() (core 0) via the hub_ui_* API, consumed by the LVGL
// task (core 1) — same volatile-flag pattern as _la_refresh_pending.  The
// _mp_act_* flags flow the other way: set in LVGL event callbacks, processed
// in the task tick (keeps event-context work minimal, like NavAction).
static uint8_t                _mount_table[5][6]     = {};   // zero = unbound
static volatile bool          _mounts_table_pending  = false;
static uint8_t                _pc_cam                = 0;    // conflict prompt cam 1-5
static uint8_t                _pc_new[6], _pc_old[6];        // claimant / current owner
static volatile bool          _pc_show_pending       = false;
static volatile bool          _pc_clear_pending      = false;
static volatile bool          _mp_act_open           = false;
static volatile bool          _mp_act_close          = false;
static volatile uint8_t       _mp_act_forget         = 0;    // cam 1-5
static volatile int8_t        _mp_act_decide         = -1;   // 0=ignore 1=replace

// _pending_screen is only used by direct lv_scr_load deferrals (no longer
// needed with NavAction but kept to avoid breaking the public API path at line 2727).
static lv_obj_t              *_pending_screen = nullptr;

// LVGL task stack allocated statically in BSS (link-time) so heap fragmentation
// from building 200+ LVGL objects cannot prevent the allocation.
// 8192 words × 4 bytes = 32 KB.
#define LVGL_TASK_STACK_WORDS  8192
static StackType_t  _lvgl_task_stack[LVGL_TASK_STACK_WORDS];
static StaticTask_t _lvgl_task_static_buf;

// ============================================================
//  Shared state
// ============================================================

static hub_send_fn_t    _send_cb   = nullptr;
static CamStatus      _cam[5];
static TileSlots      _slots[5];
static uint8_t        _tcp_clients = 0;
static uint8_t        _ws_clients  = 0;
static uint8_t        _cfg_cam     = 0;

// Speed presets in physical units — group 0: deg/s (Pan/Tilt), group 1: mm/s (Slider)
// Both arrays are populated from CONFIG_REPORT so they always match what is in EEPROM.
static int32_t _cfg_speeds[2][4] = {
    {  1,  3,  7,  15 },   // Pan/Tilt: deg/s  (firmware defaults)
    {  5, 15, 40, 100 },   // Slider:   mm/s   (firmware defaults)
};
static int32_t _cfg_accels[2][4] = {
    {  10,  30,  40,  60 },   // Pan/Tilt: deg/s² (firmware defaults)
    {  50, 150, 160, 200 },   // Slider:   mm/s²  (firmware defaults)
};
static bool _cfg_pan_inv    = false;
static bool _cfg_slider_inv = false;
static bool _cfg_zoom_inv   = false;
static bool _cfg_lanc_zoom  = false;
static bool _cfg_has_slider = true;

// Per-camera background colours matching the PC app CAM_COLORS palette.
static const uint32_t CAM_ROW_BG[5]   = { 0x0D1F0F, 0x0D1A2B, 0x1E1800, 0x001A17, 0x1A0B27 };
static const uint32_t CAM_BTN_BG[5]   = { 0x1B3A1D, 0x1A2E45, 0x332B00, 0x002E2A, 0x2E1040 };
static const uint32_t CAM_BTN_TEXT[5] = { 0xA5D6A7, 0x90CAF9, 0xD4B800, 0x80CBC4, 0xCE93D8 };

// ============================================================
//  Positions screen state
// ============================================================

static lv_obj_t *_scr_positions       = nullptr;
static lv_obj_t *_pos_slot_btn[5][10] = {};

// ============================================================
//  Detail screen state
// ============================================================

#define JOY_SIZE        180
#define JOY_THUMB_SIZE   50
#define JOY_DEAD_ZONE    12   // px from centre below which zero is sent

#define HSL_W           230   // horizontal slider: track width
#define HSL_H            64   // horizontal slider: track height
#define HSL_THUMB_W      54   // thumb width
#define HSL_THUMB_H      54   // thumb height

struct Joystick { lv_obj_t *bg; lv_obj_t *thumb; };
struct HSlider  { lv_obj_t *bg; lv_obj_t *thumb; };

static lv_obj_t   *_scr_detail        = nullptr;
static lv_obj_t   *_det_hdr           = nullptr;
static lv_obj_t   *_det_cam_btns[5]   = {};
static lv_obj_t   *_det_pos_btn[10]   = {};
static lv_obj_t   *_det_set_btn       = nullptr;
static lv_obj_t   *_det_set_lbl       = nullptr;
static lv_obj_t   *_det_clear_btn     = nullptr;
static lv_obj_t   *_det_clear_lbl     = nullptr;
static uint8_t     _detail_cam        = 0;     // 0-4
static uint8_t     _det_sel_slot      = 0xFF;  // 0xFF = none selected
static bool        _det_set_pending   = false;   // waiting for a slot tap after SET
static bool        _det_clear_confirm = false;   // clear mode active — awaiting CONTINUE?
static uint16_t    _det_clear_mask    = 0;       // bits 0-9: slots selected for clearing
static lv_timer_t *_clear_timer       = nullptr;

// ── Subject calibration state (has-slider mode) ──────────────────────────────
// Tracks where we are in the 2-point subject calibration sequence.
// 0=idle  1=moving→A  2=wait_set_A  3=moving→B  4=wait_set_B
static uint8_t _calib_state = 0;
static uint8_t _calib_slot  = 0xFF;   // subject slot (0-7) being calibrated
static uint8_t _subject_mask[5] = {};  // bit i = subject slot i stored, per mount (0-indexed)
static int8_t  _active_la_subject[5] = { -1, -1, -1, -1, -1 }; // selected look-at subject per mount (-1=none)

// Arrow button visual state per look-at mount.
// -1=grey(idle), 0=◀ moving(yellow), 1=▶ moving(yellow), 2=◀ done(green), 3=▶ done(green)
static int8_t  _la_arrow_state[5]   = { -1, -1, -1, -1, -1 };
// Flash state for yellow "moving" arrow — toggled by la_flash_timer_cb every 500 ms.
static bool    _la_flash_on         = false;
static HSlider     _hsl_sl;   // left: Slider axis (horizontal drag)
static HSlider     _hsl_zoom; // left: Zoom axis   (horizontal drag)
static Joystick    _joy_pt;   // right hand: Pan   / Tilt
static int16_t     _jog_pan   = 0;
static int16_t     _jog_tilt  = 0;
static int16_t     _jog_sl    = 0;
static int16_t     _jog_zoom  = 0;

// ============================================================
//  Arc dial widget group
// ============================================================

struct TileDial {
    lv_obj_t *arc;
    lv_obj_t *val_lbl;
    lv_obj_t *dots[5];
};

// Dot offsets for build_tile_dial — relative to the arc's (x,y) position within its parent.
// Dots are created as children of the PARENT (not the arc) so they are never clipped
// by the arc widget's bounds.  Radius 29 places them 1 px outside the arc outer edge (28).
// Arc centre in parent content = (x+30, y+30)  [60px arc, pad_all=2].
static const lv_coord_t TILE_DOT_DX[5] = { 16,  2, 30, 58, 45 };
static const lv_coord_t TILE_DOT_DY[5] = { 55, 23,  1, 23, 55 };

// Detail screen dials — declared here so TileDial is already defined
static TileDial _det_pt_dial;
static TileDial _det_sl_dial;

// Positions screen dials (one PT + one SL per camera row)
static TileDial _pos_pt_dial[5];
static TileDial _pos_sl_dial[5];

// ============================================================
//  LVGL widget handles
// ============================================================

static lv_obj_t *_scr_status     = nullptr;
static lv_obj_t *_lbl_clients[3] = {};   // [0]=Home, [1]=Positions, [2]=Config

static lv_obj_t *_tile_obj  [5];
static lv_obj_t *_tile_dot  [5];
static lv_obj_t *_tile_cstat[5];
static lv_obj_t *_tile_state[5];
static lv_obj_t *_tile_rssi [5];
static TileDial  _tile_pt_dial[5];
static TileDial  _tile_sl_dial[5];
static lv_obj_t *_tile_slot [5][10];

static lv_obj_t *_scr_config      = nullptr;
static lv_obj_t *_cfg_cam_btns[5];
static lv_obj_t *_cfg_sliders [2][4];
static lv_obj_t *_cfg_sw_paninv   = nullptr;
static lv_obj_t *_cfg_sw_slinv    = nullptr;
static lv_obj_t *_cfg_sw_zminv    = nullptr;
static lv_obj_t *_cfg_sw_lanczm   = nullptr;
static lv_obj_t *_cfg_sw_hassl    = nullptr;
static lv_obj_t *_cfg_sw_lookAt   = nullptr;   // "Look-at Mode" toggle
static lv_obj_t *_cfg_lbl_tilt    = nullptr;   // rail inclination, read-only
static lv_obj_t *_cfg_btn_findsl  = nullptr;  // "Find: Slider" button
static lv_obj_t *_cfg_btn_findzm  = nullptr;  // "Find: Zoom" button
static lv_obj_t *_cfg_find_lbl    = nullptr;  // status label (e.g. "Slider: 4800 steps")
static lv_obj_t *_cfg_btn_manualref= nullptr;
static lv_obj_t *_cfg_ref_lbl      = nullptr;  // confirmation label

// ============================================================
//  Helpers
// ============================================================

// Returns true when this camera has slider hardware AND look-at mode is enabled.
static inline bool cam_is_look_at(int i) {
    return (_cam[i].flags & FLAG_HAS_SLIDER) && (_cam[i].flags & FLAG_LOOK_AT_MODE);
}

// Slider-less mounts render their slider dial dormant (preset 0: "-", dark dots
// — the same look as a disconnected axis) and ignore taps on it, matching the
// PC app.  Showing a live, tappable slider speed for an axis that isn't fitted
// is misleading.
static inline bool cam_has_slider(int i) {
    return (_cam[i].flags & FLAG_HAS_SLIDER) != 0;
}

static const char *state_name(uint8_t s) {
    switch (s) {
        case 0: return "IDLE";
        case 1: return "JOGGING";
        case 2: return "MOVING";
        case 3: return "FIND LIMITS";
        case 4: return "ERROR";
        case 5: return "TRACKING";
        case 6: return "CALIBRATING";
        case 7: return "PRE-AIM";
        default: return "?";
    }
}

static void card_style(lv_obj_t *obj, uint32_t bg_hex, uint32_t border_hex = C_BORDER) {
    lv_obj_set_style_bg_color(obj,     lv_color_hex(bg_hex),     0);
    lv_obj_set_style_bg_opa(obj,       LV_OPA_COVER,             0);
    lv_obj_set_style_border_color(obj, lv_color_hex(border_hex), 0);
    lv_obj_set_style_border_width(obj, 1,                         0);
    lv_obj_set_style_radius(obj,       6,                         0);
    lv_obj_set_style_pad_all(obj,      8,                         0);
}

static lv_obj_t *make_label(lv_obj_t *parent, const char *txt,
                              const lv_font_t *font, uint32_t col = C_TEXT) {
    lv_obj_t *lbl = lv_label_create(parent);
    lv_label_set_text(lbl, txt);
    lv_obj_set_style_text_font(lbl,  font,              0);
    lv_obj_set_style_text_color(lbl, lv_color_hex(col), 0);
    return lbl;
}

static lv_obj_t *make_button(lv_obj_t *parent, const char *txt,
                               uint32_t bg, lv_event_cb_t cb, void *udata = nullptr) {
    lv_obj_t *btn = lv_button_create(parent);    // v9: lv_button_create
    lv_obj_set_style_bg_color(btn, lv_color_hex(bg), 0);
    lv_obj_set_style_bg_color(btn, lv_color_hex(bg), LV_STATE_PRESSED);
    lv_obj_set_style_radius(btn, 6, 0);
    if (cb) lv_obj_add_event_cb(btn, cb, LV_EVENT_CLICKED, udata);
    lv_obj_t *lbl = lv_label_create(btn);
    lv_label_set_text(lbl, txt);
    lv_obj_set_style_text_color(lbl, lv_color_hex(C_TEXT), 0);
    lv_obj_center(lbl);
    return btn;
}

static void make_hdivider(lv_obj_t *parent, int y) {
    lv_obj_t *d = lv_obj_create(parent);
    lv_obj_set_size(d, LV_PCT(100), 1);
    lv_obj_set_pos(d, 0, y);
    lv_obj_set_style_bg_color(d, lv_color_hex(C_BORDER), 0);
    lv_obj_set_style_bg_opa(d, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(d, 0, 0);
    lv_obj_set_style_radius(d, 0, 0);
    lv_obj_set_style_pad_all(d, 0, 0);
    lv_obj_remove_flag(d, LV_OBJ_FLAG_SCROLLABLE);
}

// ============================================================
//  Arc dial helper
// ============================================================

static void build_tile_dial(TileDial *d, lv_obj_t *parent, int x, int y,
                            uint32_t bg_hex = C_BG, bool show_dots = true) {
    lv_obj_t *arc = lv_arc_create(parent);
    lv_obj_set_size(arc, 60, 60);
    lv_obj_set_pos(arc, x, y);
    lv_arc_set_rotation(arc, 120);
    lv_arc_set_bg_angles(arc, 0, 300);
    lv_arc_set_range(arc, 0, 4);   // 0 = disconnected, 1-4 = active presets
    lv_arc_set_value(arc, 0);
    lv_arc_set_mode(arc, LV_ARC_MODE_NORMAL);
    lv_obj_remove_style(arc, NULL, LV_PART_KNOB);          // v9: hide knob
    lv_obj_remove_flag(arc, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_flag(arc, LV_OBJ_FLAG_OVERFLOW_VISIBLE);    // allow dots to overflow

    lv_obj_set_style_arc_color(arc, lv_color_hex(C_ARC_TRACK),  LV_PART_MAIN);
    lv_obj_set_style_arc_width(arc, 6,                           LV_PART_MAIN);
    lv_obj_set_style_arc_rounded(arc, true,                      LV_PART_MAIN);
    lv_obj_set_style_arc_color(arc, lv_color_hex(C_ARC_ACCENT),  LV_PART_INDICATOR);
    lv_obj_set_style_arc_width(arc, 8,                            LV_PART_INDICATOR);
    lv_obj_set_style_arc_rounded(arc, true,                       LV_PART_INDICATOR);
    lv_obj_set_style_bg_color(arc, lv_color_hex(bg_hex), 0);
    lv_obj_set_style_bg_opa(arc,  LV_OPA_COVER,         0);
    lv_obj_set_style_border_width(arc, 0,                0);
    lv_obj_set_style_pad_all(arc, 2,                     0);

    lv_obj_t *lbl = lv_label_create(arc);
    lv_label_set_text(lbl, "-");   // "–" (en-dash) = disconnected
    lv_obj_set_style_text_font(lbl,  &lv_font_montserrat_16,     0);
    lv_obj_set_style_text_color(lbl, lv_color_hex(C_DIM),        0);
    lv_obj_center(lbl);
    d->arc     = arc;
    d->val_lbl = lbl;

    // Dots are children of parent (not arc) so no arc clipping applies.
    for (int j = 0; j < 5; j++) {
        if (show_dots) {
            lv_obj_t *dot = lv_obj_create(parent);
            lv_obj_set_size(dot, 4, 4);
            lv_obj_set_style_radius(dot, LV_RADIUS_CIRCLE,          0);
            lv_obj_set_style_bg_color(dot, lv_color_hex(C_ARC_TRACK), 0);
            lv_obj_set_style_bg_opa(dot, LV_OPA_COVER,              0);
            lv_obj_set_style_border_width(dot, 0,                   0);
            lv_obj_set_style_pad_all(dot, 0,                        0);
            lv_obj_remove_flag(dot, (lv_obj_flag_t)(LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE));
            lv_obj_set_pos(dot, x + TILE_DOT_DX[j] - 2, y + TILE_DOT_DY[j] - 2);
            d->dots[j] = dot;
        } else {
            d->dots[j] = nullptr;
        }
    }
}

// 120 px arc dial for the detail screen — 2× the status tile size.
// Dots are children of parent (not arc) at radius 64, just outside the arc outer edge (56).
static void build_detail_dial(TileDial *d, lv_obj_t *parent, int x, int y,
                              uint32_t bg_hex = C_BG) {
    lv_obj_t *arc = lv_arc_create(parent);
    lv_obj_set_size(arc, 120, 120);
    lv_obj_set_pos(arc, x, y);
    lv_arc_set_rotation(arc, 120);
    lv_arc_set_bg_angles(arc, 0, 300);
    lv_arc_set_range(arc, 0, 4);   // 0 = disconnected, 1-4 = active presets
    lv_arc_set_value(arc, 0);
    lv_arc_set_mode(arc, LV_ARC_MODE_NORMAL);
    lv_obj_remove_style(arc, NULL, LV_PART_KNOB);
    lv_obj_remove_flag(arc, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_flag(arc, LV_OBJ_FLAG_OVERFLOW_VISIBLE);

    lv_obj_set_style_arc_color(arc, lv_color_hex(C_ARC_TRACK),  LV_PART_MAIN);
    lv_obj_set_style_arc_width(arc, 12,                          LV_PART_MAIN);
    lv_obj_set_style_arc_rounded(arc, true,                      LV_PART_MAIN);
    lv_obj_set_style_arc_color(arc, lv_color_hex(C_ARC_ACCENT),  LV_PART_INDICATOR);
    lv_obj_set_style_arc_width(arc, 16,                           LV_PART_INDICATOR);
    lv_obj_set_style_arc_rounded(arc, true,                       LV_PART_INDICATOR);
    lv_obj_set_style_bg_color(arc, lv_color_hex(bg_hex), 0);
    lv_obj_set_style_bg_opa(arc,  LV_OPA_COVER,         0);
    lv_obj_set_style_border_width(arc, 0,                0);
    lv_obj_set_style_pad_all(arc, 4,                     0);

    lv_obj_t *lbl = lv_label_create(arc);
    lv_label_set_text(lbl, "-");   // "–" (en-dash) = disconnected
    lv_obj_set_style_text_font(lbl,  &lv_font_montserrat_22,     0);
    lv_obj_set_style_text_color(lbl, lv_color_hex(C_DIM),        0);
    lv_obj_center(lbl);
    d->arc     = arc;
    d->val_lbl = lbl;

    // 5 dots at 75° intervals, radius 58 from arc centre (x+60, y+60) in parent coords.
    // 2 px outside the arc outer edge (56); dots are children of parent — no arc clipping.
    static const lv_coord_t DX[5] = {  31,   4,  60, 116,  89 };
    static const lv_coord_t DY[5] = { 110,  45,   2,  45, 110 };
    for (int j = 0; j < 5; j++) {
        lv_obj_t *dot = lv_obj_create(parent);
        lv_obj_set_size(dot, 8, 8);
        lv_obj_set_style_radius(dot, LV_RADIUS_CIRCLE,            0);
        lv_obj_set_style_bg_color(dot, lv_color_hex(C_ARC_TRACK), 0);
        lv_obj_set_style_bg_opa(dot, LV_OPA_COVER,                0);
        lv_obj_set_style_border_width(dot, 0,                     0);
        lv_obj_set_style_pad_all(dot, 0,                          0);
        lv_obj_remove_flag(dot, (lv_obj_flag_t)(LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE));
        lv_obj_set_pos(dot, x + DX[j] - 4, y + DY[j] - 4);
        d->dots[j] = dot;
    }
}

static void update_tile_dial(TileDial *d, uint8_t preset) {
    if (!d->arc) return;
    if (preset > 4) preset = 4;
    lv_arc_set_value(d->arc, preset);
    if (preset == 0) {
        lv_label_set_text(d->val_lbl, "-");   // "–" = disconnected
        lv_obj_set_style_text_color(d->val_lbl, lv_color_hex(C_DIM), 0);
    } else {
        char buf[3];
        snprintf(buf, sizeof(buf), "%d", preset);
        lv_label_set_text(d->val_lbl, buf);
        lv_obj_set_style_text_color(d->val_lbl, lv_color_hex(C_ARC_ACCENT), 0);
    }
    // 5 dots: dots 0..preset lit when connected (preset>0), all dark when preset=0
    for (int j = 0; j < 5; j++) {
        if (!d->dots[j]) continue;
        lv_color_t col = (preset > 0 && j <= (int)preset)
            ? lv_color_hex(C_ARC_ACCENT) : lv_color_hex(C_ARC_TRACK);
        lv_obj_set_style_bg_color(d->dots[j], col, 0);
    }
}

// ============================================================
//  LVGL callbacks  (v9 signatures)
// ============================================================

static void flush_cb(lv_display_t *disp, const lv_area_t *area, uint8_t *px_map) {
    if (lv_display_flush_is_last(disp)) {
        esp_lcd_panel_draw_bitmap(_panel_handle, 0, 0, LCD_WIDTH, LCD_HEIGHT,
                                  (void *)px_map);
        xSemaphoreTake(_vsync_sem, 0);
        xSemaphoreTake(_vsync_sem, pdMS_TO_TICKS(50));
    }
    lv_display_flush_ready(disp);
}

static IRAM_ATTR bool vsync_cb(esp_lcd_panel_handle_t panel,
                                const esp_lcd_rgb_panel_event_data_t *edata,
                                void *user_ctx) {
    if (!_vsync_sem) return false;
    BaseType_t higher = pdFALSE;
    xSemaphoreGiveFromISR(_vsync_sem, &higher);
    return higher == pdTRUE;
}

// Two indev instances share one GT911 read per LVGL tick.
// Indev 0 (always called first by LVGL) does the I2C read; indev 1 reuses the result.
static void _touch_fill(lv_indev_data_t *data, int idx) {
    bool active = _touch.isTouched && (_touch.touches > idx);
    data->state = active ? LV_INDEV_STATE_PRESSED : LV_INDEV_STATE_RELEASED;
    if (active) {
        data->point.x = _touch.points[idx].x;
        data->point.y = _touch.points[idx].y;
    }
}
static void touch_read_cb_0(lv_indev_t *, lv_indev_data_t *data) {
    static uint32_t _touch_count = 0;
    _touch_count++;
    if (_touch_count == 1) Serial.println("[TOUCH] first read...");
    _touch.read();          // single I2C read per tick, done here
    if (_touch_count == 1) Serial.println("[TOUCH] first read done");
    _touch_fill(data, 0);
}
static void touch_read_cb_1(lv_indev_t *, lv_indev_data_t *data) {
    _touch_fill(data, 1);   // reuse data already read by cb_0
}

// ============================================================
// ============================================================
//  Shared top bar — forward declarations + build helper
// ============================================================

// Tab navigation callbacks (implemented after ev_send_config below)
static void ev_tab_home      (lv_event_t *e);
static void ev_tab_positions (lv_event_t *e);
static void ev_tab_config    (lv_event_t *e);
// E-STOP callback (implemented in the navigation section)
static void ev_estop         (lv_event_t *e);

// active_tab: 0 = Home, 1 = Positions, 2 = Config
// Builds the 50px header on *screen* and stores its client label in _lbl_clients[active_tab].
static void ev_mounts_open(lv_event_t *e);   // pairing panel (defined with it)

static void build_top_bar(lv_obj_t *screen, int active_tab) {
    lv_obj_t *hdr = lv_obj_create(screen);
    lv_obj_set_size(hdr, LCD_WIDTH, 50);
    lv_obj_set_pos(hdr, 0, 0);
    lv_obj_set_style_bg_color(hdr, lv_color_hex(C_SURF), 0);
    lv_obj_set_style_bg_opa(hdr, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(hdr, 0, 0);
    lv_obj_set_style_radius(hdr, 0, 0);
    lv_obj_set_style_pad_left(hdr,  12, 0);
    lv_obj_set_style_pad_right(hdr, 12, 0);
    lv_obj_set_style_pad_top(hdr,    0, 0);
    lv_obj_set_style_pad_bottom(hdr, 0, 0);
    lv_obj_remove_flag(hdr, LV_OBJ_FLAG_SCROLLABLE);

    // TCP / WS client count — top-left, pre-populated from current state
    char cl_buf[32];
    snprintf(cl_buf, sizeof(cl_buf), "TCP: %u   WS: %u", _tcp_clients, _ws_clients);
    lv_obj_t *cl_lbl = make_label(hdr, cl_buf, &lv_font_montserrat_12, C_DIM);
    lv_obj_align(cl_lbl, LV_ALIGN_LEFT_MID, 0, 0);
    _lbl_clients[active_tab] = cl_lbl;

    // Paired-mounts panel (pairing stage 3) — small button after the counts
    lv_obj_t *mts = make_button(hdr, "Mounts", C_SURF2, ev_mounts_open);
    lv_obj_set_size(mts, 78, 34);
    lv_obj_align(mts, LV_ALIGN_LEFT_MID, 128, 0);
    lv_obj_set_style_text_color(lv_obj_get_child(mts, 0), lv_color_hex(C_TEXT), 0);

    // Navigation tabs — centred in the header
    lv_obj_t *tabs = lv_obj_create(hdr);
    lv_obj_set_size(tabs, 330, 38);
    lv_obj_align(tabs, LV_ALIGN_CENTER, 0, 0);
    lv_obj_set_style_bg_opa(tabs, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(tabs, 0, 0);
    lv_obj_set_style_pad_all(tabs, 0, 0);
    lv_obj_set_style_pad_column(tabs, 6, 0);
    lv_obj_set_flex_flow(tabs, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(tabs, LV_FLEX_ALIGN_CENTER,
                          LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_remove_flag(tabs, LV_OBJ_FLAG_SCROLLABLE);

    // Home tab
    {
        bool active = (active_tab == 0);
        lv_obj_t *btn = make_button(tabs, "Home",
                                    active ? C_ACCENT : C_SURF2,
                                    active ? nullptr : ev_tab_home);
        lv_obj_set_size(btn, 100, 34);
        lv_obj_set_style_text_color(lv_obj_get_child(btn, 0),
                                    lv_color_hex(active ? 0xFFFFFF : C_TEXT), 0);
    }
    // Positions tab
    {
        bool active = (active_tab == 1);
        lv_obj_t *btn = make_button(tabs, "Positions",
                                    active ? C_ACCENT : C_SURF2,
                                    active ? nullptr : ev_tab_positions);
        lv_obj_set_size(btn, 110, 34);
        lv_obj_set_style_text_color(lv_obj_get_child(btn, 0),
                                    lv_color_hex(active ? 0xFFFFFF : C_TEXT), 0);
    }
    // Config tab
    {
        bool active = (active_tab == 2);
        lv_obj_t *btn = make_button(tabs, "Config",
                                    active ? C_ACCENT : C_SURF2,
                                    active ? nullptr : ev_tab_config);
        lv_obj_set_size(btn, 100, 34);
        lv_obj_set_style_text_color(lv_obj_get_child(btn, 0),
                                    lv_color_hex(active ? 0xFFFFFF : C_TEXT), 0);
    }

    // E-STOP — top-right
    lv_obj_t *estop_btn = make_button(hdr, LV_SYMBOL_STOP "  E-STOP", C_RED, ev_estop);
    lv_obj_set_size(estop_btn, 150, 34);
    lv_obj_align(estop_btn, LV_ALIGN_RIGHT_MID, 0, 0);
    lv_obj_set_style_text_color(lv_obj_get_child(estop_btn, 0), lv_color_hex(0xFFFFFF), 0);
}

// ============================================================
//  Positions screen — slot / dial refresh
// ============================================================

// Repaint ONE camera's row.  Split out of refresh_positions_slots() so a STATUS
// update can refresh just the camera that changed: the full sweep touches all 50
// buttons (~350 LVGL style calls) and re-renders the whole screen, which is far
// too heavy to run on every incoming STATUS.
static void refresh_positions_row(int i) {
    if (!_scr_positions || i < 0 || i >= 5) return;
    {
        uint16_t occ = _slots[i].slot_occupied;
        uint16_t at  = _slots[i].slot_at;
        uint8_t  tgt = _slots[i].target_slot;
        uint8_t  st  = _slots[i].state;
        bool     la  = cam_is_look_at(i);
        for (int s = 0; s < 10; s++) {
            if (!_pos_slot_btn[i][s]) continue;
            lv_obj_t *lbl = lv_obj_get_child(_pos_slot_btn[i][s], 0);
            uint16_t bit = (uint16_t)(1u << s);
            uint32_t border_col;

            if (la && s >= 8) {
                // Arrow buttons — border reflects look-at move state.
                int8_t arr = _la_arrow_state[i];
                bool moving = (arr == 0 && s == 8) || (arr == 1 && s == 9);
                bool done   = (arr == 2 && s == 8) || (arr == 3 && s == 9);
                if (moving)
                    border_col = _la_flash_on ? C_BTN_BORDER_TGT : C_BORDER;
                else if (done)
                    border_col = C_BTN_BORDER_AT;    // green  — slider arrived
                else
                    border_col = C_BORDER;            // grey   — idle
                lv_obj_set_style_bg_color(_pos_slot_btn[i][s], lv_color_hex(C_SURF2), 0);
                lv_obj_set_style_bg_color(_pos_slot_btn[i][s], lv_color_hex(C_SURF2), LV_STATE_PRESSED);
                lv_obj_set_style_border_color(_pos_slot_btn[i][s], lv_color_hex(border_col), 0);
                lv_obj_set_style_border_width(_pos_slot_btn[i][s], 2, 0);
                if (lbl) {
                    lv_label_set_text(lbl, (s == 8) ? LV_SYMBOL_LEFT : LV_SYMBOL_RIGHT);
                    lv_obj_set_style_text_color(lbl, lv_color_hex(C_TEXT), 0);
                }
                continue;
            }

            if (la && s < 8) {
                // Subject buttons — green=active subject, red=stored, grey=empty
                if (s == (int)_active_la_subject[i])
                    border_col = C_BTN_BORDER_AT;
                else if (occ & bit)
                    border_col = C_BTN_BORDER_OCC;
                else
                    border_col = C_BTN_BORDER_EMPTY;
                lv_obj_set_style_bg_color(_pos_slot_btn[i][s], lv_color_hex(C_SURF2), 0);
                lv_obj_set_style_bg_color(_pos_slot_btn[i][s], lv_color_hex(C_SURF2), LV_STATE_PRESSED);
                lv_obj_set_style_border_color(_pos_slot_btn[i][s], lv_color_hex(border_col), 0);
                lv_obj_set_style_border_width(_pos_slot_btn[i][s], C_BTN_BORDER_W, 0);
                if (lbl) {
                    char nb[4]; snprintf(nb, sizeof(nb), "%d", s + 1);
                    lv_label_set_text(lbl, nb);
                    lv_obj_set_style_text_font(lbl,  &lv_font_montserrat_12, 0);
                    lv_obj_set_style_text_color(lbl, lv_color_hex(C_DIM), 0);
                }
                continue;
            }

            // Normal position-slot buttons.  The target flashes while the mount
            // travels — same behaviour as the PC app and the web app.
            if (tgt != 0xFF && s == (int)tgt && st == STATE_MOVING_TO_POS)
                border_col = _la_flash_on ? C_BTN_BORDER_TGT : C_BORDER;
            else if ((at & bit) && (occ & bit)) border_col = C_BTN_BORDER_AT;
            else if (occ & bit)                 border_col = C_BTN_BORDER_OCC;
            else                                border_col = C_BTN_BORDER_EMPTY;

            lv_obj_set_style_bg_color(_pos_slot_btn[i][s], lv_color_hex(C_SURF2), 0);
            lv_obj_set_style_bg_color(_pos_slot_btn[i][s], lv_color_hex(C_SURF2), LV_STATE_PRESSED);
            lv_obj_set_style_border_color(_pos_slot_btn[i][s], lv_color_hex(border_col), 0);
            lv_obj_set_style_border_width(_pos_slot_btn[i][s], C_BTN_BORDER_W, 0);
            if (lbl) {
                char nb[4]; snprintf(nb, sizeof(nb), "%d", s + 1);
                lv_label_set_text(lbl, nb);
                lv_obj_set_style_text_font(lbl,  &lv_font_montserrat_12, 0);
                lv_obj_set_style_text_color(lbl, lv_color_hex(C_DIM), 0);
            }
        }
    }
}

static void refresh_positions_slots() {
    if (!_scr_positions) return;
    for (int i = 0; i < 5; i++) refresh_positions_row(i);
}

static void refresh_positions_dials() {
    if (!_scr_positions) return;
    for (int i = 0; i < 5; i++) {
        update_tile_dial(&_pos_pt_dial[i], _cam[i].pt_preset);
        update_tile_dial(&_pos_sl_dial[i],
                         cam_has_slider(i) ? _cam[i].sl_preset : 0);
    }
}

// ============================================================
//  Positions screen — event callbacks
// ============================================================

static void ev_pos_slot_btn(lv_event_t *e) {
    uint32_t ud   = (uint32_t)(uintptr_t)lv_event_get_user_data(e);
    uint8_t  cam  = (uint8_t)((ud >> 8) & 0xFF);
    uint8_t  slot = (uint8_t)(ud & 0xFF);

    // Arrow buttons (slots 8-9) on look-at cameras use pressed/released — ignore click.
    if (cam_is_look_at(cam) && slot >= 8) return;

    // Subject tap (slots 0-7) on look-at cameras: toggle selection, send switch command.
    if (cam_is_look_at(cam) && slot < 8) {
        if (_active_la_subject[cam] == (int8_t)slot) {
            _active_la_subject[cam] = -1;   // deselect
        } else if ((_slots[cam].slot_occupied >> slot) & 1) {
            _active_la_subject[cam] = (int8_t)slot;
            if (_send_cb) {
                uint8_t sid = slot;
                _send_cb(cam + 1, CMD_SWITCH_SUBJECT, &sid, 1);
            }
        }
        refresh_positions_slots();
        return;
    }

    if (!(_slots[cam].slot_occupied & (uint16_t)(1u << slot))) return;
    uint8_t payload[2] = { slot, _cam[cam].pt_preset };
    if (_send_cb) _send_cb(cam + 1, CMD_GOTO_SLOT, payload, 2);
}

// Arrow buttons (slots 8-9) on look-at rows — mirror the detail screen logic.
// user_data = (cam << 8) | slot,  slot 8 = ◄ (dir=0/min),  slot 9 = ► (dir=1/max).

static void ev_pos_arrow_pressed(lv_event_t *e) {
    uint32_t ud   = (uint32_t)(uintptr_t)lv_event_get_user_data(e);
    uint8_t  cam  = (uint8_t)((ud >> 8) & 0xFF);
    uint8_t  slot = (uint8_t)(ud & 0xFF);
    if (!cam_is_look_at(cam)) return;

    int8_t subj = _active_la_subject[cam];
    if (subj < 0) {
        for (int b = 0; b < 8; b++)
            if (_slots[cam].slot_occupied & (1u << b)) { subj = (int8_t)b; break; }
    }
    if (subj < 0) return;

    uint8_t direction  = (slot == 8) ? 0 : 1;
    uint8_t speed_pset = (_cam[cam].sl_preset > 0) ? _cam[cam].sl_preset : 2;
    uint8_t payload[3] = { (uint8_t)subj, direction, speed_pset };
    if (_send_cb) _send_cb(cam + 1, CMD_START_LOOK_AT_MOVE, payload, 3);
}

static void ev_pos_arrow_pressing(lv_event_t *e) {
    uint32_t ud   = (uint32_t)(uintptr_t)lv_event_get_user_data(e);
    uint8_t  cam  = (uint8_t)((ud >> 8) & 0xFF);
    uint8_t  slot = (uint8_t)(ud & 0xFF);
    if (!(_cam[cam].flags & FLAG_HAS_SLIDER)) return;
    if (cam_is_look_at(cam)) return;   // look-at fires once on press, not continuously

    int16_t vel = (slot == 8) ? -500 : 500;
    uint8_t p[8] = {};
    p[4] = (uint8_t)((uint16_t)vel >> 8); p[5] = (uint8_t)vel;
    if (_send_cb) _send_cb(cam + 1, CMD_JOG, p, 8);
}

static void ev_pos_arrow_released(lv_event_t *e) {
    uint32_t ud  = (uint32_t)(uintptr_t)lv_event_get_user_data(e);
    uint8_t  cam = (uint8_t)((ud >> 8) & 0xFF);
    if (!(_cam[cam].flags & FLAG_HAS_SLIDER)) return;
    if (cam_is_look_at(cam)) return;   // look-at moves run to their natural end

    uint8_t p[8] = {};   // all zeros — stop jog
    if (_send_cb) _send_cb(cam + 1, CMD_JOG, p, 8);
}

static void ev_pos_pt_click(lv_event_t *e) {
    if (!_send_cb) return;
    uint8_t cam = (uint8_t)(uintptr_t)lv_event_get_user_data(e);
    uint8_t np  = (_cam[cam].pt_preset % 4) + 1;
    uint8_t payload[2] = { GROUP_PAN_TILT, np };
    _send_cb(cam + 1, CMD_SET_ACTIVE_PRESET, payload, 2);
    // Do NOT update local state or dials here — wait for STATUS echo from camera
}

static void ev_pos_sl_click(lv_event_t *e) {
    if (!_send_cb) return;
    uint8_t cam = (uint8_t)(uintptr_t)lv_event_get_user_data(e);
    if (!cam_has_slider(cam)) return;         // dormant — no slider fitted
    uint8_t np  = (_cam[cam].sl_preset % 4) + 1;
    uint8_t payload[2] = { GROUP_SLIDER_ZOOM, np };
    _send_cb(cam + 1, CMD_SET_ACTIVE_PRESET, payload, 2);
    // Do NOT update local state or dials here — wait for STATUS echo from camera
}

// ============================================================
//  Positions screen — build / destroy / navigation
// ============================================================

static void destroy_positions_screen();   // forward declaration
static void build_positions_screen();     // forward declaration
static void destroy_detail_screen();      // forward declaration
static void build_detail_screen();        // forward declaration
// Slot data staged by loop() (core 0) and applied by whichever side next holds
// the LVGL mutex.
//
// hub_ui_update_slots() must never block: a wait there stalls Serial1.read()
// and overflows the UART at 460800.  But it used to take the mutex BEFORE
// recording anything, so a failed take discarded the mount's new state
// outright — not deferred, lost.  The next update that did win the lock was
// then compared against state from before the drop, so the screen converged
// only when one happened to get through.
//
// The detail screen re-renders most and therefore holds the mutex longest,
// which is why its borders lagged ~10 s while the home dots and Positions
// screen — cheaper to repaint, so losing far fewer updates — looked instant.
//
// Payload first, flag last; the mutex acquisition is the barrier.  Same pattern
// as _subject_mask and _mount_table.
struct StagedSlots { uint16_t occ, at; uint8_t tgt, state; };
static StagedSlots    _slots_in[5]    = {};
static volatile bool  _slots_dirty[5] = { false, false, false, false, false };

static void apply_slots_locked(int i);    // forward declaration
static void refresh_detail_slots();       // forward declaration
static void refresh_detail_dials();       // forward declaration

static void ev_goto_positions(lv_event_t *e) {
    _pending_action = NavAction::GOTO_POSITIONS;   // task loop does the work
}

static void ev_positions_back(lv_event_t *e) {
    _pending_action = NavAction::GOTO_STATUS_FROM_POSITIONS;
}

static void destroy_positions_screen() {
    if (!_scr_positions) return;
    lv_obj_delete(_scr_positions);
    _scr_positions = nullptr;
    _lbl_clients[1] = nullptr;   // widget deleted above — prevent dangling writes
    for (int i = 0; i < 5; i++) {
        for (int s = 0; s < 10; s++) _pos_slot_btn[i][s] = nullptr;
        _pos_pt_dial[i] = {};
        _pos_sl_dial[i] = {};
    }
}

static void build_positions_screen() {
    _scr_positions = lv_obj_create(nullptr);
    lv_obj_set_size(_scr_positions, LCD_WIDTH, LCD_HEIGHT);
    lv_obj_set_style_bg_color(_scr_positions, lv_color_hex(C_BG), 0);
    lv_obj_set_style_pad_all(_scr_positions, 0, 0);
    lv_obj_remove_flag(_scr_positions, LV_OBJ_FLAG_SCROLLABLE);

    // ── Shared top bar (Positions tab active) ───────────────────
    build_top_bar(_scr_positions, 1);

    // ── 5 camera rows ────────────────────────────────────────────
    // Header h=50 → remaining 430px split equally: 430/5 = 86px per row, no gaps.
    //
    // Per-row layout (content area = 792px wide, after pad_all=4):
    //   10 slot buttons: w=62, gap=4px → 656px; x=0..655
    //   gap 8px
    //   PT arc 60×60:  x=664
    //   gap 4px
    //   SL arc 60×60:  x=728  → ends x=788 ≤ 792 ✓
    //   content_h = 86-8 = 78px; vertical centre of 60px: (78-60)/2 = 9

    const int NUM_ROWS  = 5;
    const int ROW_H    = (LCD_HEIGHT - 50) / 5;    // keep same height as 5-row layout
    const int ROW_Y0   = 50;
    const int ROW_GAP  = 0;
    const int BTN_W    = 62;
    const int BTN_GAP  = 4;
    const int BTN_H    = 60;
    const int DIAL_SL_X = 664;
    const int DIAL_PT_X = 728;
    const int DIAL_Y    = (ROW_H - 8 - 60) / 2;

    for (int i = 0; i < NUM_ROWS; i++) {
        int row_y = ROW_Y0 + i * ROW_H;

        lv_obj_t *row = lv_obj_create(_scr_positions);
        lv_obj_set_pos(row, 0, row_y);
        lv_obj_set_size(row, LCD_WIDTH, ROW_H);
        lv_obj_set_style_bg_color(row, lv_color_hex(CAM_ROW_BG[i]), 0);
        lv_obj_set_style_bg_opa(row, LV_OPA_COVER, 0);
        lv_obj_set_style_border_width(row, 0, 0);
        lv_obj_set_style_radius(row, 0, 0);
        lv_obj_set_style_pad_all(row, 4, 0);
        lv_obj_remove_flag(row, LV_OBJ_FLAG_SCROLLABLE);

        // 10 position buttons
        for (int s = 0; s < 10; s++) {
            int bx = s * (BTN_W + BTN_GAP);
            int by = (ROW_H - 8 - BTN_H) / 2;   // 8 = 2×pad_all
            lv_obj_t *btn = lv_button_create(row);
            lv_obj_set_size(btn, BTN_W, BTN_H);
            lv_obj_set_pos(btn, bx, by);
            lv_obj_set_style_bg_color(btn, lv_color_hex(C_SURF2), 0);
            lv_obj_set_style_bg_color(btn, lv_color_hex(C_SURF2), LV_STATE_PRESSED);
            lv_obj_set_style_border_color(btn, lv_color_hex(C_BTN_BORDER_EMPTY), 0);
            lv_obj_set_style_border_width(btn, C_BTN_BORDER_W, 0);
            lv_obj_set_style_radius(btn, 6, 0);
            uint32_t ud = ((uint32_t)i << 8) | (uint32_t)s;
            lv_obj_add_event_cb(btn, ev_pos_slot_btn, LV_EVENT_CLICKED, (void *)(uintptr_t)ud);
            // Arrow pressed/pressing/released only needed on the two arrow slots.
            if (s >= 8) {
                lv_obj_add_event_cb(btn, ev_pos_arrow_pressed,  LV_EVENT_PRESSED,  (void *)(uintptr_t)ud);
                lv_obj_add_event_cb(btn, ev_pos_arrow_pressing, LV_EVENT_PRESSING, (void *)(uintptr_t)ud);
                lv_obj_add_event_cb(btn, ev_pos_arrow_released, LV_EVENT_RELEASED, (void *)(uintptr_t)ud);
            }
            char nb[4];
            snprintf(nb, sizeof(nb), "%d", s + 1);
            lv_obj_t *lbl = lv_label_create(btn);
            lv_label_set_text(lbl, nb);
            lv_obj_set_style_text_font(lbl,  &lv_font_montserrat_12, 0);
            lv_obj_set_style_text_color(lbl, lv_color_hex(C_DIM), 0);
            lv_obj_center(lbl);
            _pos_slot_btn[i][s] = btn;
        }

        // SL arc dial (left) — use row background so dial blends in
        build_tile_dial(&_pos_sl_dial[i], row, DIAL_SL_X, DIAL_Y, CAM_ROW_BG[i], false);
        lv_obj_add_flag(_pos_sl_dial[i].arc, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_event_cb(_pos_sl_dial[i].arc, ev_pos_sl_click, LV_EVENT_CLICKED,
                            (void *)(uintptr_t)i);

        // PT arc dial (right) — use row background so dial blends in
        build_tile_dial(&_pos_pt_dial[i], row, DIAL_PT_X, DIAL_Y, CAM_ROW_BG[i], false);
        lv_obj_add_flag(_pos_pt_dial[i].arc, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_event_cb(_pos_pt_dial[i].arc, ev_pos_pt_click, LV_EVENT_CLICKED,
                            (void *)(uintptr_t)i);
    }

    // Do NOT call lv_obj_update_layout here — this function is called from inside
    // ev_tab_positions, which runs inside lv_timer_handler on the LVGL task.
    // The call stack is already deep (task → timer → touch → event → here), and
    // lv_obj_update_layout adds enough depth to overflow the 32 KB LVGL task stack.
    // LVGL marks all the new objects dirty automatically; the layout pass runs on
    // the next lv_timer_handler tick (~5 ms later) with a flat call stack.
}

// ============================================================
//  Screen lifecycle helpers  (lazy swap — only 2 screens in RAM at once)
// ============================================================

// Forward declarations needed by the lifecycle helpers and event callbacks
static void det_cancel_set();
static void det_cancel_clear();
static void build_config_screen();
static void ev_send_config(lv_event_t *e);  // save current camera config to mount EEPROM
static void build_detail_screen();
static void refresh_detail_slots();
static void refresh_detail_dials();

// Destroy the detail screen and clear all its widget handles.
// Safe to call even if the screen has never been built (_scr_detail == nullptr).
static void destroy_detail_screen() {
    if (!_scr_detail) return;
    // Abort any in-progress subject calibration before tearing the screen down.
    if (_calib_state != 0 && _send_cb)
        _send_cb(_detail_cam + 1, CMD_ADD_SUBJECT_ABORT, nullptr, 0);
    _calib_state = 0;
    _calib_slot  = 0xFF;
    det_cancel_set();
    det_cancel_clear();              // kills _clear_timer, resets _det_clear_confirm
    lv_obj_delete(_scr_detail);
    _scr_detail    = nullptr;
    _det_hdr       = nullptr;
    for (int i = 0; i < 5; i++) _det_cam_btns[i] = nullptr;
    for (int s = 0; s < 10; s++) _det_pos_btn[s] = nullptr;
    _det_set_btn   = nullptr;
    _det_set_lbl   = nullptr;
    _det_clear_btn = nullptr;
    _det_clear_lbl = nullptr;
    _det_pt_dial   = {};
    _det_sl_dial   = {};
    _hsl_sl        = {};
    _hsl_zoom      = {};
    _joy_pt        = {};
}

// Save slider values then destroy the config screen and clear its handles.
static void destroy_config_screen() {
    if (!_scr_config) return;
    // Persist current slider positions so they survive a rebuild
    for (int g = 0; g < 2; g++)
        for (int p = 0; p < 4; p++)
            _cfg_speeds[g][p] = lv_slider_get_value(_cfg_sliders[g][p]);
    lv_obj_delete(_scr_config);
    _scr_config    = nullptr;
    _lbl_clients[2] = nullptr;   // widget deleted above — prevent dangling writes
    _cfg_sw_paninv   = nullptr;
    _cfg_sw_slinv    = nullptr;
    _cfg_sw_zminv    = nullptr;
    _cfg_sw_lanczm   = nullptr;
    _cfg_sw_hassl    = nullptr;
    _cfg_sw_lookAt   = nullptr;
    _cfg_lbl_tilt    = nullptr;
    _cfg_btn_findsl  = nullptr;
    _cfg_btn_findzm  = nullptr;
    _cfg_find_lbl    = nullptr;
    _cfg_btn_manualref= nullptr;
    _cfg_ref_lbl      = nullptr;
    for (int i = 0; i < 5; i++) _cfg_cam_btns[i] = nullptr;
    for (int g = 0; g < 2; g++)
        for (int p = 0; p < 4; p++) _cfg_sliders[g][p] = nullptr;
}

// ============================================================
//  Find-home helpers
// ============================================================

// Enable/disable the Find Home buttons to match current mount config.
// Must be called while holding _lvgl_mux (or from the LVGL task).
static void _cfg_update_find_btns() {
    if (!_cfg_btn_findsl || !_cfg_btn_findzm) return;
    if (_cfg_has_slider)
        lv_obj_clear_state(_cfg_btn_findsl, LV_STATE_DISABLED);
    else
        lv_obj_add_state(_cfg_btn_findsl, LV_STATE_DISABLED);
    if (!_cfg_lanc_zoom)
        lv_obj_clear_state(_cfg_btn_findzm, LV_STATE_DISABLED);
    else
        lv_obj_add_state(_cfg_btn_findzm, LV_STATE_DISABLED);
}

// ============================================================
//  Event callbacks
// ============================================================

// CONFIG button on status header: swap detail out, config in.
static void ev_goto_config(lv_event_t *e)    { _pending_action = NavAction::GOTO_CONFIG; }

// Back button on config screen.
static void ev_goto_status(lv_event_t *e)    { _pending_action = NavAction::GOTO_STATUS_FROM_CONFIG; }

static void ev_estop(lv_event_t *e) {
    if (!_send_cb) return;
    _send_cb(MOUNT_BROADCAST, CMD_E_STOP, nullptr, 0);
}

// ── Tab navigation callbacks (implementations) ────────────────────────────
static void ev_tab_home     (lv_event_t *e)  { _pending_action = NavAction::GOTO_STATUS_FROM_POSITIONS; }
static void ev_tab_positions(lv_event_t *e)  { _pending_action = NavAction::GOTO_POSITIONS; }
static void ev_tab_config   (lv_event_t *e)  { _pending_action = NavAction::GOTO_CONFIG; }

static void ev_cfg_cam_select(lv_event_t *e) {
    uint8_t idx = (uint8_t)(uintptr_t)lv_event_get_user_data(e);
    if (idx == _cfg_cam) return;   // same camera — nothing to do
    // Save the current camera's settings to its EEPROM before switching.
    // Sliders still reflect the current camera so ev_send_config reads correctly.
    ev_send_config(nullptr);
    _cfg_cam = idx;
    for (int i = 0; i < 5; i++) {
        lv_obj_set_style_bg_color(_cfg_cam_btns[i],
            lv_color_hex(i == idx ? C_ACCENT : C_SURF2), 0);
    }
    // Request live config from mount — hub_ui_update_config() will populate fields
    // when the reply arrives.  Pre-fill orientation from cached flags as a fallback.
    _cfg_pan_inv    = (_cam[idx].flags >> 0) & 1;
    _cfg_slider_inv = (_cam[idx].flags >> 1) & 1;
    _cfg_has_slider = (_cam[idx].flags & FLAG_HAS_SLIDER) != 0;
    // zoom_invert / lanc_zoom are not in STATUS flags — clear now, CONFIG_REPORT fills them.
    _cfg_zoom_inv   = false;
    _cfg_lanc_zoom  = false;
    if (_cfg_pan_inv)    lv_obj_add_state(_cfg_sw_paninv, LV_STATE_CHECKED);
    else                 lv_obj_clear_state(_cfg_sw_paninv, LV_STATE_CHECKED);
    if (_cfg_slider_inv) lv_obj_add_state(_cfg_sw_slinv, LV_STATE_CHECKED);
    else                 lv_obj_clear_state(_cfg_sw_slinv, LV_STATE_CHECKED);
    lv_obj_clear_state(_cfg_sw_zminv,  LV_STATE_CHECKED);  // updated by CONFIG_REPORT
    lv_obj_clear_state(_cfg_sw_lanczm, LV_STATE_CHECKED);  // updated by CONFIG_REPORT
    if (_cfg_has_slider) lv_obj_add_state(_cfg_sw_hassl, LV_STATE_CHECKED);
    else                 lv_obj_clear_state(_cfg_sw_hassl, LV_STATE_CHECKED);
    if ((_cam[idx].flags & FLAG_LOOK_AT_MODE)) lv_obj_add_state(_cfg_sw_lookAt, LV_STATE_CHECKED);
    else lv_obj_clear_state(_cfg_sw_lookAt, LV_STATE_CHECKED);
    if (_cfg_lbl_tilt) lv_label_set_text(_cfg_lbl_tilt, "--");  // filled by CONFIG_REPORT
    if (_cfg_find_lbl) lv_label_set_text(_cfg_find_lbl, "");  // clear previous status
    _cfg_update_find_btns();
    if (_cfg_ref_lbl) lv_label_set_text(_cfg_ref_lbl, "");
    if (_send_cb) _send_cb(idx + 1, CMD_GET_CONFIG, nullptr, 0);
}

static void ev_find_home(lv_event_t *e) {
    if (!_send_cb) return;
    uint8_t axis = (uint8_t)(uintptr_t)lv_event_get_user_data(e);
    _send_cb(_cfg_cam + 1, CMD_FIND_HOME, &axis, 1);
    if (_cfg_find_lbl) lv_label_set_text(_cfg_find_lbl, "Homing...");
    // Disable both buttons while homing runs — re-enabled on HOME_COMPLETE
    if (_cfg_btn_findsl) lv_obj_add_state(_cfg_btn_findsl, LV_STATE_DISABLED);
    if (_cfg_btn_findzm) lv_obj_add_state(_cfg_btn_findzm, LV_STATE_DISABLED);
}

static void ev_manual_ref(lv_event_t *e) {
    if (!_send_cb) return;
    uint8_t subj = 0xFF;   // sentinel: zero both angles at current position
    _send_cb(_cfg_cam + 1, CMD_SET_REF, &subj, 1);
    if (_cfg_ref_lbl) lv_label_set_text(_cfg_ref_lbl, "Reference set to 0° / 0°");
}

static void ev_send_config(lv_event_t *e) {
    if (!_send_cb) return;
    uint8_t mount_id = _cfg_cam + 1;

    uint8_t ori = 0;
    if (lv_obj_has_state(_cfg_sw_paninv, LV_STATE_CHECKED)) ori |= 0x01;
    if (lv_obj_has_state(_cfg_sw_slinv,  LV_STATE_CHECKED)) ori |= 0x02;
    if (lv_obj_has_state(_cfg_sw_hassl,  LV_STATE_CHECKED)) ori |= 0x04;
    if (lv_obj_has_state(_cfg_sw_zminv,  LV_STATE_CHECKED)) ori |= 0x08;
    if (lv_obj_has_state(_cfg_sw_lanczm, LV_STATE_CHECKED)) ori |= 0x10;
    if (lv_obj_has_state(_cfg_sw_lookAt, LV_STATE_CHECKED)) ori |= 0x40;
    _send_cb(mount_id, CMD_SET_ORIENTATION, &ori, 1);

    for (uint8_t grp = 0; grp < 2; grp++) {
        for (uint8_t pr = 0; pr < 4; pr++) {
            int32_t spd = lv_slider_get_value(_cfg_sliders[grp][pr]);
            // Use the acceleration cached from the last CONFIG_REPORT so we
            // never overwrite carefully-tuned accel values with spd × 10.
            // Scale proportionally if the user moved the speed slider.
            int32_t old_spd = _cfg_speeds[grp][pr];
            int32_t acc = _cfg_accels[grp][pr];
            if (old_spd > 0 && spd != old_spd) {
                // Keep the same speed/accel ratio — scale accel with speed change.
                acc = (int32_t)((float)acc * (float)spd / (float)old_spd);
                if (acc < 1) acc = 1;
            }
            uint8_t payload[10];
            payload[0] = grp;
            payload[1] = pr + 1;
            payload[2] = (spd >> 24) & 0xFF;
            payload[3] = (spd >> 16) & 0xFF;
            payload[4] = (spd >>  8) & 0xFF;
            payload[5] =  spd & 0xFF;
            payload[6] = (acc >> 24) & 0xFF;
            payload[7] = (acc >> 16) & 0xFF;
            payload[8] = (acc >>  8) & 0xFF;
            payload[9] =  acc & 0xFF;
            _send_cb(mount_id, CMD_SET_SPEED_PRESET, payload, 10);
        }
    }
}

// ============================================================
//  Detail screen — jog helpers
// ============================================================

static void flush_jog() {
    if (!_send_cb) return;
    uint8_t p[8];
    p[0] = (uint8_t)((uint16_t)_jog_pan  >> 8); p[1] = (uint8_t)_jog_pan;
    p[2] = (uint8_t)((uint16_t)_jog_tilt >> 8); p[3] = (uint8_t)_jog_tilt;
    p[4] = (uint8_t)((uint16_t)_jog_sl   >> 8); p[5] = (uint8_t)_jog_sl;
    p[6] = (uint8_t)((uint16_t)_jog_zoom >> 8); p[7] = (uint8_t)_jog_zoom;
    _send_cb(_detail_cam + 1, CMD_JOG, p, 8);

    // In look-at mode, any physical axis movement deselects the active subject
    // immediately so the display doesn't wait for a Teensy round-trip.
    if (cam_is_look_at(_detail_cam) &&
            (_jog_pan != 0 || _jog_tilt != 0 || _jog_sl != 0)) {
        if (_active_la_subject[_detail_cam] != -1) {
            _active_la_subject[_detail_cam] = -1;
            refresh_detail_slots();
            if (_scr_positions) refresh_positions_slots();
        }
    }
}

static void det_cancel_set() {
    _det_set_pending = false;
    if (_det_set_btn) {
        lv_obj_set_style_bg_color(_det_set_btn, lv_color_hex(C_ACCENT), 0);
        lv_obj_set_style_bg_color(_det_set_btn, lv_color_hex(C_ACCENT), LV_STATE_PRESSED);
    }
    if (_det_set_lbl) lv_label_set_text(_det_set_lbl, "SET");
}

static void det_cancel_clear() {
    if (_clear_timer) { lv_timer_delete(_clear_timer); _clear_timer = nullptr; }
    _det_clear_confirm = false;
    _det_clear_mask    = 0;
    if (_det_clear_lbl) lv_label_set_text(_det_clear_lbl, "CLEAR");
    if (_det_clear_btn) {
        lv_obj_set_style_bg_color(_det_clear_btn, lv_color_hex(C_SURF2), 0);
        lv_obj_set_style_bg_color(_det_clear_btn, lv_color_hex(C_SURF2), LV_STATE_PRESSED);
    }
    // Restore SET button from CANCEL back to its normal state
    if (_det_set_lbl) lv_label_set_text(_det_set_lbl, "SET");
    if (_det_set_btn) {
        lv_obj_set_style_bg_color(_det_set_btn, lv_color_hex(C_ACCENT), 0);
        lv_obj_set_style_bg_color(_det_set_btn, lv_color_hex(C_ACCENT), LV_STATE_PRESSED);
    }
    refresh_detail_slots();
}

// Update SET button colour/label to reflect the current calibration sub-state.
// Call while holding _lvgl_mux or from within the LVGL task.
static void det_update_calib_ui() {
    if (!_det_set_btn || !_det_set_lbl) return;
    switch (_calib_state) {
        case 1:   // moving to A (rare — slider already at home when calibration starts)
            lv_label_set_text(_det_set_lbl, "MOVING...");
            lv_obj_set_style_bg_color(_det_set_btn, lv_color_hex(C_ORANGE), 0);
            lv_obj_set_style_bg_color(_det_set_btn, lv_color_hex(C_ORANGE), LV_STATE_PRESSED);
            break;
        case 3:   // slider moving to B — press SET when it stops
            lv_label_set_text(_det_set_lbl, "MOVING...");
            lv_obj_set_style_bg_color(_det_set_btn, lv_color_hex(C_ORANGE), 0);
            lv_obj_set_style_bg_color(_det_set_btn, lv_color_hex(C_ORANGE), LV_STATE_PRESSED);
            break;
        case 2:   // arrived at A — user aims and presses SET
        case 4:   // arrived at B — user aims and presses SET
            lv_label_set_text(_det_set_lbl, "SET");
            lv_obj_set_style_bg_color(_det_set_btn, lv_color_hex(C_GREEN), 0);
            lv_obj_set_style_bg_color(_det_set_btn, lv_color_hex(C_GREEN), LV_STATE_PRESSED);
            break;
        default:  // idle — restore normal blue SET button
            lv_label_set_text(_det_set_lbl, "SET");
            lv_obj_set_style_bg_color(_det_set_btn, lv_color_hex(C_ACCENT), 0);
            lv_obj_set_style_bg_color(_det_set_btn, lv_color_hex(C_ACCENT), LV_STATE_PRESSED);
            break;
    }
}

// Reset calibration state and restore the detail-screen UI to idle.
static void det_reset_calib() {
    _calib_state     = 0;
    _calib_slot      = 0xFF;
    _det_set_pending = false;
    det_update_calib_ui();
    refresh_detail_slots();
}

static void refresh_detail_slots() {
    if (!_scr_detail) return;
    int  i = _detail_cam;
    uint16_t occ = _slots[i].slot_occupied;
    uint16_t at  = _slots[i].slot_at;
    uint8_t  tgt = _slots[i].target_slot;
    uint8_t  st  = _slots[i].state;
    for (int s = 0; s < 10; s++) {
        if (!_det_pos_btn[s]) continue;
        uint16_t bit = (uint16_t)(1u << s);

        // In clear-select mode, slots toggled into the clear mask get an orange fill.
        bool pending_clear = _det_clear_confirm && (_det_clear_mask & bit);
        uint32_t bg_col = pending_clear ? C_ORANGE : C_SURF2;

        uint32_t border_col;
        if (tgt != 0xFF && s == (int)tgt && st == STATE_MOVING_TO_POS) {
            // Follow the flash phase, not a solid colour: this runs on every
            // STATUS (~10 Hz), so painting solid yellow here overrode the flash
            // timer's grey phase — the border sat yellow ~90% of the time.
            border_col = _la_flash_on ? C_BTN_BORDER_TGT : C_BORDER;
        } else if (cam_is_look_at(i) && s < 8) {
            // Look-at mode: slots 0-7 are subjects — green=active subject, red=stored.
            if (s == (int)_active_la_subject[i])
                border_col = C_BTN_BORDER_AT;    // green — camera targeting this subject
            else if (occ & bit)
                border_col = C_BTN_BORDER_OCC;   // red — subject stored, not targeting
            else
                border_col = C_BTN_BORDER_EMPTY; // grey — no subject
        } else if ((at & bit) && (occ & bit)) {
            border_col = C_BTN_BORDER_AT;    // green  — at this slot
        } else if (occ & bit) {
            border_col = C_BTN_BORDER_OCC;   // red    — stored
        } else {
            border_col = C_BTN_BORDER_EMPTY;  // grey   — empty
        }

        lv_obj_set_style_bg_color(_det_pos_btn[s], lv_color_hex(bg_col), 0);
        lv_obj_set_style_bg_color(_det_pos_btn[s], lv_color_hex(bg_col), LV_STATE_PRESSED);
        lv_obj_set_style_border_color(_det_pos_btn[s], lv_color_hex(border_col), 0);
        lv_obj_set_style_border_width(_det_pos_btn[s], C_BTN_BORDER_W, 0);
    }

    // Slots 8-9: ◄/► arrows in look-at mode, numeric "9"/"10" otherwise.
    // The border loop above handles border colour for all slots; this block
    // only needs to manage the label text and font.
    if (cam_is_look_at(i)) {
        for (int s = 8; s <= 9; s++) {
            if (!_det_pos_btn[s]) continue;
            lv_obj_t *lbl = lv_obj_get_child(_det_pos_btn[s], 0);
            if (lbl) {
                lv_label_set_text(lbl, (s == 8) ? LV_SYMBOL_LEFT : LV_SYMBOL_RIGHT);
                lv_obj_set_style_text_color(lbl, lv_color_hex(C_TEXT), 0);
            }
            lv_obj_set_style_bg_color(_det_pos_btn[s], lv_color_hex(C_SURF2), 0);
            lv_obj_set_style_bg_color(_det_pos_btn[s], lv_color_hex(C_SURF2), LV_STATE_PRESSED);
            // Apply look-at arrow state: yellow=moving, green=arrived, grey=idle.
            int8_t arr = _la_arrow_state[i];
            bool arr_moving = (arr == 0 && s == 8) || (arr == 1 && s == 9);
            bool arr_done   = (arr == 2 && s == 8) || (arr == 3 && s == 9);
            uint32_t arr_bc = arr_moving ? (_la_flash_on ? C_BTN_BORDER_TGT : C_BORDER) :
                              arr_done   ? C_BTN_BORDER_AT  : C_BORDER;
            lv_obj_set_style_border_color(_det_pos_btn[s], lv_color_hex(arr_bc), 0);
            lv_obj_set_style_border_width(_det_pos_btn[s], 2, 0);
        }
        // Highlight the subject slot currently being calibrated (yellow border).
        if (_calib_slot < 8 && _calib_state > 0 && _det_pos_btn[_calib_slot]) {
            lv_obj_set_style_border_color(_det_pos_btn[_calib_slot],
                                          lv_color_hex(C_BTN_BORDER_TGT), 0);
            lv_obj_set_style_border_width(_det_pos_btn[_calib_slot], 4, 0);
        }
    } else {
        // Not in look-at mode — restore slots 8/9 to numeric labels.
        // Use montserrat_16 / C_TEXT to match how all 10 buttons are created.
        for (int s = 8; s <= 9; s++) {
            if (!_det_pos_btn[s]) continue;
            lv_obj_t *lbl = lv_obj_get_child(_det_pos_btn[s], 0);
            if (lbl) {
                char nb[4]; snprintf(nb, sizeof(nb), "%d", s + 1);
                lv_label_set_text(lbl, nb);
                lv_obj_set_style_text_font(lbl,  &lv_font_montserrat_16, 0);
                lv_obj_set_style_text_color(lbl, lv_color_hex(C_TEXT), 0);
            }
        }
    }
}

static void refresh_detail_dials() {
    if (!_scr_detail) return;
    int i = _detail_cam;
    update_tile_dial(&_det_pt_dial, _cam[i].pt_preset);
    update_tile_dial(&_det_sl_dial, cam_has_slider(i) ? _cam[i].sl_preset : 0);
}

// ============================================================
//  Detail screen — joystick event callbacks
// ============================================================

static void joy_update(Joystick *joy, bool is_pt) {
    lv_indev_t *indev = lv_indev_get_act();
    if (!indev) return;
    lv_point_t p;
    lv_indev_get_point(indev, &p);

    lv_area_t area;
    lv_obj_get_coords(joy->bg, &area);
    int32_t cx = (area.x1 + area.x2) / 2;
    int32_t cy = (area.y1 + area.y2) / 2;
    int32_t dx = p.x - cx;
    int32_t dy = p.y - cy;

    // Clamp to inner radius (leaves thumb centred when at boundary)
    const int32_t r = JOY_SIZE / 2 - JOY_THUMB_SIZE / 2;
    float dist = sqrtf((float)(dx * dx + dy * dy));
    if (dist > (float)r) {
        float sc = (float)r / dist;
        dx = (int32_t)((float)dx * sc);
        dy = (int32_t)((float)dy * sc);
    }

    lv_obj_set_pos(joy->thumb,
                   JOY_SIZE / 2 + dx - JOY_THUMB_SIZE / 2,
                   JOY_SIZE / 2 + dy - JOY_THUMB_SIZE / 2);

    int16_t jx = (int16_t)((float)dx * 1000.0f / (float)r);
    int16_t jy = (int16_t)((float)dy * 1000.0f / (float)r);
    if (abs(dx) < JOY_DEAD_ZONE) jx = 0;
    if (abs(dy) < JOY_DEAD_ZONE) jy = 0;

    // Circular-to-square: at 45° full deflection both axes reach ±1000
    if (jx != 0 || jy != 0) {
        float mag  = sqrtf((float)jx * (float)jx + (float)jy * (float)jy);
        float maxC = (float)(abs(jx) > abs(jy) ? abs(jx) : abs(jy));
        jx = (int16_t)((float)jx / maxC * mag);
        jy = (int16_t)((float)jy / maxC * mag);
    }

    if (is_pt) { _jog_pan = jx; _jog_tilt = -jy; }  // negate: screen-up = negative dy, but tilt-up should be positive
    else        { _jog_sl  = jx; _jog_zoom =  jy; }
    flush_jog();
}

static void joy_reset(Joystick *joy, bool is_pt) {
    lv_obj_set_pos(joy->thumb,
                   JOY_SIZE / 2 - JOY_THUMB_SIZE / 2,
                   JOY_SIZE / 2 - JOY_THUMB_SIZE / 2);
    if (is_pt) { _jog_pan = 0; _jog_tilt = 0; }
    flush_jog();
}

// ---- Horizontal slider helpers ----

static void hsl_update(HSlider *hsl, int16_t *jog_val) {
    lv_indev_t *indev = lv_indev_get_act();
    if (!indev) return;
    lv_point_t p;
    lv_indev_get_point(indev, &p);

    lv_area_t area;
    lv_obj_get_coords(hsl->bg, &area);

    const int32_t r = (HSL_W - HSL_THUMB_W) / 2;   // max deflection in px
    int32_t dx = p.x - (area.x1 + area.x2) / 2;
    if (dx > r) dx = r;
    if (dx < -r) dx = -r;

    lv_obj_set_pos(hsl->thumb,
                   HSL_W / 2 + dx - HSL_THUMB_W / 2,
                   (HSL_H - HSL_THUMB_H) / 2);

    if (abs(dx) < JOY_DEAD_ZONE) *jog_val = 0;
    else *jog_val = (int16_t)((float)dx * 1000.0f / (float)r);
    flush_jog();
}

static void hsl_reset(HSlider *hsl, int16_t *jog_val) {
    lv_obj_set_pos(hsl->thumb,
                   (HSL_W - HSL_THUMB_W) / 2,
                   (HSL_H - HSL_THUMB_H) / 2);
    *jog_val = 0;
    flush_jog();
}

static void ev_hsl_sl_pressing  (lv_event_t *e) { hsl_update(&_hsl_sl,   &_jog_sl);   }
static void ev_hsl_sl_release   (lv_event_t *e) { hsl_reset (&_hsl_sl,   &_jog_sl);   }
static void ev_hsl_zoom_pressing(lv_event_t *e) { hsl_update(&_hsl_zoom, &_jog_zoom); }
static void ev_hsl_zoom_release (lv_event_t *e) { hsl_reset (&_hsl_zoom, &_jog_zoom); }

static void ev_joy_pt_pressing(lv_event_t *e) { joy_update(&_joy_pt, true); }
static void ev_joy_pt_release (lv_event_t *e) { joy_reset (&_joy_pt, true); }

// ============================================================
//  Detail screen — button event callbacks
// ============================================================

// Shared helper — update the detail screen to show camera `idx`.
// Assumes _calib_state has already been cleaned up by the caller if necessary.
static void det_switch_cam(uint8_t idx) {
    _detail_cam   = idx;
    _det_sel_slot = 0xFF;
    det_cancel_set();
    det_cancel_clear();

    // Screen and dial background colours follow the active camera.
    lv_obj_set_style_bg_color(_scr_detail,      lv_color_hex(C_CAM_BG[idx]), 0);
    lv_obj_set_style_bg_color(_det_hdr,          lv_color_hex(C_SURF),        0);
    lv_obj_set_style_bg_color(_det_sl_dial.arc,  lv_color_hex(C_CAM_BG[idx]), 0);
    lv_obj_set_style_bg_color(_det_pt_dial.arc,  lv_color_hex(C_CAM_BG[idx]), 0);

    // Highlight active camera button; reset all others.
    for (int i = 0; i < 5; i++) {
        if (!_det_cam_btns[i]) continue;
        bool active = (i == (int)idx);
        lv_obj_set_style_bg_color(_det_cam_btns[i],
            lv_color_hex(active ? CAM_BTN_BG[idx]   : C_SURF2), 0);
        lv_obj_set_style_bg_color(_det_cam_btns[i],
            lv_color_hex(active ? CAM_BTN_BG[idx]   : C_SURF2), LV_STATE_PRESSED);
        lv_obj_set_style_text_color(lv_obj_get_child(_det_cam_btns[i], 0),
            lv_color_hex(active ? CAM_BTN_TEXT[idx] : C_TEXT), 0);
    }

    refresh_detail_slots();
    refresh_detail_dials();

    // Request fresh subject list when entering a look-at camera.
    if (cam_is_look_at(idx) && _send_cb)
        _send_cb(idx + 1, CMD_GET_SUBJECTS, nullptr, 0);
}

// Camera selector button in the detail header — switches to another camera directly.
static void ev_det_cam_select(lv_event_t *e) {
    uint8_t idx = (uint8_t)(uintptr_t)lv_event_get_user_data(e);
    if (!_cam[idx].connected) return;   // ignore disconnected cameras
    if (idx == _detail_cam) return;     // already on this camera

    // Stop any active jog using the OLD camera before switching.
    if (_jog_pan || _jog_tilt || _jog_sl || _jog_zoom) {
        _jog_pan = _jog_tilt = _jog_sl = _jog_zoom = 0;
        flush_jog();   // still sends to _detail_cam (old camera) ✓
    }
    // Abort subject calibration on the OLD camera.
    if (_calib_state != 0 && _send_cb)
        _send_cb(_detail_cam + 1, CMD_ADD_SUBJECT_ABORT, nullptr, 0);
    _calib_state = 0;
    _calib_slot  = 0xFF;

    det_switch_cam(idx);
}

static void ev_open_detail(lv_event_t *e) {
    uint8_t idx = (uint8_t)(uintptr_t)lv_event_get_user_data(e);
    if (!_cam[idx].connected) return;
    _pending_cam    = idx;
    _pending_action = NavAction::OPEN_DETAIL;
}

static void ev_detail_back(lv_event_t *e) {
    // Stop any active jog immediately (safe to do in the callback — no LVGL calls).
    if (_jog_pan || _jog_tilt || _jog_sl || _jog_zoom) {
        _jog_pan = _jog_tilt = _jog_sl = _jog_zoom = 0;
        flush_jog();   // sends a network packet only, no LVGL
    }
    _pending_action = NavAction::DETAIL_BACK;
}

// Arrow-button callbacks for the detail screen — only active when FLAG_HAS_SLIDER is set.
//
// Two separate event types are used to avoid look-at commands being spammed:
//
//   LV_EVENT_PRESSED  → ev_det_arrow_pressed  (fires ONCE per finger-down)
//       In look-at mode: sends CMD_START_LOOK_AT_MOVE.  The move runs to the
//       slider limit by itself — holding the button has no extra effect.
//       Uses the selected subject if one is active, otherwise the first
//       calibrated subject in the mask.  If no subjects exist, does nothing.
//
//   LV_EVENT_PRESSING → ev_det_arrow_pressing (fires continuously while held)
//       In standard slider mode only (not look-at): raw slider jog.
//
//   LV_EVENT_RELEASED → ev_det_arrow_released
//       Stops the jog if one was running.
//
// user_data = slot index (8 = left ◄  direction=0/min,  9 = right ►  direction=1/max).

static void ev_det_arrow_pressed(lv_event_t *e) {
    uint8_t slot = (uint8_t)(uintptr_t)lv_event_get_user_data(e);
    int     i    = _detail_cam;
    if (!(_cam[i].flags & FLAG_HAS_SLIDER)) return;
    if (!cam_is_look_at(i)) return;   // standard mode handled by ev_det_arrow_pressing

    // Resolve which subject to use for the look-at tracking move.
    int8_t subj = _active_la_subject[i];
    if (subj < 0) {
        // No subject explicitly selected — find the first calibrated subject.
        for (int b = 0; b < 8; b++) {
            if (_slots[i].slot_occupied & (1u << b)) { subj = (int8_t)b; break; }
        }
    }
    if (subj < 0) return;   // no calibrated subjects at all — nothing to do

    uint8_t direction  = (slot == 8) ? 0 : 1;   // 0=min/◄  1=max/►
    uint8_t speed_pset = (_cam[i].sl_preset > 0) ? _cam[i].sl_preset : 2;
    uint8_t payload[3] = { (uint8_t)subj, direction, speed_pset };
    if (_send_cb) _send_cb(i + 1, CMD_START_LOOK_AT_MOVE, payload, 3);
}

static void ev_det_arrow_pressing(lv_event_t *e) {
    uint8_t slot = (uint8_t)(uintptr_t)lv_event_get_user_data(e);
    int     i    = _detail_cam;
    if (!(_cam[i].flags & FLAG_HAS_SLIDER)) return;
    if (cam_is_look_at(i)) return;   // look-at mode handled by ev_det_arrow_pressed (single fire)

    // Standard slider mode — jog continuously while held.
    _jog_sl = (slot == 8) ? -500 : 500;
    flush_jog();
}

static void ev_det_arrow_released(lv_event_t *e) {
    (void)e;
    int i = _detail_cam;
    if (!(_cam[i].flags & FLAG_HAS_SLIDER)) return;

    // Only stop if we were raw-jogging; look-at moves run to their natural end.
    if (!cam_is_look_at(i) && _jog_sl != 0) {
        _jog_sl = 0;
        flush_jog();
    }
}

static void ev_detail_pos_btn(lv_event_t *e) {
    uint8_t slot = (uint8_t)(uintptr_t)lv_event_get_user_data(e);
    int i = _detail_cam;

    // Arrow buttons in look-at mode are driven by pressing/released — ignore the click event.
    if (cam_is_look_at(i) && slot >= 8) return;

    // In look-at mode with SET pending: start subject calibration for the chosen slot.
    if (cam_is_look_at(i) && _det_set_pending && slot < 8) {
        _det_set_pending = false;
        _calib_slot  = slot;
        _calib_state = 1;   // moving to slider end A
        uint8_t payload[1 + SUBJECT_NAME_LEN] = {};
        payload[0] = slot;
        // Name field left empty — labels are managed by each UI independently
        if (_send_cb) _send_cb(i + 1, CMD_ADD_SUBJECT_START, payload, sizeof(payload));
        det_update_calib_ui();
        refresh_detail_slots();
        return;
    }

    if (_det_clear_confirm) {
        // ── Clear-select mode: toggle this slot in the pending-clear mask ─────
        _det_clear_mask ^= (uint16_t)(1u << slot);
        // Reset the safety timeout on each tap so it doesn't expire mid-selection
        if (_clear_timer) {
            lv_timer_reset(_clear_timer);
        }
        refresh_detail_slots();
        return;
    }

    // In look-at mode, slots 0-7 are subjects: tap to select/deselect for look-at.
    if (cam_is_look_at(i) && slot < 8) {
        if (_active_la_subject[i] == (int8_t)slot) {
            _active_la_subject[i] = -1;   // deselect
        } else if ((_slots[i].slot_occupied >> slot) & 1) {
            _active_la_subject[i] = (int8_t)slot;  // select only if stored
            // Always send CMD_SWITCH_SUBJECT so the Teensy retargets pan/tilt
            // immediately if a look-at move is already running (no-op otherwise).
            if (_send_cb) {
                uint8_t sid = slot;
                _send_cb(i + 1, CMD_SWITCH_SUBJECT, &sid, 1);
            }
        }
        refresh_detail_slots();
        return;
    }

    if (_det_set_pending) {
        // SET was pressed first — store current position to this slot
        if (_send_cb) _send_cb(i + 1, CMD_STORE_POS, &slot, 1);
        det_cancel_set();
        _det_sel_slot = slot;
    } else {
        _det_sel_slot = slot;
        if (_slots[i].slot_occupied & (uint16_t)(1u << slot)) {
            uint8_t payload[2] = { slot, _cam[i].pt_preset };
            if (_send_cb) _send_cb(i + 1, CMD_GOTO_SLOT, payload, 2);
        }
    }
    refresh_detail_slots();
}

static void ev_detail_set(lv_event_t *e) {
    int i = _detail_cam;

    if (_det_clear_confirm) {
        det_cancel_clear();
        return;
    }

    if (cam_is_look_at(i)) {
        // ── Look-at mode: SET drives the subject calibration state machine ──
        if (_calib_state == 0) {
            // Idle — enter "pick a subject slot" mode (or cancel if already pending).
            if (_det_set_pending) {
                det_cancel_set();
            } else {
                _det_set_pending = true;
                if (_det_set_lbl) lv_label_set_text(_det_set_lbl, "SELECT");
                lv_obj_set_style_bg_color(_det_set_btn, lv_color_hex(C_ORANGE), 0);
                lv_obj_set_style_bg_color(_det_set_btn, lv_color_hex(C_ORANGE), LV_STATE_PRESSED);
            }
        } else if (_calib_state == 2) {
            // Arrived at slider end A — record point A, camera will move to end B.
            if (_send_cb) _send_cb(i + 1, CMD_ADD_SUBJECT_SET_A, nullptr, 0);
            _calib_state = 3;
            det_update_calib_ui();
        } else if (_calib_state == 3 || _calib_state == 4) {
            // Slider has moved to end B (or sub=4 was received confirming arrival).
            // The Teensy does not send sub=4, so we accept SET in state 3 too —
            // the user physically sees the slider stop, aims the camera, then presses SET.
            if (_send_cb) _send_cb(i + 1, CMD_ADD_SUBJECT_SET_B, nullptr, 0);
            det_reset_calib();   // wait for CALIB_SOLVED / CALIB_ERROR to confirm
        } else {
            // Moving state 1 (moving to A) — abort calibration.
            if (_send_cb) _send_cb(i + 1, CMD_ADD_SUBJECT_ABORT, nullptr, 0);
            det_reset_calib();
        }
        return;
    }

    // ── Normal (non-slider) mode ──────────────────────────────────────────────
    if (_det_set_pending) {
        det_cancel_set();
    } else {
        _det_set_pending = true;
        lv_obj_set_style_bg_color(_det_set_btn, lv_color_hex(C_ORANGE), 0);
        lv_obj_set_style_bg_color(_det_set_btn, lv_color_hex(C_ORANGE), LV_STATE_PRESSED);
    }
}

static void clear_timer_cb(lv_timer_t *) { det_cancel_clear(); }

static void ev_detail_clear(lv_event_t *e) {
    if (_calib_state != 0) return;   // CLEAR is inactive during subject calibration
    if (!_det_clear_confirm) {
        // ── First press: enter clear-select mode ──────────────────────────────
        _det_clear_confirm = true;
        _det_clear_mask    = 0;
        lv_label_set_text(_det_clear_lbl, "CONTINUE?");
        lv_obj_set_style_bg_color(_det_clear_btn, lv_color_hex(C_RED), 0);
        lv_obj_set_style_bg_color(_det_clear_btn, lv_color_hex(C_RED), LV_STATE_PRESSED);
        det_cancel_set();   // cancel any in-progress SET mode
        // Repurpose SET button as CANCEL for clear mode
        if (_det_set_lbl) lv_label_set_text(_det_set_lbl, "CANCEL");
        if (_det_set_btn) {
            lv_obj_set_style_bg_color(_det_set_btn, lv_color_hex(C_SURF2), 0);
            lv_obj_set_style_bg_color(_det_set_btn, lv_color_hex(C_SURF2), LV_STATE_PRESSED);
        }
        // 8-second safety timeout in case the operator walks away
        if (_clear_timer) lv_timer_delete(_clear_timer);
        _clear_timer = lv_timer_create(clear_timer_cb, 8000, nullptr);
        lv_timer_set_repeat_count(_clear_timer, 1);
        refresh_detail_slots();
    } else {
        // ── Second press: execute ─────────────────────────────────────────────
        if (_clear_timer) { lv_timer_delete(_clear_timer); _clear_timer = nullptr; }
        if (_send_cb) {
            if (_det_clear_mask == 0) {
                // No slots tapped — clear everything that is occupied
                uint16_t occ = _slots[_detail_cam].slot_occupied;
                for (int s = 0; s < 10; s++) {
                    if (occ & (uint16_t)(1u << s)) {
                        uint8_t sl = (uint8_t)s;
                        _send_cb(_detail_cam + 1, CMD_CLEAR_POS, &sl, 1);
                    }
                }
            } else {
                // Clear only the tapped slots
                for (int s = 0; s < 10; s++) {
                    if (_det_clear_mask & (uint16_t)(1u << s)) {
                        uint8_t sl = (uint8_t)s;
                        _send_cb(_detail_cam + 1, CMD_CLEAR_POS, &sl, 1);
                    }
                }
            }
        }
        det_cancel_clear();
    }
}

static void ev_det_pt_click(lv_event_t *e) {
    if (!_send_cb) return;
    uint8_t np = (_cam[_detail_cam].pt_preset % 4) + 1;
    uint8_t payload[2] = { GROUP_PAN_TILT, np };
    _send_cb(_detail_cam + 1, CMD_SET_ACTIVE_PRESET, payload, 2);
    // Do NOT update local state or dial here — wait for the camera's STATUS
    // echo to confirm the change.  Updating immediately caused a visible
    // double-move: once on press, again when STATUS arrived.
}

static void ev_det_sl_click(lv_event_t *e) {
    if (!_send_cb) return;
    if (!cam_has_slider(_detail_cam)) return;  // dormant — no slider fitted
    uint8_t np = (_cam[_detail_cam].sl_preset % 4) + 1;
    uint8_t payload[2] = { GROUP_SLIDER_ZOOM, np };
    _send_cb(_detail_cam + 1, CMD_SET_ACTIVE_PRESET, payload, 2);
    // Do NOT update local state or dial here — wait for the camera's STATUS echo.
}

// ============================================================
//  Build STATUS screen
// ============================================================
//
// Tile inner content (after pad_all=10, border=1):
//   ~128px wide × ~342px tall  (800px screen, 5 flex-grow tiles, 8px gaps)
//
// Per-tile layout (y values from inner top-left):
//   y=0   "CAM N" montserrat_16                    connection dot x=116,y=5
//   y=22  flex row: "CONNECTED/OFFLINE"  …  RSSI
//   y=40  state text
//   y=56  divider
//   y=64  PT arc (60×60) x=4    SL arc (60×60) x=68
//   y=126 "PAN / TILT" x=4,w=60  "SLIDER" x=68,w=60
//   y=142 divider
//   y=148 "POSITIONS" centered
//   y=166 slot row 1: slots 1-5  (22px circles, 4px gap, x=1,27,53,79,105)
//   y=194 slot row 2: slots 6-10

static void build_status_screen() {
    _scr_status = lv_obj_create(nullptr);
    lv_obj_set_size(_scr_status, LCD_WIDTH, LCD_HEIGHT);
    lv_obj_set_style_bg_color(_scr_status, lv_color_hex(C_BG), 0);
    lv_obj_set_style_pad_all(_scr_status, 0, 0);

    // ── Shared top bar (Home tab active) ────────────────────────
    build_top_bar(_scr_status, 0);

    // ── Tile container — fills all remaining height below header ──
    lv_obj_t *tiles = lv_obj_create(_scr_status);
    lv_obj_set_pos(tiles, 8, 58);
    lv_obj_set_size(tiles, LCD_WIDTH - 16, LCD_HEIGHT - 58);
    lv_obj_set_style_bg_opa(tiles, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(tiles, 0, 0);
    lv_obj_set_style_pad_all(tiles, 0, 0);
    lv_obj_set_style_pad_column(tiles, 8, 0);
    lv_obj_remove_flag(tiles, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_flex_flow(tiles, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(tiles, LV_FLEX_ALIGN_START,
                          LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_START);

    for (int i = 0; i < 5; i++) {
        lv_obj_t *tile = lv_obj_create(tiles);
        lv_obj_set_flex_grow(tile, 1);
        lv_obj_set_height(tile, LV_PCT(100));
        card_style(tile, C_CAM_BG[i]);
        lv_obj_set_style_pad_all(tile, 10, 0);
        lv_obj_remove_flag(tile, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(tile, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_event_cb(tile, ev_open_detail, LV_EVENT_CLICKED, (void *)(uintptr_t)i);
        _tile_obj[i] = tile;

        // ── CAM N label ──────────────────────────────────────────
        char name[8];
        snprintf(name, sizeof(name), "CAM %d", i + 1);
        lv_obj_t *name_lbl = make_label(tile, name, &lv_font_montserrat_22, C_TEXT);
        lv_obj_set_size(name_lbl, 90, 20);
        lv_obj_set_pos(name_lbl, 0, 0);

        // ── Connection dot (top-right of tile) ───────────────────
        lv_obj_t *dot = lv_obj_create(tile);
        lv_obj_set_size(dot, 10, 10);
        lv_obj_set_pos(dot, 116, 5);
        lv_obj_set_style_radius(dot, LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_bg_color(dot, lv_color_hex(C_DIM), 0);
        lv_obj_set_style_bg_opa(dot, LV_OPA_COVER, 0);
        lv_obj_set_style_border_width(dot, 0, 0);
        lv_obj_set_style_pad_all(dot, 0, 0);
        lv_obj_remove_flag(dot, LV_OBJ_FLAG_SCROLLABLE);
        _tile_dot[i] = dot;

        // ── Status / RSSI / State ─────────────────────────────────
        _tile_cstat[i] = make_label(tile, "OFFLINE", &lv_font_montserrat_12, C_DIM);
        lv_obj_set_pos(_tile_cstat[i], 0, 30);

        _tile_rssi[i] = make_label(tile, "", &lv_font_montserrat_10, C_DIM);
        lv_obj_set_pos(_tile_rssi[i], 0, 46);

        _tile_state[i] = make_label(tile, "", &lv_font_montserrat_12, C_DIM);
        lv_obj_set_size(_tile_state[i], LV_PCT(100), 14);
        lv_obj_set_pos(_tile_state[i], 0, 62);

        // ── Divider 1 ────────────────────────────────────────────
        make_hdivider(tile, 82);

        // ── Arc dials — SL left, PT right ────────────────────────
        // Two 60px arcs side by side; y=126 leaves 24px gap below divider.
        build_tile_dial(&_tile_sl_dial[i], tile,  4, 126, C_CAM_BG[i]);
        build_tile_dial(&_tile_pt_dial[i], tile, 68, 126, C_CAM_BG[i]);

        // ── Dial name labels (arc bottom = 126+60=166; labels at 200) ──
        lv_obj_t *sl_lbl = make_label(tile, "SLIDER", &lv_font_montserrat_10, C_DIM);
        lv_obj_set_size(sl_lbl, 60, 14);
        lv_obj_set_pos(sl_lbl, 4, 200);
        lv_obj_set_style_text_align(sl_lbl, LV_TEXT_ALIGN_CENTER, 0);

        lv_obj_t *pt_lbl = make_label(tile, "PAN / TILT", &lv_font_montserrat_10, C_DIM);
        lv_obj_set_size(pt_lbl, 60, 14);
        lv_obj_set_pos(pt_lbl, 68, 200);
        lv_obj_set_style_text_align(pt_lbl, LV_TEXT_ALIGN_CENTER, 0);

        // ── Divider 2 ────────────────────────────────────────────
        make_hdivider(tile, 240);

        // ── Positions label ──────────────────────────────────────
        lv_obj_t *pos_lbl = make_label(tile, "POSITIONS", &lv_font_montserrat_12, C_DIM);
        lv_obj_set_size(pos_lbl, LV_PCT(100), 14);
        lv_obj_set_pos(pos_lbl, 0, 275);
        lv_obj_set_style_text_align(pos_lbl, LV_TEXT_ALIGN_CENTER, 0);

        // ── Slot circles — 2 rows of 5, 22px dia, 26px step ─────
        // Row 0 at y=322, row 1 at y=350
        for (int s = 0; s < 10; s++) {
            int col_s = s % 5;
            int row_s = s / 5;
            lv_coord_t sx = 1 + col_s * 26;
            lv_coord_t sy = (row_s == 0) ? 312 : 350;
            lv_obj_t *circle = lv_obj_create(tile);
            lv_obj_set_size(circle, 22, 22);
            lv_obj_set_pos(circle, sx, sy);
            lv_obj_set_style_radius(circle, LV_RADIUS_CIRCLE, 0);
            lv_obj_set_style_bg_color(circle, lv_color_hex(C_SLOT_EMPTY), 0);
            lv_obj_set_style_bg_opa(circle, LV_OPA_COVER, 0);
            lv_obj_set_style_border_color(circle, lv_color_hex(C_BORDER), 0);
            lv_obj_set_style_border_width(circle, 1, 0);
            lv_obj_set_style_pad_all(circle, 0, 0);
            lv_obj_remove_flag(circle, (lv_obj_flag_t)(LV_OBJ_FLAG_SCROLLABLE |
                                                        LV_OBJ_FLAG_CLICKABLE));
            _tile_slot[i][s] = circle;
        }
    }

    // Force layout resolution before first render
    lv_obj_update_layout(_scr_status);
}

// ============================================================
//  Build CONFIG screen
// ============================================================

static lv_obj_t *make_speed_slider(lv_obj_t *parent, int group, int preset,
                                    const char *label_txt,
                                    int range_min, int range_max,
                                    const char *unit) {
    lv_obj_t *row = lv_obj_create(parent);
    lv_obj_set_size(row, LV_PCT(100), 32);
    lv_obj_set_style_bg_opa(row, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(row, 0, 0);
    lv_obj_set_style_pad_all(row, 0, 0);
    lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(row, LV_FLEX_ALIGN_START,
                          LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_START);
    lv_obj_set_style_pad_column(row, 8, 0);

    lv_obj_t *lbl = make_label(row, label_txt, &lv_font_montserrat_12, C_DIM);
    lv_obj_set_width(lbl, 60);

    lv_obj_t *slider = lv_slider_create(row);
    lv_obj_set_flex_grow(slider, 1);
    lv_obj_set_height(slider, 6);
    lv_slider_set_range(slider, range_min, range_max);
    lv_slider_set_value(slider, _cfg_speeds[group][preset], LV_ANIM_OFF);
    lv_obj_set_style_bg_color(slider, lv_color_hex(C_ACCENT),  LV_PART_INDICATOR);
    lv_obj_set_style_bg_color(slider, lv_color_hex(C_ACCENT2), LV_PART_KNOB);
    _cfg_sliders[group][preset] = slider;

    char val[16];
    snprintf(val, sizeof(val), "%ld %s", (long)_cfg_speeds[group][preset], unit);
    lv_obj_t *val_lbl = make_label(row, val, &lv_font_montserrat_12, C_TEXT);
    lv_obj_set_width(val_lbl, 60);

    // Pass the unit string as the event callback's own user_data — avoids
    // touching lv_obj user_data, which some LVGL widgets use internally.
    lv_obj_add_event_cb(slider, [](lv_event_t *ev) {
        lv_obj_t   *s = (lv_obj_t *)lv_event_get_target(ev);
        lv_obj_t   *r = lv_obj_get_parent(s);
        lv_obj_t   *v = lv_obj_get_child(r, 2);
        const char *u = (const char *)lv_event_get_user_data(ev);
        char buf[16];
        snprintf(buf, sizeof(buf), "%ld %s", (long)lv_slider_get_value(s), u ? u : "");
        lv_label_set_text(v, buf);
    }, LV_EVENT_VALUE_CHANGED, (void *)unit);

    return slider;
}

static void build_config_screen() {
    _scr_config = lv_obj_create(nullptr);
    lv_obj_set_size(_scr_config, LCD_WIDTH, LCD_HEIGHT);
    lv_obj_set_style_bg_color(_scr_config, lv_color_hex(C_BG), 0);
    lv_obj_set_style_pad_all(_scr_config, 0, 0);

    // ── Shared top bar (Config tab active) ──────────────────────
    build_top_bar(_scr_config, 2);

    lv_obj_t *cam_bar = lv_obj_create(_scr_config);
    lv_obj_set_pos(cam_bar, 8, 56);
    lv_obj_set_size(cam_bar, LCD_WIDTH - 16, 42);
    lv_obj_set_style_bg_opa(cam_bar, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(cam_bar, 0, 0);
    lv_obj_set_style_pad_all(cam_bar, 0, 0);
    lv_obj_set_style_pad_column(cam_bar, 8, 0);
    lv_obj_remove_flag(cam_bar, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_flex_flow(cam_bar, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(cam_bar, LV_FLEX_ALIGN_CENTER,
                          LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_START);

    for (int i = 0; i < 5; i++) {
        char lbl[8];
        snprintf(lbl, sizeof(lbl), "CAM %d", i + 1);
        lv_obj_t *btn = make_button(cam_bar, lbl,
                                    i == 0 ? C_ACCENT : C_SURF2,
                                    ev_cfg_cam_select, (void *)(uintptr_t)i);
        lv_obj_set_size(btn, 120, 36);
        _cfg_cam_btns[i] = btn;
    }

    int content_y = 106;
    int content_h = LCD_HEIGHT - content_y - 8;
    int col_w     = (LCD_WIDTH - 24) / 2;

    lv_obj_t *left = lv_obj_create(_scr_config);
    lv_obj_set_pos(left, 8, content_y);
    lv_obj_set_size(left, col_w, content_h);
    card_style(left, C_SURF);
    lv_obj_set_style_pad_all(left, 12, 0);
    lv_obj_set_flex_flow(left, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(left, LV_FLEX_ALIGN_START,
                          LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
    lv_obj_set_style_pad_row(left, 6, 0);

    // Group 0: Pan/Tilt in deg/s,  Group 1: Slider in mm/s
    const char *group_names[]  = { "Pan / Tilt Speeds (deg/s)", "Slider Speeds (mm/s)" };
    const char *preset_names[] = { "Speed 1:", "Speed 2:", "Speed 3:", "Speed 4:" };
    // Slider widget ranges per group (physical units)
    const int   range_min[2]   = { 1,   1   };
    const int   range_max[2]   = { 50,  100 };
    const char *units[2]       = { "deg/s", "mm/s" };

    for (int g = 0; g < 2; g++) {
        lv_obj_t *grp_lbl = make_label(left, group_names[g],
                                        &lv_font_montserrat_12, C_ACCENT2);
        if (g > 0) lv_obj_set_style_pad_top(grp_lbl, 10, 0);
        for (int p = 0; p < 4; p++)
            make_speed_slider(left, g, p, preset_names[p],
                              range_min[g], range_max[g], units[g]);
    }

    lv_obj_t *right = lv_obj_create(_scr_config);
    lv_obj_set_pos(right, 8 + col_w + 8, content_y);
    lv_obj_set_size(right, col_w, content_h);
    card_style(right, C_SURF);
    lv_obj_set_style_pad_all(right, 16, 0);
    lv_obj_set_flex_flow(right, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(right, LV_FLEX_ALIGN_START,
                          LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
    lv_obj_set_style_pad_row(right, 14, 0);

    // ── Find Home ───────────────────────────────────────────────────────────
    make_label(right, "Find Home", &lv_font_montserrat_12, C_ACCENT2);

    // Row of two buttons: [Slider] [Zoom]
    lv_obj_t *fl_row = lv_obj_create(right);
    lv_obj_set_size(fl_row, LV_PCT(100), 40);
    lv_obj_set_style_bg_opa(fl_row, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(fl_row, 0, 0);
    lv_obj_set_style_pad_all(fl_row, 0, 0);
    lv_obj_set_style_pad_column(fl_row, 8, 0);
    lv_obj_set_flex_flow(fl_row, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(fl_row, LV_FLEX_ALIGN_START,
                          LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_remove_flag(fl_row, LV_OBJ_FLAG_SCROLLABLE);

    _cfg_btn_findsl = make_button(fl_row, LV_SYMBOL_HOME " Slider",
                                  0x1565C0, ev_find_home,
                                  (void *)(uintptr_t)AXIS_SLIDER);
    lv_obj_set_flex_grow(_cfg_btn_findsl, 1);
    lv_obj_set_height(_cfg_btn_findsl, 40);
    lv_obj_set_style_text_color(lv_obj_get_child(_cfg_btn_findsl, 0),
                                 lv_color_hex(0xFFFFFF), 0);

    _cfg_btn_findzm = make_button(fl_row, LV_SYMBOL_HOME " Zoom",
                                  0x1565C0, ev_find_home,
                                  (void *)(uintptr_t)AXIS_ZOOM);
    lv_obj_set_flex_grow(_cfg_btn_findzm, 1);
    lv_obj_set_height(_cfg_btn_findzm, 40);
    lv_obj_set_style_text_color(lv_obj_get_child(_cfg_btn_findzm, 0),
                                 lv_color_hex(0xFFFFFF), 0);

    _cfg_find_lbl = make_label(right, "", &lv_font_montserrat_12, C_DIM);

    // ── Divider ─────────────────────────────────────────────────────────────
    lv_obj_t *div = lv_obj_create(right);
    lv_obj_set_size(div, LV_PCT(100), 1);
    lv_obj_set_style_bg_color(div, lv_color_hex(C_BORDER), 0);
    lv_obj_set_style_bg_opa(div, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(div, 0, 0);
    lv_obj_set_style_radius(div, 0, 0);

    // ── Set Reference ────────────────────────────────────────────────────────
    make_label(right, "Set Reference", &lv_font_montserrat_12, C_ACCENT2);

    // Single button: sets pan=0, tilt=0 at current position.
    _cfg_btn_manualref = make_button(right, "Set Ref 0/0",
                                     C_ACCENT, ev_manual_ref, nullptr);
    lv_obj_set_width(_cfg_btn_manualref, LV_PCT(100));
    lv_obj_set_height(_cfg_btn_manualref, 40);
    lv_obj_set_style_text_color(lv_obj_get_child(_cfg_btn_manualref, 0),
                                lv_color_hex(0xFFFFFF), 0);

    _cfg_ref_lbl = make_label(right, "", &lv_font_montserrat_12, C_DIM);

    // ── Divider ─────────────────────────────────────────────────────────────
    lv_obj_t *div1b = lv_obj_create(right);
    lv_obj_set_size(div1b, LV_PCT(100), 1);
    lv_obj_set_style_bg_color(div1b, lv_color_hex(C_BORDER), 0);
    lv_obj_set_style_bg_opa(div1b, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(div1b, 0, 0);
    lv_obj_set_style_radius(div1b, 0, 0);

    // ── Mount Options ────────────────────────────────────────────────────────
    make_label(right, "Mount Options", &lv_font_montserrat_12, C_ACCENT2);

    auto make_toggle_row = [&](lv_obj_t *parent, const char *txt) -> lv_obj_t * {
        lv_obj_t *row = lv_obj_create(parent);
        lv_obj_set_size(row, LV_PCT(100), 36);
        lv_obj_set_style_bg_opa(row, LV_OPA_TRANSP, 0);
        lv_obj_set_style_border_width(row, 0, 0);
        lv_obj_set_style_pad_all(row, 0, 0);
        lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
        lv_obj_set_flex_align(row, LV_FLEX_ALIGN_SPACE_BETWEEN,
                              LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_START);
        make_label(row, txt, &lv_font_montserrat_12, C_TEXT);
        lv_obj_t *sw = lv_switch_create(row);
        lv_obj_set_style_bg_color(sw, lv_color_hex(C_ACCENT), LV_STATE_CHECKED);
        return sw;
    };

    _cfg_sw_paninv = make_toggle_row(right, "Pan Invert");
    _cfg_sw_slinv  = make_toggle_row(right, "Slider Invert");
    _cfg_sw_zminv  = make_toggle_row(right, "Zoom Invert");
    _cfg_sw_lanczm = make_toggle_row(right, "LANC Zoom");
    _cfg_sw_hassl  = make_toggle_row(right, "Has Slider");
    lv_obj_add_state(_cfg_sw_hassl, LV_STATE_CHECKED);
    _cfg_sw_lookAt = make_toggle_row(right, "Look-at Mode");

    // Rail inclination, reported by the mount.  Read-only: it describes how the
    // rig is physically rigged, is set once in the PC app, and is shown here so
    // it can be checked at the rig without the PC app to hand.  Note that this
    // screen's Apply sends the flags byte alone, which the mount treats as
    // "tilt unchanged" — so viewing it here can never overwrite it.
    {
        lv_obj_t *row = lv_obj_create(right);
        lv_obj_remove_style_all(row);
        lv_obj_set_size(row, LV_PCT(100), LV_SIZE_CONTENT);
        lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
        lv_obj_set_flex_align(row, LV_FLEX_ALIGN_SPACE_BETWEEN,
                              LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_START);
        make_label(row, "Slider Tilt", &lv_font_montserrat_12, C_TEXT);
        _cfg_lbl_tilt = make_label(row, "--", &lv_font_montserrat_12, C_TEXT);
    }

    lv_obj_t *div2 = lv_obj_create(right);
    lv_obj_set_size(div2, LV_PCT(100), 1);
    lv_obj_set_style_bg_color(div2, lv_color_hex(C_BORDER), 0);
    lv_obj_set_style_bg_opa(div2, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(div2, 0, 0);
    lv_obj_set_style_radius(div2, 0, 0);

    lv_obj_t *send_btn = make_button(right, LV_SYMBOL_UPLOAD "  Apply to Mount",
                                      C_GREEN, ev_send_config);
    lv_obj_set_width(send_btn, LV_PCT(100));
    lv_obj_set_height(send_btn, 44);
    lv_obj_set_style_text_color(lv_obj_get_child(send_btn, 0),
                                 lv_color_hex(0xFFFFFF), 0);

    // Set initial button states based on cached orientation flags
    _cfg_update_find_btns();
}

// ============================================================
//  Build DETAIL screen  (800 × 480, opened by tapping a camera tile)
// ============================================================
//
//  y=  0  Header: [← Back]  CAM N                               h=50
//  y= 58  10 position buttons  (w=70, gap=8, left_margin=14)    h=70
//  y=180  [SET]  [CLEAR]  centred                               h=40
//  y=248  Right joy label "PAN / TILT" (x=590)                  h=14
//  y=260  Speed dials: PT (x=270) SL (x=410)                   h=120
//  y=266  "ZOOM" label (x=10)  Right joy (x=590, PAN/TILT)     h=14/180
//  y=284  ZOOM  hslider (x=10, w=230)                           h=64
//  y=364  "SLIDER" label (x=10)                                  h=14
//  y=382  SLIDER hslider (x=10, w=230)                          h=64  → y=446
//  y=388  Dial labels (below speed dials, x=270/410)             h=14

static void build_joystick_widget(lv_obj_t *parent, int x, int y,
                                   Joystick *joy,
                                   lv_event_cb_t press_cb,
                                   lv_event_cb_t rel_cb) {
    lv_obj_t *bg = lv_obj_create(parent);
    lv_obj_set_size(bg, JOY_SIZE, JOY_SIZE);
    lv_obj_set_pos(bg, x, y);
    lv_obj_set_style_radius(bg, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(bg, lv_color_hex(C_SURF2), 0);
    lv_obj_set_style_bg_opa(bg, LV_OPA_COVER, 0);
    lv_obj_set_style_border_color(bg, lv_color_hex(C_BORDER), 0);
    lv_obj_set_style_border_width(bg, 2, 0);
    lv_obj_set_style_pad_all(bg, 0, 0);
    lv_obj_remove_flag(bg, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(bg, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(bg, press_cb, LV_EVENT_PRESSING, nullptr);
    lv_obj_add_event_cb(bg, rel_cb,   LV_EVENT_RELEASED, nullptr);

    const int tc = JOY_SIZE / 2 - JOY_THUMB_SIZE / 2;
    lv_obj_t *thumb = lv_obj_create(bg);
    lv_obj_set_size(thumb, JOY_THUMB_SIZE, JOY_THUMB_SIZE);
    lv_obj_set_pos(thumb, tc, tc);
    lv_obj_set_style_radius(thumb, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(thumb, lv_color_hex(C_ACCENT2), 0);
    lv_obj_set_style_bg_opa(thumb, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(thumb, 0, 0);
    lv_obj_set_style_pad_all(thumb, 0, 0);
    lv_obj_remove_flag(thumb, (lv_obj_flag_t)(LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE));
    joy->bg    = bg;
    joy->thumb = thumb;
}

static void build_hslider(lv_obj_t *parent, int x, int y,
                           HSlider *hsl,
                           lv_event_cb_t press_cb, lv_event_cb_t rel_cb) {
    lv_obj_t *bg = lv_obj_create(parent);
    lv_obj_set_size(bg, HSL_W, HSL_H);
    lv_obj_set_pos(bg, x, y);
    lv_obj_set_style_radius(bg, 10, 0);
    lv_obj_set_style_bg_color(bg, lv_color_hex(C_SURF2), 0);
    lv_obj_set_style_bg_opa(bg, LV_OPA_COVER, 0);
    lv_obj_set_style_border_color(bg, lv_color_hex(C_BORDER), 0);
    lv_obj_set_style_border_width(bg, 2, 0);
    lv_obj_set_style_pad_all(bg, 0, 0);
    lv_obj_remove_flag(bg, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(bg, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(bg, press_cb, LV_EVENT_PRESSING, nullptr);
    lv_obj_add_event_cb(bg, rel_cb,   LV_EVENT_RELEASED, nullptr);

    // Centre-line marker — visual reference for the neutral position
    lv_obj_t *ctr = lv_obj_create(bg);
    lv_obj_set_size(ctr, 2, HSL_H - 16);
    lv_obj_set_pos(ctr, HSL_W / 2 - 1, 8);
    lv_obj_set_style_bg_color(ctr, lv_color_hex(C_BORDER), 0);
    lv_obj_set_style_bg_opa(ctr, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(ctr, 0, 0);
    lv_obj_set_style_radius(ctr, 1, 0);
    lv_obj_set_style_pad_all(ctr, 0, 0);
    lv_obj_remove_flag(ctr, (lv_obj_flag_t)(LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE));

    lv_obj_t *thumb = lv_obj_create(bg);
    lv_obj_set_size(thumb, HSL_THUMB_W, HSL_THUMB_H);
    lv_obj_set_pos(thumb, (HSL_W - HSL_THUMB_W) / 2, (HSL_H - HSL_THUMB_H) / 2);
    lv_obj_set_style_radius(thumb, 8, 0);
    lv_obj_set_style_bg_color(thumb, lv_color_hex(C_ACCENT2), 0);
    lv_obj_set_style_bg_opa(thumb, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(thumb, 0, 0);
    lv_obj_set_style_pad_all(thumb, 0, 0);
    lv_obj_remove_flag(thumb, (lv_obj_flag_t)(LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE));
    hsl->bg    = bg;
    hsl->thumb = thumb;
}

static void build_detail_screen() {
    _scr_detail = lv_obj_create(nullptr);
    lv_obj_set_size(_scr_detail, LCD_WIDTH, LCD_HEIGHT);
    lv_obj_set_style_bg_color(_scr_detail, lv_color_hex(C_BG), 0);
    lv_obj_set_style_pad_all(_scr_detail, 0, 0);

    // ── Header ──────────────────────────────────────────────────
    _det_hdr = lv_obj_create(_scr_detail);
    lv_obj_t *hdr = _det_hdr;
    lv_obj_set_size(hdr, LCD_WIDTH, 50);
    lv_obj_set_pos(hdr, 0, 0);
    lv_obj_set_style_bg_color(hdr, lv_color_hex(C_SURF), 0);
    lv_obj_set_style_bg_opa(hdr, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(hdr, 0, 0);
    lv_obj_set_style_radius(hdr, 0, 0);
    lv_obj_set_style_pad_left(hdr,  12, 0);
    lv_obj_set_style_pad_right(hdr, 12, 0);
    lv_obj_set_style_pad_top(hdr,    0, 0);
    lv_obj_set_style_pad_bottom(hdr, 0, 0);
    lv_obj_remove_flag(hdr, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *back_btn = make_button(hdr, LV_SYMBOL_LEFT "  Back", C_SURF2, ev_detail_back);
    lv_obj_set_size(back_btn, 100, 34);
    lv_obj_align(back_btn, LV_ALIGN_LEFT_MID, 0, 0);

    // Camera selector — 5 buttons centred in the header.
    // Each "CAM X" button: 84px wide, 6px gap → 5×84 + 4×6 = 444px container.
    lv_obj_t *cam_tabs = lv_obj_create(hdr);
    lv_obj_set_size(cam_tabs, 444, 38);
    lv_obj_align(cam_tabs, LV_ALIGN_CENTER, 0, 0);
    lv_obj_set_style_bg_opa(cam_tabs, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(cam_tabs, 0, 0);
    lv_obj_set_style_pad_all(cam_tabs, 0, 0);
    lv_obj_set_style_pad_column(cam_tabs, 6, 0);
    lv_obj_set_flex_flow(cam_tabs, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(cam_tabs, LV_FLEX_ALIGN_CENTER,
                          LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_remove_flag(cam_tabs, LV_OBJ_FLAG_SCROLLABLE);
    for (int i = 0; i < 5; i++) {
        char label[8];
        snprintf(label, sizeof(label), "CAM %d", i + 1);
        lv_obj_t *btn = make_button(cam_tabs, label, C_SURF2,
                                    ev_det_cam_select, (void *)(uintptr_t)i);
        lv_obj_set_size(btn, 84, 34);
        _det_cam_btns[i] = btn;
    }

    // E-STOP — top-right (matches build_top_bar)
    lv_obj_t *estop_btn = make_button(hdr, LV_SYMBOL_STOP "  E-STOP", C_RED, ev_estop);
    lv_obj_set_size(estop_btn, 150, 34);
    lv_obj_align(estop_btn, LV_ALIGN_RIGHT_MID, 0, 0);
    lv_obj_set_style_text_color(lv_obj_get_child(estop_btn, 0), lv_color_hex(0xFFFFFF), 0);

    // ── 10 position buttons ──────────────────────────────────────
    // 10 × 70px + 9 × 8px gap = 772px, left_margin = 14px
    for (int s = 0; s < 10; s++) {
        lv_obj_t *btn = lv_button_create(_scr_detail);
        lv_obj_set_size(btn, 70, 70);
        lv_obj_set_pos(btn, 14 + s * 78, 58);
        lv_obj_set_style_bg_color(btn, lv_color_hex(C_SURF2), 0);
        lv_obj_set_style_bg_color(btn, lv_color_hex(C_SURF2), LV_STATE_PRESSED);
        lv_obj_set_style_border_color(btn, lv_color_hex(C_BTN_BORDER_EMPTY), 0);
        lv_obj_set_style_border_width(btn, C_BTN_BORDER_W, 0);
        lv_obj_set_style_radius(btn, 6, 0);
        lv_obj_add_event_cb(btn, ev_detail_pos_btn, LV_EVENT_CLICKED, (void *)(uintptr_t)s);
        if (s == 8 || s == 9) {
            lv_obj_add_event_cb(btn, ev_det_arrow_pressed,  LV_EVENT_PRESSED,   (void *)(uintptr_t)s);
            lv_obj_add_event_cb(btn, ev_det_arrow_pressing, LV_EVENT_PRESSING,  (void *)(uintptr_t)s);
            lv_obj_add_event_cb(btn, ev_det_arrow_released, LV_EVENT_RELEASED,  (void *)(uintptr_t)s);
        }
        char nb[4];
        snprintf(nb, sizeof(nb), "%d", s + 1);
        lv_obj_t *lbl = lv_label_create(btn);
        lv_label_set_text(lbl, nb);
        lv_obj_set_style_text_font(lbl,  &lv_font_montserrat_16, 0);
        lv_obj_set_style_text_color(lbl, lv_color_hex(C_TEXT),   0);
        lv_obj_center(lbl);
        _det_pos_btn[s] = btn;
    }

    // ── CLEAR + SET ───────────────────────────────────────────────
    // centred: (800 - (110+16+110)) / 2 = 282
    // CLEAR sits left of SET, matching the web app, GC screen and PC app.
    _det_clear_btn = make_button(_scr_detail, "CLEAR", C_SURF2, ev_detail_clear);
    lv_obj_set_size(_det_clear_btn, 110, 40);
    lv_obj_set_pos(_det_clear_btn, 282, 180);
    _det_clear_lbl = lv_obj_get_child(_det_clear_btn, 0);

    _det_set_btn = make_button(_scr_detail, "SET", C_ACCENT, ev_detail_set);
    lv_obj_set_size(_det_set_btn, 110, 40);
    lv_obj_set_pos(_det_set_btn, 408, 180);
    _det_set_lbl = lv_obj_get_child(_det_set_btn, 0);

    // ── Speed dials ───────────────────────────────────────────────
    // Two 120×120 arcs centred between the joysticks (x=190-590 free).
    // Total width = 120+20+120 = 260 px → x = (800-260)/2 = 270 (SL), 410 (PT).
    build_detail_dial(&_det_sl_dial, _scr_detail, 270, 260);
    lv_obj_add_flag(_det_sl_dial.arc, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(_det_sl_dial.arc, ev_det_sl_click, LV_EVENT_CLICKED, nullptr);

    build_detail_dial(&_det_pt_dial, _scr_detail, 410, 260);
    lv_obj_add_flag(_det_pt_dial.arc, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(_det_pt_dial.arc, ev_det_pt_click, LV_EVENT_CLICKED, nullptr);

    lv_obj_t *sl_dlbl = make_label(_scr_detail, "SLIDER", &lv_font_montserrat_12, C_DIM);
    lv_obj_set_size(sl_dlbl, 120, 14);
    lv_obj_set_pos(sl_dlbl, 270, 388);
    lv_obj_set_style_text_align(sl_dlbl, LV_TEXT_ALIGN_CENTER, 0);

    lv_obj_t *pt_dlbl = make_label(_scr_detail, "PAN / TILT", &lv_font_montserrat_12, C_DIM);
    lv_obj_set_size(pt_dlbl, 120, 14);
    lv_obj_set_pos(pt_dlbl, 410, 388);
    lv_obj_set_style_text_align(pt_dlbl, LV_TEXT_ALIGN_CENTER, 0);

    // ── Right joystick label + widget (PAN / TILT) ───────────────
    lv_obj_t *jlbl_pt = make_label(_scr_detail, "PAN / TILT", &lv_font_montserrat_12, C_DIM);
    lv_obj_set_size(jlbl_pt, JOY_SIZE, 14);
    lv_obj_set_pos(jlbl_pt, 590, 248);
    lv_obj_set_style_text_align(jlbl_pt, LV_TEXT_ALIGN_CENTER, 0);

    // JOY_SIZE=180 → joy ends at y=266+180=446, within 480px screen ✓
    build_joystick_widget(_scr_detail, 590, 266, &_joy_pt,
                          ev_joy_pt_pressing, ev_joy_pt_release);

    // ── Left horizontal sliders (ZOOM top, SLIDER bottom) ────────
    // Each 230×64 px, x=10; bottom of last slider = 382+64 = 446 ✓
    lv_obj_t *zlbl = make_label(_scr_detail, "ZOOM", &lv_font_montserrat_12, C_DIM);
    lv_obj_set_size(zlbl, HSL_W, 14);
    lv_obj_set_pos(zlbl, 10, 266);
    lv_obj_set_style_text_align(zlbl, LV_TEXT_ALIGN_CENTER, 0);

    build_hslider(_scr_detail, 10, 284, &_hsl_zoom,
                  ev_hsl_zoom_pressing, ev_hsl_zoom_release);

    lv_obj_t *sllbl = make_label(_scr_detail, "SLIDER", &lv_font_montserrat_12, C_DIM);
    lv_obj_set_size(sllbl, HSL_W, 14);
    lv_obj_set_pos(sllbl, 10, 364);
    lv_obj_set_style_text_align(sllbl, LV_TEXT_ALIGN_CENTER, 0);

    build_hslider(_scr_detail, 10, 382, &_hsl_sl,
                  ev_hsl_sl_pressing, ev_hsl_sl_release);

    // Do NOT call lv_obj_update_layout here — this function can be called from inside
    // ev_tab_home (inside lv_timer_handler on the LVGL task). Calling it from that
    // nested context adds enough stack depth to risk overflow. LVGL marks all new
    // objects dirty automatically; the layout pass runs on the next tick (~5 ms later).
}

// ============================================================
//  Hardware init
// ============================================================

static void init_hardware() {
    Wire.begin(PIN_SDA, PIN_SCL);
    Wire.setTimeOut(20);   // 20 ms I²C timeout — prevents GT911 read hanging lv_timer_handler

    esp_lcd_rgb_panel_config_t panel_cfg = {};
    panel_cfg.clk_src                       = LCD_CLK_SRC_DEFAULT;
    panel_cfg.timings.pclk_hz               = LCD_PCLK_HZ;
    panel_cfg.timings.h_res                 = LCD_WIDTH;
    panel_cfg.timings.v_res                 = LCD_HEIGHT;
    panel_cfg.timings.hsync_back_porch      = LCD_HBPORCH;
    panel_cfg.timings.hsync_front_porch     = LCD_HFPORCH;
    panel_cfg.timings.hsync_pulse_width     = LCD_HPULSE;
    panel_cfg.timings.vsync_back_porch      = LCD_VBPORCH;
    panel_cfg.timings.vsync_front_porch     = LCD_VFPORCH;
    panel_cfg.timings.vsync_pulse_width     = LCD_VPULSE;
    panel_cfg.data_width                    = 16;
    panel_cfg.bits_per_pixel                = 16;
    panel_cfg.hsync_gpio_num                = PIN_HSYNC;
    panel_cfg.vsync_gpio_num                = PIN_VSYNC;
    panel_cfg.de_gpio_num                   = PIN_DE;
    panel_cfg.pclk_gpio_num                 = PIN_PCLK;
    panel_cfg.disp_gpio_num                 = GPIO_NUM_NC;
    panel_cfg.data_gpio_nums[0]  = PIN_B0;
    panel_cfg.data_gpio_nums[1]  = PIN_B1;
    panel_cfg.data_gpio_nums[2]  = PIN_B2;
    panel_cfg.data_gpio_nums[3]  = PIN_B3;
    panel_cfg.data_gpio_nums[4]  = PIN_B4;
    panel_cfg.data_gpio_nums[5]  = PIN_G0;
    panel_cfg.data_gpio_nums[6]  = PIN_G1;
    panel_cfg.data_gpio_nums[7]  = PIN_G2;
    panel_cfg.data_gpio_nums[8]  = PIN_G3;
    panel_cfg.data_gpio_nums[9]  = PIN_G4;
    panel_cfg.data_gpio_nums[10] = PIN_G5;
    panel_cfg.data_gpio_nums[11] = PIN_R0;
    panel_cfg.data_gpio_nums[12] = PIN_R1;
    panel_cfg.data_gpio_nums[13] = PIN_R2;
    panel_cfg.data_gpio_nums[14] = PIN_R3;
    panel_cfg.data_gpio_nums[15] = PIN_R4;
    panel_cfg.num_fbs            = 2;
    panel_cfg.flags.fb_in_psram  = 1;
    panel_cfg.timings.flags.pclk_active_neg = 1;
    panel_cfg.bounce_buffer_size_px = LCD_WIDTH * 60;

    ESP_ERROR_CHECK(esp_lcd_new_rgb_panel(&panel_cfg, &_panel_handle));

    Wire.beginTransmission(0x38); Wire.write(0xFF & ~(1 << 3)); Wire.endTransmission();
    delay(10);
    Wire.beginTransmission(0x38); Wire.write(0xFF); Wire.endTransmission();
    delay(120);

    ESP_ERROR_CHECK(esp_lcd_panel_init(_panel_handle));
    ESP_ERROR_CHECK(esp_lcd_rgb_panel_get_frame_buffer(_panel_handle, 2, &_fb0, &_fb1));

    _vsync_sem = xSemaphoreCreateBinary();
    esp_lcd_rgb_panel_event_callbacks_t cbs = {};
    cbs.on_vsync = vsync_cb;
    ESP_ERROR_CHECK(esp_lcd_rgb_panel_register_event_callbacks(_panel_handle, &cbs, nullptr));

    // ---- GT911 touch init (cold-boot safe) --------------------------------
    //
    // The GT911 latches its I2C address from the INT pin during hardware
    // power-on reset:  INT low → 0x5D,  INT high → 0x14.
    // That latch happens before ESP32 firmware runs, so on a cold start the
    // chip is usually at 0x14 (INT floats high via board pull-up).
    // A quick power cycle works because capacitors keep VCC high enough that
    // the chip never fully resets and its address is retained from last boot.
    //
    // The GT911 soft-reset command (reg 0x8040 = 0x02) resets touch processing
    // state only — it does NOT re-latch the I2C address.  Without the RST pin
    // wired there is no way to force a re-latch from firmware.
    //
    // Solution: drive INT low early (so any future hardware reset latches 0x5D),
    // then poll both addresses and initialise the driver at whichever one
    // responds.  Touch works correctly at either address.

#define GT911_ADDR_PRI  0x5D
#define GT911_ADDR_ALT  0x14

    // Drive INT low now — if anything later causes a hardware reset the chip
    // will come back at 0x5D.
    pinMode(PIN_TOUCH_INT, OUTPUT);
    digitalWrite(PIN_TOUCH_INT, LOW);

    // Poll both addresses for up to 2 s (chip may still be starting up).
    uint8_t gt_addr = 0;
    for (int i = 0; i < 40 && !gt_addr; i++) {
        Wire.beginTransmission(GT911_ADDR_PRI);
        if (Wire.endTransmission() == 0) { gt_addr = GT911_ADDR_PRI; break; }
        Wire.beginTransmission(GT911_ADDR_ALT);
        if (Wire.endTransmission() == 0) { gt_addr = GT911_ADDR_ALT; break; }
        delay(50);
    }

    if (!gt_addr) {
        Serial.println("WARNING: GT911 not found at either I2C address");
        gt_addr = GT911_ADDR_PRI;   // proceed anyway; begin() will fail gracefully
    } else if (gt_addr == GT911_ADDR_ALT) {
        Serial.printf("GT911 at 0x%02X (cold boot — INT was high during reset)\n", gt_addr);
    }

    pinMode(PIN_TOUCH_INT, INPUT);
    delay(20);

    _touch.begin(gt_addr);          // use whichever address the chip is actually at
    _touch.setRotation(ROTATION_INVERTED);
}

// ============================================================
//  LVGL init  (v9 API)
// ============================================================

static void init_lvgl() {
    lv_init();

    // The Arduino-cached LVGL library was compiled with a ~64 KB static TLSF pool.
    // Status + detail screens together need ~51 KB, leaving < 8 KB for runtime
    // event/style allocations — too little for LVGL to process touch events.
    // Extend the TLSF heap with 32 KB from the Arduino DRAM heap (which has
    // 150+ KB free at boot; the two PSRAM framebuffers don't consume DRAM).
    void *extra_heap = malloc(32 * 1024);
    if (extra_heap) lv_mem_add_pool(extra_heap, 32 * 1024);

    // Even that is no longer enough: the v2 UI (Mounts panel, conflict prompt,
    // and the Positions screen's 50-button grid) exhausts the pool during
    // build_positions_screen() — lv_malloc() starts returning NULL, which
    // glitches renders (starved draw-layer allocs) and crashed or hung the
    // build (StoreProhibited via get_local_style(), or a silent spin).
    //
    // Beware the build cache: libraries/lv_conf.h sets LV_MEM_SIZE to 192 KB,
    // but a stale .build/ (or Arduino IDE cache) compiled before that file
    // existed bakes in LVGL's 64 KB default — and a TLSF built for a ≤64 KB
    // pool silently REJECTS any added pool larger than 64 KB, so a single
    // big lv_mem_add_pool() can be a no-op.  Hence the overflow is added as
    // several 32 KB pools — a size that registers under either TLSF build —
    // parked in PSRAM: 8 MB fitted, only ~1.5 MB used by the two frame
    // buffers, and the S3's cache makes PSRAM fine for object/style structs.
    // TLSF fills earlier pools first, so these are overflow-only.  After
    // changing lv_conf.h, rebuild clean (rm -rf .build) so it actually takes.
    int pools_ok = 0;
    for (int p = 0; p < 8; p++) {
        void *pool = heap_caps_malloc(32 * 1024, MALLOC_CAP_SPIRAM);
        if (!pool) break;
        if (!lv_mem_add_pool(pool, 32 * 1024)) { heap_caps_free(pool); break; }
        pools_ok++;
    }
    Serial.printf("[DISP] LVGL: +%d x 32 KB PSRAM overflow pools\n", pools_ok);
    if (pools_ok == 0) Serial.println("[DISP] WARNING: no LVGL PSRAM pools added");

    // Tick source
    const esp_timer_create_args_t tick_args = {
        .callback = [](void *) { lv_tick_inc(5); },
        .name     = "lvgl_tick"
    };
    esp_timer_handle_t tick_timer;
    esp_timer_create(&tick_args, &tick_timer);
    esp_timer_start_periodic(tick_timer, 5000);

    // Display — direct mode with both PSRAM framebuffers
    lv_display_t *disp = lv_display_create(LCD_WIDTH, LCD_HEIGHT);
    lv_display_set_flush_cb(disp, flush_cb);
    lv_display_set_buffers(disp, _fb0, _fb1,
                           LCD_WIDTH * LCD_HEIGHT * sizeof(uint16_t),
                           LV_DISPLAY_RENDER_MODE_DIRECT);

    // Dark theme
    lv_theme_t *theme = lv_theme_default_init(disp,
                            lv_color_hex(C_ACCENT), lv_color_hex(C_GREEN),
                            true, &lv_font_montserrat_12);
    lv_display_set_theme(disp, theme);

    // Two pointer indevs — one per GT911 touch point.
    // LVGL calls them in registration order, so cb_0 always runs first and
    // does the I2C read; cb_1 reuses the cached result.  Each indev
    // independently tracks which widget it has captured, giving true
    // simultaneous two-finger operation (e.g. joystick + slider at once).
    lv_indev_t *indev0 = lv_indev_create();
    lv_indev_set_type(indev0, LV_INDEV_TYPE_POINTER);
    lv_indev_set_read_cb(indev0, touch_read_cb_0);
    lv_indev_set_display(indev0, disp);

    lv_indev_t *indev1 = lv_indev_create();
    lv_indev_set_type(indev1, LV_INDEV_TYPE_POINTER);
    lv_indev_set_read_cb(indev1, touch_read_cb_1);
    lv_indev_set_display(indev1, disp);

    // Render at 30 fps (33 ms) — matches the indev read rate so every touch position
    // update is visible within one frame.  Only the dirty rectangle (e.g. the
    // joystick thumb, ~30×30 px) is redrawn per frame, so CPU/bus load is low.
    // Previously 300 ms; reduced now that direct-mode dirty-rect keeps per-frame
    // work proportional to changed area rather than full-screen.
    lv_timer_t *render_timer = lv_display_get_refr_timer(disp);
    if (render_timer) lv_timer_set_period(render_timer, 80);
}

// ============================================================
//  Look-at arrow flash timer (500 ms, driven by lv_timer_handler)
// ============================================================

// True while this camera is travelling to a stored position (not look-at).
static inline bool cam_moving_to_pos(int i) {
    return _slots[i].state == STATE_MOVING_TO_POS && _slots[i].target_slot != 0xFF;
}

static void la_flash_timer_cb(lv_timer_t *) {
    // Act if any look-at arrow is moving OR any mount is travelling to a stored
    // position — the target slot flashes in both cases, matching the PC and web
    // apps.  (Position targets were previously left out, so they never flashed.)
    bool any_moving = false;
    for (int i = 0; i < 5; i++) {
        if ((cam_is_look_at(i) &&
                (_la_arrow_state[i] == 0 || _la_arrow_state[i] == 1))
            || cam_moving_to_pos(i)) {
            any_moving = true;
            break;
        }
    }
    if (!any_moving) return;

    _la_flash_on = !_la_flash_on;

    lv_obj_t *active_scr = lv_scr_act();

    if (active_scr == _scr_positions && _scr_positions) {
        // Positions screen — flash the border on moving arrow buttons.
        uint32_t bc = _la_flash_on ? C_BTN_BORDER_TGT : C_BORDER;
        for (int i = 0; i < 5; i++) {
            // Normal recall: flash the slot being travelled to.
            if (!cam_is_look_at(i) && cam_moving_to_pos(i)) {
                lv_obj_t *btn = _pos_slot_btn[i][_slots[i].target_slot];
                if (btn) lv_obj_set_style_border_color(btn, lv_color_hex(bc), 0);
                continue;
            }
            if (!cam_is_look_at(i)) continue;
            int8_t arr = _la_arrow_state[i];
            for (int s = 8; s <= 9; s++) {
                lv_obj_t *btn = _pos_slot_btn[i][s];
                if (!btn) continue;
                if ((arr == 0 && s == 8) || (arr == 1 && s == 9))
                    lv_obj_set_style_border_color(btn, lv_color_hex(bc), 0);
            }
        }
    } else if (active_scr == _scr_detail && _scr_detail) {
        // Detail screen — flash the border on the moving arrow button.
        int i = _detail_cam;
        uint32_t bc = _la_flash_on ? C_BTN_BORDER_TGT : C_BORDER;
        if (!cam_is_look_at(i)) {
            if (cam_moving_to_pos(i)) {          // normal recall in progress
                lv_obj_t *btn = _det_pos_btn[_slots[i].target_slot];
                if (btn) lv_obj_set_style_border_color(btn, lv_color_hex(bc), 0);
            }
            return;
        }
        int8_t arr = _la_arrow_state[i];
        for (int s = 8; s <= 9; s++) {
            if (!_det_pos_btn[s]) continue;
            if ((arr == 0 && s == 8) || (arr == 1 && s == 9))
                lv_obj_set_style_border_color(_det_pos_btn[s], lv_color_hex(bc), 0);
        }
    } else {
        // Home/status screen — flash the tile dot bg colour.
        uint32_t dot_col = _la_flash_on ? C_SLOT_TGT : C_SLOT_EMPTY;
        for (int i = 0; i < 5; i++) {
            if (!cam_is_look_at(i)) {
                if (cam_moving_to_pos(i)) {      // normal recall in progress
                    lv_obj_t *dot = _tile_slot[i][_slots[i].target_slot];
                    if (dot) lv_obj_set_style_bg_color(dot, lv_color_hex(dot_col), 0);
                }
                continue;
            }
            int8_t arr = _la_arrow_state[i];
            for (int s = 8; s <= 9; s++) {
                if (!_tile_slot[i][s]) continue;
                if ((arr == 0 && s == 8) || (arr == 1 && s == 9))
                    lv_obj_set_style_bg_color(_tile_slot[i][s], lv_color_hex(dot_col), 0);
            }
        }
    }
}

// ============================================================
//  LVGL FreeRTOS task
// ============================================================

// ============================================================
//  Pairing (stage 3) — Mounts panel + conflict prompt overlays
// ============================================================
// Both live on lv_layer_top() so they float above whichever screen is active.
// Built once at init; event callbacks only set _mp_act_* flags, and all LVGL
// mutation + UART sends happen in the lvgl task tick.

static lv_obj_t *_mp_panel        = nullptr;   // Mounts list panel
static lv_obj_t *_mp_mac_lbl[5]   = {};
static lv_obj_t *_mp_st_lbl[5]    = {};
static lv_obj_t *_mp_forget_btn[5]= {};
static lv_obj_t *_pc_panel        = nullptr;   // conflict prompt
static lv_obj_t *_pc_l1           = nullptr;
static lv_obj_t *_pc_l2           = nullptr;

static void ev_mounts_open (lv_event_t *)  { _mp_act_open  = true; }
static void ev_mounts_close(lv_event_t *)  { _mp_act_close = true; }
static void ev_mounts_forget(lv_event_t *e) {
    _mp_act_forget = (uint8_t)(uintptr_t)lv_event_get_user_data(e);
}
static void ev_pc_replace(lv_event_t *)     { _mp_act_decide = 1; }
static void ev_pc_ignore (lv_event_t *)     { _mp_act_decide = 0; }

static void fmt_mac(char *out, size_t n, const uint8_t *m) {
    snprintf(out, n, "%02X:%02X:%02X:%02X:%02X:%02X",
             m[0], m[1], m[2], m[3], m[4], m[5]);
}

static bool table_mac_valid(int i) {
    const uint8_t *m = _mount_table[i];
    return (m[0] | m[1] | m[2] | m[3] | m[4] | m[5]) != 0;
}

// LVGL-task context only.
static void mounts_panel_refresh() {
    if (!_mp_panel) return;
    for (int i = 0; i < 5; i++) {
        char buf[24];
        bool bound = table_mac_valid(i);
        if (bound) fmt_mac(buf, sizeof(buf), _mount_table[i]);
        else       snprintf(buf, sizeof(buf), "-  unpaired  -");
        lv_label_set_text(_mp_mac_lbl[i], buf);
        lv_obj_set_style_text_color(_mp_mac_lbl[i],
            lv_color_hex(bound ? C_TEXT : C_DIM), 0);

        if (_cam[i].connected)
            snprintf(buf, sizeof(buf), "ONLINE  %d dBm", (int)_cam[i].rssi);
        else
            snprintf(buf, sizeof(buf), bound ? "offline" : "");
        lv_label_set_text(_mp_st_lbl[i], buf);
        lv_obj_set_style_text_color(_mp_st_lbl[i],
            lv_color_hex(_cam[i].connected ? C_GREEN_LIT : C_DIM), 0);

        if (bound) lv_obj_remove_flag(_mp_forget_btn[i], LV_OBJ_FLAG_HIDDEN);
        else       lv_obj_add_flag(_mp_forget_btn[i], LV_OBJ_FLAG_HIDDEN);
    }
}

// LVGL-task context only.
static void conflict_panel_fill() {
    if (!_pc_panel) return;
    char m1[20], m2[20], buf[64];
    fmt_mac(m1, sizeof(m1), _pc_new);
    fmt_mac(m2, sizeof(m2), _pc_old);
    snprintf(buf, sizeof(buf), "New device %s", m1);
    lv_label_set_text(_pc_l1, buf);
    snprintf(buf, sizeof(buf), "claims CAM %u - bound to %s", (unsigned)_pc_cam, m2);
    lv_label_set_text(_pc_l2, buf);
}

static void build_pairing_overlays() {
    // ── Mounts panel ─────────────────────────────────────────────────────
    _mp_panel = lv_obj_create(lv_layer_top());
    lv_obj_set_size(_mp_panel, 620, 424);
    lv_obj_center(_mp_panel);
    card_style(_mp_panel, C_SURF, C_ACCENT);
    lv_obj_set_style_border_width(_mp_panel, 2, 0);
    lv_obj_set_style_pad_all(_mp_panel, 16, 0);
    lv_obj_remove_flag(_mp_panel, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(_mp_panel, LV_OBJ_FLAG_HIDDEN);

    lv_obj_t *t = make_label(_mp_panel, "PAIRED MOUNTS",
                             &lv_font_montserrat_16, C_ACCENT2);
    lv_obj_set_pos(t, 4, 0);

    for (int i = 0; i < 5; i++) {
        int y = 34 + i * 58;
        char cam[8];
        snprintf(cam, sizeof(cam), "CAM %d", i + 1);
        lv_obj_t *c = make_label(_mp_panel, cam, &lv_font_montserrat_16, C_TEXT);
        lv_obj_set_pos(c, 4, y + 8);

        _mp_mac_lbl[i] = make_label(_mp_panel, "", &lv_font_montserrat_12, C_TEXT);
        lv_obj_set_pos(_mp_mac_lbl[i], 92, y + 2);

        _mp_st_lbl[i] = make_label(_mp_panel, "", &lv_font_montserrat_12, C_DIM);
        lv_obj_set_pos(_mp_st_lbl[i], 92, y + 22);

        _mp_forget_btn[i] = make_button(_mp_panel, "FORGET", C_RED,
                                        ev_mounts_forget, (void *)(uintptr_t)(i + 1));
        lv_obj_set_size(_mp_forget_btn[i], 92, 36);
        lv_obj_set_pos(_mp_forget_btn[i], 480, y);
        lv_obj_add_flag(_mp_forget_btn[i], LV_OBJ_FLAG_HIDDEN);
    }

    lv_obj_t *hint = make_label(_mp_panel,
        "Forget unbinds a slot. A live mount re-pairs itself in ~5 s\n"
        "- Forget is for retired or replaced hardware.",
        &lv_font_montserrat_12, C_DIM);
    lv_obj_set_pos(hint, 4, 334);

    lv_obj_t *close = make_button(_mp_panel, "CLOSE", C_SURF2, ev_mounts_close);
    lv_obj_set_size(close, 110, 40);
    lv_obj_set_pos(close, 462, 340);

    // ── Conflict prompt ──────────────────────────────────────────────────
    _pc_panel = lv_obj_create(lv_layer_top());
    lv_obj_set_size(_pc_panel, 520, 220);
    lv_obj_center(_pc_panel);
    card_style(_pc_panel, C_SURF, C_RED_LIT);
    lv_obj_set_style_border_width(_pc_panel, 2, 0);
    lv_obj_set_style_pad_all(_pc_panel, 16, 0);
    lv_obj_remove_flag(_pc_panel, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(_pc_panel, LV_OBJ_FLAG_HIDDEN);

    lv_obj_t *pt = make_label(_pc_panel, "PAIRING CONFLICT",
                              &lv_font_montserrat_16, C_RED_LIT);
    lv_obj_set_pos(pt, 4, 0);
    _pc_l1 = make_label(_pc_panel, "", &lv_font_montserrat_12, C_TEXT);
    lv_obj_set_pos(_pc_l1, 4, 34);
    _pc_l2 = make_label(_pc_panel, "", &lv_font_montserrat_12, C_TEXT);
    lv_obj_set_pos(_pc_l2, 4, 56);
    lv_obj_t *ph = make_label(_pc_panel,
        "REPLACE re-pairs the slot to the new device.\n"
        "IGNORE suppresses this device until the hub restarts.",
        &lv_font_montserrat_12, C_DIM);
    lv_obj_set_pos(ph, 4, 84);

    lv_obj_t *rb = make_button(_pc_panel, "REPLACE", C_RED, ev_pc_replace);
    lv_obj_set_size(rb, 150, 44);
    lv_obj_set_pos(rb, 160, 136);
    lv_obj_t *ib = make_button(_pc_panel, "IGNORE", C_SURF2, ev_pc_ignore);
    lv_obj_set_size(ib, 150, 44);
    lv_obj_set_pos(ib, 330, 136);
}

static void lvgl_task_fn(void *arg) {
    lv_timer_create(la_flash_timer_cb, 500, nullptr);
    for (;;) {
        if (xSemaphoreTake(_lvgl_mux, portMAX_DELAY)) {

            // ── Execute any pending navigation action ─────────────────────
            // All screen building and lv_scr_load calls happen HERE, outside
            // lv_timer_handler, so the call stack is flat and there is no risk
            // of overflowing the 32 KB task stack due to nested event dispatch.
            if (_pending_action != NavAction::NONE) {
                NavAction act  = _pending_action;
                _pending_action = NavAction::NONE;
                switch (act) {
                case NavAction::GOTO_POSITIONS:
                    if (lv_scr_act() == _scr_config) ev_send_config(nullptr);
                    destroy_detail_screen();
                    destroy_config_screen();
                    destroy_positions_screen();
                    build_positions_screen();
                    refresh_positions_slots();
                    refresh_positions_dials();
                    lv_obj_update_layout(_scr_positions);
                    lv_scr_load(_scr_positions);
                    break;
                case NavAction::GOTO_CONFIG:
                    destroy_detail_screen();
                    destroy_positions_screen();
                    _cfg_cam = 0;
                    if (!_scr_config) build_config_screen();
                    lv_obj_update_layout(_scr_config);
                    lv_scr_load(_scr_config);
                    if (_send_cb) _send_cb(1, CMD_GET_CONFIG, nullptr, 0);
                    break;
                case NavAction::GOTO_STATUS_FROM_POSITIONS:
                    if (lv_scr_act() == _scr_config) ev_send_config(nullptr);
                    lv_scr_load(_scr_status);
                    destroy_positions_screen();
                    destroy_config_screen();
                    if (!_scr_detail) {
                        build_detail_screen();
                        lv_obj_update_layout(_scr_detail);
                        refresh_detail_slots();
                        refresh_detail_dials();
                    }
                    break;
                case NavAction::GOTO_STATUS_FROM_CONFIG:
                    ev_send_config(nullptr);   // sliders still exist, save first
                    lv_scr_load(_scr_status);
                    destroy_config_screen();
                    if (!_scr_detail) {
                        build_detail_screen();
                        lv_obj_update_layout(_scr_detail);
                        refresh_detail_slots();
                        refresh_detail_dials();
                    }
                    break;
                case NavAction::OPEN_DETAIL: {
                    uint8_t idx = _pending_cam;
                    if (!_scr_detail) {
                        build_detail_screen();
                        lv_obj_update_layout(_scr_detail);
                    }
                    det_switch_cam(idx);
                    lv_scr_load(_scr_detail);
                    break;
                }
                case NavAction::DETAIL_BACK:
                    if (_calib_state != 0 && _send_cb)
                        _send_cb(_detail_cam + 1, CMD_ADD_SUBJECT_ABORT, nullptr, 0);
                    _calib_state = 0;
                    _calib_slot  = 0xFF;
                    det_cancel_set();
                    det_cancel_clear();
                    lv_scr_load(_scr_status);
                    break;
                case NavAction::DISCONNECT_RETURN:
                    lv_scr_load(_scr_status);
                    break;
                default: break;
                }
            }

            // ── Legacy _pending_screen (public API disconnect path) ───────
            if (_pending_screen) {
                lv_scr_load(_pending_screen);
                _pending_screen = nullptr;
            }

            // ── Pending look-at subject refresh ───────────────────────────
            // hub_ui_notify_look_at_status() writes _active_la_subject[] and
            // sets this flag from loop() (core 0) without taking the mutex.
            // We apply the refresh here, inside the mutex, so there is no
            // contention and no 100 ms timeout risk.
            //
            // Targeted refresh: only update the 8 subject border colours per
            // look-at mount row that actually changed.  The full
            // refresh_positions_slots() marks all 50 buttons dirty (~350 LVGL
            // style calls) and drives a full-screen re-render every time; the
            // targeted path makes ≤ 8 border-colour calls per look-at mount
            // and only touches the currently active screen.
            // ── Slot updates that could not take the mutex ───────────────
            // hub_ui_update_slots() stages and flags rather than blocking, so
            // anything it could not apply is applied here, inside the mutex we
            // already hold.  Without this the update is simply lost and the
            // screen shows the mount's previous state until some later update
            // happens to win the lock.
            for (int i = 0; i < 5; i++)
                if (_slots_dirty[i]) apply_slots_locked(i);

            if (_la_refresh_pending) {
                _la_refresh_pending = false;
                lv_obj_t *active_scr = lv_scr_act();

                if (active_scr == _scr_positions && _scr_positions) {
                    // Update only the subject slot border colours — leave bg,
                    // label, and non-look-at rows completely untouched.
                    for (int i = 0; i < 5; i++) {
                        if (!cam_is_look_at(i)) continue;
                        uint16_t occ = _slots[i].slot_occupied;
                        for (int s = 0; s < 10; s++) {
                            lv_obj_t *btn = _pos_slot_btn[i][s];
                            if (!btn) continue;
                            uint32_t bc;
                            if (s >= 8) {
                                // Arrow buttons (◀ = slot 8, ▶ = slot 9)
                                int8_t arr = _la_arrow_state[i];
                                bool moving = (arr == 0 && s == 8) || (arr == 1 && s == 9);
                                bool done   = (arr == 2 && s == 8) || (arr == 3 && s == 9);
                                if (moving)
                                    bc = _la_flash_on ? C_BTN_BORDER_TGT : C_BORDER;
                                else if (done)
                                    bc = C_BTN_BORDER_AT;    // green  — arrived
                                else
                                    bc = C_BORDER;            // grey   — idle
                            } else {
                                bc = (s == (int)_active_la_subject[i]) ? C_BTN_BORDER_AT :
                                     ((occ & (1u << s))                ? C_BTN_BORDER_OCC :
                                                                         C_BTN_BORDER_EMPTY);
                            }
                            lv_obj_set_style_border_color(btn, lv_color_hex(bc), 0);
                        }
                    }
                } else if (active_scr == _scr_detail && _scr_detail) {
                    refresh_detail_slots();
                } else {
                    // Home / status screen — refresh tile dot colours.
                    for (int i = 0; i < 5; i++) {
                        // Never repaint slots for an offline camera — its _slots
                        // data may be stale and would show phantom saved-position dots.
                        if (!_cam[i].connected || !cam_is_look_at(i) || !_tile_slot[i][0]) continue;
                        const TileSlots &sl = _slots[i];
                        for (int s = 0; s < 10; s++) {
                            uint16_t bit = (uint16_t)(1u << s);
                            lv_color_t col;
                            if (s >= 8) {
                                // Arrow dots — reflect look-at move direction state.
                                int8_t arr = _la_arrow_state[i];
                                bool arr_m = (arr == 0 && s == 8) || (arr == 1 && s == 9);
                                bool arr_d = (arr == 2 && s == 8) || (arr == 3 && s == 9);
                                col = lv_color_hex(arr_m ? (_la_flash_on ? C_SLOT_TGT : C_SLOT_EMPTY) :
                                                   arr_d ? C_SLOT_AT  : C_SLOT_EMPTY);
                            } else if (s == (int)_active_la_subject[i])
                                col = lv_color_hex(C_SLOT_AT);
                            else if (sl.slot_occupied & bit)
                                col = lv_color_hex(C_SLOT_OCC);
                            else
                                col = lv_color_hex(C_SLOT_EMPTY);
                            lv_obj_set_style_bg_color(_tile_slot[i][s], col, 0);
                        }
                    }
                }
            }

            // ── Pairing UI (stage 3) — panel actions + hub messages ───────
            if (_mp_act_open) {
                _mp_act_open = false;
                mounts_panel_refresh();
                lv_obj_remove_flag(_mp_panel, LV_OBJ_FLAG_HIDDEN);
                disp_send_raw(DISP_MSG_GET_MOUNT_TABLE, nullptr, 0);  // fresh copy
            }
            if (_mp_act_close) {
                _mp_act_close = false;
                lv_obj_add_flag(_mp_panel, LV_OBJ_FLAG_HIDDEN);
            }
            if (_mp_act_forget) {
                uint8_t cam = _mp_act_forget;
                _mp_act_forget = 0;
                disp_send_raw(DISP_MSG_PAIR_FORGET, &cam, 1);
                // hub replies with a MOUNT_TABLE push → row refreshes below
            }
            if (_mounts_table_pending) {
                _mounts_table_pending = false;
                if (_mp_panel && !lv_obj_has_flag(_mp_panel, LV_OBJ_FLAG_HIDDEN))
                    mounts_panel_refresh();
            }
            if (_pc_show_pending) {
                _pc_show_pending = false;
                conflict_panel_fill();
                lv_obj_remove_flag(_pc_panel, LV_OBJ_FLAG_HIDDEN);
            }
            if (_pc_clear_pending) {
                _pc_clear_pending = false;
                lv_obj_add_flag(_pc_panel, LV_OBJ_FLAG_HIDDEN);
            }
            if (_mp_act_decide >= 0) {
                uint8_t buf[8];
                buf[0] = _pc_cam;
                buf[1] = (uint8_t)_mp_act_decide;
                memcpy(buf + 2, _pc_new, 6);
                _mp_act_decide = -1;
                disp_send_raw(DISP_MSG_PAIR_DECIDE, buf, 8);
                lv_obj_add_flag(_pc_panel, LV_OBJ_FLAG_HIDDEN);
            }

            // ── Uniform health telemetry (display node) ───────────────────
            // Worst gap between LVGL task iterations is exactly the signal
            // that matters here — it is what froze in the old UI-lockup bug.
            // Sent over the display UART; the hub wraps it into CMD_HEALTH
            // (sender 0xFD) for the PC log.
            {
                static uint32_t _prev_tick_ms   = 0;
                static uint16_t _tick_max_ms    = 0;
                static uint32_t _health_last_ms = 0;
                static uint32_t _health_anom_ms = 0;
                static bool     _health_first   = false;

                uint32_t nowh = millis();
                if (_prev_tick_ms) {
                    uint32_t gap = nowh - _prev_tick_ms;
                    if (gap > _tick_max_ms)
                        _tick_max_ms = (gap > 65535) ? 65535 : (uint16_t)gap;
                }
                _prev_tick_ms = nowh;

                uint32_t free_heap = (uint32_t)esp_get_free_heap_size();
                bool anomaly = (!_health_first && nowh > 3000) ||
                               (free_heap < HEALTH_LOW_HEAP_BYTES) ||
                               (_tick_max_ms > HEALTH_LOOP_STALL_MS);
                bool periodic = (nowh - _health_last_ms >= HEALTH_INTERVAL_MS);
                if ((anomaly && nowh - _health_anom_ms >= HEALTH_ANOMALY_GAP_MS)
                        || periodic) {
                    PayloadHealth h = {};
                    h.node_type     = HEALTH_NODE_DISPLAY;
                    h.reset_reason  = (uint8_t)esp_reset_reason();
                    h.uptime_s      = nowh / 1000UL;
                    h.free_heap     = free_heap;
                    h.min_free_heap = (uint32_t)esp_get_minimum_free_heap_size();
                    h.loop_max_ms   = _tick_max_ms;
                    h.tx_fail       = 0;
                    h.rssi          = 0;
                    h.flags         = (anomaly && !periodic) ? 0x01 : 0x00;
                    h.node_u32      = 0;
                    uint8_t p[24];
                    encode_health_payload(p, &h);
                    disp_send_raw(DISP_MSG_HEALTH, p, 24);
                    _health_last_ms = nowh;
                    _tick_max_ms    = 0;
                    _health_first   = true;
                    if (anomaly) _health_anom_ms = nowh;
                }
            }

            uint32_t delay_ms = lv_timer_handler();
            xSemaphoreGive(_lvgl_mux);
            vTaskDelay(pdMS_TO_TICKS(delay_ms < 1 ? 1 : delay_ms > 5 ? 5 : delay_ms));
        }
    }
}

// ============================================================
//  Public API
// ============================================================

void hub_display_init(hub_send_fn_t send_cb) {
    Serial.println("[DISP] hub_display_init start");
    _send_cb  = send_cb;
    _lvgl_mux = xSemaphoreCreateMutex();

    Serial.println("[DISP] init_hardware...");
    init_hardware();
    Serial.println("[DISP] init_lvgl...");
    init_lvgl();
    Serial.println("[DISP] build_status_screen...");
    build_status_screen();
    Serial.println("[DISP] status screen built");

    lv_scr_load(_scr_status);
    Serial.println("[DISP] lv_refr_now...");
    lv_refr_now(lv_display_get_default());
    Serial.println("[DISP] lv_refr_now done");
    memcpy(_fb1, _fb0, LCD_WIDTH * LCD_HEIGHT * sizeof(uint16_t));

    Serial.println("[DISP] build_detail_screen...");
    build_detail_screen();
    Serial.println("[DISP] detail screen built");

    build_pairing_overlays();   // Mounts panel + conflict prompt (lv_layer_top)

    Serial.printf("[DISP] free heap before task: %u bytes\n", esp_get_free_heap_size());
    Serial.flush();   // drain USB CDC so LVGL task output isn't dropped
    Serial.println("[DISP] starting LVGL task (static stack)...");
    _lvgl_task_handle = xTaskCreateStaticPinnedToCore(
        lvgl_task_fn, "lvgl",
        LVGL_TASK_STACK_WORDS,   // words (4 KB × 4 = 16 KB), static BSS allocation
        nullptr, 2,
        _lvgl_task_stack, &_lvgl_task_static_buf, 1);
    Serial.printf("[DISP] LVGL task: %s\n", _lvgl_task_handle ? "OK" : "FAILED");
    Serial.println("[DISP] hub_display_init complete");
}

void hub_ui_tick() {
    // lv_timer_handler() is driven by lvgl_task_fn — no LVGL work here.
    //
    // Staleness sweep: a cam tile is only ever cleared by SET_DISCONNECTED from
    // the hub, but that message is sent once and can be lost (this side drops it
    // if the LVGL mutex is busy >100 ms; the hub forgets its state on reboot).
    // A lost clear used to latch a phantom "connected" tile forever — the ghost
    // cam3 0 dBm/JOGGING bug.  Self-heal instead: mounts stream STATUS →
    // UPDATE_CAM many times per second while genuinely connected, so any tile
    // not refreshed within 5 s is stale and gets cleared locally.
    static uint32_t _last_stale_check_ms = 0;
    uint32_t now = millis();
    if (now - _last_stale_check_ms < 1000) return;
    _last_stale_check_ms = now;
    for (int i = 0; i < 5; i++) {
        if (_cam[i].connected && now - _cam[i].last_seen_ms > 5000)
            hub_ui_set_disconnected(i + 1);
    }
}

void hub_ui_update_cam(uint8_t mount_id,
                       uint8_t state, uint8_t flags, int8_t rssi) {
    if (mount_id < 1 || mount_id > 5) return;
    int i = mount_id - 1;
    if (!xSemaphoreTake(_lvgl_mux, pdMS_TO_TICKS(100))) return;

    // Snapshot old values before overwriting — used to gate LVGL calls so we
    // only touch the render tree when something actually changed.  When the
    // mount is idle for hours this drops from ~10 LVGL calls/STATUS to zero.
    bool    was_connected   = _cam[i].connected;
    uint8_t old_state       = _cam[i].state;
    uint8_t old_flags       = _cam[i].flags;
    int8_t  old_rssi        = _cam[i].rssi;
    bool look_at_changed    = (old_flags & FLAG_LOOK_AT_MODE) != (flags & FLAG_LOOK_AT_MODE);

    _cam[i].connected    = true;
    _cam[i].state        = state;
    _cam[i].flags        = flags;
    _cam[i].rssi         = rssi;
    _cam[i].last_seen_ms = millis();   // feeds the staleness sweep in hub_ui_tick()

    bool state_changed = !was_connected || (old_state != state) || (old_flags != flags);
    bool rssi_changed  = !was_connected || (old_rssi  != rssi);

    // First connect: set all static "CONNECTED" elements once.
    if (!was_connected) {
        lv_obj_set_style_bg_color(_tile_dot[i], lv_color_hex(C_GREEN_LIT), 0);
        lv_label_set_text(_tile_cstat[i], "CONNECTED");
        lv_obj_set_style_text_color(_tile_cstat[i], lv_color_hex(C_GREEN_LIT), 0);
    }

    // State / motion — only update border and label when state or flags change.
    if (state_changed) {
        bool moving = (state == STATE_JOGGING || state == STATE_MOVING_TO_POS ||
                       state == STATE_FINDING_LIMITS);
        lv_obj_set_style_border_color(_tile_obj[i],
            lv_color_hex(moving ? C_ORANGE : C_GREEN_LIT), 0);
        lv_obj_set_style_border_width(_tile_obj[i], 2, 0);
        lv_label_set_text(_tile_state[i], state_name(state));
        lv_obj_set_style_text_color(_tile_state[i],
            lv_color_hex(state == STATE_ERROR   ? C_RED_LIT :
                         state >= STATE_JOGGING ? C_ORANGE  : C_DIM), 0);
    }

    // RSSI — only update text when value changed; only update colour when
    // threshold band changes (green/amber/red).
    if (rssi_changed) {
        char rssi_buf[12];
        snprintf(rssi_buf, sizeof(rssi_buf), "%d dBm", (int)rssi);
        lv_label_set_text(_tile_rssi[i], rssi_buf);

        uint32_t rssi_col     = (rssi     >= -65) ? C_GREEN_LIT :
                                (rssi     >= -75) ? C_ORANGE    : C_RED_LIT;
        uint32_t old_rssi_col = (old_rssi >= -65) ? C_GREEN_LIT :
                                (old_rssi >= -75) ? C_ORANGE    : C_RED_LIT;
        if (!was_connected || rssi_col != old_rssi_col)
            lv_obj_set_style_text_color(_tile_rssi[i], lv_color_hex(rssi_col), 0);
    }

    // Restore look-at arrow state from limit flags on power-on / reconnect.
    // Only acts when the arrow is in the unknown-idle state (-1) so it never
    // overwrites an active flash (0/1) or an already-known green (2/3).
    if (cam_is_look_at(i) && _la_arrow_state[i] == -1) {
        if      (flags & FLAG_AT_MIN_LIMIT) { _la_arrow_state[i] = 2; _la_refresh_pending = true; }
        else if (flags & FLAG_AT_MAX_LIMIT) { _la_arrow_state[i] = 3; _la_refresh_pending = true; }
    }

    // If Look-at mode toggled, clear the active subject selection and refresh
    // whichever position view is currently open.
    if (look_at_changed) {
        _active_la_subject[i] = -1;
        if (_scr_detail && _detail_cam == i)
            refresh_detail_slots();
        if (_scr_positions)
            refresh_positions_slots();
    }

    xSemaphoreGive(_lvgl_mux);
}

void hub_ui_set_disconnected(uint8_t mount_id) {
    if (mount_id < 1 || mount_id > 5) return;
    int i = mount_id - 1;
    // Idempotence gate: the hub re-sends SET_DISCONNECTED for offline mounts
    // every 5 s (reconciliation sweep) — skip the LVGL re-render when the tile
    // is already showing OFFLINE.
    if (!_cam[i].connected) return;
    if (!xSemaphoreTake(_lvgl_mux, pdMS_TO_TICKS(100))) return;

    _cam[i].connected = false;

    lv_obj_set_style_border_color(_tile_obj[i], lv_color_hex(C_BORDER), 0);
    lv_obj_set_style_border_width(_tile_obj[i], 1, 0);
    lv_obj_set_style_bg_color(_tile_dot[i], lv_color_hex(C_DIM), 0);
    lv_label_set_text(_tile_cstat[i], "OFFLINE");
    lv_obj_set_style_text_color(_tile_cstat[i], lv_color_hex(C_DIM), 0);
    lv_label_set_text(_tile_state[i], "");
    lv_label_set_text(_tile_rssi[i], "");

    _cam[i].pt_preset = 0;
    _cam[i].sl_preset = 0;
    update_tile_dial(&_tile_pt_dial[i], 0);
    update_tile_dial(&_tile_sl_dial[i], 0);
    update_tile_dial(&_pos_pt_dial[i], 0);
    update_tile_dial(&_pos_sl_dial[i], 0);

    for (int s = 0; s < 10; s++)
        lv_obj_set_style_bg_color(_tile_slot[i][s], lv_color_hex(C_SLOT_EMPTY), 0);

    // Clear the stale slot DATA and flags too — not just the visual tiles.
    // Otherwise a periodic re-render (the look-at flash keys on cam_is_look_at(),
    // which reads _cam[i].flags) repaints phantom saved-position dots from this
    // leftover data for a camera that's been offline for months.  Both are
    // repopulated by hub_ui_update_cam() the moment the mount reconnects.
    _slots[i]     = {};
    _cam[i].flags = 0;

    // If the detail screen for this camera is currently displayed, return to
    // the main status screen — there is nothing useful to show for an offline mount.
    if (lv_scr_act() == _scr_detail && _detail_cam == (uint8_t)i) {
        if (_jog_pan || _jog_tilt || _jog_sl || _jog_zoom) {
            _jog_pan = _jog_tilt = _jog_sl = _jog_zoom = 0;
            flush_jog();
        }
        _calib_state = 0;
        _calib_slot  = 0xFF;
        det_cancel_set();
        det_cancel_clear();
        _pending_action = NavAction::DISCONNECT_RETURN;   // task loop handles lv_scr_load
    }

    xSemaphoreGive(_lvgl_mux);
}

void hub_ui_update_preset(uint8_t mount_id, uint8_t pt_preset, uint8_t sz_preset) {
    if (mount_id < 1 || mount_id > 5) return;
    int i = mount_id - 1;
    // Ignore preset updates for a camera we don't believe is connected.  The hub
    // emits disp_update_preset() on every JOG (incl. to the PC's default-selected
    // mount), so without this gate a jog aimed at an offline camera leaves a
    // phantom dial value that never clears (SET_DISCONNECTED is only sent for
    // cameras that were previously seen).  A real camera is marked connected by
    // hub_ui_update_cam() before its presets arrive, so this never suppresses a
    // legitimate update.
    if (!_cam[i].connected) return;
    if (!xSemaphoreTake(_lvgl_mux, pdMS_TO_TICKS(100))) return;
    _cam[i].pt_preset = pt_preset;
    _cam[i].sl_preset = sz_preset;
    update_tile_dial(&_tile_pt_dial[i], pt_preset);
    // Slider-less mount → slider dial dormant, same as the positions/detail
    // screens.  (Must gate here too: the home-screen dial is updated by this
    // function, not by refresh_positions_dials/refresh_detail_dials.)
    update_tile_dial(&_tile_sl_dial[i], cam_has_slider(i) ? sz_preset : 0);
    if (lv_scr_act() == _scr_detail && _detail_cam == (uint8_t)i)
        refresh_detail_dials();
    if (_scr_positions && lv_scr_act() == _scr_positions)
        refresh_positions_dials();
    xSemaphoreGive(_lvgl_mux);
}


// Caller must hold _lvgl_mux.
static void apply_slots_locked(int i) {
    uint16_t slot_occupied = _slots_in[i].occ;
    uint16_t slot_at       = _slots_in[i].at;
    uint8_t  target_slot   = _slots_in[i].tgt;
    uint8_t  state         = _slots_in[i].state;
    _slots_dirty[i] = false;

    // Snapshot old slot values before overwriting.
    bool     la        = cam_is_look_at(i);
    uint16_t old_occ   = _slots[i].slot_occupied;
    uint16_t old_at    = _slots[i].slot_at;
    uint8_t  old_tgt   = _slots[i].target_slot;
    uint8_t  old_state = _slots[i].state;
    bool slots_changed = (slot_occupied != old_occ || slot_at != old_at ||
                          target_slot   != old_tgt || state   != old_state);
    _slots[i] = { slot_occupied, slot_at, target_slot, state };

    if (la) {
        // Arrow state from target_slot, which the MOUNT sets when a look-at
        // move genuinely starts and clears when the controller releases the
        // axes (arrival, E-stop or abort alike).  It used to be fed by
        // hub_ui_notify_la_move_dir() from CMD_LA_MOVE_DIR — the hub echoing
        // the command it had just relayed — so a press lost on the radio left
        // an arrow flashing here for a move that never ran, with nothing able
        // to correct it.  Deriving it from the relayed STATUS means the
        // display cannot show motion the mount is not reporting.
        int8_t want = _la_arrow_state[i];
        if      (target_slot == TARGET_SLOT_LA_MIN) want = 0;   // ◀ moving
        else if (target_slot == TARGET_SLOT_LA_MAX) want = 1;   // ▶ moving
        else if (_la_arrow_state[i] == 0)           want = 2;   // ◀ done (green)
        else if (_la_arrow_state[i] == 1)           want = 3;   // ▶ done (green)
        if (want != _la_arrow_state[i]) {
            _la_arrow_state[i]  = want;
            _la_refresh_pending = true;
        }
        if (state == STATE_JOGGING &&
                   (_la_arrow_state[i] == 0 || _la_arrow_state[i] == 1)) {
            // Manual slider jog while arrow is in "moving" state — clear to grey.
            // Do NOT clear "done" state (2/3 = green) — a brief post-move deceleration
            // jog must not wipe the green arrival indicator.
            _la_arrow_state[i] = -1;
            _la_refresh_pending = true;
        }
    }
    // Only push LVGL updates when slot data actually changed.  When the mount
    // is idle for hours these values are constant, reducing LVGL work from
    // ~40 calls/STATUS to zero and preventing progressive heap fragmentation.
    if (slots_changed) {
        for (int s = 0; s < 10; s++) {
            uint16_t bit = (uint16_t)(1u << s);
            lv_color_t col;
            // In LA mode: green = currently tracked subject; slots 8-9 = ◀/▶ arrow dots.
            // In normal mode: green = physically at that stored position (slot_at).
            bool is_active = la ? (s < 8 && s == (int)_active_la_subject[i])
                                : ((slot_at & bit) && (slot_occupied & bit));
            if (la && s >= 8) {
                // Arrow dots — reflect look-at move direction state.
                int8_t arr = _la_arrow_state[i];
                bool arr_moving = (arr == 0 && s == 8) || (arr == 1 && s == 9);
                bool arr_done   = (arr == 2 && s == 8) || (arr == 3 && s == 9);
                col = lv_color_hex(arr_moving ? (_la_flash_on ? C_SLOT_TGT : C_SLOT_EMPTY) :
                                   arr_done   ? C_SLOT_AT  : C_SLOT_EMPTY);
            } else if (target_slot != 0xFF && s == (int)target_slot &&
                    state == STATE_MOVING_TO_POS)
                col = lv_color_hex(_la_flash_on ? C_SLOT_TGT : C_SLOT_EMPTY);  // flash, don't sit solid
            else if (is_active)
                col = lv_color_hex(C_SLOT_AT);
            else if (slot_occupied & bit)
                col = lv_color_hex(C_SLOT_OCC);
            else
                col = lv_color_hex(C_SLOT_EMPTY);
            lv_obj_set_style_bg_color(_tile_slot[i][s], col, 0);
        }

        // Refresh the detail screen only when slot data changed.  Calling
        // refresh_detail_slots() unconditionally at 50 Hz was the primary cause
        // of progressive ESP32 heap fragmentation that froze the UI after hours.
        if (_scr_detail && _detail_cam == (int)i)
            refresh_detail_slots();

        // ...and the Positions screen, which was previously never repainted from
        // here — so its borders only updated when the page was opened, and a
        // recall's amber "moving" / green "arrived" transitions were invisible.
        // Only this camera's row, for the same cost reason as above.
        if (_scr_positions && lv_scr_act() == _scr_positions)
            refresh_positions_row((int)i);
    }

}

void hub_ui_update_slots(uint8_t mount_id, uint16_t slot_occupied,
                         uint16_t slot_at, uint8_t target_slot, uint8_t state) {
    if (mount_id < 1 || mount_id > 5) return;
    int i = mount_id - 1;
    // Same gate as hub_ui_update_preset(): ignore slot data for a camera we
    // don't believe is connected, so stray/stale frames can't paint phantom
    // saved-position dots that outlive the (idempotence-gated) disconnect.
    // A real camera is always marked connected by hub_ui_update_cam() first —
    // the hub sends UPDATE_CAM before UPDATE_SLOTS from the same STATUS packet.
    if (!_cam[i].connected) return;

    _slots_in[i].occ   = slot_occupied;
    _slots_in[i].at    = slot_at;
    _slots_in[i].tgt   = target_slot;
    _slots_in[i].state = state;
    _slots_dirty[i]    = true;          // flag last — see above

    // Best effort: apply now if the mutex is free, otherwise the LVGL task
    // picks it up from the dirty flag.  Either way the update is not lost.
    if (!xSemaphoreTake(_lvgl_mux, pdMS_TO_TICKS(100))) return;
    apply_slots_locked(i);
    xSemaphoreGive(_lvgl_mux);
}

void hub_ui_update_clients(uint8_t tcp_count, uint8_t ws_count) {
    _tcp_clients = tcp_count;
    _ws_clients  = ws_count;
    if (!xSemaphoreTake(_lvgl_mux, pdMS_TO_TICKS(100))) return;
    char buf[32];
    snprintf(buf, sizeof(buf), "TCP: %u   WS: %u", tcp_count, ws_count);
    for (int t = 0; t < 3; t++)
        if (_lbl_clients[t]) lv_label_set_text(_lbl_clients[t], buf);
    xSemaphoreGive(_lvgl_mux);
}

// ============================================================
//  hub_ui_update_config
//  Called when the hub forwards a CMD_CONFIG_REPORT to the display.
//  payload:     CONFIG_REPORT payload (75 bytes: orientation + speeds + stall thresholds).
//  payload_len: actual payload length — must be >= 75.
//  Updates the cached _cfg_speeds / orientation flags, and refreshes the
//  LVGL config screen widgets if the config screen is currently visible for
//  this mount.
// ============================================================

void hub_ui_update_config(uint8_t mount_id, const uint8_t *payload, uint8_t payload_len) {
    if (mount_id < 1 || mount_id > 5) return;
    if (payload_len < 75) return;
    uint8_t idx = mount_id - 1;

    // Decode orientation flags
    uint8_t ori_flags   = payload[0];
    bool pan_inv      = (ori_flags & 0x01) != 0;
    bool slider_inv   = (ori_flags & 0x02) != 0;
    bool has_slider   = (ori_flags & 0x04) != 0;
    bool zoom_inv     = (ori_flags & 0x08) != 0;
    bool lanc_zoom    = (ori_flags & 0x10) != 0;
    bool look_at_mode = (ori_flags & 0x40) != 0;

    // Decode PT presets [1..32] — group 0, presets 1-4
    // Decode SL presets [33..64] — group 1, presets 1-4
    // Each preset is 8 bytes: uint32 max_speed then uint32 accel.
    // Both are cached so ev_send_config can preserve accel when only speed changes.
    int32_t new_speeds[2][4];
    int32_t new_accels[2][4];
    for (int p = 0; p < 4; p++) {
        int off_pt = 1  + p * 8;
        int off_sl = 33 + p * 8;
        new_speeds[0][p] = (int32_t)( ((uint32_t)payload[off_pt]     << 24) |
                                      ((uint32_t)payload[off_pt + 1] << 16) |
                                      ((uint32_t)payload[off_pt + 2] <<  8) |
                                       (uint32_t)payload[off_pt + 3] );
        new_accels[0][p] = (int32_t)( ((uint32_t)payload[off_pt + 4] << 24) |
                                      ((uint32_t)payload[off_pt + 5] << 16) |
                                      ((uint32_t)payload[off_pt + 6] <<  8) |
                                       (uint32_t)payload[off_pt + 7] );
        new_speeds[1][p] = (int32_t)( ((uint32_t)payload[off_sl]     << 24) |
                                      ((uint32_t)payload[off_sl + 1] << 16) |
                                      ((uint32_t)payload[off_sl + 2] <<  8) |
                                       (uint32_t)payload[off_sl + 3] );
        new_accels[1][p] = (int32_t)( ((uint32_t)payload[off_sl + 4] << 24) |
                                      ((uint32_t)payload[off_sl + 5] << 16) |
                                      ((uint32_t)payload[off_sl + 6] <<  8) |
                                       (uint32_t)payload[off_sl + 7] );
    }

    // Clamp to slider widget ranges: PT 1-50 deg/s, SL 1-100 mm/s
    const int32_t range_min[2] = {  1,   1 };
    const int32_t range_max[2] = { 50, 100 };
    for (int g = 0; g < 2; g++)
        for (int p = 0; p < 4; p++) {
            if (new_speeds[g][p] < range_min[g]) new_speeds[g][p] = range_min[g];
            if (new_speeds[g][p] > range_max[g]) new_speeds[g][p] = range_max[g];
        }

    if (!xSemaphoreTake(_lvgl_mux, pdMS_TO_TICKS(100))) return;

    // Update cached values regardless of whether config screen is visible
    for (int g = 0; g < 2; g++)
        for (int p = 0; p < 4; p++) {
            _cfg_speeds[g][p] = new_speeds[g][p];
            if (new_accels[g][p] > 0)
                _cfg_accels[g][p] = new_accels[g][p];
        }

    // Update orientation cache for this camera's index
    if (idx == _cfg_cam) {
        _cfg_pan_inv    = pan_inv;
        _cfg_slider_inv = slider_inv;
        _cfg_zoom_inv   = zoom_inv;
        _cfg_lanc_zoom  = lanc_zoom;
        _cfg_has_slider = has_slider;
    }

    // If the config screen is visible and showing this camera, update the widgets
    if (_scr_config && lv_scr_act() == _scr_config && idx == _cfg_cam) {
        // Update speed sliders
        for (int g = 0; g < 2; g++) {
            for (int p = 0; p < 4; p++) {
                if (_cfg_sliders[g][p]) {
                    lv_slider_set_value(_cfg_sliders[g][p], new_speeds[g][p], LV_ANIM_OFF);
                    // Trigger VALUE_CHANGED so the numeric label updates
                    lv_obj_send_event(_cfg_sliders[g][p], LV_EVENT_VALUE_CHANGED, nullptr);
                }
            }
        }
        // Update orientation toggles
        if (_cfg_sw_paninv) {
            if (pan_inv) lv_obj_add_state(_cfg_sw_paninv, LV_STATE_CHECKED);
            else         lv_obj_clear_state(_cfg_sw_paninv, LV_STATE_CHECKED);
        }
        if (_cfg_sw_slinv) {
            if (slider_inv) lv_obj_add_state(_cfg_sw_slinv, LV_STATE_CHECKED);
            else            lv_obj_clear_state(_cfg_sw_slinv, LV_STATE_CHECKED);
        }
        if (_cfg_sw_zminv) {
            if (zoom_inv) lv_obj_add_state(_cfg_sw_zminv, LV_STATE_CHECKED);
            else          lv_obj_clear_state(_cfg_sw_zminv, LV_STATE_CHECKED);
        }
        if (_cfg_sw_lanczm) {
            if (lanc_zoom) lv_obj_add_state(_cfg_sw_lanczm, LV_STATE_CHECKED);
            else           lv_obj_clear_state(_cfg_sw_lanczm, LV_STATE_CHECKED);
        }
        if (_cfg_sw_hassl) {
            if (has_slider) lv_obj_add_state(_cfg_sw_hassl, LV_STATE_CHECKED);
            else            lv_obj_clear_state(_cfg_sw_hassl, LV_STATE_CHECKED);
        }
        if (_cfg_sw_lookAt) {
            if (look_at_mode) lv_obj_add_state(_cfg_sw_lookAt, LV_STATE_CHECKED);
            else              lv_obj_clear_state(_cfg_sw_lookAt, LV_STATE_CHECKED);
        }
        // Rail inclination — int16, tenths of a degree, signed.  Older mount
        // firmware sends a 75-byte report with no tilt in it; show "--" rather
        // than a made-up 0.0, so an un-updated mount cannot read as a level rail.
        if (_cfg_lbl_tilt) {
            if (payload_len >= 77) {
                int16_t t10 = (int16_t)(((uint16_t)payload[75] << 8) | payload[76]);
                lv_label_set_text_fmt(_cfg_lbl_tilt, "%s%d.%d\u00B0",
                                      t10 < 0 ? "-" : "",
                                      abs(t10) / 10, abs(t10) % 10);
            } else {
                lv_label_set_text(_cfg_lbl_tilt, "--");
            }
        }
        // Sync Find Home button states with updated has_slider / lanc_zoom
        _cfg_update_find_btns();
    }

    xSemaphoreGive(_lvgl_mux);
}

// ============================================================
//  hub_ui_notify_home_complete
//  Called when the hub forwards a CMD_HOME_COMPLETE from a mount.
//  Updates the config screen status label and re-enables the buttons.
// ============================================================

void hub_ui_notify_home_complete(uint8_t mount_id, uint8_t axis) {
    if (mount_id < 1 || mount_id > 5) return;
    if (!xSemaphoreTake(_lvgl_mux, pdMS_TO_TICKS(100))) return;

    if (_scr_config && lv_scr_act() == _scr_config &&
        (uint8_t)(mount_id - 1) == _cfg_cam) {
        if (_cfg_find_lbl) {
            char buf[32];
            const char *axis_name = (axis == AXIS_SLIDER) ? "Slider" : "Zoom";
            snprintf(buf, sizeof(buf), "%s: homed \xE2\x9C\x93", axis_name);  // ✓
            lv_label_set_text(_cfg_find_lbl, buf);
            lv_obj_set_style_text_color(_cfg_find_lbl, lv_color_hex(0x4CAF50), 0);
        }
        _cfg_update_find_btns();  // re-enable the applicable buttons
    }

    xSemaphoreGive(_lvgl_mux);
}

// ============================================================
//  hub_ui_notify_calib_prompt
//  Called when the hub forwards CMD_CALIB_PROMPT from a mount.
//  Advances the calibration state machine and updates the SET button colour.
// ============================================================

void hub_ui_notify_calib_prompt(uint8_t mount_id, uint8_t sub_state) {
    if (mount_id < 1 || mount_id > 5) return;
    int i = mount_id - 1;
    if (!xSemaphoreTake(_lvgl_mux, pdMS_TO_TICKS(100))) return;

    // Only act if the detail screen is showing this camera.
    if (lv_scr_act() == _scr_detail && _detail_cam == (uint8_t)i) {
        switch ((CalibPrompt)sub_state) {
            case CALIB_MOVING_TO_A:
                _calib_state = 1;
                det_update_calib_ui();
                break;
            case CALIB_WAIT_SET_A:
                _calib_state = 2;
                det_update_calib_ui();
                break;
            case CALIB_MOVING_TO_B:
                _calib_state = 3;
                det_update_calib_ui();
                break;
            case CALIB_WAIT_SET_B:
                _calib_state = 4;
                det_update_calib_ui();
                break;
            case CALIB_SOLVED:
                // Request an updated subject list so slot buttons reflect the new entry.
                if (_send_cb)
                    _send_cb(_detail_cam + 1, CMD_GET_SUBJECTS, nullptr, 0);
                det_reset_calib();
                break;
            case CALIB_ERROR:
                det_reset_calib();
                break;
            default:
                break;
        }
    }

    xSemaphoreGive(_lvgl_mux);
}

// ============================================================
//  Look-at status — sync active subject across all devices
// ============================================================

void hub_ui_notify_look_at_status(uint8_t mount_id, uint8_t subject_id) {
    if (mount_id < 1 || mount_id > 5) return;
    uint8_t idx = mount_id - 1;
    // int8_t write is atomic on Xtensa — safe to do from loop() without the mutex.
    _active_la_subject[idx] = (subject_id <= 7) ? (int8_t)subject_id : -1;
    // Signal the LVGL task to refresh on its next tick (every 1-5 ms).
    // The LVGL task reads this flag inside its mutex-hold section, so there is
    // no contention and no risk of the 100 ms timeout that previously caused
    // missed refreshes on the render-heavy positions screen.
    _la_refresh_pending = true;
}

// ============================================================
//  Subject mask — update slot button colours for slider cameras
// ============================================================

void hub_ui_update_subject_mask(uint8_t mount_id, uint8_t mask) {
    if (mount_id < 1 || mount_id > 5) return;
    uint8_t idx = mount_id - 1;

    // Atomic byte write — safe on ESP32 without mutex.
    // Must happen before the semaphore attempt so the value is visible even
    // if the semaphore is held (LVGL will read it on the next scheduled refresh).
    _subject_mask[idx] = mask;

    // Zero-timeout — never block loop(). Blocking loop() prevents Serial1.read()
    // from being called, causing UART RX buffer overflow at 460800 baud.
    if (!xSemaphoreTake(_lvgl_mux, 0)) return;

    // Refresh slot buttons on whichever position view is currently open.
    if (_scr_detail && _detail_cam == (int)idx)
        refresh_detail_slots();
    if (_scr_positions)
        refresh_positions_slots();

    xSemaphoreGive(_lvgl_mux);
}

// ============================================================
//  Pairing (stage 3) — public API (called from loop(), core 0)
// ============================================================

void hub_ui_update_mount_table(const uint8_t *macs30) {
    // Payload copy first, flag last — the LVGL task's semaphore acquisition
    // provides the barrier (same pattern as _la_refresh_pending).
    memcpy(_mount_table, macs30, sizeof(_mount_table));
    _mounts_table_pending = true;
}

void hub_ui_notify_pair_conflict(uint8_t cam, const uint8_t *new_mac,
                                 const uint8_t *old_mac) {
    if (cam == 0) {                 // hub says: claimant gone / decided
        _pc_clear_pending = true;
        return;
    }
    if (cam > 5) return;
    memcpy(_pc_new, new_mac, 6);
    memcpy(_pc_old, old_mac, 6);
    _pc_cam = cam;
    _pc_show_pending = true;
}
