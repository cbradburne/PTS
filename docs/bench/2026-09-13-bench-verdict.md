# The ESP-NOW bench: what 140 hours established, and why it stopped

Closing report on `firmware/espnow_bench`, a two-board rig built to answer one
question. It never answered it. It was still worth building, and this records
both halves so the next person does not rebuild it expecting more.

## The question it was built for

A node's ESP-NOW transmit path stops. `esp_now_send()` begins returning
`ESP_ERR_ESPNOW_NO_MEM`, the send-complete callback stops firing, and nothing
short of a chip reboot clears it. espressif/esp-idf#18682 describes the
mechanism — a buffer taken from a small pool, returned by a callback that never
comes — so the open question was never "why does the pool run out". It was:

> **Why does the callback stop?**

On the rig the fault takes hours, the mounts are in enclosures on a truss with
no reachable serial port, and the restart that ends it resets every counter that
would explain it. Two boards on a desk with USB on both, and every experiment a
typed line rather than a reflash, was the answer to that.

## The instrument that mattered

```
in_flight = issued - (cb_ok + cb_fail)
```

The leaked-buffer count, and the one number the rig had never had. A refusal
count cannot warn — it only moves once the pool is already empty, and by then the
radio cannot report it. `in_flight` starts climbing at the first lost buffer.
Tracked as the **floor** it has not returned below over a 5-second window, because
the instantaneous value bounces with every send.

## What it established

**The leak is real, and it has a precondition.** Same rate, same everything: no
synthetic load ran 13.3 h and 10.5 M sends with the floor at 0. With `load 40 100`
— 40 ms of blocked loop in every 100 — the floor stepped 0→1→2→3 at 5.97 h,
6.28 h and 15.17 h, each step costing **exactly 208 bytes of heap, never
returned**. It leaked at a *lower* send rate than the run that did not.

**It needs three or more sends in flight, not merely overlapping ones.** The
capped run overlapped *more* often per send than the leaking one — 101 per 1000
against 43 — and stayed clean. The cap bounds depth, not concurrency.

**`cap 2` prevents it.** 53.32 h, 26,868,324 sends, floor 0, heap drift of
exactly 0 bytes, one continuous session. Against the uncapped rate of one leak
per 2.53 M sends, 10.6 were expected and none occurred: **p ≈ 2.4 × 10⁻⁵**.

**The send queue holds exactly 32, and it drains.** `esp_now_send()` accepts 32
back to back before refusing — invariant to payload size and to PHY rate — and
all 32 callbacks return within 30–90 ms. It is a fixed-count descriptor queue
above `CONFIG_ESP_WIFI_STATIC_TX_BUFFER_NUM`, which is 8. **So an in-flight floor
of 32 is saturation, not loss**: `in_flight` physically cannot exceed it, and the
number says nothing about how long a stall ran.

## What it falsified

Five explanations, every one of them ours, each killed in minutes rather than
over nights of rig observation. This is what the bench was actually for.

| claim | measurement that killed it |
|---|---|
| A floor of 32 is sends piling up during a stall | It is a ceiling, reached in under a millisecond with nothing stalled, and it drains completely |
| A cap cannot help the rig — its average send rate is too low | Averages hide bursts; the blocked loop manufactures one, and `cap 2` prevented the leak |
| `cb_fail` means the frame was lost | 18 failures, 0 gaps: all eighteen delivered, the acknowledgements were lost |
| ...so `cb_fail` means the frame arrived | 1 failure, 1 gap, same sequence number: genuinely lost. It means neither reliably |
| AP mode is the last structural difference from the hub | 20.7 h with a station associated, 8.94 M sends, floor 0 |

The through-line: **the gap counter is ground truth precisely because it does not
depend on `cb_fail`**, and a decision table built on a counter that is ambiguous
in both directions has to be read against something independent.

## What it never did

**It never once saw a callback stop.** In roughly 140 hours across every
configuration offered — rate, payload, PHY, synthetic load, in-flight cap,
periodic scan, station mode, SoftAP mode with an associated client — the callback
kept firing. Every leak it produced was slow attrition under a blocked loop.

That matters, because the rig's fault is not slow attrition. On 2026-09-12 a
satellite went callback-dead → queue full → refusing → self-restart in **36
seconds**, and the hub refused every send for 1 h 27 m. The bench's own detector
is a floor rise over a 5-second window and would not have tripped on either.

## Why it stopped

The configuration that reproduced the leak stopped reproducing it.

```
three runs, two interface modes    49.6 h, 21.4 M sends
floor steps                        0
expected at the documented rate    8.5     (1 leak per 2.53 M sends)
P(zero)                            0.0002
```

Not a marginal miss. Either the boards differ from the ones that reproduced it,
or the trigger is not in `cap 0 / load 40 100 / rate 200 / size 32 / phy lr /
chan 1` alone.

The closing argument is that **the bench could no longer test a fix either.**
Verifying that a change prevents `NO_MEM` requires producing `NO_MEM`, and it
could not. Meanwhile the rig produced it in a single day, in the act, with the
error name — see `2026-09-12-hub-nomem.md`.

## Instrument lessons worth carrying

**Put the diagnostic where the fault cannot silence it.** The bench blocked its
own loop through a closed USB CDC port and stepped its floor as a result — the
sampler only rises if `in_flight` is ≥1 at *every* iteration for 5 s, and a
stalled loop starves it. That run was discarded. A `setTxTimeoutMs(0)` fixed the
stall; later the CDC endpoint stopped delivering entirely, twice in one day,
leaving the run blind on its only TX-side signal while still appearing to record.

**Check each source's freshness separately.** A staleness check on the run file
saw nothing wrong through a 96-minute TX outage, because the receiving board kept
writing every second.

**One-shot console lines cannot be a primary signal.** `FIRST REFUSAL` and
`*** WEDGED` print once per run and can be missed permanently by anything that
samples rather than reads every line. The durable signal is the CSV columns.

**A counter that steps on another node's events is measuring that node.** The
mounts' leak floors stepped only when the hub restarted; the one mount behind a
satellite ignored those and stepped on its satellite's restarts instead. A
differently-routed peer is the control that proves it.

## The files

Valid runs, in `bench-logs/`:

- `cap2-load40-rate200-*` — 53.32 h, 26.9 M sends, clean. The cap proof.
- `cap0-load40-rate200-seqgap-run-20260911-115435.csv` — 5.61 h null, and the
  18-failures-0-gaps episode.
- `cap0-load40-rate200-seqgap-run2-20260911-1737.csv` — 23.24 h null, and the
  single genuine on-air loss (seq 9368115).
- `ap-cap0-load40-rate200-20260912-1658.csv` — 20.70 h AP-mode null.

Do not cite `cap0-load40-rate200-seqgap-2026091*` (three files): the logger was
restarted mid-run, nothing held the ports for ~37 s, and the floor stepped inside
that window as an artefact of the stall described above.
