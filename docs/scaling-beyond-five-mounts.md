# Scaling past five mounts

Design note for supporting ten mounts now and more later, while the PC app, the
hub display and the web app each show five at a time and let the operator choose
which five.

Written 2026-09-18, before any of it is built.

## The short version

The wire protocol already supports it. The radio supports it. The cost is almost
entirely in the three user interfaces and in one whole-rig flash that cannot be
staged.

## What already works

```
mount_id            0x00 broadcast, 0xF0+ satellites, 0xFD display, 0xFE hub
                    ids 1..0xEF are free — 239 mounts
PACKET_MAX_PAYLOAD  256 bytes
MOUNT_TABLE         5 x MAC(6) = 30 B today; 10 needs 60; the ceiling is ~42 mounts
MOUNT_ROUTE         one byte per mount; irrelevant at any realistic size
OSC                 /pts/cam/N/... — N is already the mount id, not a slot
```

Nothing in the packet format needs redesigning, and OSC needs no change at all:
addressing by mount id means Companion, QLab and Stream Deck reach every mount
whether or not it is one of the five on screen. That is the behaviour wanted, and
it is what the code already does.

## The one hard ceiling

```
ESP_NOW_MAX_TOTAL_PEER_NUM = 20      per node, not per rig
```

Twenty *directly radioed* mounts per node. Satellites hold their own peer tables
and reach the hub over Ethernet, so the rig ceiling is 20 direct plus 20 per
satellite across 6 satellite slots. Ten is comfortable. Beyond roughly eighteen
direct, mounts have to be distributed across satellites by design rather than by
convenience.

## Two traps to clear before anything else

**The payload lengths are hardcoded literals.**

```c
#define MOUNT_TABLE_PAYLOAD_LEN  30   // 5 x MAC(6)
#define MOUNT_ROUTE_PAYLOAD_LEN   5   // one byte per cam
```

Raise `NUM_MOUNTS` to 10 and these stay at 30 and 5. The hub sends a 60-byte
table, the app reads 30, and mounts 6-10 vanish silently with no compile error.
The protocol check will not catch it — it compares declared constants, not
literals at call sites, which is a known blind spot. Make them
`(NUM_MOUNTS * 6)` and `NUM_MOUNTS`, and add a test that asserts they track.

**Every node flashes together.** `NUM_MOUNTS` appears 114 times in the hub, 9 in
the satellite, and in both Python modules. A hub on 10 talking to mounts on 5
misparses every table. There is no staged rollout of that step — hub, all
satellites, all mounts, the display and the app, in one session.

## The split that makes it affordable

Keeping the interfaces five-wide is what turns this from a rewrite into an
indirection.

```
DATA LAYER      grows to N. Hub tracks, routes, polls and recovers all N.
                Arrays, loops, payload lengths. Mechanical.

PRESENTATION    stays five-wide and gains a map: slot 1..5 -> mount id.
                Tabs, joystick bindings, display panels resolve through a
                lookup instead of using the index directly.
```

`main_window.py` alone has `range(1, 6)` scattered through tab construction,
per-mount state dicts, look-at subject tracking and arrow state. Those become
`for slot in visible_slots()` returning mount ids. That is the bulk of the work
and it is mostly mechanical once the map exists.

## The right model is a composable panel, not a view filter

Each control surface is an independent five-mount panel, and you add surfaces to
get capacity. PC app and hub display side by side gives ten mounts at one
position; add the web app on an iPad and it is fifteen.

That is a better model than "choose which five to show", and it simplifies
things: surfaces never need to agree, so there is nothing to coordinate, nothing
to push and nothing to reconcile.

It has one consequence worth acting on early. **Do not bake 5 in as a new
constant.** Once the slot-to-mount indirection exists, panel width is a property
of the surface, not of the rig — a PC app on a wide screen could run eight where
the hub's 7-inch panel runs five or four. Writing `PANEL_SLOTS` per surface costs
nothing now; discovering later that 5 is welded into three UIs costs what this
whole exercise is trying to avoid.

## The visible map is PER CLIENT

The deployment is two rooms. A PC app in the concert hall shows five mounts; a
second PC app in the foyer next door shows five different ones; and either can
reach the other's cameras when needed.

That makes the map a property of the client, not of the rig. Each app stores its
own, and the hub does not need to know or care.

**An earlier draft of this note argued the opposite** — that the map had to be
rig-wide, on the grounds that a joystick binds to a slot and two screens
disagreeing about slot 2 would move the wrong camera. That reasoning assumed one
control surface shared between screens. It is not: each PC app has its own
joystick, so "slot 2" resolves inside the app the stick is plugged into and is
never ambiguous. The argument was for a deployment that does not exist.

So:

```
PC app          map in local config, per install. Survives restart. Hub uninvolved.
Web app         map in browser storage, per device.
Hub display     its own map, stored in hub NVS — see below.
```

No new protocol commands are needed for any of this, which also removes the
persistence and push machinery the rig-wide version would have required.

### The hub display is the exception

It is a single physical panel with its own touch controls, so "per client" means
"the one there is". Its map belongs in hub NVS because that is where the display's
other settings live — but it is the display's map, not the rig's, and nothing
else should read it.

It is also the surface most likely to surprise someone: an operator in the
concert hall has no way to see what the panel by the rack is showing, or that
somebody is standing at it.

## Concurrent control, which the two-room setup makes real

Cross-control is the requirement, so this needs stating plainly: **there is no
arbitration today, and there never has been.**

```
MAX_CLIENTS   4 TCP slots, plus WebSocket, plus serial, plus the display, plus OSC
ownership     none
arbitration   none — last command wins
notification  none — no client is told another has taken a mount
```

`_last_client_cmd_ms` exists but is rig-wide and only defers the maintenance
restart. Nothing records which client last commanded which mount.

With one operator this never bites. With two rooms and deliberate cross-control
it will: the foyer operator takes cam3 for a shot, the concert hall operator jogs
the same camera a second later, and neither screen shows the other.

**The fix is visibility, not locking.** In live production an operator must be
able to take a camera immediately — a lock that has to be released by someone in
another room is worse than the collision it prevents. What is missing is that
nobody can *see* the collision.

The minimum useful version: the hub records which client last commanded each
mount and when, and pushes it with the rest of the mount state. A UI can then
show "cam3 — driven from Foyer, 2 s ago" and the operator decides. That is a
per-mount client id and timestamp, a few bytes, and no behaviour change to the
control path at all.

Worth building alongside the multi-mount work rather than after it, because
cross-control is one of the reasons for doing it.

## Build order

Each step ships on its own and is testable before the next.

**1. Derive the payload lengths, at NUM_MOUNTS = 5.** No behaviour change, no
visible difference, flashes normally. De-risks everything after it.

**2. Introduce the slot-to-mount map, still at 5.** The map is the identity
function, so nothing changes on screen. All three UIs start resolving through it.
This is the largest code change and it lands while the rig still behaves exactly
as it does today — which is the point.

**3. Raise NUM_MOUNTS to 10.** The whole-rig flash. Ten slots exist; only five
can be filled until step 4, which is a usable intermediate state rather than a
broken one.

**4. The pickers**, in whatever order suits:

- **AMOLED** — replace `_setup_id_btn[5]` on the SETUP screen with a numeric pad
  and an OK, modelled on the camera-pairing screen. Five fixed buttons do not
  extend; a pad does, and it is the only way to assign an id above 5.
- **PC app** — a mounts screen listing mounts 1..N top to bottom, each row
  carrying its visible-slot assignment. See the note on touch below.
- **Hub display** — the same list, scrollable, since ten rows will not fit the
  7-inch panel at the current row height.
- **Web app** — the same again. It is a single 144 KB header, so this is the
  most self-contained of the three.

## On the touch input for slot assignment

A conventional dropdown is a poor touch target and worse on the hub's panel: it
opens a list that can fall off-screen, needs a second accurate tap, and gives no
feedback about what is already assigned elsewhere.

Better for this specific case — six choices, mutually exclusive, and the operator
needs to see conflicts:

**A row of six buttons per mount: `—  1  2  3  4  5`.** One tap, no second
target, current state visible without opening anything, and a slot already taken
by another mount can be shown greyed or struck through. It also makes the
constraint self-evident: five slots, N mounts, so most rows will read `—`.

This is the same pattern the AMOLED setup screen already uses for CAM 1-5, so it
is consistent with what exists rather than a new idiom.

## Open questions worth settling early

**Does an unselected mount stay live?** It should. Keep polling, keep health,
keep the recovery ladders running. Making visibility a comms filter would mean a
mount you cannot see is a mount you cannot diagnose, and every fault this rig has
had was found by continuous reporting from a node nobody was looking at.

**What happens when a visible mount goes offline?** Leave the slot assigned and
show it offline, rather than auto-reassigning. Auto-reassignment moves cameras
under the operator's hands.

**RAM on the hub.** Per-mount state doubles. The hub runs at ~165 KB free, and
the per-mount structures are small, but this should be measured at NUM_MOUNTS=10
before the flash rather than assumed.

**Satellite distribution.** With ten mounts the 20-peer ceiling is not binding,
but the RF geometry might be: the reverse path is what governs delivery, and
adding five mounts to one hub radio concentrates that. Worth deciding which
mounts sit behind which satellite by signal rather than by convenience — see
2026-09-16-ble-baseline.md for why the reverse path is the number that matters.

## Load, measured, because the TX path was only just stabilised

A satellite relaying two mounts is offered **62 frames per 10 s**, so roughly
**3.1 frames/s per mount** of steady keepalive and polling. Fifteen mounts is
therefore about 47 frames/s — if they all hang off one radio.

Against that, the in-flight cap is 1 and the bench measured a full 32-deep queue
draining in 30-90 ms, so around 1-3 ms per callback: several hundred sends per
second of capacity. 47/s is a comfortable fraction of it.

So the arithmetic is fine, but it is a threefold increase on a transmit path that
spent a week wedging and has only been quiet since 13 September. Two things
follow:

- **Measure rather than assume.** `sends held back per 10 s` on the hub and
  `offered / sent / refused` on each satellite are the numbers that would show
  the cap starting to bind. They are already in every health line.
- **Distribute across satellites rather than concentrating on the hub.** Five
  direct plus ten relayed puts only five mounts' worth of ESP-NOW on the hub's
  own radio, and the 20-peer ceiling points the same way. Hanging fifteen off the
  hub would work on paper and would make the hub the single point that has
  historically been the one to fail.
