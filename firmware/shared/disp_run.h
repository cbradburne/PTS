#pragma once
// ---------------------------------------------------------------------------
// The hub display's RUN button: what a tap on it does.
// ---------------------------------------------------------------------------
// The web app's toggleRun() and the PC app's Run, decision for decision, for
// the camera the display's detail screen shows:
//   - a run under way: stop it, with CMD_STOP_RUN (not a zero jog, which the
//     mount no longer reads as anybody taking over);
//   - a look-at mount: ping-pong its slider following a subject — the one
//     selected, else the first stored — at its slider speed preset, ◄ first,
//     with START_LOOK_AT_MOVE's `repeat` byte set so the MOUNT turns it round;
//   - otherwise: cycle the stored positions, CMD_START_RUN, which needs two.
// The mount runs it (pos_run.h and the look-at run beside it), not the display,
// and the button follows what the mount reports in STATUS byte [10], which the
// hub passes on as UPDATE_CAM's seventh byte.
//
// Both hub displays (firmware/esp32_display, firmware/esp32_display_p4) call
// this rather than carrying a copy each, and tools/test_display_run_button.py
// compiles it on a laptop.
#include <stdint.h>
#include "protocol.h"
#include "disp_uart.h"
#include "pos_run.h"

enum class DispRunAct : uint8_t {
    STOP,             // send CMD_STOP_RUN
    START_POSITIONS,  // send CMD_START_RUN, no payload
    START_LOOK_AT,    // send CMD_START_LOOK_AT_MOVE with payload[0..3]
    // Refused, and the button says why for a moment:
    OFFLINE,          // the mount is not connected
    OLD_FIRMWARE,     // no run byte: the hub or the mount predates runs a
                      // client can see, so a run could start and never show
    TOO_FEW,          // fewer than two stored positions to run between
    NO_SUBJECT,       // a look-at mount with no stored subject
};

struct DispRunDecision {
    DispRunAct act;
    uint8_t    payload[4];   // START_LOOK_AT only: subject, direction, preset, repeat
};

// Whether a mount reports a run under way, either kind.  `run` is UPDATE_CAM's
// seventh byte: STATUS byte [10], or DISP_RUN_UNKNOWN.
static inline bool disp_run_active(uint8_t run) {
    return run != DISP_RUN_UNKNOWN
        && (run & (STATUS_RUN_POSITIONS | STATUS_RUN_LOOK_AT)) != 0;
}

// What a tap on RUN does for one camera.
//   occupied        the mount's stored slots — positions, or on a look-at
//                   mount its subjects (the display keeps both in one mask,
//                   as the hub sends them)
//   active_subject  the look-at subject selected on screen, -1 for none
//   sl_preset       the mount's slider speed preset, 1-4, 0 if not known
static inline DispRunDecision disp_run_decide(bool connected, uint8_t run,
                                              bool look_at, uint16_t occupied,
                                              int8_t active_subject,
                                              uint8_t sl_preset) {
    DispRunDecision d = {};
    if (!connected)              { d.act = DispRunAct::OFFLINE;      return d; }
    if (disp_run_active(run))    { d.act = DispRunAct::STOP;         return d; }
    if (run == DISP_RUN_UNKNOWN) { d.act = DispRunAct::OLD_FIRMWARE; return d; }
    if (look_at) {
        int8_t subj = active_subject;
        for (int b = 0; subj < 0 && b < MAX_SUBJECTS; b++)
            if (occupied & (1u << b)) subj = (int8_t)b;
        if (subj < 0) { d.act = DispRunAct::NO_SUBJECT; return d; }
        d.act = DispRunAct::START_LOOK_AT;
        d.payload[0] = (uint8_t)subj;
        d.payload[1] = 0;                              // ◄ min end first
        d.payload[2] = sl_preset > 0 ? sl_preset : 2;  // as the web app
        d.payload[3] = 1;                              // repeat: the mount turns it round
        return d;
    }
    // The mount's own rule (pos_run.h), so the button and the mount cannot
    // disagree about what "two stored positions" means.
    d.act = pos_run_stored(occupied) >= 2 ? DispRunAct::START_POSITIONS
                                          : DispRunAct::TOO_FEW;
    return d;
}
