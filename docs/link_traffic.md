# What travels between a mount and the hub

**There are two links, and they carry very different amounts.** Almost every
question about traffic on this rig resolves once that distinction is clear:

```
Teensy  ⇄  AMOLED bridge  ⇄  [satellite relay]  ⇄  hub  ⇄  PC app / web app / display
        UART                ESP-NOW
      115200 baud            radio
     private wire          contended
```

The Teensy talks to the AMOLED board over a dedicated serial wire. That board —
`esp_mount_amoled175`, which is the mount display *and* the ESP-NOW bridge —
decides what is worth putting on the air. **The high rates all live on the
UART. The radio sees almost none of them.**

That filter is the single most important thing in this document. Reading the
Teensy's send rates and assuming they are radio rates overstates the traffic by
more than an order of magnitude.

---

## Link 1 — Teensy ⇄ bridge (UART, on the mount)

Not radio. A private wire to one peer, so rate here is cheap.

| Mount → bridge | Rate | Payload |
|---|---|---|
| `STATUS` | **100 ms** (10 Hz) | 10 B |
| `POSITION` | **on request only** — nothing sends `GET_POSITION` today | 17 B |
| `HEALTH` | 10 s | — |
| `LOOK_AT_STATUS` | on change only | 14 B |
| `CONFIG_REPORT` | on request | 77 B |
| `STATE_REPORT` | on request | 182 B |
| `SUBJECT_LIST` | on request / after a calibration | 232 B |
| `CALIB_PROMPT`, `LIMITS_FOUND`, `HOME_COMPLETE`, `REF_CONFIRMED`, `ACK`, `NACK` | one-off events | small |

Framing costs 9 bytes on top of every payload.

## Link 2 — bridge ⇄ hub (ESP-NOW)

The contended link, and the one that matters.
`teensy_frame_worth_sending()` in the bridge is where the rates above become
these — it can be disabled wholesale with `TEENSY_FWD_FILTER`, which is worth
knowing exists before concluding the radio is quiet.

### Uplink (mount → hub)

| Packet | What actually goes out |
|---|---|
| `STATUS` | **on change**, otherwise a 5 s refresh (`MOUNT_STATUS_REFRESH_MS`) |
| `LOOK_AT_STATUS` | on `subject_id`/`flags` change, otherwise 5 s — the three floats beside them are deliberately excluded from the comparison, because they change every frame and nothing reads them |
| `POSITION` | **only within 2 s of a `GET_POSITION`** (`POS_ON_DEMAND_MS`). The unsolicited 5 Hz stream is dropped at the bridge and never reaches the air |
| `HEALTH` | 10 s, deferred while a jog is being forwarded |
| everything else | forwarded as-is — each is a one-off notice of something that happened, and the only notice of it |

### Downlink (hub / clients → mount)

| From | Packet | Rate |
|---|---|---|
| Hub | `PING` broadcast, 4 B timestamp | **2 s** (`BASE_HEARTBEAT_MS`) |
| PC app | `PING` broadcast | **1 s** |
| PC app | `GET_CONFIG` idle probe, per connected mount | after 3 s with no ack-tracked send |
| PC app | `GET_SUBJECTS`, look-at mounts | ~4 s |
| PC app | `GET_POSITION` | nothing sends one — see below |
| any client | `JOG` | **streamed while a stick is held** — the heaviest thing on the link |
| any client | `MOVE_REL`, `GOTO`, `SET_*`, `GET_*` | user-driven |

---

## The shape of it

**At rest, a mount costs about one packet every 5 seconds upward and a 2 second
ping down.** Nothing on the radio link is 10 Hz.

Two things dominate when they happen, and both are worth knowing before adding
anything periodic:

- **Jog streaming.** Held sticks produce continuous packets. This is real work
  and cannot be filtered — the mount must hear every one to move smoothly.
- **The position stream, if ungated.** 5 Hz per moving mount is what
  `POS_ON_DEMAND_MS` exists to prevent.

Traffic here is not a theoretical concern. An ESP-NOW TX wedge on this rig is
rate-dependent: cutting satellite traffic by 88% took one satellite from 36
restarts in 17 hours to zero.

## Before adding anything periodic

Two questions, in this order.

**1. Is it already carried?** `STATUS` goes out on every change and carries
state, flags, both speed presets, both slot masks, the target slot and the
active look-at subject. A new periodic packet duplicating any of that is a
second stream saying what the first already said.

**2. Is it the liveness signal?** Both the hub and the PC app refresh presence
on **any** packet from a mount, not on a particular command, and `STATUS` is
never gated by state. So removing a periodic packet does not usually change how
quickly a dead mount is noticed — but check, because if the thing being removed
*were* the only traffic during some state, a mount would appear dead the moment
it entered that state. That fault would only ever show up mid-event.

## Filter at the source, not downstream

The bridge filter is a safety net, not a licence. `LOOK_AT_STATUS` was broadcast
by the Teensy at 10 Hz for the whole of a look-at move while the bridge quietly
discarded almost all of it — correct on the air, wasteful on the wire and in
bridge CPU, and misleading to anyone reading the Teensy source to work out what
the rig transmits.

---

## Positions are answered, never volunteered

`CMD_POSITION` is sent only in reply to a `CMD_GET_POSITION`, and **nothing
sends one today**. So there is no position data anywhere in the system unless
something is added that asks for it.

It was not always so, and the history is a good illustration of how a stream
outlives its readers:

| | |
|---|---|
| 30 Jul | Teensy broadcast `POSITION` at 5 Hz moving / 1 Hz at rest. The PC app's Config tab showed it live. |
| 12 Aug | The bridge began gating it on a recent `GET_POSITION` — "stop putting telemetry on the air that nothing reads". The Config readout went blank, and nobody noticed. |
| 19 Aug | A zoom diagnostic polled `GET_POSITION`, which revived the readout and the position log by accident. |
| 21 Aug | The diagnostic was removed, both went quiet again, and the dead Config readout was deleted. |
| 21 Aug | The unsolicited broadcast was removed at the Teensy, since the bridge had been discarding it for nine days. |

A third diagnostic came and went the same way in August: a 5 Hz poll during
look-at moves, added because two attempts to fix an abrupt subject switch had
been reasoned from the firmware rather than measured. It found the cause in one
run — the camera pinned against a speed cap at half the peak the easing curve
asked for — and was removed once the profile came back a bell.

**Two consumers are dormant, not broken.** `comms/position_log.py` still
subscribes and would log travel and overshoot the moment positions flowed —
it is what measured the rail geometry — and anything else can subscribe the
same way. Reviving either means sending `GET_POSITION`, which is deliberately
the visible act: whatever wants positions has to ask, and shows up in the log
as the thing that opened the tap. An unsolicited stream is a producer nobody is
accountable for.

Two diagnostics that once used it have been removed, both having answered the
question they were added for:

- **`ZOOM_DIAGNOSTIC`** (PC app) — added when zoom on mount 5 crept away from
  stored positions and sprang back when the slot was pressed again. It turned on
  `GET_POSITION` polling, zoom travel logging, and naming OSC-driven jogs so
  surface-driven motion could be told from joystick motion. The creep turned out
  to be a stranded goto axis; the movement that started the hunt turned out to be
  OSC commands, which never reach the PC app at all — which is why three rounds
  of PC-app instrumentation could not see them. Removed in "remove both
  temporary diagnostics".

- **`GOTO_DEBUG`** (Teensy → clients) — had the mount report what its goto
  planner decided the moment it decided it. It is what proved the stranded axis:
  the plan said 399 ms and the moving mask still showed that axis turning three
  seconds later. Removed with the same commit.

If either is ever needed again, the shape to copy is this: report what the
firmware DECIDED, not just what it did. Position samples alone could not
distinguish an axis overshooting from an axis being commanded somewhere new.

- **`LOOK_AT_DIAGNOSTIC`** (PC app only) — asked each tracking mount for its
  position at 5 Hz and logged pan and tilt with the velocity between samples,
  a summary of the whole switch on one line, and whether the tilt reversed.
  It is what settled the subject-switch work of 2026-08-26 after three fixes
  reasoned from the firmware had changed nothing an operator could see. It
  found: the plateau was the axis pinned at TeensyStep4's `vMaxMax`; the ease
  out was 15–19 ms, less than one control tick; and the tilt arced on every
  switch. Removed once the motion was right.

  It lives complete at **`bd1604f`**, with its own test:

  ```
  git show bd1604f:tools/test_look_at_motion_probe.py
  git show bd1604f:pc_app/comms/mount_manager.py
  ```

  Two things in it cost a measurement round each and are worth keeping if it
  comes back. Time each sample by when the REQUEST went out, never by when the
  reply arrived — transport jitter lands entirely in dt while the position
  delta stays honest, and every "spike" in the early logs was that, not the
  mount. And score the motion against the curve it is meant to be following
  (`6 × travel / duration²`) rather than reporting where the biggest velocity
  step fell: a smoothstep peaks in acceleration at both ends, so once the shape
  is right, "biggest step at the ease out" is the curve, not a fault.
