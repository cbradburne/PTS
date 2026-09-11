# Bench result: the hub's peer refresh does not cost buffers

Ran the `peerdrop` probe from commit `de77427` on the two-board bench rig,
2026-09-11, after a 5.61 h sequence-gap run produced the finding that motivated
it. Two results here, one measured and one structural.

## Headline

**Deleting and re-adding an ESP-NOW peer with sends outstanding is harmless.**
Three runs, at 16, 4 and 30 sends in flight: the queue was 32 deep before and 32
deep after, every time, and not one callback was orphaned.

**And the premise behind the hub's peer refresh is still wrong.** A send failure
does not mean a lost frame — measured separately, below — so the refresh fires
on links that are working. It just does not cost anything when it does.

**The bench has been measuring the wrong interface.** Every result to date,
including this one, was taken in station mode. The hub transmits from a SoftAP.
That is a different path in the driver and it is the one difference that could
produce the hub's actual signature. `iface ap` now exists; nothing has been run
on it yet.

## What was being tested

The sequence-gap run recorded `cb_fail` going 0 → 18 over three and a half
minutes while the receiver's gap counter stayed at **0**. Eighteen sends
reported failure and all eighteen sequence numbers arrived: what was lost was
the MAC-layer acknowledgement, not the frame.

The hub acts on the opposite belief. It deletes and re-adds a peer after
`ESPNOW_MAX_CONSEC_FAILS` (4) consecutive failures
(`esp32_hub_eth.ino:663`, `:3959`), and refreshes every bound mount
unconditionally when a client connects (`:4014`). So it refreshes working links
— and it does so when the queue is deepest, because in that same three minutes
the in-flight depth climbed 2 → 24 of the 32 the driver allows. A frame awaiting
retries holds its descriptor longer, so acknowledgement loss and queue depth
arrive together by construction.

Two different things could go missing when a peer is deleted mid-flight, and
only one of them is a fault:

```
the callback     in_flight never comes down, and the board reports a leak
                 floor it does not have
the descriptor   the queue is permanently shallower, and enough of them
                 leaves the send path with nothing to allocate
```

**The deciding number is the queue depth before against after, not the callback
count.** A returned buffer refills the queue whether or not anyone was told it
came back.

## Setup

- Repo at `de77427`. Only the TX board was reflashed; the RX board unchanged.
- TX `28:84:85:55:60:88` (`bench` target, AMOLED FQBN),
  peer `3C:0F:02:D7:90:8C`.
- Both boards rebooted first. TX came up `in_flight=0 floor=0` — verifiably
  clean, which the probe re-checks and would have refused on.
- `rate=200Hz size=32 cap=0 load=40/100ms scan=0s phy=1M-LR chan=1`.
- Nothing else held the serial port; the long-run logger was stopped first.

## The three runs

| n in flight at the drop | queue before | queue after | orphaned |
|---|---|---|---|
| 16 | 32 | 32 | 0 |
| 4  | 32 | 32 | 0 |
| 30 | 32 | 32 | 0 |

Verbatim, the default run:

```
[peerdrop] queue before: 32 accepted, all back in 40 ms
[peerdrop] dropped the peer with 16 in flight: 16 of 16 callbacks back in 20 ms, 0 orphaned
[peerdrop] queue after:  32 accepted (was 32)
[peerdrop] VERDICT: harmless. Every callback came back and the queue is as deep as it was.
```

`peerdrop 4` and `peerdrop 30` returned the same verdict, 4/4 and 30/30 back,
queue 32 → 32. Thirty of thirty-two slots outstanding is 94 % of the driver's
ceiling, so this is not a result that only holds at shallow depth.

Neither branch reproduced: no descriptor consumed, no callback stranded.

## What it does not rule out

All three runs were on a freshly rebooted, idle board. The probe stops the
background stream, fills the queue deliberately, drops the peer and measures.
The hub does its refresh mid-stream, during the episodes when callbacks are
already late.

`peerdrop 30` reproduces the *depth* but not the *concurrency*, and every frame
it dropped was healthy — they drained in 40 ms, where the hub's refresh fires
precisely because frames are not draining. Nothing here rules out a delete
interacting badly with a live stream or a retry backlog. It rules out the simple
form: the delete itself does not eat buffers.

## The structural finding: interface

Worth more than the null above.

```
bench    WiFi.mode(WIFI_STA)   peers on WIFI_IF_STA   no AP, nothing associated
hub      WiFi.mode(WIFI_AP)    peers on WIFI_IF_AP    beaconing, clients can join
```

Every bench conclusion so far — 53 h clean at `cap 2`, the ceiling of 32, this
peer-drop null — was measured station-to-station. The hub transmits from an
access point. That is a different transmit path: an AP beacons on a fixed
interval whatever else it is doing, and it buffers frames for any associated
station that goes to sleep, out of the same pool the sends come from.

It is also the one difference that would produce the hub's signature, which the
bench has never reproduced in any configuration: **frames that sit rather than
fail leave `txfail` flat while callbacks stop**, and `txfail` flat at 83 from
the first minute to the last of a fourteen-hour outage is exactly what the hub
logged on 2026-09-08.

`iface sta|ap` is now a runtime switch. `sta` is the default and is
byte-for-byte what every previous run used, so the baselines stay comparable.
The report line gains an `sta=` column — stations associated to the board's own
AP — because when an AP-mode run behaves differently, whether anything was
attached is the first question and a column is the only way to answer it hours
later.

Switching the interface **changes the board's MAC**; the AP and station
addresses of one chip differ. The other board is then holding a peer that no
longer exists, which looks exactly like a dead radio. The command says so and
the boot line prints the interface beside the MAC.

Nothing has been run in AP mode yet. This document records the switch existing,
not a result from it.

## Where the data is

- Preceding sequence-gap run, 5.61 h, a valid null:
  `bench-logs/cap0-load40-rate200-seqgap-run-20260911-115435.csv` —
  2,422,627 issued, `cb_fail` 18, `floor` 0 throughout, RX gaps 0, peak
  depth 24.
- The `peerdrop` output above was read off the console; the probe stops the run
  by design and writes no CSV.
