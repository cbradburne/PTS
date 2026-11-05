// usb_pad_probe.h — what a game controller plugged into the 7" display's "USB"
// port says, and what listening to it costs.
//
// A TEST, not a feature.  It answers whether the display can take a wired
// controller with no extra board.  The Waveshare schematic (V1.2) says it can
// be wired for it:
//   - the "USB" Type-C has VBUS tied straight to the board's 5 V rail, so with
//     the board powered from its "USB to UART" Type-C, the "USB" socket powers
//     whatever is plugged into it;
//   - its data lines reach GPIO19/20 through a USB/CAN switch (FSUSB42) that a
//     10 k pull-down holds on USB, and the ESP32-S3's USB-OTG block can be the
//     host.
// Not known until measured: whether internal RAM is left over once the screen
// has its bounce buffer, and what each controller sends.
//
// Built two ways:
//   tools/build.sh flash usbprobe <port>                  on its own
//   USB_PAD_PROBE=1 tools/build.sh flash display <port>   inside the display
//                                                         firmware, UI running
// Either way it prints on UART0 — the "USB to UART" port, 115200 baud — because
// the native port now belongs to the controller.  Flash through that port too:
// it has the auto-reset circuit, so no buttons.  Switch 15 on "USB to UART".
//
// What it prints:
//   - each device as it is plugged in: VID:PID, names, every interface and
//     endpoint, and each HID report descriptor (the map of a generic pad's bytes)
//   - its input, as changes.  For the first two seconds of each report type it
//     learns which bits move on their own (timers, counters, motion sensors);
//     after that it prints a line whenever any other bit changes.  "[5] 08>28"
//     is byte 5 going from 08 to 28.
//   - reports per second, and internal RAM: free, largest block, lowest ever.
//
// Two controller families send nothing until asked, so it asks:
//   - Xbox One / Series (GIP): the power-on packet, and the acknowledgement its
//     Xbox button asks for — both as Linux's xpad driver sends them;
//   - Switch Pro: the USB handshake, as Linux's hid-nintendo sends it.
// An Xbox 360-type pad gets its player-1 light, to show the host can talk back.
#pragma once
#include <Arduino.h>
#include <stdarg.h>
#include <esp_heap_caps.h>
#include "usb/usb_host.h"

#define UPP_MAX_DEVS     4
#define UPP_MAX_STREAMS  4      // interrupt IN endpoints followed per device
#define UPP_MAX_KEYS     6      // report types told apart per endpoint
#define UPP_MAX_HID      4      // report descriptors fetched per device
#define UPP_MAX_OUT      8      // packets waiting to go to a device
#define UPP_REPORT_MAX   64     // a full-speed interrupt packet
#define UPP_DESC_MAX     1024   // longest report descriptor fetched
#define UPP_LEARN_MS     2000
#define UPP_LINE_GAP_MS  50     // at most 20 change lines a second per endpoint
#define UPP_RATE_MS      10000
#define UPP_RAM_MS       30000

struct UppDev;

struct UppKey {                          // one report type on one endpoint
    bool     used, learnt;
    uint8_t  id;                         // its first byte, when the stream is keyed
    uint8_t  len;
    uint32_t first_ms;
    uint8_t  last[UPP_REPORT_MAX];       // the report before (while learning)
    uint8_t  shown[UPP_REPORT_MAX];      // the report last printed
    uint8_t  noisy[UPP_REPORT_MAX];      // bits that moved by themselves
};

struct UppStream {                       // one interrupt IN endpoint
    UppDev         *dev;
    uint8_t         intf, ep, mps;
    bool            keyed;               // the first byte names the report type
    bool            gip;                 // byte 2 is a sequence number
    usb_transfer_t *xfer;
    uint32_t        reports, reports_then, rate_ms, line_ms;
    UppKey          keys[UPP_MAX_KEYS];
};

struct UppOut {
    uint8_t     data[16];
    uint8_t     len;
    uint32_t    at_ms;
    const char *what;                    // printed when sent; nullptr = quietly
};

struct UppDev {
    bool                used, gone;
    uint8_t             n;               // "pad<n>" in the output
    usb_device_handle_t hdl;
    uint16_t            vid, pid;
    uint8_t             mps0;
    uint8_t             claimed[8], n_claimed;
    UppStream           streams[UPP_MAX_STREAMS];
    uint8_t             n_streams;
    uint8_t             out_ep;          // where start packets go
    usb_transfer_t     *out_xfer;
    bool                out_busy;
    UppOut              outq[UPP_MAX_OUT];
    uint8_t             outq_n;
    bool                gip;
    uint8_t             gip_seq;
    uint8_t             hid_intf[UPP_MAX_HID];
    uint16_t            hid_len[UPP_MAX_HID];
    uint8_t             n_hid, hid_next, hid_cur;
    usb_transfer_t     *ctrl_xfer;
    bool                ctrl_busy;
    int                 inflight;        // transfers out and not yet back
};

static Print                    *_upp_out = nullptr;
static usb_host_client_handle_t  _upp_client = nullptr;
// In PSRAM, ~20 KB of it: the probe's own bookkeeping must not count against
// the internal RAM it is there to measure.
static UppDev                   *_upp_devs = nullptr;
static uint8_t                   _upp_next_n = 1;
static size_t                    _upp_ram_before = 0;

// ---- printing --------------------------------------------------------------------
// Into a buffer on this task's stack: Print::printf mallocs for long lines, and
// that would show up in the very RAM figures this exists to take.
static void upp_printf(const char *fmt, ...) {
    char buf[240];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    if (n > 0) _upp_out->write((const uint8_t *)buf, n < (int)sizeof buf ? n : sizeof buf - 1);
}

static void upp_hex(const char *prefix, const uint8_t *p, int n) {
    char line[192];
    for (int i = 0; i < n; i += 32) {
        int k = snprintf(line, sizeof line - 100, "%s", prefix);
        if (k > (int)sizeof line - 100) k = sizeof line - 100;
        for (int j = i; j < n && j < i + 32; j++) k += snprintf(line + k, sizeof line - k, " %02x", p[j]);
        line[k++] = '\n';
        _upp_out->write((const uint8_t *)line, k);
    }
}

static void upp_ram(const char *when) {
    size_t free_int = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    upp_printf("[USB] RAM %s: internal %u KB free (largest block %u KB, lowest ever %u KB), "
               "DMA-capable %u KB, PSRAM %u KB\n", when,
               (unsigned)(free_int / 1024),
               (unsigned)(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL) / 1024),
               (unsigned)(heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL) / 1024),
               (unsigned)(heap_caps_get_free_size(MALLOC_CAP_DMA) / 1024),
               (unsigned)(heap_caps_get_free_size(MALLOC_CAP_SPIRAM) / 1024));
}

static void upp_str(const usb_str_desc_t *s, char *out, size_t n) {
    size_t k = 0;
    if (s)
        for (int i = 0; i < (s->bLength - 2) / 2 && k + 1 < n; i++) {
            uint16_t c = s->wData[i];
            out[k++] = (c >= 32 && c < 127) ? (char)c : '?';
        }
    out[k] = 0;
}

static const char *upp_class(uint8_t c, uint8_t s, uint8_t p) {
    if (c == 0x03) return p == 1 ? "HID keyboard" : p == 2 ? "HID mouse" : "HID";
    if (c == 0xFF && s == 0x5D && p == 0x01) return "Xbox 360-type controller (XInput)";
    if (c == 0xFF && s == 0x5D) return "Xbox 360 extra (headset, security)";
    if (c == 0xFF && s == 0x47 && p == 0xD0) return "Xbox One / Series controller (GIP)";
    if (c == 0xFF) return "vendor-specific";
    if (c == 0x01) return "audio";
    if (c == 0x02 || c == 0x0A) return "serial (CDC)";
    if (c == 0x08) return "storage";
    if (c == 0x09) return "hub";
    return "other";
}

static const char *upp_status(usb_transfer_status_t st) {
    switch (st) {
        case USB_TRANSFER_STATUS_ERROR:     return "error";
        case USB_TRANSFER_STATUS_TIMED_OUT: return "timed out";
        case USB_TRANSFER_STATUS_CANCELED:  return "cancelled";
        case USB_TRANSFER_STATUS_STALL:     return "stalled";
        case USB_TRANSFER_STATUS_OVERFLOW:  return "overflow";
        case USB_TRANSFER_STATUS_NO_DEVICE: return "device gone";
        default:                            return "?";
    }
}

// Interfaces worth reading: HID, and vendor-specific ones except the parts of an
// Xbox pad that are not the pad (headset, security).
static bool upp_follows(uint8_t c, uint8_t s, uint8_t p) {
    if (c == 0x03) return true;
    if (c != 0xFF) return false;
    if (s == 0x5D) return p == 0x01;
    if (s == 0x47) return p == 0xD0;
    return true;
}

// Does a HID report descriptor number its reports?  Then the first byte of each
// report is its ID, and report types are learnt and compared apart.
static bool upp_has_report_ids(const uint8_t *p, int n) {
    for (int i = 0; i < n;) {
        uint8_t b = p[i];
        if (b == 0xFE) {                              // long item
            if (i + 1 >= n) break;
            i += 3 + p[i + 1];
            continue;
        }
        if ((b & 0xFC) == 0x84) return true;          // Report ID
        uint8_t size = b & 0x03;
        i += 1 + (size == 3 ? 4 : size);
    }
    return false;
}

// ---- reports ---------------------------------------------------------------------
static UppKey *upp_key(UppStream &s, uint8_t id, uint32_t now) {
    for (auto &k : s.keys) if (k.used && k.id == id) return &k;
    for (auto &k : s.keys) {
        if (k.used) continue;
        memset(&k, 0, sizeof k);
        k.used = true;
        k.id = id;
        k.first_ms = now;
        if (s.gip) k.noisy[2] = 0xFF;                 // every GIP message counts up here
        return &k;
    }
    return &s.keys[UPP_MAX_KEYS - 1];                 // more types than slots: the last is shared
}

static void upp_report(UppDev &d, UppStream &s, const uint8_t *p, int n) {
    if (n > UPP_REPORT_MAX) n = UPP_REPORT_MAX;
    uint32_t now = millis();
    s.reports++;
    UppKey &k = *upp_key(s, s.keyed ? p[0] : 0, now);
    char tag[24];
    if (s.keyed) snprintf(tag, sizeof tag, "pad%u ep %02x #%02x", d.n, s.ep, k.id);
    else         snprintf(tag, sizeof tag, "pad%u ep %02x", d.n, s.ep);

    if (!k.learnt) {
        if (k.len == n)
            for (int i = 0; i < n; i++) k.noisy[i] |= p[i] ^ k.last[i];
        memcpy(k.last, p, n);
        k.len = n;
        if (now - k.first_ms < UPP_LEARN_MS) return;
        k.learnt = true;
        memcpy(k.shown, p, n);
        int bits = 0;
        for (int i = 0; i < n; i++) bits += __builtin_popcount(k.noisy[i]);
        upp_printf("[USB] %s: %d bytes; %d bits moved by themselves and are ignored from "
                   "here.  Now press buttons and move sticks.  At rest:\n", tag, n, bits);
        upp_hex("[USB]  ", p, n);
        return;
    }

    int changed = 0;
    for (int i = 0; i < n && i < k.len; i++)
        if ((p[i] ^ k.shown[i]) & ~k.noisy[i]) changed++;
    if (!changed && n == k.len) return;
    if (now - s.line_ms < UPP_LINE_GAP_MS) return;    // the next report shows it
    s.line_ms = now;

    char line[220];                                   // 10 changes fill ~150 of it
    int m = snprintf(line, sizeof line, "[USB] %s:", tag);
    int listed = 0;
    for (int i = 0; i < n && i < k.len; i++) {
        if (!((p[i] ^ k.shown[i]) & ~k.noisy[i])) continue;
        if (listed++ < 10) m += snprintf(line + m, sizeof line - m, " [%d] %02x>%02x", i, k.shown[i], p[i]);
    }
    if (listed > 10) m += snprintf(line + m, sizeof line - m, " and %d more", listed - 10);
    if (n != k.len) m += snprintf(line + m, sizeof line - m, " (now %d bytes, was %d)", n, k.len);
    if (m > (int)sizeof line - 2) m = sizeof line - 2;
    line[m++] = '\n';
    _upp_out->write((const uint8_t *)line, m);
    memcpy(k.shown, p, n);
    k.len = n;
}

static void upp_queue(UppDev &d, const uint8_t *data, uint8_t len, uint32_t at_ms, const char *what) {
    if (!d.out_ep || d.outq_n >= UPP_MAX_OUT || len > sizeof d.outq[0].data) return;
    UppOut &o = d.outq[d.outq_n++];
    memcpy(o.data, data, len);
    o.len = len;
    o.at_ms = at_ms;
    o.what = what;
}

static void upp_in_submit(UppStream &s) {
    s.xfer->num_bytes = s.mps;
    if (usb_host_transfer_submit(s.xfer) == ESP_OK) s.dev->inflight++;
    else upp_printf("[USB] pad%u ep %02x: could not start reading\n", s.dev->n, s.ep);
}

static void upp_in_cb(usb_transfer_t *x) {
    UppStream &s = *(UppStream *)x->context;
    UppDev &d = *s.dev;
    d.inflight--;
    if (x->status == USB_TRANSFER_STATUS_COMPLETED) {
        const uint8_t *p = x->data_buffer;
        int n = x->actual_num_bytes;
        // The Xbox button asks to be acknowledged, and is sent again until it is.
        if (s.gip && n >= 3 && p[0] == 0x07 && (p[1] & 0x10)) {
            uint8_t ack[13] = { 0x01, 0x20, 0x00, 0x09, 0x00, 0x07, 0x20, 0x02,
                                0x00, 0x00, 0x00, 0x00, 0x00 };
            ack[2] = p[2];                            // its own sequence number, echoed
            upp_queue(d, ack, sizeof ack, millis(), nullptr);
        }
        if (n > 0) upp_report(d, s, p, n);
        if (!d.gone) upp_in_submit(s);
        return;
    }
    if (x->status != USB_TRANSFER_STATUS_NO_DEVICE && x->status != USB_TRANSFER_STATUS_CANCELED)
        upp_printf("[USB] pad%u ep %02x: reading stopped (%s)\n", d.n, s.ep, upp_status(x->status));
}

// ---- packets to the device ---------------------------------------------------------
static void upp_out_cb(usb_transfer_t *x) {
    UppDev &d = *(UppDev *)x->context;
    d.out_busy = false;
    d.inflight--;
    if (x->status != USB_TRANSFER_STATUS_COMPLETED && x->status != USB_TRANSFER_STATUS_NO_DEVICE)
        upp_printf("[USB] pad%u: a packet to it failed (%s)\n", d.n, upp_status(x->status));
}

static void upp_out_service(UppDev &d, uint32_t now) {
    if (d.out_busy || !d.outq_n || (int32_t)(now - d.outq[0].at_ms) < 0) return;
    UppOut o = d.outq[0];
    d.outq_n--;
    memmove(d.outq, d.outq + 1, d.outq_n * sizeof(UppOut));
    if (d.gip && o.data[0] != 0x01) o.data[2] = d.gip_seq++;   // an ACK keeps the pad's number
    memcpy(d.out_xfer->data_buffer, o.data, o.len);
    d.out_xfer->num_bytes = o.len;
    if (usb_host_transfer_submit(d.out_xfer) != ESP_OK) {
        upp_printf("[USB] pad%u: could not send %s\n", d.n, o.what ? o.what : "a packet");
        return;
    }
    d.out_busy = true;
    d.inflight++;
    if (o.what) {
        char tag[64];
        snprintf(tag, sizeof tag, "[USB] pad%u sent %s:", d.n, o.what);
        upp_hex(tag, o.data, o.len);
    }
}

// ---- HID report descriptors, one at a time -------------------------------------------
static void upp_ctrl_cb(usb_transfer_t *x) {
    UppDev &d = *(UppDev *)x->context;
    d.ctrl_busy = false;
    d.inflight--;
    uint8_t intf = d.hid_intf[d.hid_cur];
    if (x->status != USB_TRANSFER_STATUS_COMPLETED) {
        if (x->status != USB_TRANSFER_STATUS_NO_DEVICE)
            upp_printf("[USB] pad%u interface %u: no report descriptor (%s)\n", d.n, intf, upp_status(x->status));
        return;
    }
    const uint8_t *p = x->data_buffer + sizeof(usb_setup_packet_t);
    int n = x->actual_num_bytes - (int)sizeof(usb_setup_packet_t);
    upp_printf("[USB] pad%u interface %u report descriptor, %d bytes:\n", d.n, intf, n);
    upp_hex("[USB]  ", p, n);
    if (!upp_has_report_ids(p, n)) return;
    for (int i = 0; i < d.n_streams; i++) {
        UppStream &s = d.streams[i];
        if (s.intf != intf || s.keyed) continue;
        s.keyed = true;
        memset(s.keys, 0, sizeof s.keys);             // learn again, a report type at a time
        upp_printf("[USB] pad%u ep %02x: it numbers its reports, so each report ID is followed on "
                   "its own (learning again: hands off for 2 s)\n", d.n, s.ep);
    }
}

static void upp_ctrl_service(UppDev &d) {
    if (d.ctrl_busy || d.hid_next >= d.n_hid || !d.ctrl_xfer) return;
    uint8_t i = d.hid_next++;
    uint16_t len = d.hid_len[i] && d.hid_len[i] < UPP_DESC_MAX ? d.hid_len[i] : UPP_DESC_MAX;
    usb_setup_packet_t *sp = (usb_setup_packet_t *)d.ctrl_xfer->data_buffer;
    sp->bmRequestType = 0x81;                         // to the host, standard, interface
    sp->bRequest      = 0x06;                         // GET_DESCRIPTOR
    sp->wValue        = 0x2200;                       // HID report descriptor
    sp->wIndex        = d.hid_intf[i];
    sp->wLength       = len;
    d.ctrl_xfer->num_bytes = sizeof(usb_setup_packet_t) + (len + d.mps0 - 1) / d.mps0 * d.mps0;
    d.hid_cur = i;
    if (usb_host_transfer_submit_control(_upp_client, d.ctrl_xfer) == ESP_OK) {
        d.ctrl_busy = true;
        d.inflight++;
    } else {
        upp_printf("[USB] pad%u interface %u: could not ask for its report descriptor\n", d.n, d.hid_intf[i]);
    }
}

// ---- plugging in and out ----------------------------------------------------------------
static void upp_attach(uint8_t addr) {
    UppDev *dp = nullptr;
    for (int i = 0; i < UPP_MAX_DEVS && !dp; i++) if (!_upp_devs[i].used) dp = &_upp_devs[i];
    if (!dp) { upp_printf("[USB] a device at address %u, but %d are open already: ignored\n", addr, UPP_MAX_DEVS); return; }
    UppDev &d = *dp;
    memset(&d, 0, sizeof d);
    if (usb_host_device_open(_upp_client, addr, &d.hdl) != ESP_OK) {
        upp_printf("[USB] a device at address %u could not be opened\n", addr);
        return;
    }
    d.used = true;
    d.n = _upp_next_n++;
    usb_device_info_t info;
    const usb_device_desc_t *dd;
    const usb_config_desc_t *cd;
    usb_host_device_info(d.hdl, &info);
    usb_host_get_device_descriptor(d.hdl, &dd);
    usb_host_get_active_config_descriptor(d.hdl, &cd);
    d.vid = dd->idVendor;
    d.pid = dd->idProduct;
    d.mps0 = dd->bMaxPacketSize0 ? dd->bMaxPacketSize0 : 8;
    char man[40], prod[40];
    upp_str(info.str_desc_manufacturer, man, sizeof man);
    upp_str(info.str_desc_product, prod, sizeof prod);
    upp_printf("\n[USB] pad%u plugged in: %04x:%04x \"%s\" \"%s\", %s speed, asks for %u mA\n",
               d.n, d.vid, d.pid, man, prod,
               info.speed == USB_SPEED_LOW ? "low" : info.speed == USB_SPEED_FULL ? "full" : "high",
               cd->bMaxPower * 2);
    if (dd->bDeviceClass)
        upp_printf("[USB]   device class %02x/%02x/%02x (%s)\n", dd->bDeviceClass, dd->bDeviceSubClass,
                   dd->bDeviceProtocol, upp_class(dd->bDeviceClass, dd->bDeviceSubClass, dd->bDeviceProtocol));

    const uint8_t *p = (const uint8_t *)cd;
    int total = cd->wTotalLength;
    int intf = -1, alt = 0;
    uint8_t cls = 0, sub = 0, proto = 0;
    bool follow = false, claimed = false, xinput = false;
    for (int off = 0; off + 2 <= total; off += p[off]) {
        uint8_t len = p[off], type = p[off + 1];
        if (len < 2 || off + len > total) break;
        if (type == 0x04 && len >= 9) {                       // interface
            intf = p[off + 2]; alt = p[off + 3];
            cls = p[off + 5]; sub = p[off + 6]; proto = p[off + 7];
            follow = alt == 0 && upp_follows(cls, sub, proto);
            claimed = false;
            if (alt) upp_printf("[USB]   interface %d alt %d: class %02x/%02x/%02x %s, %u endpoints\n",
                                intf, alt, cls, sub, proto, upp_class(cls, sub, proto), p[off + 4]);
            else     upp_printf("[USB]   interface %d: class %02x/%02x/%02x %s, %u endpoints\n",
                                intf, cls, sub, proto, upp_class(cls, sub, proto), p[off + 4]);
        } else if (type == 0x21 && len >= 9 && cls == 0x03 && alt == 0) {   // HID
            if (d.n_hid < UPP_MAX_HID) {
                d.hid_intf[d.n_hid] = intf;
                d.hid_len[d.n_hid] = p[off + 7] | (p[off + 8] << 8);
                d.n_hid++;
            }
        } else if (type == 0x05 && len >= 7) {                // endpoint
            uint8_t ep = p[off + 2], kind = p[off + 3] & 0x03, ival = p[off + 6];
            uint16_t mps = (p[off + 4] | (p[off + 5] << 8)) & 0x7FF;
            static const char *const KIND[] = { "control", "isochronous", "bulk", "interrupt" };
            upp_printf("[USB]     endpoint %02x %s %s, %u bytes, every %u ms\n", ep, KIND[kind],
                       (ep & 0x80) ? "IN" : "OUT", mps, ival);
            if (!follow || kind != 3) continue;
            if (!claimed) {
                esp_err_t e = usb_host_interface_claim(_upp_client, d.hdl, intf, 0);
                if (e != ESP_OK) {
                    upp_printf("[USB]     could not claim interface %d (%s)\n", intf, esp_err_to_name(e));
                    follow = false;
                    continue;
                }
                claimed = true;
                if (d.n_claimed < sizeof d.claimed) d.claimed[d.n_claimed++] = intf;
                if (cls == 0xFF && sub == 0x47) d.gip = true;
                if (cls == 0xFF && sub == 0x5D) xinput = true;
            }
            if ((ep & 0x80) && d.n_streams < UPP_MAX_STREAMS && mps && mps <= UPP_REPORT_MAX) {
                UppStream &s = d.streams[d.n_streams++];
                s.dev = &d;
                s.intf = intf;
                s.ep = ep;
                s.mps = mps;
                s.keyed = cls == 0xFF;                   // vendor pads lead with a message type
                s.gip = cls == 0xFF && sub == 0x47;
            } else if (!(ep & 0x80) && !d.out_ep) {
                d.out_ep = ep;
            }
        }
    }

    uint32_t now = millis();
    for (int i = 0; i < d.n_streams; i++) {
        UppStream &s = d.streams[i];
        if (usb_host_transfer_alloc(s.mps, 0, &s.xfer) != ESP_OK) {
            upp_printf("[USB] pad%u ep %02x: no memory for a transfer\n", d.n, s.ep);
            continue;
        }
        s.xfer->device_handle = d.hdl;
        s.xfer->bEndpointAddress = s.ep;
        s.xfer->callback = upp_in_cb;
        s.xfer->context = &s;
        s.xfer->timeout_ms = 0;
        s.rate_ms = now;
        upp_in_submit(s);
    }
    if (d.out_ep && usb_host_transfer_alloc(UPP_REPORT_MAX, 0, &d.out_xfer) == ESP_OK) {
        d.out_xfer->device_handle = d.hdl;
        d.out_xfer->bEndpointAddress = d.out_ep;
        d.out_xfer->callback = upp_out_cb;
        d.out_xfer->context = &d;
        d.out_xfer->timeout_ms = 0;
    } else {
        d.out_ep = 0;
    }
    if (d.n_hid && usb_host_transfer_alloc(sizeof(usb_setup_packet_t) + UPP_DESC_MAX + 64, 0, &d.ctrl_xfer) == ESP_OK) {
        d.ctrl_xfer->device_handle = d.hdl;
        d.ctrl_xfer->bEndpointAddress = 0;
        d.ctrl_xfer->callback = upp_ctrl_cb;
        d.ctrl_xfer->context = &d;
        d.ctrl_xfer->timeout_ms = 0;
    }

    if (d.gip) {
        static const uint8_t power_on[] = { 0x05, 0x20, 0x00, 0x01, 0x00 };
        upp_queue(d, power_on, sizeof power_on, now + 50, "the Xbox power-on");
        if (d.vid == 0x045E && d.pid == 0x02EA) {
            static const uint8_t s_init[] = { 0x05, 0x20, 0x00, 0x0F, 0x06 };
            upp_queue(d, s_init, sizeof s_init, now + 60, "the Xbox One S start");
        }
    } else if (xinput) {
        static const uint8_t led[] = { 0x01, 0x03, 0x06 };
        upp_queue(d, led, sizeof led, now + 50, "player-1 light");
    }
    if (d.vid == 0x057E && d.pid == 0x2009) {
        static const uint8_t hs[] = { 0x80, 0x02 }, fast[] = { 0x80, 0x03 }, usb_only[] = { 0x80, 0x04 };
        upp_queue(d, hs, sizeof hs, now + 100, "the Switch Pro handshake");
        upp_queue(d, fast, sizeof fast, now + 150, "the Switch Pro link speed");
        upp_queue(d, hs, sizeof hs, now + 200, "the Switch Pro handshake");
        upp_queue(d, usb_only, sizeof usb_only, now + 250, "Switch Pro: USB only, no timeout");
    }

    if (!d.n_streams) upp_printf("[USB] pad%u: nothing here this probe reads\n", d.n);
    else upp_printf("[USB] pad%u: hands off the controller for 2 s while it learns which bits "
                    "move by themselves...\n", d.n);
    upp_ram("with it open");
}

static void upp_detach(usb_device_handle_t hdl) {
    for (int i = 0; i < UPP_MAX_DEVS; i++) {
        UppDev &d = _upp_devs[i];
        if (!d.used || d.hdl != hdl || d.gone) continue;
        d.gone = true;
        upp_printf("[USB] pad%u unplugged\n", d.n);
    }
}

// Once nothing is out any more.
static void upp_free(UppDev &d) {
    for (int i = 0; i < d.n_streams; i++) {
        usb_host_endpoint_halt(d.hdl, d.streams[i].ep);
        usb_host_endpoint_flush(d.hdl, d.streams[i].ep);
        if (d.streams[i].xfer) usb_host_transfer_free(d.streams[i].xfer);
    }
    if (d.out_ep) {
        usb_host_endpoint_halt(d.hdl, d.out_ep);
        usb_host_endpoint_flush(d.hdl, d.out_ep);
    }
    if (d.out_xfer) usb_host_transfer_free(d.out_xfer);
    if (d.ctrl_xfer) usb_host_transfer_free(d.ctrl_xfer);
    for (int i = 0; i < d.n_claimed; i++) {
        esp_err_t e = usb_host_interface_release(_upp_client, d.hdl, d.claimed[i]);
        if (e != ESP_OK) upp_printf("[USB] pad%u: releasing interface %u: %s\n", d.n, d.claimed[i], esp_err_to_name(e));
    }
    esp_err_t e = usb_host_device_close(_upp_client, d.hdl);
    if (e != ESP_OK) upp_printf("[USB] pad%u: closing: %s\n", d.n, esp_err_to_name(e));
    d.used = false;
    upp_ram("after it went");
}

static void upp_client_cb(const usb_host_client_event_msg_t *msg, void *) {
    if (msg->event == USB_HOST_CLIENT_EVENT_NEW_DEV) upp_attach(msg->new_dev.address);
    else if (msg->event == USB_HOST_CLIENT_EVENT_DEV_GONE) upp_detach(msg->dev_gone.dev_hdl);
}

// ---- tasks ---------------------------------------------------------------------------
static void upp_lib_task(void *) {
    for (;;) {
        uint32_t flags;
        usb_host_lib_handle_events(portMAX_DELAY, &flags);
    }
}

// Installs the host itself, so that its interrupt lands on this core (0), away
// from the RGB panel's: the panel refills its bounce buffer from an interrupt on
// the core that made it, and a late refill slips the picture.
static void upp_client_task(void *) {
    upp_printf("\n[USB] USB controller probe.  Plug a controller into the display's \"USB\" port.\n"
               "[USB] This is the \"USB to UART\" port: power the display through it.\n");
    if (!_upp_devs) {
        upp_printf("[USB] no memory for the probe's own tables\n");
        vTaskDelete(nullptr);
    }
    _upp_ram_before = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    upp_ram("before the USB host");

    usb_host_config_t cfg = {};
    cfg.skip_phy_setup = false;
    cfg.intr_flags = ESP_INTR_FLAG_LEVEL1;
    esp_err_t e = usb_host_install(&cfg);
    if (e != ESP_OK) {
        upp_printf("[USB] the USB host would not start: %s\n", esp_err_to_name(e));
        vTaskDelete(nullptr);
    }
    xTaskCreatePinnedToCore(upp_lib_task, "usb_lib", 4096, nullptr, 6, nullptr, 0);

    usb_host_client_config_t cc = {};
    cc.is_synchronous = false;
    cc.max_num_event_msg = 5;
    cc.async.client_event_callback = upp_client_cb;
    cc.async.callback_arg = nullptr;
    e = usb_host_client_register(&cc, &_upp_client);
    if (e != ESP_OK) {
        upp_printf("[USB] could not register with the USB host: %s\n", esp_err_to_name(e));
        vTaskDelete(nullptr);
    }
    size_t now_free = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    upp_ram("with the USB host running");
    upp_printf("[USB] the USB host took %d KB of internal RAM, its two tasks' stacks included\n",
               (int)((int)_upp_ram_before - (int)now_free) / 1024);

    uint32_t ram_ms = millis();
    for (;;) {
        usb_host_client_handle_events(_upp_client, pdMS_TO_TICKS(20));
        uint32_t now = millis();
        for (int i = 0; i < UPP_MAX_DEVS; i++) {
            UppDev &d = _upp_devs[i];
            if (!d.used) continue;
            if (d.gone) {
                if (d.inflight <= 0) upp_free(d);
                continue;
            }
            upp_out_service(d, now);
            upp_ctrl_service(d);
            for (int i = 0; i < d.n_streams; i++) {
                UppStream &s = d.streams[i];
                if (now - s.rate_ms < UPP_RATE_MS) continue;
                upp_printf("[USB] pad%u ep %02x: %u reports a second\n", d.n, s.ep,
                           (unsigned)((s.reports - s.reports_then) * 1000UL / (now - s.rate_ms)));
                s.rate_ms = now;
                s.reports_then = s.reports;
            }
        }
        if (now - ram_ms >= UPP_RAM_MS) {
            upp_ram("now");
            ram_ms = now;
        }
    }
}

static void usb_pad_probe_begin(Print &out) {
    _upp_out = &out;
    _upp_devs = (UppDev *)heap_caps_calloc(UPP_MAX_DEVS, sizeof(UppDev), MALLOC_CAP_SPIRAM);
    if (!_upp_devs) _upp_devs = (UppDev *)calloc(UPP_MAX_DEVS, sizeof(UppDev));
    xTaskCreatePinnedToCore(upp_client_task, "usb_probe", 6144, nullptr, 5, nullptr, 0);
}
