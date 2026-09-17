# What a paired camera actually costs: 18 KB of heap, and nothing else

Rewritten 2026-09-17. The first version of this file recorded a baseline and
proposed an experiment to find out whether BLE degrades the ESP-NOW link. The
experiment ran the same day and the answer is no — the two mounts that appeared
to show it were both suffering antenna obstruction, and both were fixed by hand
in under an hour.

The original version is in the history if the reasoning is wanted. What follows
is what is actually true.

## The finding

```
                min heap    camera
cam1              8198k     none
cam2              8196k     none
cam3              8197k     none
cam4              8180k     PAIRED      (8199k before pairing)
cam5              8180k     PAIRED
```

Pairing a Blackmagic camera costs **about 18 KB of heap**, permanently, and that
is the whole measurable cost. cam4 dropped 8199k → 8180k within minutes of
pairing and landed on cam5's figure to the kilobyte. Three cameraless mounts sit
at 8196–8198k.

With ~8.2 MB free on these boards it is not a constraint. It is worth knowing
only because it is real, repeatable and instant — which made it easy to mistake
for the start of something worse.

## What it does NOT cost

Nothing measurable in the radio. After both antennas were corrected, with
cameras still paired and reporting:

```
cam4   txfail frozen at 151 for 2.5 h   — not one failed send since
cam5   txfail frozen at 111 for 2.25 h  — not one failed send since
cam1/2/3 (no camera) continued their usual trickle over 31.8 h
```

The two mounts with cameras are currently the cleanest on the rig.

## The two false positives, because they are the useful part

Both mounts looked like clear evidence that BLE degrades the link. Both were
antenna obstruction.

**cam4.** Paired at 10:45, and within three hours its failure rate went from
2.9/h — over a clean 22.5-hour baseline — to 43/h. Its RSSI had also moved
−64 → −71 dBm, which was noted as a confound and then argued around. A cable had
been laid near the antenna while fitting the camera. Moving it restored −64 dBm
and the failure count stopped dead.

**cam5.** Running 20–29/h for days at a comfortable −50 dBm, which is why RF had
been discounted: the signal looked fine. It looked fine because the wrong
direction was being measured.

```
          what the mount HEARS     what the relay hears FROM it     gap
cam1          -36 dBm                    -30 to -29                  +6
cam2          -49                        -57 to -51                  -5
cam3          -49                        -50 to -47                   0
cam4          -64                        -78 to -70                 -10
cam5          -51                        -89 to -79                 -32
```

cam5's antenna had no line of sight to its satellite. It heard the relay at
−51 dBm and the relay heard it at −79 to −89 — around 7 dB above the noise
floor, against cam3's 42 dB. Standing the antenna vertical and clear of
obstructions gained ~5 dB on the forward path and stopped the failures outright.

## The lesson, stated so it discriminates

**`txfail` counts transmissions that were not acknowledged. It is therefore a
question about the RECEIVER, and the sender's own RSSI is the wrong number to
look at.** A mount can report an excellent signal and be nearly inaudible at the
other end; cam5 was 32 dB asymmetric and every instrument on the mount said it
was healthy.

The reverse path is visible in the `mount N ONLINE — rssi=` events, which is what
the hub or satellite heard. That comparison took one query and would have found
both faults immediately.

**But "suspect RF first" is only right for the right symptom**, and it would have
been wrong earlier in this project:

```
txfail CLIMBING steadily          an RF question. Check the reverse path first.
txfail FLAT while sends stop      NOT RF. Nothing on air can freeze a counter
                                  of on-air failures — that is the ESP-NOW
                                  TX wedge, see 2026-09-12-hub-nomem.md.
```

Days went into RF, range and antennas for the wedge before it was ruled out, and
correctly ruled out — a mount ran clean for hours at −80 to −85 dBm. The
distinguishing fact is whether the counter is moving.

## For the remaining cameras

Three more are to be fitted. On this evidence the procedure is short:

1. **Check the antenna has clear air before fitting**, and check nothing has been
   laid across it afterwards. That single step accounts for everything this
   investigation found.
2. **Compare both directions after pairing**, not just the mount's own RSSI — the
   table above is the check, and an asymmetry beyond ~10 dB is worth acting on.
3. **Expect ~18 KB of heap to go** and nothing else. If the failure rate climbs
   and stays climbing with a clear antenna and a symmetric link, that would be
   new and worth reporting.

## One note on how this was found

The logs pointed at BLE twice and were wrong twice. Both causes were found
physically, by inspection, within an hour — after this document had proposed a
multi-day experiment to test the wrong hypothesis. The reverse-path data that
would have settled it was already in the log the whole time and was not being
read.
