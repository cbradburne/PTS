/*
 * render_main.cpp — draws the P4 display's screens on a Mac, to PNG.
 *
 * The real hub_display.cpp and the real LVGL (the repo's libraries/lvgl, its
 * lv_conf.h), with the board, the USB controller and the hub replaced by
 * stand-ins.  The UI's own task loop runs: each vTaskDelay() it makes moves a
 * fake clock on and plays the next step of the script below, so screens are
 * reached the way a tap reaches them (the same NavAction path) and drawn by the
 * same flush the panel gets.
 *
 *   firmware/esp32_display_p4/host/render.sh   (PNGs in .build/p4-host/shots)
 */
#include <setjmp.h>
#include <vector>
#include <string>
#include <zlib.h>
#include "stubs/Arduino.h"
#include "../hub_display.cpp"

HostSerial Serial;
static uint32_t _now_ms = 0;
uint32_t millis() { return _now_ms; }

// ---- the board ----------------------------------------------------------------
static uint16_t           _fb[BOARD_LCD_W * BOARD_LCD_H];
static board_draw_done_fn _done_fn = nullptr;
static void              *_done_ctx = nullptr;

bool board_display_init(board_draw_done_fn done, void *ctx) { _done_fn = done; _done_ctx = ctx; return true; }
void board_display_draw(int x1, int y1, int x2, int y2, const void *pixels) {
    const uint16_t *src = (const uint16_t *)pixels;
    int w = x2 - x1 + 1;
    for (int y = y1; y <= y2; y++) memcpy(&_fb[y * BOARD_LCD_W + x1], src + (y - y1) * w, w * 2);
    if (_done_fn) _done_fn(_done_ctx);
}
void board_backlight(uint8_t) {}
bool board_touch_init() { return true; }
int  board_touch_read(int16_t *, int16_t *, int) { return 0; }

// ---- the controller -------------------------------------------------------------
static PadSnapshot _fake_pad;
void usb_pad_begin() {}
void usb_pad_get(PadSnapshot *out) { _fake_pad.reports += 5; *out = _fake_pad; }
void usb_pad_set_light(uint8_t r, uint8_t g, uint8_t b, uint8_t player) {
    fprintf(stderr, "[host] light bar #%02X%02X%02X, player %u\n", r, g, b, player);
}

// ---- the hub ---------------------------------------------------------------------
void disp_send_raw(uint8_t type, const uint8_t *, uint8_t len) {
    fprintf(stderr, "[host] to hub: message 0x%02X, %u bytes\n", type, len);
}
static void fake_send(uint8_t mount, CmdType cmd, const uint8_t *p, uint8_t n) {
    if (cmd == CMD_JOG && n == 8)
        fprintf(stderr, "[host] CAM %u jog pan %d tilt %d slider %d zoom %d\n", mount,
                (int16_t)(p[0] << 8 | p[1]), (int16_t)(p[2] << 8 | p[3]),
                (int16_t)(p[4] << 8 | p[5]), (int16_t)(p[6] << 8 | p[7]));
    else
        fprintf(stderr, "[host] CAM %u command 0x%02X, %u bytes\n", mount, (unsigned)cmd, n);
}

// ---- PNG -------------------------------------------------------------------------
static void put32(std::vector<uint8_t> &v, uint32_t x) {
    v.push_back(x >> 24); v.push_back(x >> 16); v.push_back(x >> 8); v.push_back(x);
}
static void chunk(FILE *f, const char *type, const std::vector<uint8_t> &data) {
    std::vector<uint8_t> c;
    put32(c, (uint32_t)data.size());
    c.insert(c.end(), type, type + 4);
    c.insert(c.end(), data.begin(), data.end());
    uint32_t crc = crc32(0, c.data() + 4, (uInt)(c.size() - 4));
    put32(c, crc);
    fwrite(c.data(), 1, c.size(), f);
}
static std::string _outdir = ".";
static void shot(const char *name) {
    lv_refr_now(_disp);
    const int W = BOARD_LCD_W, H = BOARD_LCD_H;
    std::vector<uint8_t> raw((size_t)(W * 3 + 1) * H);
    for (int y = 0; y < H; y++) {
        uint8_t *row = &raw[(size_t)y * (W * 3 + 1)];
        row[0] = 0;
        for (int x = 0; x < W; x++) {
            uint16_t c = _fb[y * W + x];
            uint8_t r = (c >> 11) & 0x1F, g = (c >> 5) & 0x3F, b = c & 0x1F;
            row[1 + x * 3] = (r << 3) | (r >> 2);
            row[2 + x * 3] = (g << 2) | (g >> 4);
            row[3 + x * 3] = (b << 3) | (b >> 2);
        }
    }
    uLongf zn = compressBound(raw.size());
    std::vector<uint8_t> z(zn);
    compress2(z.data(), &zn, raw.data(), raw.size(), 6);
    z.resize(zn);
    std::string path = _outdir + "/" + name + ".png";
    FILE *f = fopen(path.c_str(), "wb");
    static const uint8_t SIG[8] = { 0x89, 'P', 'N', 'G', 0x0D, 0x0A, 0x1A, 0x0A };
    fwrite(SIG, 1, 8, f);
    std::vector<uint8_t> ihdr;
    put32(ihdr, W); put32(ihdr, H);
    ihdr.push_back(8); ihdr.push_back(2); ihdr.push_back(0); ihdr.push_back(0); ihdr.push_back(0);
    chunk(f, "IHDR", ihdr);
    chunk(f, "IDAT", z);
    chunk(f, "IEND", {});
    fclose(f);
    fprintf(stderr, "[host] wrote %s\n", path.c_str());
}

// ---- the script --------------------------------------------------------------------
// One step per 400 ms of fake time: long enough for LVGL to settle, the pad
// timer to run and the look-at flash to tick.
static jmp_buf _done;
static int     _step = 0;
static uint32_t _next_ms = 400;

static void pad_plugged(bool on) {
    _fake_pad = PadSnapshot();
    if (!on) return;
    _fake_pad.connected = true;
    _fake_pad.kind = PadKind::DUALSENSE;
    _fake_pad.vid = 0x054C; _fake_pad.pid = 0x0CE6;
    strcpy(_fake_pad.name, "DualSense Wireless Controller");
}

static void script() {
    switch (_step++) {
    case 0: shot("1_home"); break;
    case 1: _fake_pad.in.rx = 0.62f; _fake_pad.in.ly = -0.35f;   // a stick held, for the panel's bars
            ev_pad_open(nullptr); break;
    case 2: shot("2_controller_panel");
            ev_pad_close(nullptr);
            _fake_pad.in = PadInput();
            _pending_cam = 1; _pending_action = NavAction::OPEN_DETAIL; break;
    case 3: shot("3_detail_cam2"); _pending_action = NavAction::DETAIL_BACK; break;
    case 4: _pending_action = NavAction::GOTO_POSITIONS;
            _fake_pad.in.buttons = PAD_BTN_SOUTH; break;           // Cross: autofocus on CAM 2
    case 5: _fake_pad.in.buttons = 0;
            shot("4_positions"); _pending_action = NavAction::GOTO_CONFIG; break;
    case 6: shot("5_config"); _pending_action = NavAction::GOTO_STATUS_FROM_CONFIG; break;
    case 7: _mp_act_open = true; break;
    case 8: shot("6_mounts_panel"); _mp_act_close = true; pad_plugged(false); break;
    case 9: shot("7_home_no_controller"); break;
    default: longjmp(_done, 1);
    }
}

void vTaskDelay(TickType_t ms) {
    _now_ms += ms ? ms : 1;
    if (_now_ms >= _next_ms) { _next_ms += 400; script(); }
}

int main(int argc, char **argv) {
    if (argc > 1) _outdir = argv[1];
    hub_display_init(fake_send);

    // A plausible evening: four mounts up, one off; CAM 3 has a slider in
    // look-at mode; a controller plugged in, driving CAM 2.
    hub_ui_update_clients(1, 2);
    const uint8_t  states[5] = { 0, 1, 5, 0, 0 };
    const uint8_t  flags[5]  = { 0, 0, FLAG_HAS_SLIDER | FLAG_LOOK_AT_MODE, FLAG_HAS_SLIDER, 0 };
    const int8_t   rssi[5]   = { -54, -61, -67, -72, 0 };
    const bool     linked[5] = { true, true, false, true, false };   // a Blackmagic camera paired
    for (int i = 0; i < 4; i++) {
        hub_ui_update_cam(i + 1, states[i], flags[i], rssi[i], linked[i] ? DISP_CAM_BLE_LINKED : 0, 0);
        hub_ui_update_preset(i + 1, 1 + i % 4, flags[i] & FLAG_HAS_SLIDER ? 2 : 0);
    }
    hub_ui_update_slots(1, 0x001F, 0x0004, 0xFF, 0);
    hub_ui_update_slots(2, 0x0063, 0x0002, 0xFF, 0);
    hub_ui_update_slots(3, 0x0007, 0x0000, 0xFF, 0);
    hub_ui_update_slots(4, 0x0101, 0x0001, 0xFF, 0);
    uint8_t macs[30] = {};
    for (int i = 0; i < 4; i++) {
        uint8_t m[6] = { 0x24, 0x58, 0x7C, 0x10, (uint8_t)(0x20 + i), (uint8_t)(0x90 + 7 * i) };
        memcpy(macs + i * 6, m, 6);
    }
    hub_ui_update_mount_table(macs);
    pad_plugged(true);
    _pad_cam = 1;

    if (!setjmp(_done)) lvgl_task_fn(nullptr);
    return 0;
}
