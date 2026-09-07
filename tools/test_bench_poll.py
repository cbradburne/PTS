"""The bench answers commands, and the logger records that it did.

`poll` exists because a metronome never has two sends outstanding — 542,067
sends at 50 Hz over three hours and in_flight never once exceeded 1. The rig
does: the bridge answers every non-JOG command the instant it arrives, on top
of periodic reports already in flight. This checks the bench reproduces that
shape, and — the part that can quietly cost a whole night — that the logger
still parses a board that has not been reflashed.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib, re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

INO = (REPO / "firmware/espnow_bench/espnow_bench.ino").read_text()

# ---- 1. the logger reads both builds ---------------------------------------
# A strict regex drops an unmatched line into the notes as free text. Flash one
# board and not the other and half the run stops being data — discovered, if at
# all, in the morning.
print("1. the logger against both firmware builds:")
import bench_log

OLD = ("t=612s issued=30600 cb_ok=30594 cb_fail=0 refused=0 nomem=0 "
       "in_flight=6 floor=0 max=9 rx=0 heap=284512 err=0x0")
MID = OLD + " cmd=1220 ack=1220"           # the build that could not see overlap
NEW = MID + " ovl=47"

m = bench_log.LINE.search(OLD)
assert m, "the previous build's line no longer parses — half a run would be lost"
row = [m.groupdict()[k] or "0" for k in bench_log.FIELDS]
assert len(row) == len(bench_log.FIELDS), "row width does not match the header"
assert row[-2:] == ["0", "0"], f"missing cmd/ack should read 0, got {row[-2:]}"
assert None not in row, "a None would raise inside the join and kill the reader"
print(f"   old line -> {len(row)} columns, cmd/ack default to 0     OK")

m = bench_log.LINE.search(MID)
assert m, "the intermediate build's line does not parse"
assert (m.group("cmd"), m.group("ack"), m.group("ovl")) == ("1220", "1220", None), \
    f"cmd/ack build misread: {m.groupdict()}"

m = bench_log.LINE.search(NEW)
assert m, "the new line does not parse at all"
d = m.groupdict()
assert d["cmd"] == "1220" and d["ack"] == "1220", f"cmd/ack misread: {d}"
assert d["ovl"] == "47", f"the overlap count misread: {d}"
assert d["heap"] == "284512" and d["err"] == "0x0", \
    "an optional group ate part of the line before it"
print("   new line -> cmd/ack/ovl read, nothing else disturbed  OK")

# The header the reader writes must match the row width, or every column after
# the break is silently misaligned.
hdr = ("wall_iso,side," + ",".join(bench_log.FIELDS)).split(",")
assert len(hdr) == len(row) + 2, \
    f"header has {len(hdr)} columns, rows have {len(row) + 2}"
print(f"   header and rows agree at {len(hdr)} columns              OK")

# ---- 2. the reply is issued where the rig issues it ------------------------
print("\n2. where the reply comes from:")
cb = INO[INO.index("static void on_recv("):]
cb = cb[:cb.index("\n}")]
assert "_cmd_rx = _cmd_rx + 1" in cb, "commands are not counted on arrival"
assert "esp_now_send" not in cb and "bench_send" not in cb, \
    "the reply is sent from inside the receive callback. The bridge queues and\n" \
    "    returns, answering from loop() — sending from the WiFi task would make\n" \
    "    the bench reproduce a bug the rig does not have."
print("   counted in the callback, sent from loop()          OK")

# The MAC is published before the flag that advertises it, because the loop
# reads that address the moment it sees the flag.
assert cb.index("memcpy(_rx_peer") < cb.index("_rx_first_ms ="), \
    "the flag is set before the MAC is copied — the loop can register a peer of\n" \
    "    six zero bytes"
print("   MAC copied before the flag that publishes it       OK")

# ---- 3. both streams share one accounting ----------------------------------
# Two send sites keeping their own books is how you measure one stream and leak
# through the other. They take buffers from the same pool.
print("\n3. one send path:")
calls = [l for l in INO.splitlines()
         if "esp_now_send(" in l and not l.lstrip().startswith(("*", "//"))
         and "//" not in l.split("esp_now_send(")[0]]
assert len(calls) == 1, \
    f"{len(calls)} send sites — the counters only mean something if every send\n" \
    "    goes through the same one:\n      " + "\n      ".join(c.strip() for c in calls)
body = INO[INO.index("void loop()"):]
for stream in ("FRAME_DATA", "FRAME_ACK", "FRAME_CMD"):
    assert f"bench_send(" in body and stream in body, f"{stream} is never sent"
print("   data, ack and cmd all go through bench_send()      OK")

send = INO[INO.index("static bool bench_send("):]
send = send[:send.index("\n}\n")]
assert "_cfg.cap && in_flight() >= _cfg.cap" in send, \
    "the cap no longer applies to every send, so `cap 1` would bound one stream\n" \
    "    and not the other"
assert send.index("== ESP_OK") < send.index("_issued = _issued + 1"), \
    "a refused send is counted as issued, which reads as a leak that is not there"
print("   the cap and the ESP_OK guard cover both streams    OK")

# ---- 4. the drain cannot run away ------------------------------------------
print("\n4. the reply drain:")
drain = INO[INO.index("while (_cmd_acked != _cmd_rx"):]
drain = drain[:drain.index("\n    }")]
assert "budget" in drain, \
    "the drain is unbounded. The bridge dequeues from a finite queue that drops\n" \
    "    when full; an unbounded drain invents a burst the rig cannot produce and\n" \
    "    stalls the loop doing it."
assert "_cmd_acked = _cmd_acked + 1" in drain, "replies are not counted"
assert drain.index("_cmd_acked = _cmd_acked + 1") < drain.index("bench_send"), \
    "the count is incremented after the send, so a refused reply is retried\n" \
    "    forever — the rig does not retry an ACK at all"
print("   bounded per pass, counted before the attempt       OK")

# Reset together or the drain fires a burst for commands that predate the run.
reset = INO[INO.index("static void counters_reset()"):]
reset = reset[:reset.index("\n}")]
assert "_cmd_rx = 0" in reset and "_cmd_acked = 0" in reset, \
    "one of the pair survives a reset, so `go` would owe replies for commands\n" \
    "    that arrived before it"
print("   both counters zeroed by the same reset             OK")

# A leak survives `go`; only a reboot clears it. Measuring a second leak run on
# the same power cycle starts from a false zero, and the run after the first
# reproduction did exactly that — in_flight read 3 from first sample to last,
# which was the PREVIOUS run's damage.
# Zeroing while sends are outstanding leaves cb_ok permanently AHEAD of issued —
# their callbacks land after the reset with no matching send. in_flight() clamps
# negatives to zero, so it then reads 0 for the whole run and the first leaked
# buffers are invisible. A cap test against three leaks in twenty hours could
# have come back clean while leaking.
assert "in_flight()" in reset and "delay(" in reset, \
    "the counters are zeroed without letting outstanding sends land, so a run\n" \
    "    started while sends were in flight begins with cb_ok ahead of issued and\n" \
    "    in_flight pinned at 0 for its whole duration"
assert reset.index("delay(") < reset.index("_issued = 0"), \
    "the drain runs after the zeroing, which is no drain at all"
print("   outstanding sends drained before zeroing            OK")

assert "_in_flight_floor" in reset and "REBOOT" in reset, \
    "`go` does not warn when the board has already leaked. It zeroes the\n" \
    "    counters and not the radio, so the next run measures from a false zero\n" \
    "    and looks clean while carrying the last run's losses."
assert reset.index("REBOOT") < reset.index("_in_flight_floor = 0") \
       if "_in_flight_floor = 0" in reset else True, \
    "the warning reads the floor after zeroing it, so it can never fire"
print("   `go` warns if the board has already leaked         OK")

# ---- 5. the overlap is sampled fast enough to see ---------------------------
# The whole verdict rests on this. An overlap lasts about a millisecond;
# report() runs once a second. Tracking the high water mark there missed
# essentially all of them, and said so plainly in a real run: a board sending
# 20 frames a second logged max=0 — never one in flight, which cannot be true.
# The board then declared NO OVERLAP, which was a fact about the sampler.
print("\n5. sampling rate of the instrument:")
rep = INO[INO.index("static void report("):]
rep = rep[:rep.index("\n}")]
for counter in ("_max_in_flight =", "_overlaps ="):
    assert counter not in rep, \
        f"{counter.split(' ')[0]} is updated inside report(), which runs once a\n" \
        "    second. A millisecond-long overlap between two reports is invisible,\n" \
        "    and the run reports 'no overlap' having never looked."

lp = INO[INO.index("void loop()"):]
floor_blk = lp[lp.index("static uint32_t floor_win_ms"):]
floor_blk = floor_blk[:floor_blk.index("// ---- ", 10)]
assert "if (inf > _max_in_flight) _max_in_flight = inf;" in floor_blk, \
    "the high water mark is not tracked beside the floor, at loop rate"
assert "_overlaps = _overlaps + 1" in floor_blk, \
    "overlaps are not counted at loop rate"
assert "prev_inf <= base + 1" in floor_blk, \
    "overlap is counted per sample rather than per transition — a loop running\n" \
    "    tens of thousands of times a second would score one 1 ms overlap dozens\n" \
    "    of times, and the count would measure loop speed"
print("   max and overlaps tracked in loop(), not report()   OK")
print("   counted on the transition, so it counts events      OK")

# Measured against the floor, not a fixed 1. Once buffers leak, in_flight never
# returns below the floor, so a fixed threshold stops counting altogether: the
# first leak run froze ovl at 437,307 for thirteen hours and read as "overlap
# stopped" when what had happened was the floor reaching 2.
assert "uint32_t base = _in_flight_floor;" in floor_blk, \
    "the overlap threshold is not taken from the floor, so the number stops\n" \
    "    counting the moment a leak starts — exactly when it is worth reading"
assert "inf > base + 1" in floor_blk, \
    "the threshold is still a fixed 1 rather than floor+1"
print("   measured above the floor, so it survives a leak    OK")

# A floor rise is the headline event and has to be said out loud, with the heap
# beside it: the two together are the proof.
win = INO[INO.index("if (now - floor_win_ms >= 5000UL)"):]
win = win[:win.index("\n    }", win.index("floor_min = 0xFFFFFFFF;"))]
assert "LEAK" in win and "getFreeHeap" in win, \
    "a floor rise is not announced with the heap beside it. A buffer that never\n" \
    "    came back and the memory it took are one fact, and the run file should\n" \
    "    carry it whether or not anyone was watching the logger."
assert win.index("Serial.printf") < win.index("_in_flight_floor = floor_min;"), \
    "the announcement prints the new floor as the old one — it has to run before\n" \
    "    the assignment to say '2 -> 3' rather than '3 -> 3'"
print("   a floor rise is announced, with the heap           OK")

# The verdict has to read the counter that can actually see one.
verdict = INO[INO.index("static bool overlap_said"):]
verdict = verdict[:verdict.index("static bool overlap_warned")]
assert "_overlaps" in verdict, \
    "the OVERLAP announcement still keys off the high water mark rather than the\n" \
    "    event count"
print("   the announcement reads the event count             OK")

# ---- 6. poll 0 is the old bench, byte for byte -----------------------------
# Two runs that differ in one variable is the whole method. If poll changed
# anything while off, last night's three hours stop being a baseline.
print("\n6. poll 0:")
for guard in ("_cfg.poll_hz && _rx_peer_ready", "_cfg.role == 2"):
    assert guard in body, f"the poll stream is not guarded by {guard}"
print("   the command stream is off unless poll and role rx  OK")
assert "if (_cfg.poll_hz > 2000)" in INO, "an absurd poll rate is not clamped"
# Appended to Cfg, so a blob written by the previous build still loads.
cfg = INO[INO.index("struct Cfg {"):INO.index("};", INO.index("struct Cfg {"))]
assert cfg.rstrip().endswith("poll_hz;   // RX only: commands per second sent AT the TX") \
       or "poll_hz" in cfg.split("running;")[1], \
    "poll_hz is not the last field — an existing NVS blob would load shifted,\n" \
    "    silently changing every setting the bench has saved"
print("   poll_hz appended last, so saved settings survive   OK")

# ---- 7. the one alarm that has to be right -------------------------------
# "Frames arriving while the callbacks stopped" is the question the whole bench
# exists to answer, and it is the alarm most easily believed. It used to compare
# cumulative rx against cumulative callbacks — counters with different origins,
# because `go` zeroes the TX's and not the receiver's. A real run flew it on
# every line for a constant 742-frame offset while cb == issued throughout,
# which is the exact opposite of the fault.
print("\n7. arriving-without-callbacks:")
src = (REPO / "tools/bench_log.py").read_text()
blk = src[src.index("if r_last:"):src.index("if floor > last_floor")]
assert "d_got" in blk and "d_cb" in blk, \
    "the alarm is back to comparing cumulative totals, which cannot survive the\n" \
    "    TX and RX counting from different moments"
assert "issued < prev_issued" in blk, \
    "rx_got is not re-baselined when the TX's counters reset, so it reads high\n" \
    "    by however many frames arrived before `go`"


def alarm(samples):
    """Replay (issued, cbs, rx) samples through the alarm's arithmetic."""
    fired, prev_i = [], (None, None, None)
    for issued, cbs, got in samples:
        pi, pc, pg = prev_i
        d_got = got - pg if pg is not None else -1
        d_cb  = cbs - pc if pc is not None else -1
        d_iss = issued - pi if pi is not None else -1
        fired.append(d_got > 0 and d_iss > 0 and d_cb >= 0 and d_cb < d_got * 0.1)
        prev_i = (issued, cbs, got)
    return fired


# The real run, verbatim: a constant 742 offset, every send answered.
healthy = [(2058, 2058, 2800), (3458, 3458, 4200),
           (4858, 4858, 5600), (6258, 6258, 7000)]
assert not any(alarm(healthy)), \
    "a healthy board with an offset baseline still trips the alarm"
print("   constant 742 offset, cb == issued  -> silent      OK")

# The fault: sends still accepted, frames still landing, callbacks stopped.
wedging = [(2058, 2058, 2800), (3458, 2058, 4200), (4858, 2058, 5600)]
assert alarm(wedging)[1:] == [True, True], \
    "callbacks stopped while frames keep arriving and the alarm stayed quiet —\n" \
    "    this is the one case the bench exists to catch"
print("   callbacks stop, frames keep landing -> fires      OK")

# A reboot on either side must not read as the fault.
assert not any(alarm([(6258, 6258, 7000), (14, 14, 20), (1414, 1414, 1420)])), \
    "a restart reads as the fault"
print("   a restart on either board            -> silent    OK")

print("\nALL CHECKS PASSED")
