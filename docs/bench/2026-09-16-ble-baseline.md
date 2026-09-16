# Baseline before the cameras go on: four mounts without BLE, one with

Recorded 2026-09-16, because it is about to become unrepeatable. Four Blackmagic
cameras are going onto the remaining mounts over the coming days, starting with
one tomorrow, and at that point the rig stops having a no-BLE arm.

Right now all five mounts run **identical firmware** — flashed 10:18 on
2026-09-16 — on the same hub, in the same building, over the same window. Only
cam5 has a camera paired. That is as close to a controlled comparison as this rig
will get, and it exists by accident rather than design.

## The numbers

Uptime 12.2–12.4 h on every mount, no reboots since the flash.

```
        txfail/h   reinits   rssi     min heap   loopmax   camera
cam1       3.5        2      -36 dBm   8198k       9 ms    none
cam2       2.6        2      -50 dBm   8196k       9 ms    none
cam3       1.0        0      -48 dBm   8197k      11 ms    none
cam4       4.4        1      -64 dBm   8199k       9 ms    none
cam5      28.8        4      -49 dBm   8182k      16 ms    PAIRED, subscribed,
                                                            reporting
```

cam5 is roughly **ten times the mean of the other four** on failed sends, and it
is high on three further measures at the same time: twice the stack reinits of
any other mount, 14–17 KB less minimum heap, and the worst loop pass.

Delivery was 100 % on all five throughout, so none of this is currently costing
commands. It is a difference in how hard the node is working, not in what it
delivers.

## What the table already excludes

Three obvious explanations are ruled out by the baseline itself, which is most of
why it is worth writing down:

- **Not signal.** cam5 sits at −49 dBm. cam4 is the weakest link on the rig at
  −64 dBm and is the second *cleanest* at 4.4/h. The correlation runs the wrong
  way for an RF explanation.
- **Not routing.** cam4 and cam5 are both relayed through the Foyer satellite;
  cam1–3 are direct. cam4 is at 4.4/h and cam5 at 28.8/h, so the shared path is
  not what separates them.
- **Not firmware or timing.** Same binary, same 12-hour window, same hub.

What remains is the one thing only cam5 has. That is a hypothesis, not a finding
— a single node differing on a single attribute is exactly the shape of evidence
that looks conclusive and often is not.

## The experiment the cameras make possible

Converter stock means one camera goes on tomorrow rather than four at once,
which is better for this than having them all. Staggering turns a before/after
into a sequence.

**Put the first camera on cam4.** It is the only other mount relayed through
Foyer, so pairing it holds routing constant against cam5 and changes only the
camera. It also has a clean 4.4/h baseline to move from.

**Keep cam3 without a camera for as long as is tolerable.** It is the quietest
node on the rig — 1.0/h, zero reinits — and a control that stays untouched is
what separates "BLE does this" from "something changed that week". Without one,
a rig-wide drift would be indistinguishable from the thing being tested.

**Read it on txfail per hour, not on wedges.** Wedges are rare and noisy; the
failed-send rate is continuous, already differs tenfold, and moves within hours.
Minimum heap and reinit count are the corroborating measures — if cam4's heap
drops toward cam5's 8182k as its txfail climbs, that is two independent signals
agreeing.

**The baseline improves with every quiet day.** It is twelve hours old here. If
the mounts stay up until the camera goes on, it will be around twenty-four, and
a week would be considerably stronger. Nothing needs doing to collect it except
not rebooting them.

## Why this matters beyond curiosity

The ESP32-S3 shares one 2.4 GHz radio between WiFi and BLE, and pairing has to
stop WiFi outright. Contention between the two is real and documented. It was
examined and set aside early in the ESP-NOW investigation — correctly, on the
evidence then: the transmit wedge predates the Blackmagic code existing at all,
and cam1 was the node wedging while cam5 was the only one with a camera.

That argument still holds for the *wedge*. It says nothing about the steady
tenfold difference in failed sends visible here, which is a separate question and
was not what was being asked at the time.

If pairing a second camera moves cam4 onto cam5's numbers, then BLE coexistence
is a real cost on this hardware and it is worth knowing before all five mounts
carry cameras. If cam4 does not move, cam5 is simply an outlier for some other
reason and this table stops being interesting — which is also worth knowing, and
cheaper to establish now than to argue about later.
