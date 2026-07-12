#pragma once
#include <lvgl.h>

// Arc strip — a partial arc (one side of the screen) with 4 detent dots.
struct ArcStrip {
    lv_obj_t *arc;
    lv_obj_t *dots[5];   // 5 tick dots: arc-start + one per segment end
};
