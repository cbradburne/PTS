"""The web app's satellite-name decode, checked against the PC app's.

Both clients read the same CMD_SAT_NAMES broadcast and must agree about it —
the web app used to say "via SAT 2" where the PC app said "via Foyer", for the
same rig at the same moment, because this page was the one client ignoring the
names the hub had been sending all along.

The JavaScript is EXTRACTED FROM web_app.h and run in node, not transcribed
here. A transcription would only prove that two copies of my own understanding
agree; running the real thing proves the page a browser is served does.

Skips cleanly when node is absent — that is a missing tool, not a failure.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))
import re, json, shutil, subprocess, tempfile

from comms.protocol import (decode_sat_names, Cmd, SAT_SLOTS, SAT_NAME_LEN,
                            SAT_NAMES_PAYLOAD_LEN)

WEB = (REPO / "firmware/esp32_hub/web_app.h").read_text()

# ---- 1. the constants the browser slices with ------------------------------
# Wrong stride here and every name comes out mangled, so these are not allowed
# to drift from protocol.py.
js = {k: int(v) for k, v in
      re.findall(r"const (SAT_SLOTS|SAT_NAME_LEN)\s*=\s*(\d+);", WEB)}
m = re.search(r"const CMD_SAT_NAMES\s*=\s*(0x[0-9A-Fa-f]+);", WEB)
assert m, "CMD_SAT_NAMES not defined in web_app.h"
print("1. web app constants vs protocol.py:")
assert js.get("SAT_SLOTS") == SAT_SLOTS, (js.get("SAT_SLOTS"), SAT_SLOTS)
assert js.get("SAT_NAME_LEN") == SAT_NAME_LEN, (js.get("SAT_NAME_LEN"), SAT_NAME_LEN)
assert int(m.group(1), 16) == int(Cmd.SAT_NAMES), (m.group(1), Cmd.SAT_NAMES)
print(f"   SAT_SLOTS={SAT_SLOTS} SAT_NAME_LEN={SAT_NAME_LEN} "
      f"CMD_SAT_NAMES={m.group(1)}        OK")

# ---- 2. the page must actually USE the names -------------------------------
# The decode existing while the row still prints "via SAT n" is the exact bug
# this file exists to prevent coming back.
assert "satNames[mountRoute[i - 1]]" in WEB, \
    "the mount row is not using satNames — it will still print 'via SAT n'"
assert "'via SAT '" not in WEB.replace("|| ('SAT ' + mountRoute[i - 1])", ""), \
    "a bare 'via SAT ' literal is still being rendered somewhere"
print("2. mount row renders the name, number only as fallback  OK")


def payload(names):
    """Build the payload exactly as the hub's send_sat_names() does."""
    buf = bytearray(SAT_NAMES_PAYLOAD_LEN)
    for slot, n in names.items():
        b = n.encode()[:SAT_NAME_LEN - 1]
        buf[(slot - 1) * SAT_NAME_LEN:(slot - 1) * SAT_NAME_LEN + len(b)] = b
    return bytes(buf)


CASES = [
    {1: "Foyer", 2: "Basement"},     # the actual rig
    {2: "Basement"},                 # a gap in the slots
    {},                              # nothing has named itself yet
    {1: "Foyer", 6: "Stage Left"},   # last slot, and a space in the name
    {1: "TwelveCharXY"},             # exactly SAT_NAME_MAX, no NUL to find
    {3: "Café"},                # multi-byte UTF-8
]

if not shutil.which("node"):
    print("\n3. node not installed — skipping the browser-side run.")
    print("   (the constants and rendering checks above still ran)")
    print("\nALL CHECKS PASSED")
    raise SystemExit(0)

# ---- 3. the REAL JavaScript, run in node -----------------------------------
blk = re.search(r"( *)if \(cmd === CMD_SAT_NAMES.*?\n\1\}", WEB, re.S)
assert blk, "CMD_SAT_NAMES handler not found in web_app.h"
lines = blk.group(0).splitlines()
body = "\n".join(l for l in lines[1:-1] if "_extActive" not in l)

harness = f"""
const SAT_SLOTS = {SAT_SLOTS}, SAT_NAME_LEN = {SAT_NAME_LEN};
const cases = JSON.parse(process.argv[2]);
const out = [];
for (const arr of cases) {{
    const buf = Uint8Array.from(arr);
    const off = 0;
    let satNames = {{}};
{body}
    out.push(satNames);
}}
console.log(JSON.stringify(out));
"""
tmp = pathlib.Path(tempfile.mkdtemp()) / "h.js"
tmp.write_text(harness)

r = subprocess.run(
    ["node", str(tmp), json.dumps([list(b"\x00" * 7 + payload(c)) for c in CASES])],
    capture_output=True, text=True)
assert r.returncode == 0, f"node failed:\n{r.stderr[:600]}"

print("\n3. web_app.h JavaScript in node vs the PC app decoder:")
for case, got in zip(CASES, json.loads(r.stdout)):
    want = decode_sat_names(payload(case))
    got = {int(k): v for k, v in got.items()}
    assert got == want, (case, got, want)
    print(f"   {str(case):32} -> {got}")
print("   both clients decode identically                      OK")

print("\nALL CHECKS PASSED")
