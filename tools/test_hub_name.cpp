// Exercise hub_name_set()'s sanitiser and SSID composition as written.
#include <cstdio>
#include <string>
#include <cstring>
#include <cstdint>
#define AP_SSID_PREFIX  "PTS-"
#define HUB_NAME_MAX    12
static char AP_SSID[5 + HUB_NAME_MAX] = AP_SSID_PREFIX "Hub";
static char _hub_name[HUB_NAME_MAX + 1] = "";
static uint8_t FAKE_MAC[6] = {0,0,0,0,0x4B,0x2C};

static void hub_name_apply() {
    if (_hub_name[0] == '\0')
        snprintf(_hub_name, sizeof(_hub_name), "Hub-%02X%02X", FAKE_MAC[4], FAKE_MAC[5]);
    snprintf(AP_SSID, sizeof(AP_SSID), "%s%s", AP_SSID_PREFIX, _hub_name);
}
static bool hub_name_set(const char *name) {
    if (!name) return false;
    char clean[HUB_NAME_MAX + 1] = {};
    size_t j = 0;
    for (size_t i = 0; name[i] && j < HUB_NAME_MAX; i++)
        if (name[i] >= 0x20 && name[i] < 0x7F) clean[j++] = name[i];
    while (j > 0 && clean[j - 1] == ' ') clean[--j] = '\0';
    if (j == 0) return false;
    strncpy(_hub_name, clean, sizeof(_hub_name));
    _hub_name[HUB_NAME_MAX] = '\0';
    hub_name_apply();
    return true;
}
static int fails = 0;
static void chk(const char*l, const char*g, const char*w){
    bool ok=!strcmp(g,w); if(!ok)fails++;
    printf("  [%s] %-40s %-20s%s\n", ok?"ok  ":"FAIL", l, g, ok?"":(std::string("want ")+w).c_str());
}
int main(){
    printf("Default from MAC when nothing stored:\n");
    _hub_name[0]='\0'; hub_name_apply();
    chk("fresh unit is identifiable", AP_SSID, "PTS-Hub-4B2C");

    printf("\nReal names:\n");
    struct { const char* in; const char* ssid; } ok[] = {
        {"Concert Hall", "PTS-Concert Hall"},
        {"Foyer",        "PTS-Foyer"},
        {"Lecture Thtr", "PTS-Lecture Thtr"},
        {"Studio B",     "PTS-Studio B"},
    };
    for (auto &t : ok) { hub_name_set(t.in); chk(t.in, AP_SSID, t.ssid); }

    printf("\nSSID never exceeds what a mount can show (16 chars):\n");
    for (auto &t : ok) { hub_name_set(t.in);
        printf("  %-18s %2zu chars %s\n", AP_SSID, strlen(AP_SSID),
               strlen(AP_SSID) <= 16 ? "ok" : "TOO LONG");
        if (strlen(AP_SSID) > 16) fails++; }

    printf("\nOver-length is truncated, not rejected:\n");
    hub_name_set("Main Auditorium South");
    chk("clipped to 12 chars", AP_SSID, "PTS-Main Auditor");   // "Main Auditor" = 12

    printf("\nRubbish is refused rather than stored:\n");
    hub_name_set("Foyer");
    printf("  [%s] empty string refused                 %s\n",
           !hub_name_set("") ? "ok  " : "FAIL", AP_SSID);
    if (hub_name_set("")) fails++;
    printf("  [%s] whitespace-only refused              %s\n",
           !hub_name_set("   ") ? "ok  " : "FAIL", AP_SSID);
    if (hub_name_set("   ")) fails++;
    chk("previous name survives a refusal", AP_SSID, "PTS-Foyer");

    printf("\nControl characters stripped (LVGL label safety):\n");
    hub_name_set("Fo\ny\ter");
    chk("newline/tab removed", AP_SSID, "PTS-Foyer");
    hub_name_set("Studio B   ");
    chk("trailing spaces trimmed", AP_SSID, "PTS-Studio B");

    printf("\nRESULT: %s\n", fails ? "FAILURES" : "ALL PASS");
    return fails ? 1 : 0;
}
