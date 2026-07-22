#!/bin/sh
# build.sh — compile-test every firmware target with arduino-cli.
#
#   tools/build.sh                # compile all four targets
#   tools/build.sh hub display    # compile a subset
#   tools/build.sh flash hub [port]   # compile + upload one target
#
# Board settings from libraries/README.md are pinned in the FQBNs below, so
# there is no Tools-menu state to get wrong.  Libraries come from the repo's
# pinned libraries/ folder — the versions documented there are the versions
# compiled.  Build output goes to .build/<target>/ (gitignored).
#
# Requires: arduino-cli (brew install arduino-cli) with the esp32 (3.x) and
# teensy cores installed — arduino-cli shares the Arduino IDE 2.x cores in
# ~/Library/Arduino15, so an existing IDE setup needs nothing extra.

set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LIBS="$REPO/libraries"
OUT="$REPO/.build"

FQBN_HUB="esp32:esp32:XIAO_ESP32S3:USBMode=default,CDCOnBoot=default,PartitionScheme=default_8MB,FlashSize=8M"
FQBN_DISPLAY="esp32:esp32:esp32s3:CDCOnBoot=cdc,FlashSize=16M,PartitionScheme=default_8MB,PSRAM=opi"
FQBN_AMOLED="esp32:esp32:esp32s3:CDCOnBoot=cdc,FlashSize=16M,PartitionScheme=default_8MB,PSRAM=opi"
FQBN_TEENSY="teensy:avr:teensy41:usb=serial,speed=600,opt=o2std"

sketch_for() {
    case "$1" in
        hub)     echo "$REPO/firmware/esp32_hub" ;;
        hubdemo) echo "$REPO/firmware/esp32_hub" ;;
        display) echo "$REPO/firmware/esp32_display" ;;
        amoled)  echo "$REPO/firmware/esp_mount_amoled175" ;;
        teensy)  echo "$REPO/firmware/teensy41_mount" ;;
        *)       echo "" ;;
    esac
}

fqbn_for() {
    case "$1" in
        hub)     echo "$FQBN_HUB" ;;
        hubdemo) echo "$FQBN_HUB" ;;
        display) echo "$FQBN_DISPLAY" ;;
        amoled)  echo "$FQBN_AMOLED" ;;
        teensy)  echo "$FQBN_TEENSY" ;;
    esac
}

# Extra compiler flags per target.  "hubdemo" is the ordinary hub firmware built
# with DEMO_MODE=1: it invents five mounts with stored positions so the display,
# PC app and web app can all be photographed without a rig.  Flash "hub" to go
# back to a real rig.  Demo builds never persist the pairing table.
props_for() {
    case "$1" in
        hubdemo) echo "compiler.cpp.extra_flags=-DDEMO_MODE=1" ;;
        *)       echo "" ;;
    esac
}

compile_one() {
    t="$1"
    sk="$(sketch_for "$t")"
    if [ -z "$sk" ]; then echo "unknown target: $t"; return 2; fi
    printf '── %-8s %s\n' "$t" "$(fqbn_for "$t")"
    props="$(props_for "$t")"
    if [ -n "$props" ]; then
        arduino-cli compile \
            --fqbn "$(fqbn_for "$t")" \
            --libraries "$LIBS" \
            --build-path "$OUT/$t" \
            --build-property "$props" \
            --warnings default \
            "$sk"
    else
        arduino-cli compile \
            --fqbn "$(fqbn_for "$t")" \
            --libraries "$LIBS" \
            --build-path "$OUT/$t" \
            --warnings default \
            "$sk"
    fi
}

if [ "${1:-}" = "flash" ]; then
    t="${2:?usage: build.sh flash <target> [port]}"
    port="${3:-}"
    compile_one "$t" || exit 1
    if [ -z "$port" ]; then
        port="$(arduino-cli board list 2>/dev/null | awk 'NR>1 && $1 ~ /dev/ {print $1; exit}')"
        [ -z "$port" ] && { echo "no port found — pass one explicitly"; exit 1; }
        echo "auto-detected port: $port"
    fi
    exec arduino-cli upload --fqbn "$(fqbn_for "$t")" \
        --input-dir "$OUT/$t" -p "$port" "$(sketch_for "$t")"
fi

targets="${*:-hub display amoled teensy}"
fails=""
for t in $targets; do
    compile_one "$t" || fails="$fails $t"
    echo ""
done

if [ -n "$fails" ]; then
    echo "FAILED:$fails"
    exit 1
fi
echo "ALL TARGETS COMPILE OK ($targets)"
