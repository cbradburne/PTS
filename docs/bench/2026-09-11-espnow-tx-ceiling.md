# Bench result: ESP-NOW send ceiling measured directly

Ran the `ceiling` probe from commit 222c42b on the two-board bench rig,
2026-09-11. This is the measurement that was asked for: how many sends
`esp_now_send()` accepts back-to-back before refusing, and whether those
buffers come back.

## Headline

**The ceiling is exactly 32, in every configuration tested, and it drains
completely every time.** It is a queue depth, not a leak.

**This contradicts the stated theory that 32 was sends piling up between the
callback stopping and the recovery ladder firing at 6 s.** Detail below.

## Setup

- Repo `/Users/col/Documents/GitHub/PTS`, HEAD `222c42b` (pull was a no-op,
  already current).
- TX board: `bench` target (AMOLED FQBN) on `/dev/cu.usbmodem232301`,
  MAC `28:84:85:55:60:88`.
- RX board: `benchrx` target (HUBETH FQBN) on `/dev/cu.usbmodem2401`,
  MAC `3C:0F:02:D7:90:8C`. Passive receiver, left stopped.
- Both on channel 1. Link confirmed up before probing
  (`[bench] first frame from 28:84:85:55:60:88 — the link is up`).
- TX config restored from NVS at boot:
  `rate=200Hz size=32 cap=2 load=40/100ms scan=0s phy=1M-LR`

Two stored settings that look like they should matter but do not, both because
`cmd_ceiling()` runs inside `handle_line()` and so blocks `loop()` for its whole
duration:

- `cap=2` — the probe lifts the cap via the `_probe_uncapped` flag, so the cap
  clause in `bench_send()` is bypassed for the burst.
- `load=40/100ms` — the synthetic LVGL load lives further down `loop()` and
  cannot interleave with the burst or with the drain wait.
- `rate=200Hz` — irrelevant, the probe sets `_cfg.running = 0` first.

## Baseline run, verbatim

`size 32`, `phy 1M-LR` (the rig's own configuration):

```
[ceiling] firing with no cap until the driver refuses...
[bench] FIRST REFUSAL at t=30s err=NO_MEM (the buffer pool is empty — THE WEDGE) in_flight=32
[ceiling] accepted 32 before NO_MEM (the buffer pool is empty — THE WEDGE), peak in_flight 32
[ceiling] after 40 ms: 32 of 32 callbacks returned, in_flight 0
[ceiling] VERDICT: every buffer came back — that ceiling is a queue depth, not a leak.
[ceiling] run stopped. 'reset' then 'go' to resume.
```

The `FIRST REFUSAL` line appears only in this run — that bookkeeping fires once
per counter reset, not once per probe. Stats line immediately after:

```
t=31s issued=32 cb_ok=32 cb_fail=0 refused=1 nomem=1 in_flight=0 floor=0 max=0 rx=0 heap=265716 err=0x3067 cmd=0 ack=0 ovl=0
```

Note `cb_fail=0` and `floor=0`: all 32 were transmitted successfully and the
leak floor never moved.

## Variations — the ceiling does not move

| Run | Accepted | Peak in flight | Returned | Drain | in_flight after |
|---|---|---|---|---|---|
| baseline (size 32, phy lr) | 32 | 32 | 32/32 | 40 ms | 0 |
| `size 8` | 32 | 32 | 32/32 | 30 ms | 0 |
| `size 240` | 32 | 32 | 32/32 | 90 ms | 0 |
| `phy def` (size 32) | 32 | 32 | 32/32 | 40 ms | 0 |
| `phy lr` (size 32) | 32 | 32 | 32/32 | 40 ms | 0 |

Every run refused with `NO_MEM` (`err=0x3067`, `ESP_ERR_ESPNOW_NO_MEM`) and
every run ended `in_flight 0` with the queue-depth verdict.

On the PHY step: the board came back from NVS **already** on `phy=1M-LR`, so
`phy lr` alone would have been a no-op. `phy def` was run as well so the pair is
a real comparison, and `size 32` was restored first so only PHY varied. Both
settings need a reboot to apply (`radio_start()` re-applies the long-rate peer
config at boot when `_cfg.phy_lr` is set); reboot was done with `role tx`, and
the boot line confirmed `phy=default` and `phy=1M-LR` respectively.

## What this says about the 32

**1. 32 is a hard structural ceiling on `esp_now_send()`, not an accumulation.**

The probe reaches 32 back-to-back in under a millisecond, with nothing stalled,
no callback stopped, and no recovery ladder involved. The 6 s window cannot be
what sets the number, because the number is already there before any window
opens.

This also removes the coincidence from "exactly 32, twice". Under the
accumulation theory, 32 is the product of send rate and stall duration, and that
product has to land on the same integer on two separate occasions. Under
saturation, 32 is the only value it can take: once the queue is full every
further send is refused `NO_MEM`, so `in_flight` **cannot** climb past 32 however
long the stall runs. A ceiling explains a repeated round number; an accumulation
has to get lucky twice.

**2. The sdkconfig reasoning was too narrow.**

`CONFIG_ESP_WIFI_STATIC_TX_BUFFER_NUM=8` is correct, and confirmed:

```
CONFIG_ESP_WIFI_STATIC_TX_BUFFER=y
# CONFIG_ESP_WIFI_DYNAMIC_TX_BUFFER is not set
CONFIG_ESP_WIFI_TX_BUFFER_TYPE=0
CONFIG_ESP_WIFI_STATIC_TX_BUFFER_NUM=8
```

(from `~/Library/Arduino15/packages/esp32/tools/esp32s3-libs/3.3.11/sdkconfig`)

But ruling out "a buffer pool" on that basis skipped a step. The driver refuses
with `ESP_ERR_ESPNOW_NO_MEM` at exactly 32, so there is a **32-deep queue sitting
above those 8 static buffers**. It is invariant to payload size — 8 bytes and 240
bytes both stop at 32 — which is the signature of a fixed-count descriptor queue.
A byte-budgeted pool would differ by roughly 30× across that range. Drain time
*does* scale with payload (30 / 40 / 90 ms), so the queue is genuinely being
transmitted, not discarded.

So: not the static TX buffer pool, but still a pool. "32 is not a buffer pool"
was the wrong conclusion from the right sdkconfig line.

**3. The send path does not leak.**

32 of 32 buffers returned, every configuration, every time, within 30–90 ms.
There is no loss in the send path under normal operation.

The consequence for the hub: `floor=32` at a stall means the queue filled and the
callback never returned it — saturation, not loss. The counter was reading "the
pipe is full", and given the ceiling it could never have read anything else. Any
stall long enough to fill 32 slots pins the floor at 32 and holds it there, so
the value carries no information about how long the stall lasted or how much was
actually lost.

## Caveats

- This is the **bench firmware on a bench board, not the hub**. The hub is also
  an esp32s3 and shares the same precompiled-core sdkconfig, so the 32 should
  transfer, but it was not measured on hub firmware.
- This says nothing about **why the callback stops**. It shows the send path is
  healthy while the callback is alive, which places the fault upstream of
  anything measured here.
- No firmware was modified. All five runs are the stock `ceiling` probe from
  222c42b.

## Board state left behind

- TX: `size 32 / phy 1M-LR / rate 200Hz / cap 2 / load 40/100ms / scan 0s` —
  i.e. exactly as found. Run stopped (`reset` then `go` to resume).
- RX: passive at `3C:0F:02:D7:90:8C`. It has `poll=20Hz` in NVS but was left
  stopped, so it never polled during any probe.
