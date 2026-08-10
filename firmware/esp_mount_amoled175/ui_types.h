#pragma once
#include <lvgl.h>

// Arc strip — a partial arc (one side of the screen) with 4 detent dots.
struct ArcStrip {
    lv_obj_t *arc;
    lv_obj_t *dots[5];   // 5 tick dots: arc-start + one per segment end
    // Last preset actually pushed to LVGL.  0xFF = nothing yet, so the first
    // update always draws.  Without this the arc was repainted on every status
    // packet whether or not it had moved, and an arc's bounding box is a large
    // fraction of a round screen — see ui_update().
    uint8_t   shown = 0xFF;
};
