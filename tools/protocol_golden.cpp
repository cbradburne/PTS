/*
 * protocol_golden.cpp — C side of the cross-language protocol golden test.
 *
 * Compiled and driven by tools/check_protocol.py.  Includes the CANONICAL
 * protocol header (firmware/shared/protocol.h) so the exact code the firmware
 * runs is what gets tested against the Python implementation.
 *
 * Modes:
 *   golden emit            print CRC vectors and built packets:
 *                            CRC <input-hex|-> <crc16 as 4 hex digits>
 *                            PKT <case-name> <packet-hex>
 *   golden parse           read one hex string per line on stdin, feed it
 *                          byte-by-byte through pkt_feed(), print per line:
 *                            PARSE <n> OK <mount> <seq> <cmd-hex> <plen> <payload-hex|->
 *                            PARSE <n> REJ
 *
 * The case parameters here are mirrored in check_protocol.py — a mismatch in
 * either the parameters or the wire encoding fails the check.
 */
#include <cstdio>
#include <cstring>
#include <vector>

#include "../firmware/shared/protocol.h"

static void print_hex(const uint8_t *d, size_t n) {
    if (n == 0) { fputs("-", stdout); return; }
    for (size_t i = 0; i < n; i++) printf("%02x", d[i]);
}

static void emit_crc(const uint8_t *d, uint16_t n) {
    fputs("CRC ", stdout);
    print_hex(d, n);
    printf(" %04x\n", crc16(d, n));
}

static void emit_pkt(const char *name, const uint8_t *pkt, uint16_t n) {
    printf("PKT %s ", name);
    print_hex(pkt, n);
    fputs("\n", stdout);
}

static int do_emit() {
    // ── CRC vectors ────────────────────────────────────────────────────────
    emit_crc(nullptr, 0);                                    // empty → 0xFFFF
    const uint8_t z = 0x00;
    emit_crc(&z, 1);
    const char *check = "123456789";                         // CCITT-FALSE → 0x29B1
    emit_crc((const uint8_t *)check, 9);

    uint8_t buf[PKT_BUF_SIZE + 4];
    uint16_t n;

    // a: empty payload
    n = build_packet(buf, MOUNT_BROADCAST, 0, CMD_GET_STATE, nullptr, 0);
    emit_pkt("empty_get_state", buf, n);

    // b: 1-byte payload via helper
    n = build_calib_prompt(buf, 3, 0xBEEF, CALIB_SOLVED);
    emit_pkt("calib_prompt", buf, n);

    // c: 10-byte STATUS via build_status — exercises every field incl. the
    //    newest state (STATE_LOOK_AT_PRE_AIM) and active_la_subject byte.
    PayloadStatus st;
    st.state              = STATE_LOOK_AT_PRE_AIM;
    st.flags              = 0xA5;
    st.active_pt_preset   = 2;
    st.active_sl_preset   = 3;
    st.slot_occupied_mask = 0x03FF;
    st.slot_at_mask       = 0x0001;
    st.target_slot        = 0xFF;
    st.active_la_subject  = 0x07;
    n = build_status(buf, 5, 0x1234, &st);
    emit_pkt("status_10b", buf, n);

    // d: ACK helper
    n = build_ack(buf, 2, 0x0102, 0x0304);
    emit_pkt("ack", buf, n);

    // e: NACK helper
    n = build_nack(buf, 4, 0x0505, 0x0607, NACK_NO_REF);
    emit_pkt("nack_no_ref", buf, n);

    // f: max-size payload (232-byte SUBJECT_LIST)
    uint8_t subj[SUBJECT_LIST_PAYLOAD_LEN];
    for (int i = 0; i < SUBJECT_LIST_PAYLOAD_LEN; i++) subj[i] = (uint8_t)(i & 0xFF);
    n = build_subject_list(buf, 1, 0xFFFF, subj);
    emit_pkt("subject_list_232b", buf, n);

    // g: floats on the wire (big-endian IEEE 754)
    n = build_look_at_status(buf, 3, 42, 123.456f, -45.5f, 12.25f, 2, 0x60);
    emit_pkt("look_at_status_floats", buf, n);

    return 0;
}

static int hexval(int c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static int do_parse() {
    char line[4096];
    int lineno = 0;
    while (fgets(line, sizeof(line), stdin)) {
        lineno++;
        std::vector<uint8_t> bytes;
        for (int i = 0; line[i] && line[i] != '\n' && line[i + 1]; i += 2) {
            int hi = hexval(line[i]), lo = hexval(line[i + 1]);
            if (hi < 0 || lo < 0) break;
            bytes.push_back((uint8_t)((hi << 4) | lo));
        }

        PacketParser parser;
        pkt_parser_init(&parser);
        ParsedPacket pkt;
        bool got = false;
        // Copy out the first completed packet (payload points into parser buf).
        uint8_t  payload_copy[PACKET_MAX_PAYLOAD];
        uint8_t  plen = 0, mount = 0;
        uint16_t seq = 0; uint8_t cmd = 0;
        for (uint8_t b : bytes) {
            if (pkt_feed(&parser, b, &pkt)) {
                got   = true;
                mount = pkt.mount_id;
                seq   = pkt.seq;
                cmd   = (uint8_t)pkt.cmd;
                plen  = pkt.payload_len;
                memcpy(payload_copy, pkt.payload, plen);
                break;
            }
        }
        if (got) {
            printf("PARSE %d OK %u %u %02x %u ", lineno, mount, seq, cmd, plen);
            print_hex(payload_copy, plen);
            fputs("\n", stdout);
        } else {
            printf("PARSE %d REJ\n", lineno);
        }
    }
    return 0;
}

int main(int argc, char **argv) {
    if (argc >= 2 && strcmp(argv[1], "emit") == 0)  return do_emit();
    if (argc >= 2 && strcmp(argv[1], "parse") == 0) return do_parse();
    fprintf(stderr, "usage: %s emit|parse\n", argv[0]);
    return 2;
}
