/*
 * lv_conf.h — SHARED LVGL v9 configuration for both LVGL boards:
 *   - esp32_display        (Waveshare 7" 800×480, hub touchscreen)
 *   - esp_mount_amoled175  (1.75" 466×466 round AMOLED, mount bridge)
 *
 * This file sits next to the lvgl/ library folder, which is where LVGL's
 * lv_conf_internal.h looks (#include "../../lv_conf.h") when compiling the
 * library's own sources — for tools/build.sh (--libraries libraries/) and for
 * anyone who clones the repo and copies libraries/ into their Arduino dir.
 *
 * Union of both boards' needs: fonts used by either UI, 16-bit colour,
 * 192 KB pool (fits both — the S3s keep their frame buffers in PSRAM).
 */

#if 1 /* Set to 0 to disable this configuration */

#ifndef LV_CONF_H
#define LV_CONF_H

#define LV_COLOR_DEPTH     16

/* Keep the internal (DRAM) pool SMALL.  The 7" display's RGB panel needs a
 * ~96 KB bounce buffer in internal DRAM, and that allocation must succeed or
 * esp_lcd_new_rgb_panel() aborts at boot (blank screen, no serial — it dies
 * before USB CDC enumerates).  A 192 KB pool here left only ~95 KB free and
 * broke exactly that.  Capacity instead comes from the PSRAM overflow pools
 * added in hub_display.cpp's init_lvgl(), which is where the big UI lives. */
#define LV_MEM_SIZE        (64U * 1024U)

#define LV_USE_THEME_DEFAULT 1
#define LV_USE_FLEX          1

/* Fonts — union of both UIs */
#define LV_FONT_MONTSERRAT_8  1
#define LV_FONT_MONTSERRAT_10 1
#define LV_FONT_MONTSERRAT_12 1
#define LV_FONT_MONTSERRAT_14 1
#define LV_FONT_MONTSERRAT_16 1
#define LV_FONT_MONTSERRAT_18 1
#define LV_FONT_MONTSERRAT_20 1
#define LV_FONT_MONTSERRAT_22 1
#define LV_FONT_MONTSERRAT_24 1
#define LV_FONT_MONTSERRAT_26 1
#define LV_FONT_MONTSERRAT_28 1
#define LV_FONT_MONTSERRAT_30 1
#define LV_FONT_MONTSERRAT_48 1   /* AMOLED large camera number */

#define LV_FONT_DEFAULT &lv_font_montserrat_16

/* Everything else: LVGL v9 defaults (widgets, draw engine, OS=none). */

#endif /* LV_CONF_H */

#endif /* End of "Content enable" */
