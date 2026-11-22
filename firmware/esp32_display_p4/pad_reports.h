#pragma once
/*
 * pad_reports.h — turns a USB controller's input report into sticks and buttons.
 *
 * Plain C++, no Arduino and no USB: usb_pad.cpp hands it the bytes, and
 * host/test_pad.py compiles it on the Mac and feeds it reports.
 *
 * Every controller comes out in one convention, the one pygame gives the PC
 * app: sticks -1..1, x positive to the RIGHT, y positive DOWN.  pad_map.h then
 * turns that into a jog exactly as pc_app/motion/joystick.py does.
 *
 * Report layouts:
 *   DualSense (054C:0CE6, Edge 0DF2), USB report 0x01 — as Linux's hid-playstation
 *   DualShock 4 (054C:05C4, 09CC, 0BA0), USB report 0x01 — the same driver
 *   Xbox 360 type (interface FF/5D/01) — as Linux's xpad
 *   Xbox One / Series (interface FF/47/D0, "GIP"), message 0x20 — xpad
 *   Switch Pro (057E:2009), full report 0x30 — as Linux's hid-nintendo
 *   anything else that says it is a HID game pad or joystick: read from its own
 *   report descriptor (hid_parse_descriptor below)
 */
#include <stdint.h>
#include <string.h>
#include <math.h>

// The same bits as shared/pad_link.h, so a pad read here and one read by the
// Bluetooth receiver mean the same thing.
#ifndef PAD_BTN_SOUTH
#define PAD_BTN_SOUTH    0x00000001UL   // Cross / A
#define PAD_BTN_EAST     0x00000002UL   // Circle / B
#define PAD_BTN_WEST     0x00000004UL   // Square / X
#define PAD_BTN_NORTH    0x00000008UL   // Triangle / Y
#define PAD_BTN_L1       0x00000010UL
#define PAD_BTN_R1       0x00000020UL
#define PAD_BTN_L2       0x00000040UL
#define PAD_BTN_R2       0x00000080UL
#define PAD_BTN_L3       0x00000100UL
#define PAD_BTN_R3       0x00000200UL
#define PAD_BTN_SYSTEM   0x00000400UL   // PS / Xbox / Home
#define PAD_BTN_SELECT   0x00000800UL   // Create / Share / View / −
#define PAD_BTN_START    0x00001000UL   // Options / Menu / +
#define PAD_BTN_CAPTURE  0x00002000UL   // Mute / Capture
#define PAD_BTN_UP       0x00004000UL
#define PAD_BTN_DOWN     0x00008000UL
#define PAD_BTN_LEFT     0x00010000UL
#define PAD_BTN_RIGHT    0x00020000UL
#endif

enum class PadKind : uint8_t {
    NONE = 0,
    DUALSENSE,
    DUALSHOCK4,
    XINPUT,        // Xbox 360 and the many pads that copy it
    XBOX_GIP,      // Xbox One / Series
    SWITCH_PRO,
    HID_GENERIC,   // read from its report descriptor
};

static inline const char *pad_kind_name(PadKind k) {
    switch (k) {
        case PadKind::DUALSENSE:   return "DualSense";
        case PadKind::DUALSHOCK4:  return "DualShock 4";
        case PadKind::XINPUT:      return "Xbox 360 type";
        case PadKind::XBOX_GIP:    return "Xbox One / Series";
        case PadKind::SWITCH_PRO:  return "Switch Pro";
        case PadKind::HID_GENERIC: return "USB game pad";
        default:                   return "none";
    }
}

struct PadInput {
    float    lx = 0, ly = 0, rx = 0, ry = 0;   // -1..1, x right +, y down +
    uint32_t buttons = 0;                      // PAD_BTN_*
};

// ---- small helpers -----------------------------------------------------------

static inline float pad_clamp1(float v) { return v < -1.0f ? -1.0f : v > 1.0f ? 1.0f : v; }

// 0..255 with 128 at rest (PlayStation).
static inline float pad_u8(uint8_t v) { return pad_clamp1(((int)v - 128) / 127.0f); }

// A signed 16-bit stick (Xbox).  -32768 is one count further than +32767.
static inline float pad_s16(int16_t v) { return pad_clamp1(v / 32767.0f); }

static inline int16_t pad_le16(const uint8_t *p) { return (int16_t)(p[0] | (p[1] << 8)); }

// A hat switch, 0 = up and clockwise in eighths; anything else is let go.
static inline uint32_t pad_hat(int v) {
    switch (v) {
        case 0: return PAD_BTN_UP;
        case 1: return PAD_BTN_UP | PAD_BTN_RIGHT;
        case 2: return PAD_BTN_RIGHT;
        case 3: return PAD_BTN_DOWN | PAD_BTN_RIGHT;
        case 4: return PAD_BTN_DOWN;
        case 5: return PAD_BTN_DOWN | PAD_BTN_LEFT;
        case 6: return PAD_BTN_LEFT;
        case 7: return PAD_BTN_UP | PAD_BTN_LEFT;
        default: return 0;
    }
}

static inline uint32_t pad_bit(uint8_t byte, int bit, uint32_t flag) {
    return (byte >> bit) & 1 ? flag : 0;
}

// ---- PlayStation ---------------------------------------------------------------

// DualSense over USB: report 0x01, 64 bytes.
//   [1] LX [2] LY [3] RX [4] RY [5] L2 [6] R2 [7] counter
//   [8]  hat (low nibble), square, cross, circle, triangle
//   [9]  L1 R1 L2 R2 create options L3 R3
//   [10] PS, touchpad, mute
static inline bool pad_parse_dualsense(const uint8_t *p, int n, PadInput *o) {
    if (n < 11 || p[0] != 0x01) return false;
    o->lx = pad_u8(p[1]); o->ly = pad_u8(p[2]);
    o->rx = pad_u8(p[3]); o->ry = pad_u8(p[4]);
    uint8_t b0 = p[8], b1 = p[9], b2 = p[10];
    o->buttons = pad_hat(b0 & 0x0F)
               | pad_bit(b0, 4, PAD_BTN_WEST)  | pad_bit(b0, 5, PAD_BTN_SOUTH)
               | pad_bit(b0, 6, PAD_BTN_EAST)  | pad_bit(b0, 7, PAD_BTN_NORTH)
               | pad_bit(b1, 0, PAD_BTN_L1)    | pad_bit(b1, 1, PAD_BTN_R1)
               | pad_bit(b1, 2, PAD_BTN_L2)    | pad_bit(b1, 3, PAD_BTN_R2)
               | pad_bit(b1, 4, PAD_BTN_SELECT)| pad_bit(b1, 5, PAD_BTN_START)
               | pad_bit(b1, 6, PAD_BTN_L3)    | pad_bit(b1, 7, PAD_BTN_R3)
               | pad_bit(b2, 0, PAD_BTN_SYSTEM)| pad_bit(b2, 2, PAD_BTN_CAPTURE);
    return true;
}

// DualShock 4 over USB: report 0x01, 64 bytes.
//   [1] LX [2] LY [3] RX [4] RY
//   [5] hat (low nibble), square, cross, circle, triangle
//   [6] L1 R1 L2 R2 share options L3 R3
//   [7] PS, touchpad, then a counter
static inline bool pad_parse_ds4(const uint8_t *p, int n, PadInput *o) {
    if (n < 10 || p[0] != 0x01) return false;
    o->lx = pad_u8(p[1]); o->ly = pad_u8(p[2]);
    o->rx = pad_u8(p[3]); o->ry = pad_u8(p[4]);
    uint8_t b0 = p[5], b1 = p[6], b2 = p[7];
    o->buttons = pad_hat(b0 & 0x0F)
               | pad_bit(b0, 4, PAD_BTN_WEST)  | pad_bit(b0, 5, PAD_BTN_SOUTH)
               | pad_bit(b0, 6, PAD_BTN_EAST)  | pad_bit(b0, 7, PAD_BTN_NORTH)
               | pad_bit(b1, 0, PAD_BTN_L1)    | pad_bit(b1, 1, PAD_BTN_R1)
               | pad_bit(b1, 2, PAD_BTN_L2)    | pad_bit(b1, 3, PAD_BTN_R2)
               | pad_bit(b1, 4, PAD_BTN_SELECT)| pad_bit(b1, 5, PAD_BTN_START)
               | pad_bit(b1, 6, PAD_BTN_L3)    | pad_bit(b1, 7, PAD_BTN_R3)
               | pad_bit(b2, 0, PAD_BTN_SYSTEM);
    return true;
}

// ---- Xbox ------------------------------------------------------------------------

// Xbox 360 type: message 0x00, length 0x14.
//   [2] up down left right start back L3 R3
//   [3] LB RB guide - A B X Y
//   [4] LT [5] RT [6..13] LX LY RX RY, signed 16-bit, Y UP positive
static inline bool pad_parse_xinput(const uint8_t *p, int n, PadInput *o) {
    if (n < 14 || p[0] != 0x00 || p[1] < 0x14) return false;
    o->lx =  pad_s16(pad_le16(p + 6));
    o->ly = -pad_s16(pad_le16(p + 8));       // to y-down
    o->rx =  pad_s16(pad_le16(p + 10));
    o->ry = -pad_s16(pad_le16(p + 12));
    uint8_t b0 = p[2], b1 = p[3];
    o->buttons = pad_bit(b0, 0, PAD_BTN_UP)    | pad_bit(b0, 1, PAD_BTN_DOWN)
               | pad_bit(b0, 2, PAD_BTN_LEFT)  | pad_bit(b0, 3, PAD_BTN_RIGHT)
               | pad_bit(b0, 4, PAD_BTN_START) | pad_bit(b0, 5, PAD_BTN_SELECT)
               | pad_bit(b0, 6, PAD_BTN_L3)    | pad_bit(b0, 7, PAD_BTN_R3)
               | pad_bit(b1, 0, PAD_BTN_L1)    | pad_bit(b1, 1, PAD_BTN_R1)
               | pad_bit(b1, 2, PAD_BTN_SYSTEM)
               | pad_bit(b1, 4, PAD_BTN_SOUTH) | pad_bit(b1, 5, PAD_BTN_EAST)
               | pad_bit(b1, 6, PAD_BTN_WEST)  | pad_bit(b1, 7, PAD_BTN_NORTH)
               | (p[4] > 0x80 ? PAD_BTN_L2 : 0) | (p[5] > 0x80 ? PAD_BTN_R2 : 0);
    return true;
}

// Xbox One / Series (GIP), message 0x20.
//   [4] sync - menu view A B X Y
//   [5] up down left right LB RB L3 R3
//   [6..9] LT RT, 0..1023   [10..17] LX LY RX RY, signed 16-bit, Y UP positive
// The Xbox button travels on its own, message 0x07: [4] bit 0.  It is kept
// here between calls in *guide so a stick report doesn't drop it.
static inline bool pad_parse_gip(const uint8_t *p, int n, PadInput *o, bool *guide) {
    if (n >= 5 && p[0] == 0x07) {
        *guide = p[4] & 1;
        o->buttons = (o->buttons & ~PAD_BTN_SYSTEM) | (*guide ? PAD_BTN_SYSTEM : 0);
        return true;
    }
    if (n < 18 || p[0] != 0x20) return false;
    o->lx =  pad_s16(pad_le16(p + 10));
    o->ly = -pad_s16(pad_le16(p + 12));
    o->rx =  pad_s16(pad_le16(p + 14));
    o->ry = -pad_s16(pad_le16(p + 16));
    uint8_t b0 = p[4], b1 = p[5];
    int lt = (uint16_t)pad_le16(p + 6), rt = (uint16_t)pad_le16(p + 8);
    o->buttons = pad_bit(b0, 2, PAD_BTN_START) | pad_bit(b0, 3, PAD_BTN_SELECT)
               | pad_bit(b0, 4, PAD_BTN_SOUTH) | pad_bit(b0, 5, PAD_BTN_EAST)
               | pad_bit(b0, 6, PAD_BTN_WEST)  | pad_bit(b0, 7, PAD_BTN_NORTH)
               | pad_bit(b1, 0, PAD_BTN_UP)    | pad_bit(b1, 1, PAD_BTN_DOWN)
               | pad_bit(b1, 2, PAD_BTN_LEFT)  | pad_bit(b1, 3, PAD_BTN_RIGHT)
               | pad_bit(b1, 4, PAD_BTN_L1)    | pad_bit(b1, 5, PAD_BTN_R1)
               | pad_bit(b1, 6, PAD_BTN_L3)    | pad_bit(b1, 7, PAD_BTN_R3)
               | (lt > 512 ? PAD_BTN_L2 : 0)   | (rt > 512 ? PAD_BTN_R2 : 0)
               | (*guide ? PAD_BTN_SYSTEM : 0);
    return true;
}

// ---- Switch Pro --------------------------------------------------------------------

// Its sticks are 12-bit, Y UP positive, and their centre differs from pad to
// pad.  The factory calibration lives in the pad's own flash; rather than read
// it, the centre is taken from the first reports, when the sticks are at rest
// (nobody plugs a pad in with a thumb on a stick).  A centre far from the
// middle means a stick WAS held, so it falls back to the middle.
struct SwitchCal {
    int  n = 0;              // reports averaged so far
    long sum[4] = {};
    int  centre[4] = { 2048, 2048, 2048, 2048 };
};
#define SWITCH_CAL_REPORTS   8
#define SWITCH_HALF_RANGE    1500.0f   // centre to full deflection, counts
#define SWITCH_CENTRE_SLACK  400       // a "centre" further off than this was a held stick

// [3] Y X B A SR SL R ZR   [4] − + Rstick Lstick home capture
// [5] down up right left SR SL L ZL   [6..8] left stick  [9..11] right stick
static inline bool pad_parse_switch(const uint8_t *p, int n, PadInput *o, SwitchCal *cal) {
    if (n < 12 || p[0] != 0x30) return false;
    int raw[4] = {
        p[6] | ((p[7] & 0x0F) << 8),  (p[7] >> 4) | (p[8] << 4),
        p[9] | ((p[10] & 0x0F) << 8), (p[10] >> 4) | (p[11] << 4),
    };
    float v[4] = { 0, 0, 0, 0 };
    if (cal->n < SWITCH_CAL_REPORTS) {
        // Still learning where rest is: the sticks read as centred meanwhile,
        // never as wherever the default centre would put them.
        for (int i = 0; i < 4; i++) cal->sum[i] += raw[i];
        if (++cal->n == SWITCH_CAL_REPORTS)
            for (int i = 0; i < 4; i++) {
                int c = (int)(cal->sum[i] / SWITCH_CAL_REPORTS);
                cal->centre[i] = (c > 2048 - SWITCH_CENTRE_SLACK && c < 2048 + SWITCH_CENTRE_SLACK) ? c : 2048;
            }
    } else {
        for (int i = 0; i < 4; i++) v[i] = pad_clamp1((raw[i] - cal->centre[i]) / SWITCH_HALF_RANGE);
    }
    o->lx = v[0]; o->ly = -v[1]; o->rx = v[2]; o->ry = -v[3];
    uint8_t r = p[3], s = p[4], l = p[5];
    // Nintendo's letters are mirrored: B is the bottom button, A the right.
    o->buttons = pad_bit(r, 0, PAD_BTN_WEST)   | pad_bit(r, 1, PAD_BTN_NORTH)
               | pad_bit(r, 2, PAD_BTN_SOUTH)  | pad_bit(r, 3, PAD_BTN_EAST)
               | pad_bit(r, 6, PAD_BTN_R1)     | pad_bit(r, 7, PAD_BTN_R2)
               | pad_bit(s, 0, PAD_BTN_SELECT) | pad_bit(s, 1, PAD_BTN_START)
               | pad_bit(s, 2, PAD_BTN_R3)     | pad_bit(s, 3, PAD_BTN_L3)
               | pad_bit(s, 4, PAD_BTN_SYSTEM) | pad_bit(s, 5, PAD_BTN_CAPTURE)
               | pad_bit(l, 0, PAD_BTN_DOWN)   | pad_bit(l, 1, PAD_BTN_UP)
               | pad_bit(l, 2, PAD_BTN_RIGHT)  | pad_bit(l, 3, PAD_BTN_LEFT)
               | pad_bit(l, 6, PAD_BTN_L1)     | pad_bit(l, 7, PAD_BTN_L2);
    return true;
}

// ---- any other HID game pad ----------------------------------------------------------

struct HidField {
    uint16_t bit = 0;        // offset after the report ID byte
    uint8_t  size = 0;       // bits; 0 = not in this report
    int32_t  lmin = 0, lmax = 0;
};

struct HidLayout {
    bool     ok = false;
    uint8_t  report_id = 0;  // 0 = the reports carry no ID byte
    HidField x, y, z, rx, ry, rz, hat;
    uint16_t btn_bit = 0;    // the first button's bit, consecutive after it
    uint8_t  btn_count = 0;
};

// Walks a HID report descriptor and keeps the first game pad or joystick
// report it finds: its sticks (X Y Z Rx Ry Rz), hat and buttons.  Enough for
// the pads that follow the usual pattern; a pad that describes itself some
// other way simply comes out with fewer controls, never wrong ones.
static inline bool hid_parse_descriptor(const uint8_t *d, int len, HidLayout *out) {
    *out = HidLayout();
    // lmax_u: the logical maximum as written, unsigned — see the trap below.
    struct Globals { uint16_t page; int32_t lmin, lmax; uint32_t lmax_u; uint8_t rsize, rid; uint8_t rcount; };
    Globals g = {}, stack[4];
    int sp = 0;
    // A usage written in 4 bytes carries its own page in the top half; a
    // shorter one takes the page in force at the main item.  Kept whole here.
    uint32_t usages[16]; int n_usages = 0;
    uint32_t umin = 0, umax = 0; bool have_range = false;
    int depth = 0, pad_depth = -1;          // the collection the pad lives in
    uint16_t bitpos[256] = {};              // next free bit, per report ID
    bool found_id = false;

    for (int i = 0; i < len; ) {
        uint8_t prefix = d[i];
        if (prefix == 0xFE) {                               // long item: skip it
            if (i + 1 >= len) break;
            i += 3 + d[i + 1];
            continue;
        }
        int size = prefix & 3; if (size == 3) size = 4;
        int type = (prefix >> 2) & 3, tag = prefix >> 4;
        if (i + 1 + size > len) break;
        uint32_t u = 0;
        for (int k = 0; k < size; k++) u |= (uint32_t)d[i + 1 + k] << (8 * k);
        int32_t s = (int32_t)u;
        if (size == 1) s = (int8_t)u; else if (size == 2) s = (int16_t)u;
        i += 1 + size;

        if (type == 1) {                                    // global
            switch (tag) {
                case 0: g.page = (uint16_t)u; break;
                case 1: g.lmin = s; break;
                case 2: g.lmax = s; g.lmax_u = u; break;
                case 7: g.rsize = (uint8_t)u; break;
                case 8: g.rid = (uint8_t)u; break;
                case 9: g.rcount = (uint8_t)u; break;
                case 10: if (sp < 4) stack[sp++] = g; break;
                case 11: if (sp > 0) g = stack[--sp]; break;
            }
        } else if (type == 2) {                             // local
            uint32_t full = size == 4 ? u : 0xFFFF0000u | u;   // 0xFFFF: page still to come
            if (tag == 0 && n_usages < 16) usages[n_usages++] = full;
            else if (tag == 1) { umin = full; have_range = true; }
            else if (tag == 2) { umax = full; have_range = true; }
        } else if (type == 0) {                             // main
            if (tag == 10) {                                // collection
                // Application collection (0x01) whose usage is game pad (5) or joystick (4).
                uint32_t cu = n_usages ? usages[0] : 0;
                if ((cu >> 16) == 0xFFFF) cu = ((uint32_t)g.page << 16) | (cu & 0xFFFF);
                if (pad_depth < 0 && u == 0x01 && (cu == 0x00010005 || cu == 0x00010004))
                    pad_depth = depth;
                depth++;
            } else if (tag == 12) {                         // end collection
                depth--;
                if (pad_depth >= 0 && depth == pad_depth && out->ok) return true;
            } else if (tag == 8) {                          // input
                uint8_t rid = g.rid;
                uint16_t &pos = bitpos[rid];
                bool in_pad = pad_depth >= 0;
                bool constant = u & 1, variable = u & 2;
                if (in_pad && !constant && variable && (!found_id || rid == out->report_id)) {
                    // A logical max of 0xFF written in one byte reads as -1 when
                    // taken as signed, as the spec says to: with a minimum of 0
                    // the descriptor meant 255.
                    int32_t lmax = (g.lmin >= 0 && g.lmax < 0) ? (int32_t)g.lmax_u : g.lmax;
                    for (int k = 0; k < g.rcount; k++) {
                        uint32_t usage = have_range ? (umin + (uint32_t)k > umax ? umax : umin + (uint32_t)k)
                                       : (n_usages ? usages[k < n_usages ? k : n_usages - 1] : 0);
                        if ((usage >> 16) == 0xFFFF) usage = ((uint32_t)g.page << 16) | (usage & 0xFFFF);
                        uint16_t page = (uint16_t)(usage >> 16);
                        uint16_t at = (uint16_t)(pos + k * g.rsize);
                        HidField f; f.bit = at; f.size = g.rsize; f.lmin = g.lmin; f.lmax = lmax;
                        HidField *slot = nullptr;
                        if (page == 0x01) {
                            switch (usage & 0xFFFF) {
                                case 0x30: slot = &out->x;  break;
                                case 0x31: slot = &out->y;  break;
                                case 0x32: slot = &out->z;  break;
                                case 0x33: slot = &out->rx; break;
                                case 0x34: slot = &out->ry; break;
                                case 0x35: slot = &out->rz; break;
                                case 0x39: slot = &out->hat; break;
                            }
                        } else if (page == 0x09 && g.rsize == 1 && out->btn_count == 0 && k == 0) {
                            out->btn_bit = at;
                            out->btn_count = g.rcount > 32 ? 32 : g.rcount;
                        }
                        if (slot && !slot->size) *slot = f;
                    }
                    if (!found_id) { found_id = true; out->report_id = rid; }
                    out->ok = out->x.size && out->y.size;
                }
                pos = (uint16_t)(pos + g.rsize * g.rcount);
            }
            n_usages = 0; have_range = false; umin = umax = 0;  // locals end at a main item
        }
    }
    return out->ok;
}

static inline uint32_t hid_bits(const uint8_t *p, int n, uint16_t bit, uint8_t size) {
    uint32_t v = 0;
    for (int k = 0; k < size; k++) {
        int b = bit + k;
        if ((b >> 3) >= n) break;
        v |= (uint32_t)((p[b >> 3] >> (b & 7)) & 1) << k;
    }
    return v;
}

static inline float hid_axis(const uint8_t *p, int n, const HidField &f) {
    if (!f.size || f.lmax == f.lmin) return 0.0f;
    uint32_t raw = hid_bits(p, n, f.bit, f.size);
    int32_t v = (int32_t)raw;
    if (f.lmin < 0 && f.size < 32 && (raw >> (f.size - 1)) & 1) v = (int32_t)(raw | (~0u << f.size));
    return pad_clamp1(2.0f * (float)(v - f.lmin) / (float)(f.lmax - f.lmin) - 1.0f);
}

// The usual DirectInput order: 1-4 faces, 5/6 shoulders, 7/8 triggers,
// 9 select, 10 start, 11/12 stick clicks, 13 home.
static inline bool pad_parse_hid(const HidLayout &L, const uint8_t *p, int n, PadInput *o) {
    if (!L.ok) return false;
    if (L.report_id) {
        if (n < 1 || p[0] != L.report_id) return false;
        p++; n--;
    }
    o->lx = hid_axis(p, n, L.x);
    o->ly = hid_axis(p, n, L.y);
    // The right stick is Z/Rz on most USB pads (the DirectInput habit, and
    // PlayStation copies, which put their TRIGGERS on Rx/Ry); Rx/Ry only when
    // a pad has no Z/Rz pair.
    if (L.z.size && L.rz.size) { o->rx = hid_axis(p, n, L.z);  o->ry = hid_axis(p, n, L.rz); }
    else                       { o->rx = hid_axis(p, n, L.rx); o->ry = hid_axis(p, n, L.ry); }
    static const uint32_t ORDER[13] = {
        PAD_BTN_WEST, PAD_BTN_SOUTH, PAD_BTN_EAST, PAD_BTN_NORTH, PAD_BTN_L1, PAD_BTN_R1,
        PAD_BTN_L2, PAD_BTN_R2, PAD_BTN_SELECT, PAD_BTN_START, PAD_BTN_L3, PAD_BTN_R3,
        PAD_BTN_SYSTEM,
    };
    uint32_t b = 0;
    uint32_t raw = hid_bits(p, n, L.btn_bit, L.btn_count > 32 ? 32 : L.btn_count);
    for (int k = 0; k < L.btn_count && k < 13; k++) if ((raw >> k) & 1) b |= ORDER[k];
    if (L.hat.size) {
        int h = (int)hid_bits(p, n, L.hat.bit, L.hat.size) - L.hat.lmin;
        if (h >= 0 && h <= 7) b |= pad_hat(h);
    }
    o->buttons = b;
    return true;
}
