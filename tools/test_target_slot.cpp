// Exercise the target_slot clear rule exactly as written in teensy41_mount.ino,
// including the look_at_mode gate that protects position slots 9 and 10.
#include <cstdio>
#include <cstdint>
#define TARGET_SLOT_NONE    0xFF
#define TARGET_SLOT_LA_MIN  8
#define TARGET_SLOT_LA_MAX  9
enum { STATE_IDLE=0, STATE_JOGGING=1, STATE_MOVING_TO_POS=2, STATE_LOOK_AT_MOVE=5,
       STATE_LOOK_AT_PRE_AIM=7 };

struct Cfg { bool look_at_mode; };
static Cfg _cfg;
static uint8_t  _target_slot;
static uint16_t _slot_at;
static uint8_t  _prev_motion_state;

static void tick(uint8_t cur_state) {
    if (_target_slot != TARGET_SLOT_NONE) {
        bool la_arrow = _cfg.look_at_mode &&
                        (_target_slot == TARGET_SLOT_LA_MIN ||
                         _target_slot == TARGET_SLOT_LA_MAX);
        if (la_arrow) {
            bool la_running = (cur_state == STATE_LOOK_AT_MOVE ||
                               cur_state == STATE_LOOK_AT_PRE_AIM);
            if (!la_running) _target_slot = TARGET_SLOT_NONE;
        } else {
            bool move_done = (_prev_motion_state == STATE_MOVING_TO_POS &&
                              cur_state          != STATE_MOVING_TO_POS);
            bool arrived   = !!(_slot_at & (1u << _target_slot));
            if (move_done || arrived) _target_slot = TARGET_SLOT_NONE;
        }
    }
    _prev_motion_state = cur_state;
}

static int fails = 0;
static void chk(const char* label, uint8_t got, uint8_t want) {
    bool ok = got == want;
    if (!ok) fails++;
    printf("  [%s] %-52s got=%3d want=%3d\n", ok ? "ok  " : "FAIL", label, got, want);
}
static void reset(bool la, uint8_t target, uint16_t at) {
    _cfg.look_at_mode = la; _target_slot = target; _slot_at = at;
    _prev_motion_state = STATE_IDLE;
}

int main() {
    printf("Look-at mount, right arrow running:\n");
    reset(true, TARGET_SLOT_LA_MAX, 0);
    tick(STATE_LOOK_AT_PRE_AIM); chk("holds through pre-aim",        _target_slot, 9);
    tick(STATE_LOOK_AT_MOVE);    chk("holds through the move",       _target_slot, 9);
    tick(STATE_LOOK_AT_MOVE);    chk("still holding",                _target_slot, 9);
    tick(STATE_IDLE);            chk("clears on arrival (IDLE)",     _target_slot, 255);

    printf("\nLook-at arrow interrupted:\n");
    reset(true, TARGET_SLOT_LA_MIN, 0);
    tick(STATE_LOOK_AT_MOVE);    chk("left arrow running",           _target_slot, 8);
    tick(STATE_IDLE);            chk("E-stop/abort clears it",       _target_slot, 255);

    printf("\nNON-look-at mount recalling position slots 9 and 10:\n");
    reset(false, 8, 0);
    tick(STATE_MOVING_TO_POS);   chk("slot 9 survives (not an arrow)", _target_slot, 8);
    tick(STATE_MOVING_TO_POS);   chk("  still moving",                 _target_slot, 8);
    _slot_at = (1u << 8);
    tick(STATE_MOVING_TO_POS);   chk("  clears on AT_POSITION",        _target_slot, 255);
    reset(false, 9, 0);
    tick(STATE_MOVING_TO_POS);   chk("slot 10 survives",               _target_slot, 9);
    tick(STATE_IDLE);            chk("  clears when move ends",        _target_slot, 255);

    printf("\nOrdinary slot recall on a LOOK-AT mount (slots 0-7):\n");
    reset(true, 3, 0);
    tick(STATE_MOVING_TO_POS);   chk("slot 4 uses the GOTO rule",      _target_slot, 3);
    _slot_at = (1u << 3);
    tick(STATE_MOVING_TO_POS);   chk("  clears on AT_POSITION",        _target_slot, 255);

    printf("\nIdle with nothing pending:\n");
    reset(true, TARGET_SLOT_NONE, 0);
    tick(STATE_IDLE);            chk("stays none",                     _target_slot, 255);

    printf("\nRESULT: %s\n", fails ? "FAILURES" : "ALL PASS");
    return fails ? 1 : 0;
}
