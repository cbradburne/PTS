#!/bin/sh
# Host-side test suites.  No rig, no hardware — everything here runs on a laptop.
#
#   tools/run_tests.sh          run everything
#   tools/run_tests.sh cam      run only the suites whose name contains "cam"
#
# What these are for: several of them read the FIRMWARE SOURCE and check the PC
# app against it — the byte literals in esp32_hub_eth.ino, the section enum in
# esp32_satellite.ino whose ORDER is the wire encoding.  That is deliberate.
# Both halves of this rig have been wrong at the same time and agreed with each
# other, which is exactly what a test comparing one copy against another copy
# cannot catch.  Checking against the source that actually ships can.
#
# The protocol check (tools/check_protocol.py) runs separately, on every commit,
# via .githooks/pre-commit.

cd "$(git rev-parse --show-toplevel)" 2>/dev/null || cd "$(dirname "$0")/.." || exit 1

FILTER="$1"
fail=0
ran=0

# PyQt6 drives the dialog suites.  A machine without it is not broken — the
# iMac runs the app, this may not be it — so say so and skip rather than
# reporting a failure that means nothing.
if ! python3 -c "import PyQt6" 2>/dev/null; then
    echo "PyQt6 not installed — the camera-dialog suites need it."
    echo "Running only the suites that do not."
    NO_QT=1
fi

for t in tools/test_*.py; do
    name=$(basename "$t" .py)
    case "$name" in
        test_companion_osc) continue ;;          # removed with the PC OSC server
    esac
    if [ -n "$FILTER" ]; then
        case "$name" in *"$FILTER"*) ;; *) continue ;; esac
    fi
    if [ -n "$NO_QT" ]; then
        case "$name" in test_cam_*) continue ;; esac
    fi
    ran=$((ran + 1))
    out=$(python3 "$t" 2>&1 | grep -v "qt.qpa")
    if printf '%s' "$out" | tail -1 | grep -q "ALL CHECKS PASSED"; then
        printf "  %-20s ok\n" "$name"
    else
        printf "  %-20s FAILED\n" "$name"
        printf '%s\n' "$out" | tail -20 | sed 's/^/      /'
        fail=$((fail + 1))
    fi
done

echo ""
if [ "$fail" -eq 0 ]; then
    echo "OK — $ran suite(s) passed"
else
    echo "$fail of $ran suite(s) FAILED"
fi
exit $fail
