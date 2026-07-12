# Camera Mount Controller — Version 2 Design

## What's new in v2

V2 adds **look-at tracking**: the camera continuously aims at a named subject
in 3D space while the slider moves independently.  Pan and tilt become
*derived* values — computed from the current slider position and the
subject's calibrated 3D world coordinates — rather than independent motor
targets.

V1 is preserved in `/Users/col/New CC` and remains fully functional.

---

## Coordinate system

```
                         Z  (away from rail, into room)
                         │
               subject ──┼──────────────────●  (sx, sy, sz)
                         │               ╱
                         │            ╱  look-at vector
                         │         ╱
  Y (up)                 │      ╱
    │                    │   ╱
    │   slider rail ─────●──────────────────▶  X (along rail)
    │                  cam at
    └──────────────────  (slider_pos, cam_height, 0)
```

- **X axis** — along the slider rail; 0 = slider home (min limit)
- **Y axis** — vertical; 0 = camera pivot height (set at calibration)
- **Z axis** — perpendicular to rail, into the room (positive = toward subjects)
- All units: **millimetres**

### Look-at angles

Given current camera position `(cx, 0, 0)` and subject `(sx, sy, sz)`:

```
dx = sx - cx
dz = sz          (camera Z is always 0)
dy = sy          (camera Y pivot is 0 by definition)

pan_rad  = atan2(dx, dz)                       // horizontal
tilt_rad = atan2(dy, sqrt(dx*dx + dz*dz))      // vertical
```

These are converted to motor steps using the existing
`physToUSteps` / degrees-to-steps calibration.

---

## Subjects vs Slider Locations

The 10-slot grid is split into two independent concepts:

| Concept | Count | Stored on | Description |
|---|---|---|---|
| **Subjects** | up to 8 | Teensy EEPROM | 3D world coords of a person/object |
| **Slider moves** | up to 8 | Teensy EEPROM | Slider start+end position + speed |

A **shot** combines one slider move with one subject: "move the slider from
A to B while keeping the camera aimed at Subject 3."  Shots can be chained.

---

## Subject calibration workflow

1. User presses **"Add Subject"** (PC / hub / web)
2. Firmware auto-moves slider to `slider_min` (home)
3. UI prompts: *"Aim camera at subject, then press SET"*
4. User jogs pan/tilt to subject, presses SET →
   firmware records `(slider_pos_A, pan_steps_A, tilt_steps_A)`
5. Firmware auto-moves slider to `slider_max`
6. UI prompts: *"Re-aim camera at same subject, then press SET"*
7. User jogs pan/tilt, presses SET →
   firmware records `(slider_pos_B, pan_steps_B, tilt_steps_B)`
8. Firmware solves for `(sx, sy, sz)` from the two observations and saves
   to EEPROM.

### Solving for 3D position from two observations

At slider pos `xA`, camera aims along unit vector `vA`.
At slider pos `xB`, camera aims along unit vector `vB`.

Two rays in 3D space; solve for their nearest intersection
(least-squares, since real measurements won't be perfect):

```
Ray A:  P = (xA, 0, 0) + t * vA
Ray B:  P = (xB, 0, 0) + s * vB
```

This is a standard ray-ray closest-point problem solved with a 2×2 linear
system.  The midpoint of the shortest segment between the two rays is the
estimated subject position.

---

## New firmware state machine

```
STATE_IDLE
STATE_JOGGING            (unchanged from v1)
STATE_MOVING_TO_POS      (unchanged — used for simple GOTO)
STATE_FINDING_LIMITS     (unchanged)
STATE_LOOK_AT_MOVE       (NEW)
  └─ slider moves on its own profile
  └─ pan/tilt updated every LOOK_AT_INTERVAL_MS from current slider pos
  └─ subject can be switched mid-move (pan/tilt smoothly steers)
  └─ pan/tilt speed capped at LOOK_AT_MAX_PT_DEG_S (configurable)
STATE_CALIBRATING_SUBJECT (NEW)
  └─ sub-states: WAITING_POINT_A → MOVING_TO_B → WAITING_POINT_B → SOLVING
```

---

## Look-at controller (replaces moveTo for pan/tilt during a slider move)

Runs every `LOOK_AT_INTERVAL_MS` (20 ms = 50 Hz) inside `update()`:

```cpp
void MountMotion::_updateLookAt() {
    // 1. Get current slider position in mm
    float cx = stepsToMm(AXIS_SLIDER, _stepper[AXIS_SLIDER]->getPosition());

    // 2. Compute required pan/tilt angles
    float dx = _lookat_subject.x - cx;
    float dy = _lookat_subject.y;        // constant vertical offset
    float dz = _lookat_subject.z;

    float pan_rad  = atan2f(dx, dz);
    float tilt_rad = atan2f(dy, sqrtf(dx*dx + dz*dz));

    // 3. Convert to steps
    int32_t pan_target  = radToSteps(AXIS_PAN,  pan_rad);
    int32_t tilt_target = radToSteps(AXIS_TILT, tilt_rad);

    // 4. Speed-limited move toward target (follower-style)
    _driveTowardTarget(AXIS_PAN,  pan_target,  _lookat_max_pt_speed);
    _driveTowardTarget(AXIS_TILT, tilt_target, _lookat_max_pt_speed);
}
```

`_driveTowardTarget()` uses `rotateAsync()` + `overrideSpeed()` with a
speed proportional to distance, capped at `_lookat_max_pt_speed`.

---

## Pan/tilt absolute angle reference

### The problem

Pan and tilt have no hardware end stops.  On every power cycle the step
counter resets to zero at whatever physical position the head is in.  For
look-at tracking to work, the firmware must know: *"at this step count, the
camera is physically pointing at this angle."*

### How deg/step is established (once, at first calibration)

During subject calibration (2-point process), two observations of the same
subject from different slider positions give two known pan angles and two
known tilt angles with their corresponding step counts.  This yields:

```
pan_deg_per_step  = (pan_B_deg  - pan_A_deg)  / (pan_steps_B  - pan_steps_A)
tilt_deg_per_step = (tilt_B_deg - tilt_A_deg) / (tilt_steps_B - tilt_steps_A)
```

These are stored in EEPROM and never need recalculating unless the
mechanical drive ratio changes (e.g. gears replaced).

### The session reference ("Set Ref")

`deg_per_step` tells us the *scale* but not the *offset* — i.e. where in
physical angle terms step-count-zero currently sits.  This offset is
invalidated whenever the mount is powered off, or whenever someone physically
moves the head while powered on.

**Solution: a "Set Ref" button, available at any time from all interfaces.**

Workflow:
1. Operator aims the camera at any already-calibrated subject (or any known
   reference point with a stored subject record).
2. Presses **Set Ref** and selects the subject they are aimed at.
3. Firmware calculates the expected pan/tilt angles for the current slider
   position looking at that subject's 3D coords, then sets the step-count
   offset so the current step count maps to those angles.

There is no timer.  The button can be pressed:
- At the start of a session after powering on
- After physically repositioning the head
- Any time the operator suspects the reference has drifted
- Repeatedly on different subjects to verify consistency

If the reference is slightly off on the first try, the operator simply
re-aims and presses Set Ref again.  No move is commanded — the button only
updates the internal angle offset.

### Future: optical homing (Option A, hardware upgrade path)

Adding an optical end stop to pan and/or tilt would allow automatic homing
on power-on, making the Set Ref step unnecessary.  The firmware architecture
is designed to accommodate this — `setRef()` would simply become an
internally-called function after the home move completes rather than a
user-triggered one.  This will be investigated separately.

---

## New protocol commands (additions to v1)

| Command | Direction | Payload | Description |
|---|---|---|---|
| `CMD_ADD_SUBJECT_START` | PC→Mount | `subject_id (1B)` | Begin calibration for subject N |
| `CMD_ADD_SUBJECT_SET_A` | PC→Mount | — | Record point A (slider at home, pan/tilt current) |
| `CMD_ADD_SUBJECT_SET_B` | PC→Mount | — | Record point B (slider at max, pan/tilt current) |
| `CMD_ADD_SUBJECT_ABORT` | PC→Mount | — | Cancel calibration |
| `CMD_DELETE_SUBJECT` | PC→Mount | `subject_id (1B)` | Delete a subject |
| `CMD_SUBJECT_LIST` | Mount→PC | 8 × `{id, name[16], x,y,z floats}` | All subjects |
| `CMD_SET_REF` | PC→Mount | `subject_id (1B)` | Set pan/tilt angle reference using named subject at current slider pos |
| `CMD_SET_SLIDER_MOVE` | PC→Mount | `slot, start_mm, end_mm, speed_preset` | Store a slider move |
| `CMD_START_LOOK_AT_MOVE` | PC→Mount | `slider_slot, subject_id, max_pt_speed` | Start tracking move |
| `CMD_SWITCH_SUBJECT` | PC→Mount | `subject_id` | Switch target mid-move |
| `CMD_LOOK_AT_STATUS` | Mount→PC | `slider_pos_mm, pan_deg, tilt_deg, subject_id` | Live telemetry |
| `CMD_REF_CONFIRMED` | Mount→PC | `pan_deg, tilt_deg (2× float)` | Echoes computed reference angles so UI can confirm |

---

## EEPROM additions (v2)

```c
#define MAX_SUBJECTS   8
#define MAX_SLIDER_MOVES 8
#define SUBJECT_NAME_LEN 16

typedef struct {
    float x, y, z;           // world coords in mm
    char  name[SUBJECT_NAME_LEN];
    bool  valid;
} SubjectRecord;

typedef struct {
    int32_t start_steps;
    int32_t end_steps;
    uint8_t speed_preset;
    bool    valid;
} SliderMoveRecord;

// Added to EepromConfig:
SubjectRecord    subjects[MAX_SUBJECTS];
SliderMoveRecord slider_moves[MAX_SLIDER_MOVES];
float            slider_mm_per_step;   // set during findLimits (mm travel / total steps)
float            pan_deg_per_step;     // set during first subject calibration
float            tilt_deg_per_step;    // set during first subject calibration
```

The **session reference** (pan/tilt step-count offset) is held in RAM only —
it is intentionally not saved to EEPROM because it is always invalid after a
power cycle.  The operator sets it via the **Set Ref** button each session.

```c
// RAM only — not in EepromConfig:
static float _pan_ref_deg;      // physical pan angle at step count 0
static float _tilt_ref_deg;     // physical tilt angle at step count 0
static bool  _ref_set;          // false until Set Ref pressed at least once
```

Look-at moves are gated on `_ref_set == true`.  If the operator attempts a
look-at move before setting the reference, the firmware rejects it and sends
an error packet so all UIs can display a clear warning.

---

## PC app additions

- `config/subject_store.py` — load/save subject names (labels only; 3D
  coords live on Teensy EEPROM)
- `ui/dialogs/subject_calibration_dialog.py` — guided 2-point calibration
  workflow
- `ui/widgets/subject_grid.py` — replaces/extends `position_grid.py` with
  two tabs: Subjects and Slider Moves
- `comms/protocol.py` — new command IDs
- `comms/mount_manager.py` — new send methods for look-at commands

---

## Implementation order

1. **Protocol** — define all new command IDs in `shared/protocol.h` and
   `pc_app/comms/protocol.py`
2. **Teensy firmware** — add subject/slider-move EEPROM structs, new state
   machine states, look-at controller, calibration state machine, new
   command handlers
3. **PC app** — calibration dialog, subject grid widget, send commands
4. **Hub display** — subject selection buttons, look-at status overlay
5. **AMOLED display** — subject buttons on jog screen
6. **Web app** — subject selection and look-at controls

---

## What does NOT change from v1

- Jog (pan/tilt/slider/zoom) — identical
- Find limits / find home — identical
- Speed presets / orientation / stall thresholds — identical
- Config dialog — identical
- E-stop — identical
- CV tracking — carried forward unchanged
- LANC zoom — identical
- ESP-NOW wireless protocol between hub and mounts — identical
