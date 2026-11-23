#!/bin/sh
# build_p4.sh — the P4 display through tools/build.sh, the way every other board
# is built.  Kept so the command first given for it still works:
#
#   firmware/esp32_display_p4/build_p4.sh              = tools/build.sh p4display
#   firmware/esp32_display_p4/build_p4.sh flash [port] = tools/build.sh flash p4display [port]
#
# The options pass straight through: CHIP=prev3, DISPLAY_FLIP=0,
# TOUCH_MIRROR_XY=0, PAD_USB=0 — see tools/build.sh.
set -eu
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
if [ "${1:-}" = "flash" ]; then
    exec "$REPO/tools/build.sh" flash p4display ${2:+"$2"}
fi
exec "$REPO/tools/build.sh" p4display
