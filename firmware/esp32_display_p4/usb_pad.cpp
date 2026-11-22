/*
 * usb_pad.cpp — reads one USB game controller through ESP-IDF's USB host library.
 *
 * Built from firmware/shared/usb_pad_probe.h (the S3 display's probe): the same
 * transfers and the same start-up packets, cut down to one controller and to
 * the one interface it is read through, and printing only what an operator
 * needs — what was plugged in, and whether it is being read.
 *
 * Three families need a word from the host before they send anything, and get it:
 *   - Xbox One / Series (GIP): the power-on packet, and an acknowledgement each
 *     time the Xbox button is pressed — as Linux's xpad driver sends them;
 *   - Switch Pro: the USB handshake and "full reports, please" — as Linux's
 *     hid-nintendo sends them;
 *   - anything else that is a HID game pad: nothing to send, but its report
 *     descriptor is read first, to learn where its sticks are.
 *
 * Every callback here runs on the client task (core 0); the snapshot is the
 * only thing other tasks see, behind _snap_lock.
 */
#include <Arduino.h>
#include <esp_heap_caps.h>
#include "usb/usb_host.h"
#include "usb_pad.h"

#define PAD_REPORT_MAX   64
#define PAD_DESC_MAX     1024
#define PAD_MAX_OUT      8
#define PAD_MAX_CAND     4      // HID interfaces tried, for a pad known only by its descriptor

#define PAD_LOG(...)     Serial.printf(__VA_ARGS__)

struct PadOut { uint8_t data[PAD_REPORT_MAX]; uint8_t len; uint32_t at_ms; };

struct PadCand { uint8_t intf, in_ep, out_ep; uint16_t in_mps, desc_len; };

struct PadDev {
    bool                 used, gone, ready;
    bool                 refused;              // not a controller this reads: closed once idle
    bool                 ds_setup_sent;        // DualSense: its light bar released from start-up
    usb_device_handle_t  hdl;
    uint16_t             vid, pid;
    uint8_t              mps0;
    PadKind              kind;
    char                 name[40];
    int                  intf;                 // the claimed interface, -1 = none yet
    uint8_t              in_ep, out_ep;
    uint16_t             in_mps;
    usb_transfer_t      *in_xfer, *out_xfer, *ctrl_xfer;
    int                  inflight;
    bool                 out_busy, ctrl_busy;
    PadOut               outq[PAD_MAX_OUT];
    int                  outq_n;
    uint8_t              gip_seq, sw_count;
    bool                 guide;
    SwitchCal            cal;
    HidLayout            hid;
    PadCand              cand[PAD_MAX_CAND];
    int                  n_cand, cand_next, cand_cur;
    uint32_t             light_sent;           // _light_seq last sent to this pad
    PadInput             in;                   // kept between reports (GIP's guide button)
};

static usb_host_client_handle_t _client = nullptr;
static PadDev     *_dev = nullptr;             // the one controller (PSRAM)
static PadSnapshot _snap;
static portMUX_TYPE _snap_lock = portMUX_INITIALIZER_UNLOCKED;

// The light the UI asked for, and a count that moves each time it does.
static uint8_t  _light_rgb[3] = { 0, 0, 0 };
static uint8_t  _light_player = 0;
static uint32_t _light_seq = 0;

// ---- the snapshot ------------------------------------------------------------

void usb_pad_get(PadSnapshot *out) {
    taskENTER_CRITICAL(&_snap_lock);
    *out = _snap;
    taskEXIT_CRITICAL(&_snap_lock);
}

void usb_pad_set_light(uint8_t r, uint8_t g, uint8_t b, uint8_t player) {
    taskENTER_CRITICAL(&_snap_lock);
    if (_light_rgb[0] != r || _light_rgb[1] != g || _light_rgb[2] != b || _light_player != player) {
        _light_rgb[0] = r; _light_rgb[1] = g; _light_rgb[2] = b;
        _light_player = player;
        _light_seq++;
    }
    taskEXIT_CRITICAL(&_snap_lock);
}

static void snap_connected(const PadDev &d) {
    taskENTER_CRITICAL(&_snap_lock);
    _snap.connected = true;
    _snap.kind = d.kind;
    _snap.vid = d.vid;
    _snap.pid = d.pid;
    memcpy(_snap.name, d.name, sizeof _snap.name);
    _snap.in = PadInput();
    _snap.reports = 0;
    taskEXIT_CRITICAL(&_snap_lock);
}

static void snap_gone() {
    taskENTER_CRITICAL(&_snap_lock);
    _snap.connected = false;
    _snap.kind = PadKind::NONE;
    _snap.in = PadInput();
    taskEXIT_CRITICAL(&_snap_lock);
}

static void snap_refused(const char *what) {
    taskENTER_CRITICAL(&_snap_lock);
    strncpy(_snap.refused, what, sizeof _snap.refused - 1);
    _snap.refused[sizeof _snap.refused - 1] = 0;
    taskEXIT_CRITICAL(&_snap_lock);
}

// ---- small helpers ---------------------------------------------------------------

static void usb_str(const usb_str_desc_t *s, char *out, size_t n) {
    size_t k = 0;
    if (s && n)
        for (int i = 0; i < (s->bLength - 2) / 2 && k + 1 < n; i++) {
            uint16_t c = s->wData[i];
            out[k++] = (c >= 0x20 && c < 0x7F) ? (char)c : '?';
        }
    if (n) out[k] = 0;
}

static const char *xfer_status(usb_transfer_status_t st) {
    switch (st) {
        case USB_TRANSFER_STATUS_COMPLETED: return "completed";
        case USB_TRANSFER_STATUS_ERROR:     return "error";
        case USB_TRANSFER_STATUS_TIMED_OUT: return "timed out";
        case USB_TRANSFER_STATUS_CANCELED:  return "cancelled";
        case USB_TRANSFER_STATUS_STALL:     return "stalled";
        case USB_TRANSFER_STATUS_OVERFLOW:  return "overflow";
        case USB_TRANSFER_STATUS_SKIPPED:   return "skipped";
        case USB_TRANSFER_STATUS_NO_DEVICE: return "no device";
        default:                            return "?";
    }
}

// ---- packets to the controller -------------------------------------------------------

static void out_queue(PadDev &d, const uint8_t *data, uint8_t len, uint32_t at_ms) {
    if (!d.out_ep || d.outq_n >= PAD_MAX_OUT || len > PAD_REPORT_MAX) return;
    PadOut &o = d.outq[d.outq_n++];
    memcpy(o.data, data, len);
    o.len = len;
    o.at_ms = at_ms;
}

static void out_cb(usb_transfer_t *x) {
    PadDev &d = *(PadDev *)x->context;
    d.out_busy = false;
    d.inflight--;
    if (x->status != USB_TRANSFER_STATUS_COMPLETED && x->status != USB_TRANSFER_STATUS_NO_DEVICE)
        PAD_LOG("[PAD] a packet to the controller failed (%s)\n", xfer_status(x->status));
}

static void out_service(PadDev &d, uint32_t now) {
    if (d.out_busy || !d.outq_n || !d.out_xfer || (int32_t)(now - d.outq[0].at_ms) < 0) return;
    PadOut o = d.outq[0];
    d.outq_n--;
    memmove(d.outq, d.outq + 1, d.outq_n * sizeof(PadOut));
    if (d.kind == PadKind::XBOX_GIP && o.data[0] != 0x01) o.data[2] = d.gip_seq++;  // an ACK keeps the pad's number
    memcpy(d.out_xfer->data_buffer, o.data, o.len);
    d.out_xfer->num_bytes = o.len;
    if (usb_host_transfer_submit(d.out_xfer) != ESP_OK) return;
    d.out_busy = true;
    d.inflight++;
}

// A Switch Pro subcommand: output report 0x01 with a neutral rumble.
static void switch_subcmd(PadDev &d, uint8_t cmd, uint8_t arg, uint32_t at_ms) {
    uint8_t p[12] = { 0x01, (uint8_t)(d.sw_count++ & 0x0F),
                      0x00, 0x01, 0x40, 0x40, 0x00, 0x01, 0x40, 0x40, cmd, arg };
    out_queue(d, p, sizeof p, at_ms);
}

// The lights, in whatever form this controller takes them.
static void send_light(PadDev &d, uint32_t now) {
    uint8_t rgb[3], player;
    uint32_t seq;
    taskENTER_CRITICAL(&_snap_lock);
    memcpy(rgb, _light_rgb, 3);
    player = _light_player;
    seq = _light_seq;
    taskEXIT_CRITICAL(&_snap_lock);
    if (seq == d.light_sent) return;
    d.light_sent = seq;
    switch (d.kind) {
        case PadKind::DUALSENSE: {
            // Output report 0x02, as Linux's hid-playstation lays it out.  The
            // light bar first has to be let go of by the pad's own start-up
            // fade, in a report of its own, before a colour sticks.
            static const uint8_t LEDS[6] = { 0x00, 0x04, 0x0A, 0x15, 0x1B, 0x1F };
            if (!d.ds_setup_sent) {
                uint8_t s[48] = {};
                s[0]  = 0x02;
                s[39] = 0x02;             // valid_flag2: light bar set-up
                s[42] = 0x02;             // light bar set-up: light out
                out_queue(d, s, sizeof s, now);
                d.ds_setup_sent = true;
            }
            uint8_t p[48] = {};
            p[0]  = 0x02;
            p[2]  = 0x04 | 0x10;          // light bar, player lights
            p[44] = LEDS[player <= 5 ? player : 0];
            p[45] = rgb[0]; p[46] = rgb[1]; p[47] = rgb[2];
            out_queue(d, p, sizeof p, now);
            break;
        }
        case PadKind::DUALSHOCK4: {
            uint8_t p[32] = {};
            p[0] = 0x05;
            p[1] = 0x02;                  // light bar
            p[6] = rgb[0]; p[7] = rgb[1]; p[8] = rgb[2];
            out_queue(d, p, sizeof p, now);
            break;
        }
        case PadKind::XINPUT: {
            // 0x06-0x09: quadrant 1-4 lit; 0x0D: alternating, for a fifth.
            uint8_t led = player >= 1 && player <= 4 ? 0x05 + player : player == 5 ? 0x0D : 0x00;
            uint8_t p[3] = { 0x01, 0x03, led };
            out_queue(d, p, sizeof p, now);
            break;
        }
        case PadKind::SWITCH_PRO: {
            static const uint8_t LIGHTS[6] = { 0x00, 0x01, 0x03, 0x07, 0x0F, 0x09 };
            switch_subcmd(d, 0x30, LIGHTS[player <= 5 ? player : 0], now);
            break;
        }
        default: break;
    }
}

// ---- reading it -----------------------------------------------------------------------

static void in_submit(PadDev &d) {
    d.in_xfer->num_bytes = d.in_mps;
    if (usb_host_transfer_submit(d.in_xfer) == ESP_OK) d.inflight++;
    else PAD_LOG("[PAD] could not start reading the controller\n");
}

static void in_cb(usb_transfer_t *x) {
    PadDev &d = *(PadDev *)x->context;
    d.inflight--;
    if (x->status == USB_TRANSFER_STATUS_COMPLETED) {
        const uint8_t *p = x->data_buffer;
        int n = x->actual_num_bytes;
        // The Xbox button asks to be acknowledged, and is sent again until it is.
        if (d.kind == PadKind::XBOX_GIP && n >= 3 && p[0] == 0x07 && (p[1] & 0x10)) {
            uint8_t ack[13] = { 0x01, 0x20, p[2], 0x09, 0x00, 0x07, 0x20, 0x02,
                                0x00, 0x00, 0x00, 0x00, 0x00 };
            out_queue(d, ack, sizeof ack, millis());
        }
        bool ok = false;
        PadInput &in = d.in;
        switch (d.kind) {
            case PadKind::DUALSENSE:   ok = pad_parse_dualsense(p, n, &in); break;
            case PadKind::DUALSHOCK4:  ok = pad_parse_ds4(p, n, &in); break;
            case PadKind::XINPUT:      ok = pad_parse_xinput(p, n, &in); break;
            case PadKind::XBOX_GIP:    ok = pad_parse_gip(p, n, &in, &d.guide); break;
            case PadKind::SWITCH_PRO:  ok = pad_parse_switch(p, n, &in, &d.cal); break;
            case PadKind::HID_GENERIC: ok = pad_parse_hid(d.hid, p, n, &in); break;
            default: break;
        }
        if (ok) {
            taskENTER_CRITICAL(&_snap_lock);
            _snap.in = in;
            _snap.reports++;
            taskEXIT_CRITICAL(&_snap_lock);
        }
        if (!d.gone) in_submit(d);
        return;
    }
    if (x->status != USB_TRANSFER_STATUS_NO_DEVICE && x->status != USB_TRANSFER_STATUS_CANCELED)
        PAD_LOG("[PAD] reading the controller stopped (%s)\n", xfer_status(x->status));
}

// Claims the interface it is read through and starts reading.
static bool start_reading(PadDev &d, uint8_t intf, uint8_t in_ep, uint16_t in_mps, uint8_t out_ep) {
    esp_err_t e = usb_host_interface_claim(_client, d.hdl, intf, 0);
    if (e != ESP_OK) {
        PAD_LOG("[PAD] could not claim interface %u (%s)\n", intf, esp_err_to_name(e));
        return false;
    }
    d.intf = intf;
    d.in_ep = in_ep;
    d.in_mps = in_mps;
    if (usb_host_transfer_alloc(in_mps, 0, &d.in_xfer) != ESP_OK) {
        PAD_LOG("[PAD] no memory for a transfer\n");
        return false;
    }
    d.in_xfer->device_handle = d.hdl;
    d.in_xfer->bEndpointAddress = in_ep;
    d.in_xfer->callback = in_cb;
    d.in_xfer->context = &d;
    d.in_xfer->timeout_ms = 0;
    if (out_ep && usb_host_transfer_alloc(PAD_REPORT_MAX, 0, &d.out_xfer) == ESP_OK) {
        d.out_ep = out_ep;
        d.out_xfer->device_handle = d.hdl;
        d.out_xfer->bEndpointAddress = out_ep;
        d.out_xfer->callback = out_cb;
        d.out_xfer->context = &d;
        d.out_xfer->timeout_ms = 0;
    }
    in_submit(d);
    d.ready = true;
    snap_connected(d);
    PAD_LOG("[PAD] reading %s \"%s\" (%04x:%04x) on interface %u\n",
            pad_kind_name(d.kind), d.name, d.vid, d.pid, intf);
    return true;
}

// ---- report descriptors, for a pad known only by its own description ----------

static void ctrl_next(PadDev &d);

static void ctrl_cb(usb_transfer_t *x) {
    PadDev &d = *(PadDev *)x->context;
    d.ctrl_busy = false;
    d.inflight--;
    if (d.gone) return;
    const PadCand &c = d.cand[d.cand_cur];
    if (x->status == USB_TRANSFER_STATUS_COMPLETED) {
        const uint8_t *p = x->data_buffer + sizeof(usb_setup_packet_t);
        int n = x->actual_num_bytes - (int)sizeof(usb_setup_packet_t);
        HidLayout L;
        if (hid_parse_descriptor(p, n, &L)) {
            d.hid = L;
            d.kind = PadKind::HID_GENERIC;
            PAD_LOG("[PAD] interface %u describes a game pad: sticks %s, %u buttons%s, report %u\n",
                    c.intf, (L.z.size && L.rz.size) ? "X/Y + Z/Rz" : (L.rx.size && L.ry.size) ? "X/Y + Rx/Ry" : "X/Y only",
                    L.btn_count, L.hat.size ? ", d-pad" : "", L.report_id);
            if (start_reading(d, c.intf, c.in_ep, c.in_mps, c.out_ep)) return;
        } else {
            PAD_LOG("[PAD] interface %u is not a game pad (%d-byte report descriptor)\n", c.intf, n);
        }
    } else if (x->status != USB_TRANSFER_STATUS_NO_DEVICE) {
        PAD_LOG("[PAD] interface %u: no report descriptor (%s)\n", c.intf, xfer_status(x->status));
    }
    ctrl_next(d);
}

static void ctrl_next(PadDev &d) {
    if (d.ctrl_busy || d.ready || !d.ctrl_xfer) return;
    if (d.cand_next >= d.n_cand) {
        PAD_LOG("[PAD] \"%s\" (%04x:%04x) is not a controller this reads\n", d.name, d.vid, d.pid);
        char what[48];
        snprintf(what, sizeof what, "%s (%04x:%04x)", d.name, d.vid, d.pid);
        snap_refused(what);
        d.refused = true;
        return;
    }
    d.cand_cur = d.cand_next++;
    const PadCand &c = d.cand[d.cand_cur];
    uint16_t len = c.desc_len && c.desc_len < PAD_DESC_MAX ? c.desc_len : PAD_DESC_MAX;
    usb_setup_packet_t *sp = (usb_setup_packet_t *)d.ctrl_xfer->data_buffer;
    sp->bmRequestType = 0x81;                 // to the host, standard, interface
    sp->bRequest      = 0x06;                 // GET_DESCRIPTOR
    sp->wValue        = 0x2200;               // HID report descriptor
    sp->wIndex        = c.intf;
    sp->wLength       = len;
    d.ctrl_xfer->num_bytes = sizeof(usb_setup_packet_t) + (len + d.mps0 - 1) / d.mps0 * d.mps0;
    if (usb_host_transfer_submit_control(_client, d.ctrl_xfer) == ESP_OK) {
        d.ctrl_busy = true;
        d.inflight++;
    } else {
        PAD_LOG("[PAD] interface %u: could not ask for its report descriptor\n", c.intf);
    }
}

// ---- plugging in and out ------------------------------------------------------------

static bool is_dualsense(uint16_t v, uint16_t p) { return v == 0x054C && (p == 0x0CE6 || p == 0x0DF2); }
static bool is_ds4(uint16_t v, uint16_t p)       { return v == 0x054C && (p == 0x05C4 || p == 0x09CC || p == 0x0BA0); }
static bool is_switch_pro(uint16_t v, uint16_t p){ return v == 0x057E && p == 0x2009; }

static void attach(uint8_t addr) {
    if (_dev->used) {
        PAD_LOG("[PAD] a second USB device: left alone (one controller at a time)\n");
        return;
    }
    PadDev &d = *_dev;
    memset(&d, 0, sizeof d);
    d.cal = SwitchCal();          // its centre defaults are not zero
    d.hid = HidLayout();
    d.in  = PadInput();
    d.intf = -1;
    if (usb_host_device_open(_client, addr, &d.hdl) != ESP_OK) {
        PAD_LOG("[PAD] a device at address %u could not be opened\n", addr);
        return;
    }
    d.used = true;
    usb_device_info_t info;
    const usb_device_desc_t *dd;
    const usb_config_desc_t *cd;
    usb_host_device_info(d.hdl, &info);
    usb_host_get_device_descriptor(d.hdl, &dd);
    usb_host_get_active_config_descriptor(d.hdl, &cd);
    d.vid = dd->idVendor;
    d.pid = dd->idProduct;
    d.mps0 = dd->bMaxPacketSize0 ? dd->bMaxPacketSize0 : 8;
    usb_str(info.str_desc_product, d.name, sizeof d.name);
    PAD_LOG("[PAD] plugged in: %04x:%04x \"%s\", %s speed, asks for %u mA\n", d.vid, d.pid, d.name,
            info.speed == USB_SPEED_LOW ? "low" : info.speed == USB_SPEED_FULL ? "full" : "high",
            cd->bMaxPower * 2);

    // Walk the configuration: per interface, its class and first interrupt IN/OUT.
    struct Intf { uint8_t num, cls, sub, proto, in_ep, out_ep; uint16_t in_mps, desc_len; };
    Intf ifs[8]; int n_if = 0;
    const uint8_t *p = (const uint8_t *)cd;
    int total = cd->wTotalLength;
    Intf *cur = nullptr;
    for (int off = 0; off + 2 <= total; off += p[off]) {
        uint8_t len = p[off], type = p[off + 1];
        if (len < 2 || off + len > total) break;
        if (type == 0x04 && len >= 9) {                       // interface
            cur = nullptr;
            if (p[off + 3] == 0 && n_if < 8) {                // alt 0 only
                cur = &ifs[n_if++];
                memset(cur, 0, sizeof *cur);
                cur->num = p[off + 2];
                cur->cls = p[off + 5]; cur->sub = p[off + 6]; cur->proto = p[off + 7];
            }
        } else if (type == 0x21 && len >= 9 && cur && cur->cls == 0x03) {   // HID
            cur->desc_len = p[off + 7] | (p[off + 8] << 8);
        } else if (type == 0x05 && len >= 7 && cur) {         // endpoint
            uint8_t ep = p[off + 2], kind = p[off + 3] & 0x03;
            uint16_t mps = (p[off + 4] | (p[off + 5] << 8)) & 0x7FF;
            if (kind != 3) continue;                          // interrupt only
            if ((ep & 0x80) && !cur->in_ep && mps && mps <= PAD_REPORT_MAX) { cur->in_ep = ep; cur->in_mps = mps; }
            else if (!(ep & 0x80) && !cur->out_ep) cur->out_ep = ep;
        }
    }

    const Intf *use = nullptr;
    uint32_t now = millis();
    if (is_dualsense(d.vid, d.pid) || is_ds4(d.vid, d.pid) || is_switch_pro(d.vid, d.pid)) {
        d.kind = is_dualsense(d.vid, d.pid) ? PadKind::DUALSENSE
               : is_ds4(d.vid, d.pid)       ? PadKind::DUALSHOCK4 : PadKind::SWITCH_PRO;
        for (int i = 0; i < n_if && !use; i++) if (ifs[i].cls == 0x03 && ifs[i].in_ep) use = &ifs[i];
    } else {
        for (int i = 0; i < n_if && !use; i++)
            if (ifs[i].cls == 0xFF && ifs[i].sub == 0x47 && ifs[i].proto == 0xD0 && ifs[i].in_ep) {
                use = &ifs[i]; d.kind = PadKind::XBOX_GIP;
            }
        for (int i = 0; i < n_if && !use; i++)
            if (ifs[i].cls == 0xFF && ifs[i].sub == 0x5D && ifs[i].proto == 0x01 && ifs[i].in_ep) {
                use = &ifs[i]; d.kind = PadKind::XINPUT;
            }
    }
    if (!d.name[0]) strncpy(d.name, pad_kind_name(d.kind), sizeof d.name - 1);

    if (use) {
        if (!start_reading(d, use->num, use->in_ep, use->in_mps, use->out_ep)) return;
        if (d.kind == PadKind::XBOX_GIP) {
            static const uint8_t power_on[] = { 0x05, 0x20, 0x00, 0x01, 0x00 };
            out_queue(d, power_on, sizeof power_on, now + 50);
            if (d.vid == 0x045E && d.pid == 0x02EA) {           // Xbox One S
                static const uint8_t s_init[] = { 0x05, 0x20, 0x00, 0x0F, 0x06 };
                out_queue(d, s_init, sizeof s_init, now + 60);
            }
        } else if (d.kind == PadKind::SWITCH_PRO) {
            static const uint8_t hs[] = { 0x80, 0x02 }, fast[] = { 0x80, 0x03 }, usb_only[] = { 0x80, 0x04 };
            out_queue(d, hs, sizeof hs, now + 100);             // handshake
            out_queue(d, fast, sizeof fast, now + 150);         // link speed
            out_queue(d, hs, sizeof hs, now + 200);             // handshake again
            out_queue(d, usb_only, sizeof usb_only, now + 250); // USB only, no timeout
            switch_subcmd(d, 0x03, 0x30, now + 300);            // full reports (0x30)
        }
        d.light_sent = _light_seq - 1;                          // its lights, once it is up
        return;
    }

    // Not known by name: try each HID interface's own description of itself.
    for (int i = 0; i < n_if && d.n_cand < PAD_MAX_CAND; i++)
        if (ifs[i].cls == 0x03 && ifs[i].in_ep) {
            PadCand &c = d.cand[d.n_cand++];
            c.intf = ifs[i].num; c.in_ep = ifs[i].in_ep; c.in_mps = ifs[i].in_mps;
            c.out_ep = ifs[i].out_ep; c.desc_len = ifs[i].desc_len;
        }
    if (d.n_cand &&
        usb_host_transfer_alloc(sizeof(usb_setup_packet_t) + PAD_DESC_MAX + 64, 0, &d.ctrl_xfer) == ESP_OK) {
        d.ctrl_xfer->device_handle = d.hdl;
        d.ctrl_xfer->bEndpointAddress = 0;
        d.ctrl_xfer->callback = ctrl_cb;
        d.ctrl_xfer->context = &d;
        d.ctrl_xfer->timeout_ms = 0;
        d.light_sent = _light_seq;                              // a generic pad has no lights we know
        ctrl_next(d);
        return;
    }
    PAD_LOG("[PAD] \"%s\" (%04x:%04x) is not a controller this reads\n", d.name, d.vid, d.pid);
    char what[48];
    snprintf(what, sizeof what, "%s (%04x:%04x)", d.name, d.vid, d.pid);
    snap_refused(what);
    d.refused = true;     // closed from the task loop, so a controller plugged in next is read
}

static void detach(usb_device_handle_t hdl) {
    if (!_dev->used || _dev->hdl != hdl || _dev->gone) return;
    _dev->gone = true;
    PAD_LOG("[PAD] unplugged: \"%s\"\n", _dev->name);
    if (_dev->ready) snap_gone();
}

// Once nothing is out any more.
static void dev_free(PadDev &d) {
    if (d.in_ep) { usb_host_endpoint_halt(d.hdl, d.in_ep); usb_host_endpoint_flush(d.hdl, d.in_ep); }
    if (d.out_ep) { usb_host_endpoint_halt(d.hdl, d.out_ep); usb_host_endpoint_flush(d.hdl, d.out_ep); }
    if (d.in_xfer) usb_host_transfer_free(d.in_xfer);
    if (d.out_xfer) usb_host_transfer_free(d.out_xfer);
    if (d.ctrl_xfer) usb_host_transfer_free(d.ctrl_xfer);
    if (d.intf >= 0) usb_host_interface_release(_client, d.hdl, (uint8_t)d.intf);
    usb_host_device_close(_client, d.hdl);
    d.used = false;
}

static void client_cb(const usb_host_client_event_msg_t *msg, void *) {
    if (msg->event == USB_HOST_CLIENT_EVENT_NEW_DEV) attach(msg->new_dev.address);
    else if (msg->event == USB_HOST_CLIENT_EVENT_DEV_GONE) detach(msg->dev_gone.dev_hdl);
}

// ---- tasks --------------------------------------------------------------------------

static void lib_task(void *) {
    for (;;) {
        uint32_t flags;
        usb_host_lib_handle_events(portMAX_DELAY, &flags);
    }
}

static void client_task(void *) {
    usb_host_config_t cfg = {};
    cfg.skip_phy_setup = false;
    cfg.intr_flags = ESP_INTR_FLAG_LEVEL1;
    cfg.peripheral_map = 0;          // the default: the high-speed port, the USB-A socket
    esp_err_t e = usb_host_install(&cfg);
    if (e != ESP_OK) {
        PAD_LOG("[PAD] the USB host would not start: %s\n", esp_err_to_name(e));
        vTaskDelete(nullptr);
    }
    xTaskCreatePinnedToCore(lib_task, "usb_lib", 4096, nullptr, 6, nullptr, 0);

    usb_host_client_config_t cc = {};
    cc.is_synchronous = false;
    cc.max_num_event_msg = 5;
    cc.async.client_event_callback = client_cb;
    cc.async.callback_arg = nullptr;
    e = usb_host_client_register(&cc, &_client);
    if (e != ESP_OK) {
        PAD_LOG("[PAD] could not register with the USB host: %s\n", esp_err_to_name(e));
        vTaskDelete(nullptr);
    }
    PAD_LOG("[PAD] USB host running: plug a controller into the USB-A port\n");

    // A device that finished enumerating before this client registered sends
    // no NEW_DEV, so pick up whatever is already there.
    uint8_t addrs[4];
    int n_addrs = 0;
    if (usb_host_device_addr_list_fill(4, addrs, &n_addrs) == ESP_OK)
        for (int i = 0; i < n_addrs; i++) attach(addrs[i]);

    for (;;) {
        usb_host_client_handle_events(_client, pdMS_TO_TICKS(20));
        PadDev &d = *_dev;
        if (!d.used) continue;
        if (d.gone || d.refused) {
            if (d.inflight <= 0) dev_free(d);
            continue;
        }
        uint32_t now = millis();
        if (d.ready) send_light(d, now);
        out_service(d, now);
    }
}

void usb_pad_begin() {
    _dev = (PadDev *)heap_caps_calloc(1, sizeof(PadDev), MALLOC_CAP_SPIRAM);
    if (!_dev) _dev = (PadDev *)calloc(1, sizeof(PadDev));
    if (!_dev) {
        PAD_LOG("[PAD] no memory for the controller's tables: USB controller off\n");
        return;
    }
    xTaskCreatePinnedToCore(client_task, "usb_pad", 6144, nullptr, 5, nullptr, 0);
}
