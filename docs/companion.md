# Bitfocus Companion (and QLab) control

**The hub is the OSC server.** One server, on the hub, UDP port **9700** —
`192.168.4.1` on the CamMount AP, reachable from the LAN through a WiFi→LAN
bridge joined to that AP, or from any machine joined to CamMount directly. No
PC needed: hub + display + mounts + Stream Deck is a complete rig.

There used to be a second server inside the PC app on the same port and the
same `/pts/...` namespace, and the two had drifted apart — `tally` and `record`
existed only in the PC app, `autofocus` and `zoom` only in the hub. A tally
button aimed at the hub was a perfectly good message arriving at a server that
had never heard of tally, and unknown addresses are dropped in silence. The PC
app's server is gone; every address below is the hub's.

The practical consequence: the hub is always on and a laptop is not, so a tally
lamp or a record cue no longer depends on anyone's PC app being open.

## Companion connection

1. Companion → **Connections** → add **Generic: OSC**
2. **Target IP** = the hub (`192.168.4.1` on the CamMount AP) ·
   **Target port** = `9700`
3. Use the connection's **"Send message"** actions on buttons as below.
   Argument type matters: use **integer** arguments (floats also accepted).

## Address reference

| Address                  | Args                | Action |
|--------------------------|---------------------|--------|
| `/pts/estop`             | –                   | E-STOP **all** mounts |
| `/pts/cam/N/estop`       | –                   | E-STOP mount N |
| `/pts/cam/N/goto`        | slot `1-10`         | Recall stored position (uses active speed presets) |
| `/pts/cam/N/store`       | slot `1-10`         | Store current position |
| `/pts/cam/N/clear`       | slot `1-10`         | Clear a stored position |
| `/pts/cam/N/jog`         | pan tilt slider zoom (`-1000..1000`) | Start/refresh a jog — server re-streams at 20 Hz |
| `/pts/cam/N/jog/stop`    | –                   | Stop jogging |
| `/pts/cam/N/jog/left`    | `1` press / `0` release | Pan left |
| `/pts/cam/N/jog/right`   | `1` / `0`           | Pan right |
| `/pts/cam/N/jog/up`      | `1` / `0`           | Tilt up |
| `/pts/cam/N/jog/down`    | `1` / `0`           | Tilt down |
| `/pts/cam/N/jog/slide/left`  | `1` / `0`       | Slide left |
| `/pts/cam/N/jog/slide/right` | `1` / `0`       | Slide right |
| `/pts/cam/N/jog/zoom/in`     | `1` / `0`       | Zoom in |
| `/pts/cam/N/jog/zoom/out`    | `1` / `0`       | Zoom out |
| `/pts/cam/N/speed/pt`    | `1-4`               | Active pan/tilt speed preset |
| `/pts/cam/N/speed/sl`    | `1-4`               | Active slider speed preset |
| `/pts/cam/N/speed/pt/up`   | –                 | Pan/tilt speed one step faster (clamped at 4) |
| `/pts/cam/N/speed/pt/down` | –                 | Pan/tilt speed one step slower (clamped at 1) |
| `/pts/cam/N/speed/sl/up`   | –                 | Slider speed one step faster |
| `/pts/cam/N/speed/sl/down` | –                 | Slider speed one step slower |
| `/pts/cam/N/speed/pt/inc`  | –                 | Pan/tilt speed 1→2→3→4→1 (wraps) |
| `/pts/cam/N/speed/sl/inc`  | –                 | Slider speed 1→2→3→4→1 (wraps) |
| `/pts/cam/N/subject`     | `0-7`               | Select look-at subject (switches live mid-move) |
| `/pts/cam/N/lookat`      | `0` (◀ min) / `1` (▶ max) | Look-at slider move with the selected subject |
| `/pts/cam/N/autofocus`   | –                   | Instantaneous autofocus on that mount's Blackmagic camera |
| `/pts/cam/N/tally`       | `0`/`1`, or `0.0-1.0` | Tally lamp **brightness**, both lamps |
| `/pts/cam/N/tally/front` | `0`/`1`, or `0.0-1.0` | Front lamp only (facing talent) |
| `/pts/cam/N/tally/rear`  | `0`/`1`, or `0.0-1.0` | Rear lamp only (facing operator) |
| `/pts/cam/N/record`      | `0` stop / `1` start | Start or stop recording on that camera |
| `/pts/cam/N/record/toggle` | –                 | Start if stopped, stop if rolling — reads the camera's reported transport, not the last command |
| `/pts/refresh`           | –                   | Resend all feedback for every mount |
| `/pts/cam/N/refresh`     | –                   | Resend all feedback for mount N |

`N` = camera 1-5.  Slots and speed presets are 1-based, subjects 0-based —
matching what every screen in the system shows.

## Button recipes

**Recall shot 3 on camera 2** — the bread-and-butter Stream Deck button:
- Press actions: `Send message` → `/pts/cam/2/goto`, int arg `3`

**Hold-to-jog pan-left on camera 1** (uses Companion's press *and* release
action lists):
- Press actions:   `/pts/cam/1/jog`, ints `-400 0 0 0`
- Release actions: `/pts/cam/1/jog/stop`

**Look-at: track subject 2, slider to the right, camera 3**:
- Button A press: `/pts/cam/3/subject`, int `2`
- Button B press: `/pts/cam/3/lookat`,  int `1`
  (pressing Button A during the move switches the tracked subject live)

**Speed page** — four buttons per camera: `/pts/cam/N/speed/pt` with `1..4`.

**Record with a tally that tells the truth** — two halves of one button:
- Press action: `Send message` → `/pts/cam/5/record/toggle`
- Feedback: OSC value of `/pts/cam/5/recording` → `1` lights the button

The button lights from the camera's transport report, not from the press, so a
press that did not take — no card in the camera, Bluetooth dropped — leaves it
dark rather than lying about it.

**Tally lamp** — `/pts/cam/N/tally` with int `1` for full and `0` for off, or a
float for anything between.

> **The Pocket Cinema Camera 4K ignores this.** Tested on the rig on
> 2026-08-17: three tally commands reached a paired, actively reporting camera
> and it neither answered nor changed. On connect that camera volunteers
> categories 0, 1, 3, 4, 9, 10 and 12 — ISO, white balance, shutter angle,
> battery, transport, lens type, reel, take, operator — and has never once
> mentioned the tally category. A body that describes itself that thoroughly
> would report tally if it had it.
>
> The address is kept because it is correct by the published spec and a studio
> or URSA body — which is what the tally group is aimed at — should accept it.
> **On a Pocket, the thing that lights the front indicator is the camera
> recording.** So the tally you can actually drive is `/pts/cam/N/record`, and
> `/pts/cam/N/recording` coming back is the camera confirming it.

### Direction buttons

The named `jog` directions exist for button surfaces; `/pts/cam/N/jog` with its
four signed values is still there for a stick or a fader.

Bind **press → `1`** and **release → `0`** on the same address.  Any non-zero
value is full deflection — a button sends `1`, and honouring that as a
magnitude would creep the mount rather than move it.  Speed comes from the
active preset, not from the argument.

Each direction owns one axis and leaves the others alone, so holding *left* and
*up* together gives a diagonal, and releasing one leaves the other running.

If a release is lost, the 15 s TTL stops the jog anyway, and the mount's own
500 ms dead-man stops it if the hub goes away entirely.

Speed `up`/`down` need no argument and clamp at 1 and 4, so one button can walk
the preset without Companion tracking which one is active, and a button held at
either end is inert rather than wrapping round mid-shot.

`inc` wraps instead — 1→2→3→4→1 — which is what the web app and the PC app do
from a single control.  Use `inc` for one button that cycles, `up`/`down` for a
pair that cannot overshoot.

**Show-stopper**: a big red `/pts/estop` (no argument needed).

## QLab

QLab network cues speak OSC natively, so camera moves can be *cues in the
show script*: patch a network destination to the PC's IP port `9700`, then a
cue like `/pts/cam/2/goto 4` fires a camera recall in the cue stack, exactly
in time with lighting and sound.  (QLab sends numbers as floats — the server
accepts that.)

## Feedback (hub → Companion)

The hub replies to **whoever last sent it a command** — no configuration at
either end, and a second surface starts receiving as soon as it takes over.
Messages go to that sender's IP and source port; if your controller transmits
from an ephemeral port and listens on a fixed one, set `OSC_REPLY_PORT` in
`esp32_hub_eth.ino` to that number.

All arguments are a single int.

| Address                          | Value | Meaning |
|----------------------------------|-------|---------|
| `/pts/cam/N/active`              | 0/1   | Mount is online (STATUS seen recently) |
| `/pts/cam/N/state`               | int   | Mount state — 0 IDLE, others per `MountState` |
| `/pts/cam/N/target`              | 0-10  | Slot being moved to, 0 = not moving to one |
| `/pts/cam/N/speed/pt`            | 1-4   | Active pan/tilt speed preset |
| `/pts/cam/N/speed/sl`            | 0-4   | Active slider speed preset — **0 = this mount has no rail** |
| `/pts/cam/N/slot/M/state`        | 0-3   | Slot M — see below |
| `/pts/cam/N/recording`           | 0/1   | That camera is **rolling** — see below |
| `/pts/cam/N/lookat/mode`         | 0/1   | 1 = look-at mode — **buttons 9 and 10 are the ◀/▶ rail ends, not slots** |
| `/pts/cam/N/lookat/subject`      | -1..7 | Subject currently being tracked, -1 = none |
| `/pts/cam/N/calib/prompt`        | 0-6   | Calibration step in progress — see below |

`/pts/cam/N/recording` is the camera's own account of its transport mode, not
an echo of the record command. Blackmagic never acknowledges a command, so a
record cue that failed — no media in the camera, Bluetooth dropped — produces
nothing here and the button stays dark. A lit button is a camera that is
actually recording.

It reads 0 until the camera reports otherwise, which is also what it reads when
no camera is paired to that mount.

### Look-at

`/pts/cam/N/lookat/mode` is the one to build a page around. It decides what
buttons 9 and 10 **mean** — two more stored positions, or the two ends of the
rail. A page that assumes one or the other goes wrong the moment the mode is
changed from the PC app, the web app or the hub display, so drive the button
labels from this rather than setting them by hand.

With it at 1, slots 9 and 10 report the arrows through the ordinary slot
addresses: `2` (AT) when the slider is parked at that end, `3` (MOVING) while
it is travelling there, `0` otherwise. Slots 1-8 report `1` (OCCUPIED) for each
subject that has been calibrated.

`/pts/cam/N/lookat/subject` is the one being tracked right now — the green
border on every other client. The slot addresses say which subjects exist; this
says which is live.

`/pts/cam/N/calib/prompt` follows a subject calibration:

| Value | Meaning |
|-------|---------|
| `0` | nothing in progress |
| `1` | moving the slider to the home end |
| `2` | at home — aim the camera, then Set A |
| `3` | moving the slider to the far end |
| `4` | at the far end — re-aim at the same subject, then Set B |
| `5` | solved and stored |
| `6` | failed — the two observations were too alike |

Calibration cannot yet be *started* from Companion; there is no OSC command for
it. This publishes the prompt so a Deck can show where a calibration driven
from another client has got to — useful when the person aiming the camera is
not the person at the PC. `5` and `6` clear themselves back to `0` after eight
seconds, so a button shows the outcome and then goes quiet.

Slot state is one address per slot carrying one value:

| Value | Meaning |
|-------|---------|
| 0 | empty — nothing stored here |
| 1 | occupied — stored, mount elsewhere |
| 2 | moving — mount is on its way here |
| 3 | at — mount is here |

A button binds to that single address and picks its colour from the value, with
no bitwise expression and nothing to combine.  It is the same picture the
display and the web app show — stored, moving-to, arrived — and because it
arrives as one message it can never be seen half-updated.

Only changes are sent, so a rig at rest is silent.  Everything is resent every
5 s regardless, so a surface that joins late — or misses a UDP packet — catches
up on its own without having to ask.  The first message from an address the hub
has not heard from also triggers a full send.

A mount with no slider reports `speed/sl` as **0**, which no preset ever uses,
so a button can hide or grey itself instead of showing a speed for an axis that
cannot move.  The hub also ignores `speed/sl/...` and `jog/slide/...` for those
mounts, so a button pressed against a rail-less mount does nothing rather than
walking a number that controls nothing.  This matches the web app, which has
always zeroed the slider axis for a mount without one.

### Camera control

`/pts/cam/N/autofocus` triggers instantaneous autofocus on the Blackmagic camera
attached to that mount, over the mount's own Bluetooth link — no SDI cable and
no converter.

It is fire-and-forget: the Blackmagic protocol has no acknowledgement, so
nothing comes back to say the lens moved.  If a camera is off, asleep or not
paired, the command is simply dropped at the mount.  Whether a mount's camera
link is up shows in the PC app's **CC** panel and in `comms.log` as
`BLE PAIRED`.

**Each mount must be paired with its camera once, on a bench.**  Every mount
ships with camera control built in, but Bluetooth pairing needs the six-digit
code the camera puts on screen, typed in while someone is watching — so it
cannot happen out on a rig.  The bond is then stored on the mount and survives
power cycles and reflashes.

The bond is also how a mount knows which camera is *its own*.  With several
cameras on a rig they all advertise the same way, so an unpaired mount has
nothing but signal strength to go on and can reach for a neighbour's camera;
a paired one goes straight to the camera it was paired with and ignores the
rest.  A mount that has never been paired says so
specifically, in the **CC** panel as *not paired* and in `comms.log` as
`BLE NOT PAIRED` — distinct from *camera off*, because the fix is different.

To pair, hold the mount's screen to reach SETUP, then hold again for CAMERA
PAIRING.  The camera shows six digits; type them on the keypad.  **FORGET**
clears every camera this mount is bonded to, which is what a camera moved to a
different mount needs.  The mount is off the air while pairing, so the screen
leaves on its own after two minutes.

`/pts/refresh` asks for that full send on demand.  Bind it to a Companion
startup trigger, or to a button, for the case the automatic paths do not cover:
a Companion that restarts on the same port is not a new peer, so without asking
it would show stale buttons until the next 5 s resend.

## Safety notes

- Jogs started over OSC are re-streamed at 20 Hz by whichever server received
  them (the mount's own 500 ms dead-man requires a live stream).  If the
  *release* message is lost (UDP), a **15 s TTL** stops the jog anyway — for
  long moves prefer `goto` / `lookat`, which are position-bounded.
- The mount-side dead-man still applies end-to-end: if the hub or PC app dies
  mid-jog, motion stops within 500 ms regardless of Companion.
- E-STOP over OSC also cancels any OSC-held jog streams immediately.
- Hub-side extras: OSC jogs engage the same radio-contention protection as
  web-app jogs, OSC `lookat` triggers the ◀/▶ arrow flash on every connected
  UI, and Companion activity defers the hub's idle maintenance restart.
