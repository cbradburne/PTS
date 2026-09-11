#!/usr/bin/env python3
"""Record an espnow_bench run from BOTH boards at once.

    tools/bench_log.py --tx /dev/cu.usbmodem101 --rx /dev/cu.usbmodem201 \
                       --tag cap0-rate50
    tools/bench_log.py --compare bench-logs/*.csv

Both boards on USB into one logger, one PC clock over the pair. That matters
more than convenience, because it is the only way to ask the question the whole
bench exists for:

    when the send callback stops firing, are the frames still ARRIVING?

  frames arrive, callbacks stopped  -> the radio is fine and the CALLBACK is the
                                       fault. The pool leaks because buffers are
                                       never returned, not because sends fail.
  frames stop too                   -> the transmit path really is down, and the
                                       callback is telling the truth.

Those want opposite fixes and one board cannot tell them apart. The TX side sees
its own counters go quiet either way.

    in_flight = issued - (cb_ok + cb_fail)     buffers not given back
    floor                                      lowest in_flight has returned to;
                                               only ever rises, so a rising floor
                                               IS the leak

`refused` cannot warn — a refusal is the radio declining a send, so the report
carrying the number is the one thing that cannot go out.

A run that does not wedge is a result. Tag it and keep it: it is what rules a
configuration out later.
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys
import threading
import time
from datetime import datetime

# cmd/ack are optional so a board still running the previous build logs rather
# than falling through to the notes as unparsed text — which is what a strict
# regex does when only one of the two boards has been flashed.
LINE = re.compile(
    r"t=(?P<t>\d+)s issued=(?P<issued>\d+) cb_ok=(?P<cb_ok>\d+) "
    r"cb_fail=(?P<cb_fail>\d+) refused=(?P<refused>\d+) nomem=(?P<nomem>\d+) "
    r"in_flight=(?P<in_flight>\d+) floor=(?P<floor>\d+) max=(?P<max>\d+) "
    r"rx=(?P<rx>\d+) heap=(?P<heap>\d+) err=(?P<err>0x[0-9A-Fa-f]+)"
    r"(?: cmd=(?P<cmd>\d+) ack=(?P<ack>\d+))?(?: ovl=(?P<ovl>\d+))?"
    # gaps: sequences the RX board never received. Optional like the two
    # groups above, so a board running older firmware still logs every other
    # column instead of failing the match outright.
    r"(?: gaps=(?P<gaps>\d+))?")

FIELDS = ("t", "issued", "cb_ok", "cb_fail", "refused", "nomem",
          "in_flight", "floor", "max", "rx", "heap", "err", "cmd", "ack", "ovl",
          "gaps")


def open_port(dev: str, baud: int, fatal: bool = True):
    try:
        import serial                      # pyserial
    except ImportError:
        sys.exit("pyserial is not installed:  python3 -m pip install pyserial")
    try:
        return serial.Serial(dev, baud, timeout=1)
    except Exception as e:
        if fatal:
            sys.exit(f"cannot open {dev}: {e}")
        return None


class Side:
    """One board's latest numbers, updated by its own reader thread."""

    def __init__(self, name: str):
        self.name  = name
        self.last: dict[str, str] = {}
        self.seen  = 0
        self.alive = 0.0          # when we last heard anything at all
        self.reconnects = 0
        self.notes: list[str] = []


def reader(dev: str, baud: int, side: Side, ports: dict, out,
           lock: threading.Lock, stop: threading.Event) -> None:
    """Read one board, and get it back when it goes away.

    A board that reboots drops its USB CDC device and re-enumerates. The reader
    used to die silently on the stale handle, and the log would show that side
    frozen on its last values while everything looked like it was still running.

    That is not a corner case here — it is the case. THE FAULT BEING HUNTED ENDS
    IN A SELF-RESTART, so without this the logger goes deaf at exactly the
    moment worth watching, and the run says "wedged" with no record of what
    happened next.
    """
    port = ports.get(side.name)
    while not stop.is_set():
        if port is None:
            time.sleep(1.0)
            port = open_port(dev, baud, fatal=False)
            if port is None:
                continue
            ports[side.name] = port          # so the console can write to it again
            side.reconnects += 1
            msg = (f"{side.name} PORT BACK after {side.reconnects} "
                   f"reconnect(s) — the board rebooted, its counters start again")
            with lock:
                out.write(f"# {datetime.now().isoformat(timespec='seconds')} {msg}\n")
            print(f"  ~~~ [{side.name}] reconnected ({dev}) — board rebooted, "
                  f"counters restart")
        try:
            raw = port.readline().decode("utf-8", "replace").strip()
        except Exception as e:
            with lock:
                out.write(f"# {datetime.now().isoformat(timespec='seconds')} "
                          f"{side.name} PORT LOST {e}\n")
            print(f"  ~~~ [{side.name}] port lost ({e}) — waiting for it to come back")
            try:
                port.close()
            except Exception:
                pass
            port = None
            ports[side.name] = None
            continue
        if not raw:
            continue
        now  = datetime.now()
        iso  = now.isoformat(timespec="seconds")
        side.alive = time.time()
        m = LINE.search(raw)
        with lock:
            if not m:
                # FIRST REFUSAL, *** WEDGED, scan notices, the banner. Kept in
                # the same file as the numbers, in time order, because that is
                # how they will be read.
                side.notes.append(f"{iso} {raw}")
                out.write(f"# {iso} {side.name} {raw}\n")
                print(f"  · [{side.name}] {raw}")
            else:
                d = m.groupdict()
                side.last = d
                side.seen += 1
                out.write(f"{iso},{side.name},"
                          + ",".join(d[k] or "0" for k in FIELDS) + "\n")


def console(ports: dict, out, lock: threading.Lock, stop: threading.Event) -> None:
    """Type at the boards from the window that is logging them.

    The logger holds both serial ports, so without this there is no way to say
    `go` — or to change `cap` mid-run — without stopping the recording. Worse,
    a setting changed in another terminal would not appear in the log at all,
    and a run file that does not say what was varied is not evidence.

    Plain text goes to the TX board. Prefix with `rx ` for the receiver.
    `newfile [tag]` is the one command handled here rather than sent on.
    """
    while not stop.is_set():
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            stop.set()
            return
        line = line.strip()
        if not line:
            continue

        # Roll the CSV without touching the boards. A run changing variable —
        # `cap 2` on top of a load that leaks — wants its own file, and the
        # alternative is restarting the logger, which drops samples across the
        # gap and resets the rx baseline and the deltas the wedge alarm needs.
        if line.split()[0] in ("newfile", "roll"):
            bits = line.split(None, 1)
            new_tag = bits[1].strip() if len(bits) > 1 else None
            base = os.path.basename(out.path)
            stem = new_tag or base.rsplit("-", 2)[0]
            nxt = os.path.join(os.path.dirname(out.path),
                               f"{stem}-{datetime.now():%Y%m%d-%H%M%S}.csv")
            with lock:
                old = out.roll(nxt)
            print(f"  --- rolled: {os.path.basename(old)} -> "
                  f"{os.path.basename(nxt)}")
            print("      the boards were not touched — counters carry on. Say "
                  "`go` if you want them zeroed.")
            continue

        side = "tx"
        if line.startswith("rx ") or line.startswith("rx:"):
            side, line = "rx", line[3:].strip()
        port = ports.get(side)
        if port is None:
            print(f"  (no {side} board connected)")
            continue
        try:
            port.write((line + "\n").encode())
        except Exception as e:
            print(f"  (write to {side} failed: {e})")
            continue
        # Into the run file as well as onto the wire: "cap 1 at t=3600" is the
        # single most important thing a comparison needs and the easiest to
        # forget you did.
        with lock:
            out.write(f"# {datetime.now().isoformat(timespec='seconds')} "
                      f"{side} <<< {line}\n")
        print(f"  >>> [{side}] {line}")


class Out:
    """The run file, swappable underneath the threads writing to it.

    `newfile` starts a fresh CSV without touching the boards. Restarting the
    logger would do it too, but at a cost: it drops the samples either side of
    the gap and resets the rx baseline and the deltas the wedge alarm is built
    on. Rolling the file leaves the run running and the boards untouched.

    The threads hold this object rather than the handle, so one swap reaches
    all of them.
    """

    def __init__(self, path: str):
        self.path = path
        self.f = open(path, "w", buffering=1)
        self.rolled: list[str] = []
        self._header()

    def _header(self) -> None:
        self.f.write("wall_iso,side," + ",".join(FIELDS) + "\n")

    def write(self, s: str) -> None:
        self.f.write(s)

    def roll(self, path: str) -> str:
        # A note at each end, so neither file is a fragment of unknown origin.
        old = self.path
        self.f.write(f"# {datetime.now().isoformat(timespec='seconds')} "
                     f"continues in {os.path.basename(path)}\n")
        self.f.close()
        self.rolled.append(old)
        self.path = path
        self.f = open(path, "w", buffering=1)
        self._header()
        self.f.write(f"# {datetime.now().isoformat(timespec='seconds')} "
                     f"continued from {os.path.basename(old)} — the boards were "
                     f"not touched, their counters carry on\n")
        return old

    def close(self) -> None:
        self.f.close()


def record(tx_dev: str, rx_dev: str | None, tag: str, outdir: str,
           baud: int, every: int) -> None:
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, f"{tag}-{datetime.now():%Y%m%d-%H%M}.csv")

    tx = Side("tx")
    rx = Side("rx") if rx_dev else None
    lock = threading.Lock()
    stop = threading.Event()

    tx_port = open_port(tx_dev, baud)
    rx_port = open_port(rx_dev, baud) if rx_dev else None
    ports = {"tx": tx_port, "rx": rx_port}

    print(f"recording -> {path}")
    print(f"  tx {tx_dev}")
    print(f"  rx {rx_dev}" if rx_dev else "  rx (not connected — the arriving/"
          "not-arriving question cannot be answered)")
    print("\nType commands here: plain text goes to the TX board, prefix `rx `")
    print("for the receiver. Try `help`, or `stats`. First time:")
    print("    rx role rx        then read its MAC off the line it prints")
    print("    role tx")
    print("    peer AA:BB:CC:DD:EE:FF")
    print("    go")
    print("`newfile [tag]` starts a fresh CSV without touching the boards —")
    print("use it when you change the variable under test.")
    print("Ctrl-C to stop and summarise.\n")

    started = time.time()
    f = Out(path)
    try:
        threads = [threading.Thread(target=reader,
                                    args=(tx_dev, baud, tx, ports, f, lock, stop),
                                    daemon=True)]
        if rx_dev:
            threads.append(threading.Thread(target=reader,
                                            args=(rx_dev, baud, rx, ports, f, lock, stop),
                                            daemon=True))
        threads.append(threading.Thread(target=console,
                                        args=(ports, f, lock, stop), daemon=True))
        for t in threads:
            t.start()

        last_floor = 0
        base_rx = None
        prev_cbs = prev_got = prev_issued = None
        try:
            while True:
                time.sleep(every)
                with lock:
                    t_last, r_last = dict(tx.last), dict(rx.last) if rx else {}
                if not t_last:
                    print("  (no report from the TX board yet — type `role tx` "
                          "then `go` here, or `help`)")
                    continue

                # Stale is not the same as steady. Two identical lines a minute
                # apart read as "nothing is changing" when what happened is that
                # the board stopped talking — which is how a frozen TX side
                # looked like a healthy one.
                tx_age = time.time() - tx.alive
                if tx_age > max(5.0, every * 1.5):
                    print(f"  [tx] SILENT for {tx_age:.0f}s — last values below "
                          f"are stale, not current")

                floor  = int(t_last["floor"])
                issued = int(t_last["issued"])
                cbs    = int(t_last["cb_ok"]) + int(t_last["cb_fail"])
                line = (f"  t={t_last['t']:>6}s issued={issued:<9} cb={cbs:<9} "
                        f"in_flight={t_last['in_flight']:<4} floor={floor:<4} "
                        f"refused={t_last['refused']}")

                # Whether `poll` is doing its job. A run with commands flowing
                # but max still 1 is a metronome with extra steps, and the
                # sooner that is on screen the less of a night it wastes.
                if int(t_last.get("cmd") or 0):
                    ovl = int(t_last.get("ovl") or 0)
                    line += f" | acked={t_last['ack']} peak={t_last['max']} ovl={ovl}"
                    line += "" if ovl else "  <-- STILL NO OVERLAP"

                if r_last:
                    got = int(r_last["rx"])
                    if base_rx is None or got < base_rx:
                        base_rx = got        # first sample, or the board rebooted
                    # `go` on the TX zeroes ITS counters and not the receiver's,
                    # so without this the two are counted from different moments
                    # and rx_got reads permanently high by however many frames
                    # arrived before the TX was started.
                    if prev_issued is not None and issued < prev_issued:
                        base_rx = got
                    line += f" | rx_got={got - base_rx}"

                    # The comparison only one logger can make: what the sender
                    # thinks it completed, against what the receiver actually
                    # got. They diverge in the interesting case.
                    #
                    # Asked as a RATE, not a total. Comparing cumulative rx
                    # against cumulative callbacks meant comparing two counters
                    # with different origins, and a run where the TX was started
                    # second flew this alarm all night on a CONSTANT offset —
                    # while cb == issued on every single line, which is the
                    # opposite of the fault. Deltas have no origin to disagree
                    # about.
                    d_got = got - prev_got if prev_got is not None else -1
                    d_cb  = cbs - prev_cbs if prev_cbs is not None else -1
                    d_iss = issued - prev_issued if prev_issued is not None else -1
                    if (d_got > 0 and d_iss > 0 and d_cb >= 0
                            and d_cb < d_got * 0.1):
                        line += "  <-- ARRIVING WITHOUT CALLBACKS"
                    prev_got = got
                    if time.time() - rx.alive > 10:
                        line += "  <-- RX BOARD SILENT"
                prev_cbs, prev_issued = cbs, issued
                print(line)

                if floor > last_floor:
                    print(f"  FLOOR {last_floor} -> {floor}   buffers not coming "
                          f"back — this is the leak")
                    last_floor = floor
        except KeyboardInterrupt:
            stop.set()
    finally:
        f.close()

    # The file that was open at the end, not the one opened at the start —
    # `newfile` may have rolled it several times since.
    summarise_run(f.path, time.time() - started)
    if f.rolled:
        print(f"  (earlier parts of this session: "
              f"{', '.join(os.path.basename(p) for p in f.rolled)})")


def read_csv(path: str):
    rows = []
    with open(path) as f:
        for line in f:
            if line.startswith("#") or line.startswith("wall_iso"):
                continue
            p = line.strip().split(",")
            if len(p) != len(FIELDS) + 2:
                continue
            d = dict(zip(FIELDS, p[2:]))
            d["side"] = p[1]
            rows.append(d)
    return rows


def summarise_run(path: str, secs: float) -> None:
    rows = read_csv(path)
    txr = [r for r in rows if r["side"] == "tx"]
    rxr = [r for r in rows if r["side"] == "rx"]
    print(f"\n{path}")
    print(f"  ran {secs / 60:.1f} min")
    if not txr:
        print("  no TX samples — was the board told 'go'?")
        return
    last = txr[-1]
    lost = int(last["issued"]) - int(last["cb_ok"]) - int(last["cb_fail"])
    ref  = next((r["t"] for r in txr if int(r["refused"])), None)
    wed  = next((r["t"] for r in txr if int(r["nomem"]) > 50), None)
    print(f"  issued             {last['issued']}")
    print(f"  callbacks          {int(last['cb_ok']) + int(last['cb_fail'])}")
    print(f"  never returned     {lost}")
    print(f"  floor reached      {last['floor']}")
    print(f"  first refusal at   {ref + 's' if ref else 'never'}")
    print(f"  wedged at          {wed + 's' if wed else 'never'}")
    if rxr:
        got = int(rxr[-1]["rx"]) - int(rxr[0]["rx"])
        print(f"  receiver got       {got}")
        cbs = int(last["cb_ok"]) + int(last["cb_fail"])
        if lost > 20 and got > cbs * 1.05:
            print("  -> frames ARRIVED that never produced a callback. The radio "
                  "sent them;\n     the callback is the fault, and the pool leaks "
                  "because of it.")
        elif lost > 20:
            print("  -> frames stopped arriving as the callbacks stopped. The "
                  "transmit path\n     itself is down, not just the notification.")
    else:
        print("  receiver           not connected")
    if int(last["floor"]) == 0 and ref is None:
        print("  -> no leak in this run. That is a result: it rules this "
              "configuration out.")


def compare(paths: list[str]) -> None:
    """Two runs differing in one variable, read against each other. That
    comparison is the finding; a single run says almost nothing."""
    print(f"{'run':<32} {'mins':>6} {'issued':>9} {'lost':>6} {'floor':>6} "
          f"{'rx_got':>8} {'1st ref':>9} {'wedged':>8}")
    for p in sorted(paths):
        rows = read_csv(p)
        txr = [r for r in rows if r["side"] == "tx"]
        rxr = [r for r in rows if r["side"] == "rx"]
        name = os.path.basename(p)
        if not txr:
            print(f"{name:<32} {'(no tx samples)':>6}")
            continue
        last = txr[-1]
        lost = int(last["issued"]) - int(last["cb_ok"]) - int(last["cb_fail"])
        ref  = next((r["t"] for r in txr if int(r["refused"])), None)
        wed  = next((r["t"] for r in txr if int(r["nomem"]) > 50), None)
        got  = (int(rxr[-1]["rx"]) - int(rxr[0]["rx"])) if rxr else None
        print(f"{name:<32} {int(last['t']) / 60:6.1f} {last['issued']:>9} "
              f"{lost:>6} {last['floor']:>6} "
              f"{(got if got is not None else '-'):>8} "
              f"{(ref + 's') if ref else 'never':>9} "
              f"{(wed + 's') if wed else 'never':>8}")
    print("\n'lost' is issued minus callbacks — buffers the stack never gave "
          "back.\n'rx_got' is what the other board actually received. lost high "
          "with rx_got\nhealthy means the frames went out and only the callback "
          "was missing.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tx", help="serial port of the sending board")
    ap.add_argument("--rx", help="serial port of the receiving board")
    ap.add_argument("--tag", default="run",
                    help="name the run after its variables, e.g. cap0-rate50-load40")
    ap.add_argument("--outdir", default="bench-logs")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--every", type=int, default=10,
                    help="seconds between status lines (default 10)")
    ap.add_argument("--compare", nargs="+", metavar="CSV",
                    help="summarise finished runs side by side")
    a = ap.parse_args()

    if a.compare:
        files = [f for pat in a.compare for f in glob.glob(pat)] or a.compare
        compare(files)
        return
    if not a.tx:
        ap.error("give --tx (and ideally --rx), or --compare some CSVs")
    record(a.tx, a.rx, a.tag, a.outdir, a.baud, a.every)


if __name__ == "__main__":
    main()
