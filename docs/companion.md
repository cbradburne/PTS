# Bitfocus Companion (and QLab) control

Two OSC control servers speak the same `/pts/...` address space on UDP port
**9700** — point Companion at whichever fits the rig:

| Target | When to use |
|---|---|
| **The hub itself** (`192.168.4.1`) | No PC needed — hub + display + mounts + Stream Deck is a complete rig.  Reachable from the LAN via a WiFi→LAN bridge joined to the CamMount AP, or from any machine joined to CamMount directly. |
| **The PC app's machine** | When the PC app is running anyway (its server is configurable via `osc_enabled` / `osc_port` in the app config). |

Both accept the identical addresses below, so Companion pages work unchanged
against either.  QLab network cues likewise.

## Companion connection

1. Companion → **Connections** → add **Generic: OSC**
2. **Target IP** = `192.168.4.1` (hub) or the PC app machine's IP ·
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
| `/pts/cam/N/speed/pt`    | `1-4`               | Active pan/tilt speed preset |
| `/pts/cam/N/speed/sl`    | `1-4`               | Active slider speed preset |
| `/pts/cam/N/subject`     | `0-7`               | Select look-at subject (switches live mid-move) |
| `/pts/cam/N/lookat`      | `0` (◀ min) / `1` (▶ max) | Look-at slider move with the selected subject |
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
| `/pts/cam/N/speed/sl`            | 1-4   | Active slider speed preset |
| `/pts/cam/N/slot/M/state`        | 0-3   | Slot M — see below |

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
