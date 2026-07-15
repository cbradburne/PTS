# Bitfocus Companion (and QLab) control

The PC app runs an OSC control server (UDP, default port **9700** — change or
disable via `osc_enabled` / `osc_port` in the app's config file).  Anything
that can send OSC can drive the mounts: Companion, QLab network cues,
TouchOSC, etc.  The PC app must be running and connected to the hub as usual;
OSC rides its existing link.

## Companion connection

1. Companion → **Connections** → add **Generic: OSC**
2. **Target IP** = the PC running the PC app (`127.0.0.1` if Companion runs on
   the same machine) · **Target port** = `9700`
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

## Safety notes

- Jogs started over OSC are re-streamed by the PC app at 20 Hz (the mount's
  own 500 ms dead-man requires a live stream).  If the *release* message is
  lost (UDP), a **15 s TTL** stops the jog anyway — for long moves prefer
  `goto` / `lookat`, which are position-bounded.
- The mount-side dead-man still applies end-to-end: if the PC app dies
  mid-jog, motion stops within 500 ms regardless of Companion.
- E-STOP over OSC also cancels any OSC-held jog streams immediately.
