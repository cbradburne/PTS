"""One hub firmware, built for two boards — and it stays one.

2026-09-26: the XIAO hub (esp32_hub/esp32_hub.ino) had fallen a month behind
the Ethernet hub — no recovery ladders, no stall detector, no hub name — which
is what two copies of a hub always do.  Now firmware/esp32_hub_eth is the only
hub.  Compiled for the Seeed Studio XIAO ESP32S3 it is the WiFi-only "simple
kit" hub (HUB_WIRED 0); for the Waveshare ESP32-S3-ETH it is the wired hub with
Ethernet and satellites (HUB_WIRED 1).  The Ethernet build was fingerprinted
before and after the change and is identical: every symbol and section size.

`tools/build.sh hub hubeth` compiles both; this test guards what cannot be seen
without flashing.

WHAT THIS TEST IS PROTECTING.

  the XIAO has no wire        with HUB_WIRED 0 nothing brings up Ethernet or a
                              satellite listener: the W5500's pins are not the
                              XIAO's, and no satellite could reach it anyway
  the wired hub keeps it      with HUB_WIRED 1 all of that is still there
  the board chooses           the XIAO board selects 0, everything else 1, and
                              -DHUB_WIRED overrides
  one of everything           no second hub sketch, one web_app.h, one
                              hub_types.h — a stale second copy of the latter
                              sat in the hub folder for eight weeks
  both scripts agree          build.sh and build.bat build "hub" from the one
                              source with the same XIAO FQBN — Hardware CDC, and
                              CDCOnBoot=default, which on the XIAO means ENABLED
                              (its menu is the reverse of the generic S3's)

Run directly, or via tools/run_tests.sh with the rest.
"""
import pathlib
import re
import subprocess

REPO = pathlib.Path(__file__).resolve().parent.parent
HUB = REPO / "firmware/esp32_hub_eth/esp32_hub_eth.ino"
SRC = HUB.read_text(encoding="utf-8")


def as_built(src: str, wired: int) -> str:
    """The code a HUB_WIRED=<wired> build compiles, comments removed.

    Only HUB_WIRED conditionals are decided; any other #if keeps both branches,
    which can only make the checks below stricter."""
    out, stack = [], []          # stack of (is_hub_cond, branch_active, parent_active)
    for line in src.splitlines():
        s = line.strip()
        active = stack[-1][1] if stack else True
        if re.match(r"#\s*if\s+HUB_WIRED\b", s):
            stack.append((True, active and wired == 1, active)); continue
        if re.match(r"#\s*if\s+!\s*HUB_WIRED\b", s):
            stack.append((True, active and wired == 0, active)); continue
        if re.match(r"#\s*(if|ifdef|ifndef)\b", s):
            stack.append((False, active, active)); continue
        if re.match(r"#\s*else\b", s) and stack:
            hub, branch, parent = stack[-1]
            stack[-1] = (hub, (parent and not branch) if hub else parent, parent); continue
        if re.match(r"#\s*endif\b", s) and stack:
            stack.pop(); continue
        if active:
            out.append(line)
    code = "\n".join(out)
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    return re.sub(r"//[^\n]*", "", code)


ETH_ONLY = {
    "Ethernet bring-up":         r"\beth_begin\s*\(",
    "the wire's link report":    r"\beth_report_once_if_down\s*\(",
    "the satellite listener":    r"\b_sat_server\.begin\s*\(",
    "the Ethernet interface":    r"\bETH\.",
    "the W5500 header":          r'#include\s+"\.\./shared/board_eth\.h"',
}

# ---- 1. the XIAO build has no wire ------------------------------------------
print("1. HUB_WIRED 0 (the XIAO):")
xiao = as_built(SRC, 0)
for what, pat in ETH_ONLY.items():
    assert not re.search(pat, xiao), \
        f"the XIAO build still compiles {what} ({pat}) — guard it with #if HUB_WIRED"
assert "Satellites : none in this build" in xiao, \
    "the XIAO build no longer says at boot that it has no satellites"
assert "Satellites : none in this build" not in as_built(SRC, 1), \
    "the wired build claims to have no satellites"
print("   no Ethernet, no satellite listener   OK")

# ---- 2. the wired build keeps it ----------------------------------------------
print("\n2. HUB_WIRED 1 (the Waveshare ESP32-S3-ETH):")
wired = as_built(SRC, 1)
for what, pat in ETH_ONLY.items():
    assert re.search(pat, wired), f"the wired build lost {what} ({pat})"
print("   Ethernet and the satellite listener still there   OK")

# ---- 3. the board chooses -------------------------------------------------------
print("\n3. the switch:")
m = re.search(r"#ifndef HUB_WIRED\s*\n\s*#if defined\(ARDUINO_XIAO_ESP32S3\)\s*\n"
              r"\s*#define HUB_WIRED 0\s*\n\s*#else\s*\n\s*#define HUB_WIRED 1\s*\n"
              r"\s*#endif\s*\n\s*#endif", SRC)
assert m, "HUB_WIRED is no longer chosen from the board (XIAO -> 0, else 1) " \
          "with a -DHUB_WIRED override"
assert SRC.index("#define HUB_WIRED 0") < SRC.index('#include "../shared/board_eth.h"'), \
    "HUB_WIRED is decided after the Ethernet header it guards"
print("   XIAO -> 0, anything else -> 1, overridable   OK")

# ---- 4. one of everything ---------------------------------------------------------
print("\n4. one copy of each:")
files = subprocess.run(["git", "ls-files", "firmware"], cwd=REPO, capture_output=True,
                       text=True, check=True).stdout.split()
assert "firmware/esp32_hub/esp32_hub.ino" not in files, \
    "the old XIAO hub sketch is back — there must be one hub firmware"
for name in ("web_app.h", "hub_types.h"):
    copies = [f for f in files if f.endswith("/" + name)]
    assert copies == [f"firmware/shared/{name}"], f"{name} copies: {copies}"
sat = (REPO / "firmware/esp32_satellite/esp32_satellite.ino").read_text(encoding="utf-8")
assert '#include "../shared/web_app.h"' in SRC and '#include "../shared/web_app.h"' in sat
assert '#include "../shared/hub_types.h"' in SRC
print("   one hub sketch, one web_app.h, one hub_types.h, all included from shared/   OK")

# ---- 5. both build scripts agree ----------------------------------------------
print("\n5. the build scripts:")
sh = (REPO / "tools/build.sh").read_text(encoding="utf-8")
bat = (REPO / "tools/build.bat").read_text(encoding="utf-8")
for t in ("hub", "hubdemo", "hubeth"):
    assert re.search(rf'^\s*{t}\)\s+echo "\$REPO/firmware/esp32_hub_eth"', sh, re.M), \
        f"build.sh builds '{t}' from somewhere other than the one hub source"
fq = re.search(r'^FQBN_HUB="([^"]+)"', sh, re.M).group(1)
assert fq.startswith("esp32:esp32:XIAO_ESP32S3:"), fq
assert "USBMode=hwcdc" in fq, f"the XIAO hub is not on Hardware CDC: {fq}"
assert "CDCOnBoot=default" in fq, \
    f"CDCOnBoot on the XIAO must be 'default' — its ENABLED key: {fq}"
bhub = re.search(r'if /i "%~1"=="hub" \(\s*set "FQBN=([^"]+)"\s*set "SKETCH=([^"]+)"'
                 r'\s*set "SKETCHNAME=([^"]+)"', bat)
assert bhub, "build.bat has no hub target"
assert bhub.group(1) == fq, f"build.bat's XIAO FQBN differs from build.sh's:\n  {bhub.group(1)}\n  {fq}"
assert bhub.group(2) == r"firmware\esp32_hub_eth" and bhub.group(3) == "esp32_hub_eth.ino", \
    f"build.bat builds hub from {bhub.group(2)}\\{bhub.group(3)}"
print("   hub, hubdemo and hubeth from the one source; one XIAO FQBN   OK")

# The inverted key, checked against the board definition itself where it is
# installed — it is the fact the FQBN above depends on.
boards = sorted(pathlib.Path.home().glob(
    "Library/Arduino15/packages/esp32/hardware/esp32/*/boards.txt")) + sorted(
    pathlib.Path.home().glob(".arduino15/packages/esp32/hardware/esp32/*/boards.txt"))
if boards:
    txt = boards[-1].read_text(encoding="utf-8", errors="replace")
    assert "XIAO_ESP32S3.menu.CDCOnBoot.default.build.cdc_on_boot=1" in txt, \
        f"{boards[-1]}: the XIAO's CDCOnBoot 'default' is no longer ENABLED — " \
        "FQBN_HUB needs re-checking"
    print(f"   and the installed core agrees: XIAO CDCOnBoot 'default' = Enabled   OK")

print("\nALL CHECKS PASSED")
