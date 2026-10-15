"""Every Set Ref 0/0 button sends what the Teensy reads as 0/0, and the web app
says what the mount made of it.

The operator, 2026-10-15: a look-at location would not store; the slider never
moved.  The mount was refusing every calibration with NACK_NO_REF, since a
flash the day before had cleared its session reference, and "I repeatedly
pressed Set 0/0 on the webapp".  The Teensy takes CMD_SET_REF's subject_id
SET_REF_MANUAL (0xFF) as "here is 0/0"; 0-7 derive the reference from that
stored subject and refuse one that is not stored.  The web app's button sent 0,
and its encoder masked to 0-7 so it could not have sent 0xFF anyway: its Set
Ref 0/0 had never set anything, since RC2.  Both the refusal and the silence
were invisible: the web app ignored NACK, and the hub did not pass NACK or
REF_CONFIRMED to it.

  1. one value everywhere: protocol.h's SET_REF_MANUAL is what the Teensy's
     manual branch tests, what the 7" display and the PC app send, and — read
     from the real page in WebKit — what the web app's button sends;
  2. the web app: subjects 0-7 still go as themselves, REF_CONFIRMED says
     "reference set", NACK_NO_REF says to Set Ref first, and another refusal
     says nothing;
  3. the hub forces REF_CONFIRMED and NACK through to the web app; a
     satellite passes everything but STATUS through already.

tools/wkshot.swift does the rendering (macOS: swiftc and WebKit); without it
section 1 checks the page's source instead.  Run directly, or via
tools/run_tests.sh with the rest.
"""
import json, pathlib, re, shutil, subprocess, sys, tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
FW = REPO / "firmware"
PROTO = (FW / "shared/protocol.h").read_text()
WEB = (FW / "shared/web_app.h").read_text()
TEENSY = (FW / "teensy41_mount/teensy41_mount.ino").read_text()
DISPLAY = (FW / "esp32_display/hub_display.cpp").read_text()
HUB = (FW / "esp32_hub_eth/esp32_hub_eth.ino").read_text()
SAT = (FW / "esp32_satellite/esp32_satellite.ino").read_text()
PC_DIALOG = (REPO / "pc_app/ui/dialogs/config_dialog.py").read_text()


def strip(code):
    return re.sub(r"//[^\n]*", "", code)


def body(code, head):
    """The function starting at head, to its closing brace in column 0."""
    i = code.index(head)
    return code[i:code.index("\n}\n", i)]


m = re.search(r"#define\s+SET_REF_MANUAL\s+(0x[0-9A-Fa-f]+|\d+)", PROTO)
assert m, "protocol.h does not name the manual-reference subject_id (SET_REF_MANUAL)"
MANUAL = int(m.group(1), 0)

# ---- 1. one value everywhere -----------------------------------------------------
print(f"1. Set Ref 0/0 is subject_id 0x{MANUAL:02X} (SET_REF_MANUAL) everywhere:")
setref = strip(TEENSY)[strip(TEENSY).index("case CMD_SET_REF: {"):]
setref = setref[:setref.index("case CMD_SET_SLIDER_MOVE")]
t = re.search(r"if \(subj_id == (0x[0-9A-Fa-f]+|\d+|SET_REF_MANUAL)\)", setref)
assert t, "the Teensy's CMD_SET_REF has no manual (0/0) branch on a subject_id"
assert t.group(1) == "SET_REF_MANUAL" or int(t.group(1), 0) == MANUAL, \
    f"the Teensy reads 0/0 as subject_id {t.group(1)}, protocol.h says 0x{MANUAL:02X}"
print("   the Teensy's manual branch tests it                                OK")
d = re.search(r"uint8_t subj = (0x[0-9A-Fa-f]+|\d+|SET_REF_MANUAL);",
              body(strip(DISPLAY), "static void ev_manual_ref("))
assert d and (d.group(1) == "SET_REF_MANUAL" or int(d.group(1), 0) == MANUAL), \
    "the 7\" display's Set Ref 0/0 does not send it"
print("   the 7\" display's Set Ref 0/0 sends it                              OK")
sys.path.insert(0, str(REPO / "pc_app"))
from comms.protocol import encode_set_ref  # noqa: E402
c = re.findall(r"send_set_ref\(mount_id, (0x[0-9A-Fa-f]+|\d+)\)", PC_DIALOG)
assert c and all(int(v, 0) == MANUAL for v in c), \
    f"the PC config dialog's Set Ref sends {c}, not 0x{MANUAL:02X}"
assert encode_set_ref(MANUAL) == bytes([MANUAL]), \
    f"the PC encodes it as {encode_set_ref(MANUAL)!r}: masked?"
print("   the PC app's Set Ref sends it, unmasked                            OK")

SETUP = r"""
const _sent = [];
wsSend = b => _sent.push(Array.from(b));
const toastText = () => (_padToastEl ? _padToastEl.textContent : '');
function feed(cam, cmd, bytes) { _onOnePkt(buildPkt(cam, cmd, new Uint8Array(bytes)), 0); }
const R = window.__results = {stepErrors: []};
window.__measure = () => Object.assign({errors: window.__errors}, window.__results);
function step(f) { try { f(); } catch (e) { R.stepErrors.push(String(e)); } }
step(() => {
    feed(1, CMD_STATUS, [0, FLAG_HAS_SLIDER, 2, 2, 0, 0, 0, 0, 0xFF, 0xFF, 0]);
    setExtView(true); showExtPage('config');
    _sent.length = 0;
    document.getElementById('ext-cfg-ref-1').click();
    R.button = _sent.filter(b => b[6] === CMD_SET_REF).map(b => [b[3], b[2] - 4, b[7]]);
});
step(() => { R.subj = [0, 3, 7, SET_REF_MANUAL].map(s => Array.from(mkSetRef(1, s))[7]); });
step(() => {
    feed(1, CMD_REF_CONFIRMED, [0, 0, 0, 0, 0, 0, 0, 0]);  R.confirmed = toastText();
    feed(1, CMD_NACK, [0, 9, NACK_NO_REF]);               R.noRef = toastText();
    // Something else on screen first, so a BUSY that wrongly toasted the
    // NO_REF text could not pass for "nothing new".
    feed(1, CMD_REF_CONFIRMED, [0, 0, 0, 0, 0, 0, 0, 0]);
    feed(1, CMD_NACK, [0, 9, 0x05]);                      R.busy = toastText();
});
"""

if sys.platform == "darwin" and shutil.which("swiftc"):
    pm = re.search(r'R"rawhtml\((.*)\)rawhtml"', WEB, re.S)
    assert pm, "the page's raw string not found in web_app.h"
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="wksetref-"))
    (tmp / "webapp.html").write_text(pm.group(1))
    (tmp / "setup.js").write_text(SETUP)
    r = subprocess.run(["swiftc", "-O", str(REPO / "tools/wkshot.swift"), "-o", str(tmp / "wkshot")],
                       capture_output=True, text=True)
    assert r.returncode == 0, "wkshot.swift does not compile:\n" + r.stderr[-2000:]
    rr = subprocess.run([str(tmp / "wkshot"), str(tmp / "webapp.html"), "1133", "720", "-",
                         str(tmp / "setup.js")], capture_output=True, text=True, timeout=60)
    shutil.rmtree(tmp, ignore_errors=True)
    assert rr.returncode == 0, f"wkshot failed ({rr.returncode}): {rr.stderr.strip()}"
    got = json.loads(rr.stdout.strip().splitlines()[-1])
    assert not got["errors"], f"the page threw: {got['errors']}"

    class Steps(dict):
        def __missing__(self, key):
            raise AssertionError(f"the step that reads {key!r} did not run: {got['stepErrors']}")
    w = Steps(got)

    assert w["button"] == [[1, 1, MANUAL]], \
        f"the web app's Set Ref 0/0 sent [mount, length, subject_id] {w['button']}, " \
        f"want [[1, 1, 0x{MANUAL:02X}]]"
    print("   the web app's Set Ref 0/0 button sends it (clicked, in WebKit)     OK")

    # ---- 2. the web app ------------------------------------------------------
    print("\n2. the web app:")
    assert w["subj"] == [0, 3, 7, MANUAL], f"mkSetRef sent subject_ids {w['subj']}"
    print("   subjects 0, 3 and 7 go as themselves, the sentinel unmasked         OK")
    assert "reference set" in w["confirmed"], \
        f"REF_CONFIRMED showed {w['confirmed']!r}, want it to say the reference is set"
    print(f"   REF_CONFIRMED: \"{w['confirmed']}\"                              OK")
    assert "Set Ref 0/0 first" in w["noRef"], \
        f"NACK_NO_REF showed {w['noRef']!r}, want it to say Set Ref 0/0 first"
    print(f"   NACK_NO_REF: \"{w['noRef']}\"   OK")
    assert w["busy"] == w["confirmed"], \
        f"a BUSY refusal showed {w['busy']!r}: only NO_REF has an action"
    print("   another refusal (BUSY) shows nothing new                           OK")
else:
    print("   swiftc and WebKit not here: the web app's button checked from source")
    click = re.search(r"refBtn\.addEventListener\('click'.*?mkSetRef\(i, (\w+)\)", WEB)
    assert click and click.group(1) == "SET_REF_MANUAL", "the web app's button does not send it"
    print("\n2. the web app: not run here (needs WebKit)")

# ---- 3. the way back to the web app ----------------------------------------------
print("\n3. the hub and satellites pass the answers on:")
relay = strip(HUB)
assert re.search(r"if \(pkt\.cmd == CMD_REF_CONFIRMED \|\| pkt\.cmd == CMD_NACK\)\s*ws_force = true;",
                 relay), "the hub does not force REF_CONFIRMED and NACK through to the web app"
print("   the hub forces REF_CONFIRMED and NACK through to the web app      OK")
assert "if (cmd != CMD_STATUS) return true;" in body(strip(SAT), "static bool ws_should_send("), \
    "the satellite no longer passes everything but STATUS through"
print("   a satellite passes everything but STATUS through                  OK")

print("\nALL CHECKS PASSED")
