// Exercise the presence gate as written in esp32_hub_eth.ino.
#include <cstdio>
#include <cstdint>
#define NUM_MOUNTS 5
#define MOUNT_PRESENT_MS 30000UL

static uint32_t _mount_last_seen[NUM_MOUNTS];
static bool     _valid[NUM_MOUNTS];
static uint32_t _now;
static int      _sent[NUM_MOUNTS];

static bool mount_mac_valid(int i) { return _valid[i]; }

static void espnow_send_if_present(int idx) {
    if (idx < 0 || idx >= NUM_MOUNTS) return;
    if (!mount_mac_valid(idx)) return;
    uint32_t seen = _mount_last_seen[idx];
    if (seen == 0 || (_now - seen) > MOUNT_PRESENT_MS) return;
    _sent[idx]++;
}

static int fails = 0;
static void chk(const char *l, int got, int want) {
    bool ok = got == want; if (!ok) fails++;
    printf("  [%s] %-50s sent=%d\n", ok ? "ok  " : "FAIL", l, got);
}

int main() {
    // The real rig: 5 paired, cam1 permanent, others out per event.
    for (int i = 0; i < NUM_MOUNTS; i++) _valid[i] = true;
    _now = 100000;
    _mount_last_seen[0] = _now - 100;      // cam1, always on
    _mount_last_seen[1] = 0;               // cam2, never seen this boot
    _mount_last_seen[2] = 0;               // cam3, never seen
    _mount_last_seen[3] = _now - 5000;     // cam4, present
    _mount_last_seen[4] = _now - 200;      // cam5, present

    printf("Broadcast with cams 2 and 3 switched off:\n");
    for (int i = 0; i < NUM_MOUNTS; i++) espnow_send_if_present(i);
    chk("cam1 (on)  sent",        _sent[0], 1);
    chk("cam2 (off) NOT sent",    _sent[1], 0);
    chk("cam3 (off) NOT sent",    _sent[2], 0);
    chk("cam4 (on)  sent",        _sent[3], 1);
    chk("cam5 (on)  sent",        _sent[4], 1);

    printf("\nA mount goes quiet — grace, then cut off:\n");
    _sent[3] = 0;
    _mount_last_seen[3] = _now - 29000;  espnow_send_if_present(3);
    chk("silent 29s — still sent",  _sent[3], 1);
    _mount_last_seen[3] = _now - 31000;  espnow_send_if_present(3);
    chk("silent 31s — cut off",     _sent[3], 1);

    printf("\nSwitched back on — resumes with no operator action:\n");
    _mount_last_seen[3] = _now;  espnow_send_if_present(3);
    chk("announces itself, sends resume", _sent[3], 2);

    printf("\nUnpaired slot is still skipped:\n");
    _sent[4] = 0; _valid[4] = false; espnow_send_if_present(4);
    chk("unpaired — no send", _sent[4], 0);

    printf("\nFailed-send load before vs after, one hour, 3/s of broadcast:\n");
    int before = 2 * 3 * 3600;   // cams 2+3, every broadcast, as it was
    printf("      before: %d failed sends/hour to absent mounts\n", before);
    printf("      after : 0\n");

    printf("\nRESULT: %s\n", fails ? "FAILURES" : "ALL PASS");
    return fails ? 1 : 0;
}
