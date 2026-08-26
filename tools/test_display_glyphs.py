"""Every character the hub display draws must exist in its font.

"Slider: homed ✓" rendered the tick as a filled box. LVGL draws a missing glyph
that way — it is not a corrupted string, it is a character the font does not
contain.

The built-in Montserrat fonts are generated with:

    -r 0x20-0x7F,0xB0,0x2022   plus the FontAwesome symbol range

So: ASCII, the degree sign, the bullet, and LVGL's own LV_SYMBOL_* set. Nothing
else. A tick (U+2713), an en-dash, an arrow or a check mark pasted from
somewhere will all draw as boxes, and only on the hardware — the source looks
perfectly reasonable, which is why this one shipped.

LVGL has its own tick, LV_SYMBOL_OK, which maps into the FontAwesome range the
font does carry. Where a symbol exists, use it; where one does not, use ASCII.

This file scans the display source for string literals containing anything
outside those ranges. It skips comments, and skips Serial.printf, which goes to
USB where the terminal's font decides and anything is fair game.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent

SRC = REPO / "firmware/esp32_display/hub_display.cpp"
src = SRC.read_text()

# What lv_font_montserrat_* actually contains.
ASCII      = range(0x20, 0x80)
EXTRAS     = {0xB0, 0x2022}            # degree, bullet
FONTAWESOME = range(0xF000, 0xF900)    # where LV_SYMBOL_* live


def renderable(cp: int) -> bool:
    return cp in ASCII or cp in EXTRAS or cp in FONTAWESOME


def unescape(lit: str) -> str:
    """Turn \\xNN and \\uNNNN back into characters, so a UTF-8 sequence written
    as escapes is seen as the codepoint it will actually be drawn as."""
    out = bytearray()
    i = 0
    while i < len(lit):
        if lit[i:i + 2] == "\\x" and len(lit) >= i + 4:
            try:
                out.append(int(lit[i + 2:i + 4], 16))
                i += 4
                continue
            except ValueError:
                pass
        if lit[i:i + 2] == "\\u" and len(lit) >= i + 6:
            try:
                out.extend(chr(int(lit[i + 2:i + 6], 16)).encode())
                i += 6
                continue
            except ValueError:
                pass
        out.extend(lit[i].encode())
        i += 1
    return out.decode("utf-8", errors="replace")


def strip_comment(line: str) -> str:
    """Drop // comments, respecting quotes. The en-dashes in this file live in
    trailing comments explaining what an ASCII '-' means, and a scanner that
    cannot tell those apart reports three faults that are not there."""
    out, in_str, esc = [], False, False
    i = 0
    while i < len(line):
        c = line[i]
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif c == '"':
            in_str = not in_str
        elif not in_str and line[i:i + 2] == "//":
            break
        out.append(c)
        i += 1
    return "".join(out)


print("1. scanning display strings:")
offenders: dict[tuple, list] = {}
for n, raw in enumerate(src.splitlines(), 1):
    line = strip_comment(raw)
    # USB serial is not the display; the terminal's font decides there.
    if "Serial.print" in line:
        continue
    for lit in re.findall(r'"((?:[^"\\]|\\.)*)"', line):
        for ch in unescape(lit):
            if not renderable(ord(ch)):
                offenders.setdefault((ord(ch), ch), []).append(n)

for (cp, ch), lines in sorted(offenders.items()):
    print(f"   U+{cp:04X} {ch!r} at lines {lines}")
assert not offenders, (
    "the display draws characters its font does not contain; they will render "
    "as filled boxes on the hardware and look correct in the source"
)
print("   nothing outside ASCII, degree, bullet and LV_SYMBOL   OK")

# ---- 2. the tick that started this --------------------------------------
print("\n2. the homing message:")
assert "\\xE2\\x9C\\x93" not in src and "✓" not in strip_comment(src), \
    "a literal tick is back — LVGL has no glyph for U+2713"
assert '"%s: homed " LV_SYMBOL_OK' in src, \
    "the homing message no longer uses LVGL's own tick"
print("   uses LV_SYMBOL_OK                                     OK")

# ---- 3. and the ranges are the font's, not invented ---------------------
print("\n3. where those ranges come from:")
font = pathlib.Path.home() / ("Documents/Arduino/libraries/lvgl/src/font/"
                              "lv_font_montserrat_12.c")
if font.exists():
    opts = font.read_text()[:2000]
    assert "-r 0x20-0x7F,0xB0,0x2022" in opts, \
        "the installed font's build options differ from what this test assumes"
    print("   confirmed against the installed font's build options  OK")
else:
    print("   (LVGL not installed here — ranges taken from the header)")

print("\nALL CHECKS PASSED")
