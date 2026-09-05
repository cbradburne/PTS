# espnow_bench — finding the TX wedge

Two boards on a desk, both on USB serial, existing only to reproduce and then
kill the ESP-NOW transmit wedge that takes a mount off the air.

```bash
tools/build.sh flash bench /dev/cu.usbmodemXXXX     # same binary on both boards
```

On one board type `role rx` — it reboots and prints its MAC. On the other:

```
role tx
peer AA:BB:CC:DD:EE:FF
rate 50
go
```

Both boards persist their settings in NVS, so an overnight run survives a power
cut and resumes where it was. `help` lists every command.

## What the numbers mean

One line a second from the TX board:

```
t=612s issued=30600 cb_ok=30594 cb_fail=0 refused=0 nomem=0
       in_flight=6 floor=0 max=9 rx=0 heap=284512 err=0x0
```

**`in_flight = issued − (cb_ok + cb_fail)`** is the whole point. It is the number
of buffers `esp_now_send()` has taken from the stack's pool that the
send-complete callback has not yet given back.

- It bouncing between 0 and a few is **normal** — that is just sends in flight.
- **`floor` is the number to watch.** It is the lowest `in_flight` has returned
  to in the last five seconds, and it only ever rises. A floor that climbs and
  never comes down is buffers being *lost*, not buffers in use. That is the leak,
  and you are watching it happen — hours before `nomem` moves.
- `nomem` climbing means the pool is already empty. By then it is over.

The rig has never had `floor`. It only ever had `refused`, which cannot warn:
a refusal means the radio will not accept a send, so the report carrying the
number is the one thing that cannot go out. cam1's own health report 18 seconds
before it wedged read zero.

## The experiments, in order

Each one changes exactly one thing. Run the baseline first so you know what
"not wedging" looks like on your bench.

### 1. Baseline — does it reproduce at all?

```
cap 0        # fire and forget, exactly what the mount does today
rate 50
phy lr       # 1 Mbps long preamble, as the rig runs
load 0 0
scan 0
go
```

Leave it. The rig takes 11–31 hours; the bench may be faster because there is
nothing else competing. If `floor` stays at 0 for a day, the send stream alone
is **not** the cause and the answer is in one of the variables below.

### 2. Rate — is it really rate?

Cutting a satellite's traffic by 88% took it from 36 restarts in 17 hours to
zero, so rate is the strongest clue there is. Run `rate 10`, `rate 50`,
`rate 200` and record time-to-first-refusal for each. If the time scales with
rate, it is a per-send leak and the arithmetic will tell you the leak rate per
thousand sends.

### 3. The cap — is the fix three lines?

```
cap 1        # never a second send outstanding — the textbook ESP-NOW discipline
```

The mount today calls `esp_now_send()` with no regard for how many sends are
outstanding, and `espnow_tx()` says so in as many words: *"none of them can do
anything useful about a refusal in the moment"*. If `cap 1` (or 2, or 4) stops
the floor rising where `cap 0` does not, **that is the fix**, and it is a small
change to `espnow_tx()`.

### 4. Load — is it the display, not the radio?

```
cap 0
load 40 100      # block the loop 40 ms in every 100 — a heavy LVGL flush
```

The mount renders 37 KB draw buffers out of PSRAM on this same chip, and the
firmware already notes that rendering is bandwidth-bound. If starving the loop
leaks buffers, the display is the cause and the radio is innocent.

**This is the hypothesis the field data points at.** cam1 wedged twice at
−33 dBm — the strongest link on the rig — while cam4 sat at −68 dBm through a
satellite and never did. A radio-quality explanation has that backwards; a
"cam1's screen is doing more work" explanation does not.

Try `load 10 100`, `40 100`, `100 200`. If there is a threshold, find it.

### 5. Scan — does retuning the radio drop callbacks?

```
load 0 0
scan 30
```

The mount scans while hunting for a hub. A scan retunes the radio out from
under an in-flight send.

### 6. PHY rate

```
phy def      # then reboot
```

1 Mbps with a long preamble buys 6–10 dB of range and makes every frame roughly
eight times longer on air. Longer frames mean longer in flight, and more of them
outstanding at the same send rate.

## Recording a run

The serial output is one line per second and already CSV-ish:

```bash
screen -L -Logfile bench-cap0-rate50.log /dev/cu.usbmodemXXXX 115200
```

Name the file after the variables. A run that does not wedge is a result and
worth keeping — it is what rules a hypothesis out.

## What a finding looks like

Something that separates two runs differing in one variable, with a mechanism:

> `cap 0` at 50 Hz: floor reaches 11 in 40 minutes, first refusal at 71 min.
> `cap 1` at 50 Hz: floor 0 after 14 hours.
> → the leak is per-outstanding-send, and bounding in-flight prevents it.

That is enough to change `espnow_tx()` and put a number on the expected
improvement before touching the rig.
