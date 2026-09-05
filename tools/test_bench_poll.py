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
NEW = OLD + " cmd=1220 ack=1220"

m = bench_log.LINE.search(OLD)
assert m, "the previous build's line no longer parses — half a run would be lost"
row = [m.groupdict()[k] or "0" for k in bench_log.FIELDS]
assert len(row) == len(bench_log.FIELDS), "row width does not match the header"
assert row[-2:] == ["0", "0"], f"missing cmd/ack should read 0, got {row[-2:]}"
assert None not in row, "a None would raise inside the join and kill the reader"
print(f"   old line -> {len(row)} columns, cmd/ack default to 0     OK")

m = bench_log.LINE.search(NEW)
assert m, "the new line does not parse at all"
d = m.groupdict()
assert d["cmd"] == "1220" and d["ack"] == "1220", f"cmd/ack misread: {d}"
assert d["heap"] == "284512" and d["err"] == "0x0", \
    "the optional group ate part of the line before it"
print("   new line -> cmd=1220 ack=1220, nothing else disturbed  OK")

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

# ---- 5. poll 0 is the old bench, byte for byte -----------------------------
# Two runs that differ in one variable is the whole method. If poll changed
# anything while off, last night's three hours stop being a baseline.
print("\n5. poll 0:")
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

print("\nALL CHECKS PASSED")
