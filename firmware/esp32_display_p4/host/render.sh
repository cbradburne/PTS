#!/bin/sh
# render.sh — draws the P4 display's screens on this Mac, to PNG.
#
#   firmware/esp32_display_p4/host/render.sh
#
# Builds the real hub_display.cpp and the repo's LVGL (libraries/lvgl with
# libraries/lv_conf.h) for the Mac, with the board, controller and hub replaced
# by stand-ins (render_main.cpp, stubs/), and writes each screen at 1024 × 600
# to .build/p4-host/shots/.  LVGL's object files are kept between runs.
# Needs only the Xcode command-line tools.  (Every path is quoted: the repo
# lives in "New CC v2".)
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
OUT="$REPO/.build/p4-host"
export REPO OUT
mkdir -p "$OUT/obj" "$OUT/shots"

# LVGL: each file once, again only if lv_conf.h has changed since.
find "$REPO/libraries/lvgl/src" -name '*.c' -print0 | xargs -0 -n 1 -P 8 sh -c '
    f="$1"
    rel="${f#"$REPO/libraries/lvgl/src/"}"
    o="$OUT/obj/$(printf "%s" "$rel" | tr "/" "_" | sed "s/\.c\$/.o/")"
    if [ ! -f "$o" ] || [ "$REPO/libraries/lv_conf.h" -nt "$o" ]; then
        cc -O1 -w -DLV_CONF_INCLUDE_SIMPLE -I"$REPO/libraries" -I"$REPO/libraries/lvgl" -c "$f" -o "$o"
    fi' _

c++ -std=c++17 -O1 -Wno-deprecated-declarations \
    -I"$HERE/stubs" -DLV_CONF_INCLUDE_SIMPLE -I"$REPO/libraries" -I"$REPO/libraries/lvgl" \
    "$HERE/render_main.cpp" "$OUT"/obj/*.o -lz -o "$OUT/render"
"$OUT/render" "$OUT/shots"
