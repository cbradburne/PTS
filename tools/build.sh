#!/bin/sh
# build.sh — compile-test every firmware target with arduino-cli.
#
#   tools/build.sh                # compile every target
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
# Waveshare ESP32-S3-ETH.  GPIO33-37 are broken out on this board, so the module
# is NOT octal-PSRAM; PSRAM stays disabled until the exact variant is confirmed.
FQBN_SAT="esp32:esp32:esp32s3:CDCOnBoot=cdc,FlashSize=16M,PartitionScheme=default_8MB,PSRAM=disabled"
# Hardware CDC, deliberately: TinyUSB mode can require the BOOT button to
# flash, which is no good on a unit in an enclosure.  HWCDC has no
# reboot-on-DTR behaviour to disable, so the enableReboot() the XIAO hub needs
# is simply not required here.  See the note in esp32_hub_eth.ino.
FQBN_HUBETH="esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc,FlashSize=16M,PartitionScheme=default_8MB,PSRAM=disabled"
FQBN_TEENSY="teensy:avr:teensy41:usb=serial,speed=600,opt=o2std"

sketch_for() {
    case "$1" in
        hub)     echo "$REPO/firmware/esp32_hub" ;;
        hubdemo) echo "$REPO/firmware/esp32_hub" ;;
        display) echo "$REPO/firmware/esp32_display" ;;
        amoled)  echo "$REPO/firmware/esp_mount_amoled175" ;;
        sat)     echo "$REPO/firmware/esp32_satellite" ;;
        hubeth)  echo "$REPO/firmware/esp32_hub_eth" ;;
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
        sat)     echo "$FQBN_SAT" ;;
        hubeth)  echo "$FQBN_HUBETH" ;;
        teensy)  echo "$FQBN_TEENSY" ;;
    esac
}

# Extra compiler flags per target.  "hubdemo" is the ordinary hub firmware built
# with DEMO_MODE=1: it invents five mounts with stored positions so the display,
# PC app and web app can all be photographed without a rig.  Flash "hub" to go
# back to a real rig.  Demo builds never persist the pairing table.
#
# A satellite is named at build time, because it has no UI to be renamed
# through — see SAT_NAME in esp32_satellite.ino:
#
#   SAT_NAME="Foyer" tools/build.sh flash sat
#
# Keeping it in the environment rather than the sketch means flashing three
# satellites in a row leaves no diff behind, and none of them can inherit the
# name of the one before it.
#
# DIAG=1 compiles IN the scaffolding telemetry — [RATE] on a satellite, [BCAST]
# on a hub — for a debugging session:
#
#   DIAG=1 tools/build.sh flash sat
#
# Off by default: a working rig should not be narrating itself.  Fault reports
# are never gated either way, so a quiet build still says when something breaks.
props_for() {
    _f=""
    case "$1" in
        hubdemo) _f="$_f -DDEMO_MODE=1" ;;
        # arduino-cli hands extra_flags to the compiler verbatim — it does no
        # unquoting — so these quotes are the C string's own delimiters and must
        # NOT be escaped.  Escaping them compiles happily and bakes the quote
        # characters into the SSID: PTS-"Foyer".  Use _ for spaces; a literal
        # space here would be split into two arguments.
        sat)     [ -n "${SAT_NAME:-}" ] && _f="$_f -DSAT_NAME=\"$SAT_NAME\"" ;;
    esac
    [ -n "${DIAG:-}" ] && _f="$_f -DPTS_DIAG=$DIAG"
    # BLE camera-control SPIKE (amoled only) — measures what a BLE link to the
    # camera costs the ESP-NOW link.  Not a feature; see ble_cam_spike.h.
    #   BLE_CAM=1 tools/build.sh flash amoled
    # The pairing code is typed into the serial monitor when the camera shows
    # it — it cannot be a build flag, see ble_cam_spike.h.
    [ -n "${BLE_CAM:-}" ] && _f="$_f -DBLE_CAM_SPIKE=$BLE_CAM"
    # Camera's Bluetooth name (substring). Default "BMPCC" matches "Colin BMPCC".
    [ -n "${BLE_CAM_NAME:-}" ] && _f="$_f -DBLECAM_NAME=\"$BLE_CAM_NAME\"
    [ -n "$_f" ] && echo "compiler.cpp.extra_flags=${_f# }"
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
        # macOS always exposes /dev/cu.debug-console and
        # /dev/cu.Bluetooth-Incoming-Port.  arduino-cli lists both as serial
        # ports, they sort ahead of every real board, and the old "first /dev/
        # line wins" therefore picked one of them on every Mac.  Neither is
        # ever a flash target, so drop them by name.
        cands="$(arduino-cli board list 2>/dev/null \
                 | awk 'NR>1 && $1 ~ /^\/dev\// { print $1 }' \
                 | grep -Ev 'debug-console|Bluetooth-Incoming-Port|wlan-debug|\.BLTH')"
        # A real board is a USB serial device.  If any are present, ignore
        # whatever else is still in the list.
        usb="$(printf '%s\n' "$cands" \
               | grep -E 'usbmodem|usbserial|wchusbserial|SLAB_USBtoUART')"
        [ -n "$usb" ] && cands="$usb"
        n="$(printf '%s\n' "$cands" | grep -c '[^[:space:]]')"
        if [ "$n" -eq 0 ]; then
            echo "no board port found — is it plugged in and not held by a serial monitor?"
            echo "  tools/build.sh flash $t /dev/cu.usbmodemXXXX"
            exit 1
        fi
        if [ "$n" -gt 1 ]; then
            # Several boards attached: guessing risks flashing the wrong one,
            # which on this rig means a mount running the hub's firmware.
            echo "several candidate ports — pass one explicitly:"
            printf '%s\n' "$cands" | sed 's/^/  /'
            exit 1
        fi
        port="$cands"
        echo "auto-detected port: $port"
    fi
    exec arduino-cli upload --fqbn "$(fqbn_for "$t")" \
        --input-dir "$OUT/$t" -p "$port" "$(sketch_for "$t")"
fi

targets="${*:-hub hubeth display amoled sat teensy}"
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
