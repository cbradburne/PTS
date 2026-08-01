// Exercise the saved-hub list rules as written in esp_mount_amoled175.ino:
// move-to-front eviction, and the short-read NVS migration.
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <cstddef>
#define MAX_KNOWN_HUBS 16
struct KnownHub { uint8_t mac[6]; uint8_t channel; char ssid[17]; };
struct MountCfg { uint8_t magic, mount_id, n_hubs, last_hub; KnownHub hubs[MAX_KNOWN_HUBS]; };
#define CFG_MAGIC 0xC3
static MountCfg _cfg;

static KnownHub mk(uint8_t id) { KnownHub h{}; h.mac[5]=id; h.channel=1;
                                 snprintf(h.ssid,17,"PTS-%d",id); return h; }

static void pair(KnownHub sel) {                       // mirrors setup_save()
    int8_t found = -1;
    for (uint8_t i=0;i<_cfg.n_hubs;i++)
        if (memcmp(_cfg.hubs[i].mac, sel.mac, 6)==0) { found=(int8_t)i; break; }
    uint8_t at;
    if (found >= 0)                       at = (uint8_t)found;
    else if (_cfg.n_hubs<MAX_KNOWN_HUBS){ at = _cfg.n_hubs; _cfg.n_hubs++; }
    else                                  at = MAX_KNOWN_HUBS-1;
    for (uint8_t i=at;i>0;i--) _cfg.hubs[i]=_cfg.hubs[i-1];
    _cfg.hubs[0]=sel; _cfg.last_hub=0;
}
static void order(char*out){ int p=0; for(uint8_t i=0;i<_cfg.n_hubs;i++)
    p+=snprintf(out+p,64-p,"%d ",_cfg.hubs[i].mac[5]); out[p?p-1:0]='\0'; }

static int fails=0;
static void chk(const char*l,const char*g,const char*w){ bool ok=!strcmp(g,w);
    if(!ok)fails++; printf("  [%s] %-44s %s\n", ok?"ok  ":"FAIL", l, ok?g:
        (printf("got '%s' want '%s'",g,w),"")); }

int main(){
    char buf[64];
    printf("Recency ordering (newest first):\n");
    memset(&_cfg,0,sizeof(_cfg)); _cfg.magic=CFG_MAGIC; _cfg.mount_id=1;
    for (uint8_t i=1;i<=4;i++) pair(mk(i));
    order(buf); chk("paired 1,2,3,4", buf, "4 3 2 1");
    pair(mk(2)); order(buf); chk("re-pair 2 promotes it", buf, "2 4 3 1");

    printf("\nGrows past the old limit of 4:\n");
    for (uint8_t i=5;i<=16;i++) pair(mk(i));
    printf("  n_hubs = %d\n", _cfg.n_hubs);
    chk("holds 16", _cfg.n_hubs==16?"16":"?", "16");

    printf("\nFull list evicts the OLDEST, not slot 4:\n");
    order(buf); printf("  before: %s\n", buf);
    pair(mk(99)); order(buf); printf("  after : %s\n", buf);
    chk("newest at front", _cfg.hubs[0].mac[5]==99?"99":"?", "99");
    // Check the ARRAY, not the printed string — " 1" also matches inside " 16".
    bool has1=false; for(uint8_t i=0;i<_cfg.n_hubs;i++) if(_cfg.hubs[i].mac[5]==1) has1=true;
    chk("oldest (1) evicted", has1?"still there":"gone", "gone");
    bool has3=false; for(uint8_t i=0;i<_cfg.n_hubs;i++) if(_cfg.hubs[i].mac[5]==3) has3=true;
    chk("second-oldest (3) retained", has3?"kept":"lost", "kept");
    chk("still 16", _cfg.n_hubs==16?"16":"?", "16");

    printf("\nShort-read migration — an old 4-slot blob:\n");
    struct OldCfg { uint8_t magic,mount_id,n_hubs,last_hub; KnownHub hubs[4]; };
    OldCfg old{}; old.magic=CFG_MAGIC; old.mount_id=3; old.n_hubs=3; old.last_hub=1;
    for (int i=0;i<3;i++) old.hubs[i]=mk((uint8_t)(10+i));
    MountCfg fresh; memset(&fresh,0,sizeof(fresh));
    size_t n = sizeof(OldCfg);
    memcpy(&fresh, &old, n);                       // the short getBytes()
    const size_t hdr = offsetof(MountCfg,hubs);
    const size_t slots_in = (n>hdr)?(n-hdr)/sizeof(KnownHub):0;
    bool valid = (n >= hdr+sizeof(KnownHub) && n <= sizeof(fresh) &&
                  fresh.magic==CFG_MAGIC && fresh.mount_id>=1 && fresh.mount_id<=5 &&
                  fresh.n_hubs>=1 && fresh.n_hubs<=MAX_KNOWN_HUBS &&
                  fresh.n_hubs<=slots_in && fresh.last_hub<fresh.n_hubs);
    printf("  read %zu of %zu bytes, %zu slots present\n", n, sizeof(fresh), slots_in);
    chk("old config still valid (no re-pairing)", valid?"valid":"REJECTED", "valid");
    chk("mount_id preserved", fresh.mount_id==3?"3":"?", "3");
    chk("hub 0 preserved", fresh.hubs[0].ssid, "PTS-10");
    chk("hub 2 preserved", fresh.hubs[2].ssid, "PTS-12");
    chk("unused slot zeroed", fresh.hubs[9].ssid[0]==0?"empty":"stale", "empty");

    printf("\nCorrupt n_hubs claiming more than was read:\n");
    memcpy(&fresh,&old,n); fresh.n_hubs=12;
    valid = (fresh.n_hubs<=slots_in);
    chk("rejected", valid?"accepted":"rejected", "rejected");

    printf("\nRESULT: %s\n", fails?"FAILURES":"ALL PASS");
    return fails?1:0;
}
