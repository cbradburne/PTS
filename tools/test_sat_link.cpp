#include <cstdio>
#include <cstring>
#include <vector>
#include "protocol.h"
#include "sat_link.h"

static int fails = 0;
static void chk(const char *l, bool ok, const char *detail = "") {
    if (!ok) fails++;
    printf("  [%s] %-52s %s\n", ok ? "ok  " : "FAIL", l, detail);
}

int main() {
    uint8_t frame[64];
    uint16_t flen = build_packet(frame, 4, 1234, CMD_STATUS, (const uint8_t*)"\x01\x02", 2);
    uint8_t mac[6] = {0xDE,0xAD,0xBE,0xEF,0x00,0x04};

    printf("Round trip:\n");
    uint8_t env[SAT_ENV_MAX];
    uint16_t elen = sat_env_build(env, mac, -41, frame, flen);
    chk("envelope built", elen == SAT_ENV_HDR_LEN + flen);

    SatEnvParser p; sat_env_init(&p); SatEnv out; bool got = false;
    for (uint16_t i = 0; i < elen; i++) got |= sat_env_feed(&p, env[i], &out);
    chk("parsed", got);
    chk("MAC survives",   got && memcmp(out.mac, mac, 6) == 0);
    chk("RSSI survives",  got && out.rssi == -41, got ? "" : "-");
    chk("frame intact",   got && out.frame_len == flen && memcmp(out.frame, frame, flen) == 0);

    printf("\nThe frame the hub then sees is a valid protocol packet:\n");
    PacketParser pp; pkt_parser_init(&pp); ParsedPacket pk; bool ok = false;
    for (uint16_t i = 0; i < out.frame_len; i++) if (pkt_feed(&pp, out.frame[i], &pk)) ok = true;
    chk("re-parses", ok);
    chk("mount_id preserved", ok && pk.mount_id == 4);
    chk("cmd preserved",      ok && pk.cmd == CMD_STATUS);

    printf("\nSplit across TCP reads (byte at a time is the worst case):\n");
    sat_env_init(&p); got = false;
    for (uint16_t i = 0; i < elen; i++) {
        bool r = sat_env_feed(&p, env[i], &out);
        if (r && i != elen - 1) { chk("fired early", false); }
        got |= r;
    }
    chk("resolves only on the last byte", got);

    printf("\nResync after a satellite reboots mid-envelope:\n");
    sat_env_init(&p); got = false;
    for (uint16_t i = 0; i < elen / 2; i++) sat_env_feed(&p, env[i], &out);   // truncated
    for (uint16_t i = 0; i < elen; i++) got |= sat_env_feed(&p, env[i], &out); // clean one
    chk("recovers on the next good envelope", got);
    chk("  and its contents are right", got && memcmp(out.mac, mac, 6) == 0);

    printf("\nGarbage before the magic is skipped:\n");
    sat_env_init(&p); got = false;
    for (uint8_t junk : {0x00, 0xFF, 0xA5, 0x12, 0xAA, 0x55}) sat_env_feed(&p, junk, &out);
    for (uint16_t i = 0; i < elen; i++) got |= sat_env_feed(&p, env[i], &out);
    chk("hunts to the real envelope", got);

    printf("\nRejects the malformed:\n");
    uint8_t bad[SAT_ENV_HDR_LEN] = {SAT_ENV_MAGIC_1, SAT_ENV_MAGIC_2, 0,0,0,0,0,0, 0, 0};
    sat_env_init(&p); got = false;
    for (uint8_t b : bad) got |= sat_env_feed(&p, b, &out);
    chk("zero-length frame refused", !got);
    chk("over-long frame refused", sat_env_build(env, mac, -40, frame, 256) == 0);

    printf("\nMagic cannot collide with a protocol frame:\n");
    chk("envelope magic != packet magic",
        !(SAT_ENV_MAGIC_1 == PKT_START_1 && SAT_ENV_MAGIC_2 == PKT_START_2));

    printf("\nRESULT: %s\n", fails ? "FAILURES" : "ALL PASS");
    return fails ? 1 : 0;
}
