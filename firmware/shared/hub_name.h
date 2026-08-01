#pragma once
// ---------------------------------------------------------------------------
// Location name for a hub or satellite, carried in its AP SSID.
// ---------------------------------------------------------------------------
// A mount builds its setup list from a WiFi scan, before any ESP-NOW link
// exists, so the beacon is the only thing it can read at that moment: a
// friendly name has to be IN the SSID.  Both the hub and the satellites need
// exactly this, and a mount cannot tell them apart, so the rule lives here
// rather than being copied — a divergent second copy of a wire-visible format
// is how the 9-byte STATUS bug happened.
//
// Requires, from the including sketch:
//   AP_SSID_PREFIX   scan filter, must equal HUB_SSID_PREFIX in the mount
//   HUB_NAME_MAX     usable name characters
//   AP_SSID          char[] the composed SSID is written into
//   _prefs           an open-able Preferences instance
//
#include <Preferences.h>
#include <esp_mac.h>

// ---------------------------------------------------------------------------
// Hub location name  ("Concert Hall", "Foyer", ...)
// ---------------------------------------------------------------------------
// Stored in NVS and composed into the AP SSID at boot, because a mount picks
// its hub from a WiFi scan and the beacon is the only thing it can read at that
// point.  Capped at HUB_NAME_MAX so the whole SSID fits the 16 characters a
// mount keeps per entry — a longer name would be silently truncated on the
// mount's screen, which is worse than refusing it here.

static char _hub_name[HUB_NAME_MAX + 1] = "";
// Set by the /hubname POST handler; loop() bounces the AP once this passes,
// so the reply reaches the browser before its connection is torn down.
static uint32_t _hub_name_restart_ms = 0;

// Compose AP_SSID from the stored name, falling back to a MAC-derived default
// so a factory-fresh unit is still distinguishable in a building of them.
static void hub_name_apply() {
    if (_hub_name[0] == '\0') {
        uint8_t mac[6] = {};
        esp_read_mac(mac, ESP_MAC_WIFI_SOFTAP);
        snprintf(_hub_name, sizeof(_hub_name), "Hub-%02X%02X", mac[4], mac[5]);
    }
    snprintf(AP_SSID, sizeof(AP_SSID), "%s%s", AP_SSID_PREFIX, _hub_name);
}

static void hub_name_load() {
    _prefs.begin("mounts", false);
    _prefs.getString("hubname", _hub_name, sizeof(_hub_name));
    _prefs.end();
    _hub_name[HUB_NAME_MAX] = '\0';
    hub_name_apply();
}

// Returns false if the name is unusable; the caller reports that rather than
// silently storing something the mount cannot display.
static bool hub_name_set(const char *name) {
    if (!name) return false;
    char clean[HUB_NAME_MAX + 1] = {};
    size_t j = 0;
    for (size_t i = 0; name[i] && j < HUB_NAME_MAX; i++) {
        // Printable ASCII only: the mount renders this on an LVGL label and a
        // stray control character would corrupt the row.
        if (name[i] >= 0x20 && name[i] < 0x7F) clean[j++] = name[i];
    }
    while (j > 0 && clean[j - 1] == ' ') clean[--j] = '\0';   // trim trailing space
    if (j == 0) return false;
    strncpy(_hub_name, clean, sizeof(_hub_name));
    _hub_name[HUB_NAME_MAX] = '\0';
    _prefs.begin("mounts", false);
    _prefs.putString("hubname", _hub_name);
    _prefs.end();
    hub_name_apply();
    return true;
}

