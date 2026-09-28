"""The camera scan keeps what it is looking for, and nothing else.

2026-09-28, the Foyer: cam5's camera switched off from 11:39 to 13:42, and the
mount scanning for it every ten seconds.  The BLE library's BLEScan kept a heap
object for every advertiser it heard — 130-210 phones a scan, 40-67 KB — in the
internal RAM the WiFi driver sends from.  Internal RAM reached 0.0 KB, the NO_MEM
ladder ran 194 times, and about one command in thirty was lost, until the camera
came back.  Freeing each scan once it ended could not help while one was running.

The scan is NimBLE's own discovery now (ble_camera.h, bc_disc_event): each report
is read in place, what the choice needs goes into a dozen fixed slots, and the
rest is counted and dropped.

WHAT THIS TEST IS PROTECTING.

  the same listening        active, 80 ms in every 100, five seconds, each
                            device once — exactly what BLEScan was given
  the camera still found    its service from the advertisement and its name
                            from the scan response, merged into one entry; a
                            complete name beats a shortened one
  nothing kept per report   a crowd of 300 allocates nothing and fills twelve
                            slots; the crowd is still counted, for the health
                            report
  room for what matters     BLEScan's copy kept the first twelve devices it
                            heard, so in a crowd whether a mount's own camera
                            made the list was luck.  Full, the list now makes
                            room for the bonded camera, or anything that looks
                            like a Blackmagic camera, by dropping the weakest
                            device that is neither
  a scan always ends        completed, stopped (a cancel sends no completion,
                            so the stop says so itself), or failed to start;
                            reports after the end are ignored
  the address as before     BLEAddress::toString()'s text, which bc_connect()
                            parses back into the same bytes

The scan code is lifted out of ble_camera.h and run against a stand-in NimBLE.

Run directly, or via tools/run_tests.sh with the rest.
"""
import pathlib
import re
import shutil
import subprocess
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
BLE = (REPO / "firmware/esp_mount_amoled175/ble_camera.h").read_text()


def braced(i: int) -> str:
    """From BLE[i] to the brace that closes the first one opened after it."""
    j = BLE.index("{", i)
    depth = 0
    for k in range(j, len(BLE)):
        depth += {"{": 1, "}": -1}.get(BLE[k], 0)
        if depth == 0:
            return BLE[i:k + 1]
    raise AssertionError("unbalanced braces")


if not shutil.which("c++"):
    print("no C++ compiler — skipping the behavioural check.")
    print("\nALL CHECKS PASSED")
    raise SystemExit(0)

cand = BLE[BLE.index("#define BC_MAX_CAND"):BLE.index("static uint8_t _bc_ncand = 0;")]
cand += "static uint8_t _bc_ncand = 0;\n"
cam_name = re.search(r"#ifndef CAM_NAME\n#define CAM_NAME.*\n#endif", BLE).group(0)
cam_svc = re.search(r"^#define CAM_SERVICE .*$", BLE, re.M).group(0)
scan = BLE[BLE.index("static uint8_t _bc_svc_le[16];"):]
scan = scan[:scan.index("static bool bc_scan_stop() {")] + \
    braced(BLE.index("static bool bc_scan_stop() {"))

RIG = r"""
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <new>
// ---- a stand-in NimBLE: the types and calls the scan uses ----
typedef struct { uint8_t type; uint8_t val[6]; } ble_addr_t;
struct ble_gap_disc_desc { uint8_t event_type; uint8_t length_data; ble_addr_t addr;
                           int8_t rssi; const uint8_t *data; ble_addr_t direct_addr; };
struct ble_gap_event { uint8_t type;
                       union { struct ble_gap_disc_desc disc; struct { int reason; } disc_complete; }; };
struct ble_gap_disc_params { uint16_t itvl; uint16_t window; uint8_t filter_policy;
                             uint8_t limited:1; uint8_t passive:1; uint8_t filter_duplicates:1; };
#define BLE_GAP_EVENT_DISC                 7
#define BLE_GAP_EVENT_DISC_COMPLETE        8
#define BLE_HCI_ADV_RPT_EVTYPE_ADV_IND     0
#define BLE_HCI_ADV_RPT_EVTYPE_DIR_IND     1
#define BLE_HCI_ADV_RPT_EVTYPE_SCAN_IND    2
#define BLE_HCI_ADV_RPT_EVTYPE_NONCONN_IND 3
#define BLE_HCI_ADV_RPT_EVTYPE_SCAN_RSP    4
#define BLE_HCI_SCAN_FILT_NO_WL            0
#define BLE_OWN_ADDR_PUBLIC                0
#define BLE_GAP_SCAN_ITVL_MS(t)            ((t) * 1000 / 625)
#define BLE_GAP_SCAN_WIN_MS(t)             ((t) * 1000 / 625)
static int (*g_cb)(ble_gap_event *, void *) = nullptr;
static ble_gap_disc_params g_p; static int32_t g_dur; static int g_own = -1;
static bool g_active = false; static int g_start_rc = 0;
static int ble_gap_disc(uint8_t own, int32_t dur, const ble_gap_disc_params *p,
                        int (*cb)(ble_gap_event *, void *), void *) {
    if (g_start_rc) return g_start_rc;
    g_own = own; g_dur = dur; g_p = *p; g_cb = cb; g_active = true;
    return 0;
}
static int ble_gap_disc_cancel() { if (!g_active) return 2; g_active = false; return 0; }
static bool ble_gap_disc_active() { return g_active; }
static ble_addr_t g_bonded[8]; static int g_nbonded = 0;
static int ble_store_util_bonded_peers(ble_addr_t *out, int *n, int max) {
    for (int i = 0; i < g_nbonded && i < max; i++) out[i] = g_bonded[i];
    *n = g_nbonded;
    return 0;
}
static uint32_t _millis = 1000;
static uint32_t millis() { return _millis; }
static struct {
    template <class... A> void printf(const char *, A...) {}
    void print(const char *) {}
    void println(const char * = "") {}
} Serial;
// Every C++ allocation, so a report handler that allocates is caught.
static long g_news = 0;
void *operator new(size_t n) { g_news++; void *p = malloc(n ? n : 1); if (!p) throw std::bad_alloc(); return p; }
void *operator new[](size_t n) { g_news++; void *p = malloc(n ? n : 1); if (!p) throw std::bad_alloc(); return p; }
void operator delete(void *p) noexcept { free(p); }
void operator delete[](void *p) noexcept { free(p); }
void operator delete(void *p, size_t) noexcept { free(p); }
void operator delete[](void *p, size_t) noexcept { free(p); }
// ---- lifted from ble_camera.h ----
@CAMSVC@
@CAND@
@CAMNAME@
@SCAN@
// ---- the rig ----
static uint8_t buf[64];
static void report(const uint8_t v[6], uint8_t evt, int8_t rssi, const uint8_t *d, uint8_t n,
                   uint8_t type = 0) {
    ble_gap_event e = {};
    e.type = BLE_GAP_EVENT_DISC;
    e.disc.event_type = evt; e.disc.rssi = rssi; e.disc.addr.type = type;
    memcpy(e.disc.addr.val, v, 6);
    memcpy(buf, d, n);               // the report's bytes live only for the call
    e.disc.data = buf; e.disc.length_data = n;
    g_cb(&e, nullptr);
    memset(buf, 0xEE, sizeof(buf));  // ...and are gone after it
}
static void complete() {
    ble_gap_event e = {};
    e.type = BLE_GAP_EVENT_DISC_COMPLETE;
    g_active = false;
    g_cb(&e, nullptr);
}
static int find(const uint8_t v[6]) {
    for (int i = 0; i < _bc_ncand; i++) if (!memcmp(_bc_cand[i].nat, v, 6)) return i;
    return -1;
}
static uint32_t rng = 12345;
static uint8_t rnd() { rng = rng * 1103515245u + 12345u; return (uint8_t)(rng >> 16); }
static void phone(int i, int8_t rssi, bool with_rsp = true) {
    uint8_t v[6] = {rnd(), rnd(), rnd(), rnd(), rnd(), (uint8_t)(0x40 | (i & 0x3F))};
    uint8_t adv[] = {0x02, 0x01, 0x1A, 0x03, 0x03, 0x6F, 0xFD,        // flags, a 16-bit UUID
                     0x05, 0xFF, 0x4C, 0x00, 0x10, 0x05};              // manufacturer data
    report(v, BLE_HCI_ADV_RPT_EVTYPE_ADV_IND, rssi, adv, sizeof(adv), 1);
    if (with_rsp) {
        char nm[16]; int n = snprintf(nm, sizeof nm, "iPhone %d", i);
        uint8_t rsp[20] = {(uint8_t)(n + 1), 0x09};
        memcpy(rsp + 2, nm, n);
        report(v, BLE_HCI_ADV_RPT_EVTYPE_SCAN_RSP, rssi, rsp, (uint8_t)(n + 2), 1);
    }
}
int main() {
    _bc_svc_ok = bc_svc_parse();
    printf("svc %d %02x %02x\n", (int)_bc_svc_ok, _bc_svc_le[0], _bc_svc_le[15]);

    // The camera: its service in the advertisement, its (shortened) name in the
    // scan response, as the BMPCC4K on the rig sends them.
    const uint8_t cam[6] = {0xdf, 0x50, 0xb4, 0x9f, 0xfd, 0x90};
    uint8_t cam_adv[3 + 18] = {0x02, 0x01, 0x06, 0x11, 0x07};
    memcpy(cam_adv + 5, _bc_svc_le, 16);
    const uint8_t cam_rsp[] = {0x0C, 0x08, 'C','o','l','i','n',' ','B','M','P','C','C'};

    // A. the listening, and the camera among 300 phones
    bool started = bc_scan_start();
    printf("params %d %d %d %u %u %u %u %ld\n", (int)started, g_own, (int)_bc_scanning,
           g_p.itvl, g_p.window, (unsigned)g_p.passive, (unsigned)g_p.filter_duplicates,
           (long)g_dur);
    printf("filter %u %u\n", (unsigned)g_p.filter_policy, (unsigned)g_p.limited);
    long before = g_news;
    report(cam, BLE_HCI_ADV_RPT_EVTYPE_ADV_IND, -45, cam_adv, sizeof(cam_adv));
    int c = find(cam);
    printf("adv %d %d %d \"%s\"\n", c, (int)_bc_cand[c].svc, (int)_bc_cand[c].named, _bc_cand[c].name);
    report(cam, BLE_HCI_ADV_RPT_EVTYPE_SCAN_RSP, -44, cam_rsp, sizeof(cam_rsp));
    printf("rsp %d %d %d %d \"%s\" %d\n", find(cam), (int)_bc_cand[c].svc, (int)_bc_cand[c].named,
           (int)_bc_cand[c].name_full, _bc_cand[c].name, _bc_cand[c].rssi);
    printf("addr %s %u\n", _bc_cand[c].addr, (unsigned)_bc_cand[c].adv_type);
    unsigned v[6]; uint8_t back[6];
    sscanf(_bc_cand[c].addr, "%x:%x:%x:%x:%x:%x", &v[0], &v[1], &v[2], &v[3], &v[4], &v[5]);
    for (int i = 0; i < 6; i++) back[i] = (uint8_t)v[5 - i];    // as bc_connect() does
    printf("roundtrip %d\n", (int)!memcmp(back, cam, 6));
    for (int i = 0; i < 300; i++) phone(i, (int8_t)(-60 - (i % 30)));
    for (int i = 0; i < 5; i++)
        report(cam, BLE_HCI_ADV_RPT_EVTYPE_ADV_IND, -40, cam_adv, sizeof(cam_adv));
    printf("crowd %d %d %d %ld\n", (int)_bc_ncand, (int)_bc_heard, find(cam), g_news - before);
    // A complete name beats the shortened one; a shortened one never replaces it.
    const uint8_t full[] = {0x0E, 0x09, 'C','o','l','i','n',' ','B','M','P','C','C','4','K'};
    report(cam, BLE_HCI_ADV_RPT_EVTYPE_SCAN_RSP, -40, full, sizeof(full));
    report(cam, BLE_HCI_ADV_RPT_EVTYPE_SCAN_RSP, -40, cam_rsp, sizeof(cam_rsp));
    printf("name \"%s\" %d %d\n", _bc_cand[c].name, (int)_bc_cand[c].name_full, _bc_cand[c].rssi);
    complete();
    printf("done %d %d %d\n", (int)_bc_scanning, (int)_bc_scan_ready, (int)_bc_scan_heard);
    uint8_t late[6] = {1, 2, 3, 4, 5, 6};
    int n_before = _bc_ncand;
    _bc_ncand = 3;                       // room in the list, so only the end can refuse it
    report(late, BLE_HCI_ADV_RPT_EVTYPE_ADV_IND, -50, cam_adv, sizeof(cam_adv));
    printf("after %d\n", find(late));
    _bc_ncand = (uint8_t)n_before;

    // B. a full list makes room for what matters, and only for that
    const uint8_t mine[6] = {0x11, 0x22, 0x33, 0x44, 0x55, 0x66};    // bonded, says nothing
    memcpy(g_bonded[0].val, mine, 6); g_nbonded = 1;
    _bc_scan_ready = false;
    bc_scan_start();
    for (int i = 0; i < 12; i++) phone(1000 + i, (int8_t)(-50 - i));   // -61 is the weakest
    const uint8_t bare[] = {0x02, 0x01, 0x06};
    report(mine, BLE_HCI_ADV_RPT_EVTYPE_ADV_IND, -48, bare, sizeof(bare));
    int weakest_gone = 1;
    for (int i = 0; i < _bc_ncand; i++) if (_bc_cand[i].rssi == -61) weakest_gone = 0;
    printf("bonded %d %d %d\n", (int)(find(mine) >= 0), (int)_bc_ncand, weakest_gone);
    const uint8_t other[6] = {0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0x01};   // a neighbour's camera
    report(other, BLE_HCI_ADV_RPT_EVTYPE_ADV_IND, -70, cam_adv, sizeof(cam_adv));
    const uint8_t late_nm[6] = {0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0x02}; // name only in its response
    report(late_nm, BLE_HCI_ADV_RPT_EVTYPE_ADV_IND, -52, bare, sizeof(bare));
    int dropped_first = find(late_nm) < 0;
    report(late_nm, BLE_HCI_ADV_RPT_EVTYPE_SCAN_RSP, -52, cam_rsp, sizeof(cam_rsp));
    printf("wanted %d %d %d %d\n", (int)(find(other) >= 0), dropped_first,
           (int)(find(late_nm) >= 0), (int)(find(late_nm) >= 0 && _bc_cand[find(late_nm)].named));
    phone(2000, -30);                    // strong, but only a phone
    printf("phone %d\n", (int)_bc_ncand);
    // Twelve more cameras, stronger than ours: they take the phones' slots, then
    // are heard and dropped — none evicts a camera, least of all the bonded one.
    for (int i = 0; i < 12; i++) {
        uint8_t cv[6] = {0xc0, 0, 0, 0, 0, (uint8_t)i};
        report(cv, BLE_HCI_ADV_RPT_EVTYPE_ADV_IND, -30, cam_adv, sizeof(cam_adv));
    }
    int wanted = 0, kept_mine = 0;
    for (int i = 0; i < _bc_ncand; i++) {
        wanted    += _bc_cand[i].svc || _bc_cand[i].named || !memcmp(_bc_cand[i].nat, mine, 6);
        kept_mine += !memcmp(_bc_cand[i].nat, mine, 6);
    }
    printf("full %d %d %d\n", (int)_bc_ncand, wanted, kept_mine);

    // C. stopping, a failed start, and nonsense on the air
    bc_scan_start();
    bool stopped = bc_scan_stop();
    printf("stop %d %d %d\n", (int)stopped, (int)_bc_scanning, (int)_bc_scan_ready);
    _bc_scan_ready = false;
    printf("stop-idle %d %d\n", (int)bc_scan_stop(), (int)_bc_scan_ready);
    g_start_rc = 6;
    printf("fail %d %d\n", (int)bc_scan_start(), (int)_bc_scanning);
    g_start_rc = 0;
    bc_scan_start();
    const uint8_t bad1[] = {0x20, 0x09, 'B', 'M', 'P', 'C', 'C'};    // length past the end
    const uint8_t bad2[] = {0x00, 0x09, 'B', 'M'};                     // a zero length
    const uint8_t w1[6] = {9, 9, 9, 9, 9, 1}, w2[6] = {9, 9, 9, 9, 9, 2};
    report(w1, BLE_HCI_ADV_RPT_EVTYPE_ADV_IND, -60, bad1, sizeof(bad1));
    report(w2, BLE_HCI_ADV_RPT_EVTYPE_ADV_IND, -60, bad2, sizeof(bad2));
    printf("malformed %d \"%s\" \"%s\"\n", (int)_bc_ncand, _bc_cand[0].name, _bc_cand[1].name);
    return 0;
}
"""

src = (RIG.replace("@CAMSVC@", cam_svc).replace("@CAND@", cand)
          .replace("@CAMNAME@", cam_name).replace("@SCAN@", scan))
d = pathlib.Path(tempfile.mkdtemp())
(d / "s.cpp").write_text(src)
r = subprocess.run(["c++", "-std=c++17", "-Wall", "-Wno-unused-function", "-o", str(d / "s"),
                    str(d / "s.cpp")], capture_output=True, text=True)
assert r.returncode == 0, f"the lifted scan code did not compile:\n{r.stderr[:2000]}"
run = subprocess.run([str(d / "s")], capture_output=True)
# Replaced, not strict: a parser that reads past a report prints whatever bytes it
# found, and that has to fail the check about malformed reports, not the decode.
stdout = run.stdout.decode("utf-8", errors="replace")
assert run.returncode == 0, \
    f"the rig crashed: {run.returncode}\n{run.stderr.decode('utf-8', errors='replace')[:500]}"
out = dict(l.split(" ", 1) for l in stdout.strip().splitlines())

print("1. listening as BLEScan did:")
assert out["svc"] == "1 d3 29", \
    f"CAM_SERVICE is not turned into the bytes on the air (least significant first): {out['svc']}"
started, own, scanning, itvl, win, passive, dup, dur = out["params"].split()
assert (started, own, scanning) == ("1", "0", "1"), f"the scan did not start as expected: {out['params']}"
assert (itvl, win) == ("160", "128"), \
    f"the scan listens {int(win) * 0.625:g} ms in every {int(itvl) * 0.625:g}, not BLEScan's 80 in 100"
assert passive == "0", "the scan is passive — no scan requests, so no scan responses, where the name is"
assert dup == "1", "every advertisement of every device is reported, not each device once"
assert dur == "5000", f"the scan lasts {dur} ms, not BLEScan's five seconds"
assert out["filter"] == "0 0", f"the scan filters devices BLEScan did not: {out['filter']}"
print("   active, 80 ms in 100, five seconds, each device once             OK")

print("\n2. the camera, found as before:")
assert out["adv"] == '0 1 0 ""', f"the camera's advertisement did not make an entry with its service: {out['adv']}"
assert out["rsp"] == '0 1 1 0 "Colin BMPCC" -44', \
    f"the scan response did not merge into the camera's entry (name, match, rssi): {out['rsp']}"
assert out["addr"] == "90:fd:9f:b4:50:df 0", \
    f"the address is not BLEAddress::toString()'s form, or the advert type is lost: {out['addr']}"
assert out["roundtrip"] == "1", "bc_connect() would parse the listed address back into a different device"
assert out["name"] == '"Colin BMPCC4K" 1 -40', \
    f"a complete name does not beat the shortened one, or a shortened one replaced it: {out['name']}"
print("   service + name merged from two reports; the address round-trips  OK")

print("\n3. a crowd of 300, and what it costs:")
ncand, heard, cam_at, news = (int(x) for x in out["crowd"].split())
assert news == 0, f"handling the reports allocated {news} times — the hoard, back"
assert ncand == 12 and cam_at == 0, f"the list holds {ncand}, the camera at {cam_at}"
assert 250 <= heard <= 301, f"the crowd of 301 is counted as {heard}"
assert out["done"] == f"0 1 {heard}", f"the end of the scan is not reported, or its count: {out['done']}"
assert out["after"] == "-1", "a report arriving after the scan ended was taken into the list"
print(f"   301 devices heard (counted {heard}), 12 slots, no allocation      OK")

print("\n4. a full list makes room only for what matters:")
assert out["bonded"] == "1 12 1", \
    f"the bonded camera arriving after twelve phones was not kept in place of the weakest: {out['bonded']}"
assert out["wanted"] == "1 1 1 1", \
    f"a neighbour's camera, or one named only in its scan response, was not kept: {out['wanted']}"
assert out["phone"] == "12", "a phone arriving at a full list was kept"
ncand, wanted, kept_mine = (int(x) for x in out["full"].split())
assert ncand == 12 and wanted == 12 and kept_mine == 1, \
    f"with the list full of cameras the bonded one was evicted or the list overran: {out['full']}"
print("   bonded or camera-like displaces the weakest phone; phones never do  OK")

print("\n5. every scan ends; nonsense is survived:")
assert out["stop"] == "1 0 1", f"a stopped scan was not reported as over: {out['stop']}"
assert out["stop-idle"] == "0 0", f"stopping with no scan running reported a scan's end: {out['stop-idle']}"
assert out["fail"] == "0 0", f"a scan NimBLE refused to start is left marked as running: {out['fail']}"
assert out["malformed"] == '2 "" ""', f"malformed advertisements were misread: {out['malformed']}"
print("   stopped, refused, or complete — all end; bad lengths read nothing  OK")

print("\nALL CHECKS PASSED")
