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
| `POSITION` | **200 ms** moving, **1 s** at rest | 17 B |
| `HEALTH` | 10 s | — |
| `LOOK_AT_STATUS` | on change only | 14 B |
| `CONFIG_REPORT` | on request | 77 B |
| `STATE_REPORT` | on request | 182 B |
| `SUBJECT_LIST` | on request / after a calibration | 232 B |
| `CALIB_PROMPT`, `LIMITS_FOUND`, `HOME_COMPLETE`, `REF_CONFIRMED`, `ACK`, `NACK` | one-off events | small |
| `GOTO_DEBUG` | per goto — **temporary, see below** | — |

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
| PC app | `GET_POSITION` | only under `ZOOM_DIAGNOSTIC` — **temporary, see below** |
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

## Temporary diagnostics — remove when done

Two instrumentation patches are live. Both were added to answer a specific
question, both have answered it.

### `ZOOM_DIAGNOSTIC` — PC app (`comms/mount_manager.py:80`)

Added when zoom on mount 5 crept away from stored positions and sprang back when
the slot was pressed again. Turns on three things: `GET_POSITION` polling (so
there is any position data at all), per-axis zoom travel logging, and naming
OSC-driven jogs in the log so surface-driven motion can be told from joystick
motion.

It did its job twice over. The creep during a recall was a stranded goto axis —
a jog took the state away from the P-loop and left the axis turning with a
frozen speed override — fixed in "a jog no longer strands a goto axis turning
forever". The separate zoom movement that started the hunt was OSC commands,
which never touch the PC app, which is why three rounds of PC-app instrumentation
could not see them.

Commits: `73a2837`, `1f1f97e`, `336a5bc`.

### `GOTO_DEBUG` — Teensy → clients (`shared/protocol.h`, `CMD_GOTO_DEBUG`)

Added so the mount reports what its goto planner decided the moment it decides
it: move time, which path (`moveTo` or `retargetTo`), whether the axes were
synchronised, and per-axis target, position and speed. Produces the
`GOTO PLAN cam5 via moveTo — t_move=399ms sync=True` lines.

It is what proved the stranded-axis bug: the plan said the move would take
399 ms, and the moving mask still showed that axis turning three seconds later.
Without the planner's own account there was no way to compare intent against
behaviour.

Commit: `a7c70f7`.

**Both can go when you are confident the goto and zoom behaviour is settled.**
`ZOOM_DIAGNOSTIC` also gates the only `GET_POSITION` traffic on the rig, so
removing it takes the position polling with it.
