#pragma once
/*
 * hub_display.h — LVGL display interface for Waveshare ESP32-S3-Touch-LCD-7
 *
 * Two screens:
 *   STATUS  — live camera tiles (connection, state, positions, RSSI)
 *   CONFIG  — per-camera speed presets, pan/slider invert, has_slider toggle
 *
 * Required Arduino libraries (install via Library Manager):
 *   - lvgl                   (v9.x)
 *   - GFX Library for Arduino by Moon on a Stick
 *   - TAMC_GT911             by TAMCorp
 *
 * Required lv_conf.h setup — see bottom of this file.
 */

#include <stdint.h>
#include "../shared/protocol.h"

// ---- Callback for sending packets to mounts from the UI ----
typedef void (*hub_send_fn_t)(uint8_t mount_id, CmdType cmd,
                               const uint8_t *payload, uint8_t plen);

// ---- Per-camera status ----
struct CamStatus {
    bool     connected    = false;
    int8_t   rssi         = 0;
    uint8_t  state        = 0;
    uint8_t  flags        = 0;
    uint8_t  pt_preset    = 0;   // active PT preset 1-4, 0 = disconnected
    uint8_t  sl_preset    = 0;   // active SL preset 1-4, 0 = disconnected
    uint32_t last_seen_ms = 0;   // millis() of last UPDATE_CAM — staleness sweep
};

// ---- Per-tile slot state ----
struct TileSlots {
    uint16_t slot_occupied = 0;
    uint16_t slot_at       = 0;
    uint8_t  target_slot   = 0xFF;
    uint8_t  state         = 0;
};

// ---- Public API ----
void hub_display_init(hub_send_fn_t send_cb);
void hub_ui_tick();
void hub_ui_update_cam(uint8_t mount_id,
                       uint8_t state, uint8_t flags, int8_t rssi);
void hub_ui_set_disconnected(uint8_t mount_id);
void hub_ui_update_clients(uint8_t tcp_count, uint8_t ws_count);
void hub_ui_update_preset(uint8_t mount_id, uint8_t pt_preset, uint8_t sz_preset);
void hub_ui_update_slots(uint8_t mount_id, uint16_t slot_occupied,
                         uint16_t slot_at, uint8_t target_slot, uint8_t state);
void hub_ui_update_config(uint8_t mount_id, const uint8_t *payload, uint8_t payload_len);
void hub_ui_notify_home_complete(uint8_t mount_id, uint8_t axis);
void hub_ui_notify_calib_prompt(uint8_t mount_id, uint8_t sub_state);
void hub_ui_notify_look_at_status(uint8_t mount_id, uint8_t subject_id);
void hub_ui_update_subject_mask(uint8_t mount_id, uint8_t mask);

// ---- Pairing (stage 3) ----
// Paired-mount table push from the hub: 30 bytes = 5 × MAC(6), zero = unbound.
void hub_ui_update_mount_table(const uint8_t *macs30);
// Conflict prompt: cam 1-5 shows "new device claims CAM n — Replace / Ignore";
// cam 0 dismisses it (claimant went quiet or was decided elsewhere).
void hub_ui_notify_pair_conflict(uint8_t cam, const uint8_t *new_mac,
                                 const uint8_t *old_mac);

// Raw display→hub UART send (pairing decisions / table requests).
// Implemented by the sketch (esp32_display.ino).
void disp_send_raw(uint8_t type, const uint8_t *payload, uint8_t len);


/*
 * lv_conf.h setup
 * ───────────────
 * 1. Find your LVGL library folder:  ~/Documents/Arduino/libraries/lvgl/
 * 2. Copy  lv_conf_template.h  →  ../lv_conf.h  (one level UP, next to lvgl/)
 * 3. Open lv_conf.h, change  #if 0  to  #if 1  (line ~16)
 * 4. Set these values:
 *
 *    #define LV_COLOR_DEPTH        16
 *    #define LV_MEM_SIZE           (192 * 1024)
 *    #define LV_USE_THEME_DEFAULT   1
 *    #define LV_USE_FLEX            1
 *    #define LV_FONT_MONTSERRAT_12  1
 *    #define LV_FONT_MONTSERRAT_16  1
 *    #define LV_FONT_MONTSERRAT_24  1
 *    #define LV_USE_LABEL           1
 *    #define LV_USE_BTN             1
 *    #define LV_USE_SLIDER          1
 *    #define LV_USE_SWITCH          1
 *    #define LV_USE_ARC             1
 *
 * Arduino Board settings:
 *   Board    : ESP32S3 Dev Module
 *   Flash    : 16MB
 *   PSRAM    : OPI PSRAM   ← important
 */
