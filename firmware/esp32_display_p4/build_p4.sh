#!/bin/sh
# build_p4.sh — compile, and optionally flash, the P4 display firmware.
#
#   firmware/esp32_display_p4/build_p4.sh                    # compile
#   firmware/esp32_display_p4/build_p4.sh flash <port>       # compile + upload
#
# Flash through the board's "UART" Type-C (the CH343P USB-serial chip).
#
# Options, as environment variables:
#   CHIP=v3              a chip that reports v3.00 or newer (400 MHz); the
#                        boards shipping now are earlier (360 MHz), the default
#   DISPLAY_FLIP=1       the picture upside down, for a display mounted that way
#   TOUCH_MIRROR_XY=0    if taps land in the diagonally opposite corner
#   PAD_USB=0            no USB controller
# Each combination builds in its own .build/ directory, so a test build never
# overwrites the one that ships.  Libraries come from the repo's libraries/,
# as for every other target (tools/build.sh).
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

case "${CHIP:-}" in
    v3|postv3) VARIANT=postv3 ;;
    ""|prev3)  VARIANT=prev3 ;;
    *) echo "CHIP must be v3 or left unset"; exit 2 ;;
esac
FQBN="esp32:esp32:esp32p4:ChipVariant=$VARIANT,PSRAM=enabled,FlashSize=32M,PartitionScheme=app5M_fat24M_32MB,USBMode=hwcdc,CDCOnBoot=default"

FLAGS=""
TAG=""
for v in DISPLAY_FLIP TOUCH_MIRROR_XY PAD_USB; do
    eval "x=\${$v:-}"
    if [ -n "$x" ]; then
        case "$x" in 0|1) ;; *) echo "$v must be 0 or 1"; exit 2 ;; esac
        FLAGS="$FLAGS -D$v=$x"
        TAG="$TAG-$(echo "$v" | tr 'A-Z_' 'a-z.' )$x"
    fi
done
OUT="$REPO/.build/display-p4-$VARIANT$TAG"

printf -- '-- display-p4  %s\n' "$FQBN"
printf '   flags  %s\n' "${FLAGS:- (none - plain build)}"
if [ -n "$FLAGS" ]; then
    arduino-cli compile --fqbn "$FQBN" --libraries "$REPO/libraries" --build-path "$OUT" \
        --build-property "compiler.cpp.extra_flags=$FLAGS" --warnings default "$HERE"
else
    arduino-cli compile --fqbn "$FQBN" --libraries "$REPO/libraries" --build-path "$OUT" \
        --warnings default "$HERE"
fi

[ "${1:-}" = "flash" ] || exit 0
port="${2:-}"
if [ -z "$port" ]; then
    echo "no port given.  Attached boards:"
    arduino-cli board list
    exit 2
fi
arduino-cli upload --fqbn "$FQBN" --input-dir "$OUT" -p "$port" "$HERE"
echo "Flashed.  To watch it boot:  arduino-cli monitor -p $port -c baudrate=115200"
