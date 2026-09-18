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

## The decision that matters most: who owns the selection

**The visible map must be rig-wide, owned by the hub, and pushed to every
client. Not per-client.**

This is a safety argument, not a tidiness one. A physical joystick binds to a
slot. If the PC app thinks slot 2 is mount 7 and the hub display thinks it is
mount 3, then pushing the stick moves a camera the operator is not looking at.
Two operators, two screens, one rig, and no way to tell from either screen that
they disagree.

So it needs a command pair alongside `CMD_MOUNT_ROUTE`:

```
CMD_SET_VISIBLE   client -> hub   5 bytes: slot 1..5 -> mount id (0 = empty)
CMD_VISIBLE_MAP   hub -> clients  5 bytes, pushed on change and on request
```

Same shape as the existing route and table pushes, persisted in hub NVS so it
survives a restart.

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
