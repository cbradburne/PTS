"""
Unhandled errors are logged, not fatal.

Under PyQt6 an exception that escapes a slot, timer callback or event handler
ends in qFatal(): the app aborts.  Measured 2026-09-25 on PyQt6 6.11, a KeyError
raised in a QTimer callback killed the process with exit code 134.  On this rig
that is one bug in one button handler closing the console mid-show.

Replacing sys.excepthook changes it: PyQt calls the hook instead, the handler
that raised is abandoned, and the app carries on.  threading.excepthook does the
same for background threads, which otherwise die with a traceback on stderr that
nobody at the rig sees.  The bridge's own threads guard themselves; this covers
everything else.

Each distinct error is logged in full once a minute at most, with a count of the
repeats in between.  A fault in a fast timer would otherwise write a traceback
on every tick, and ten-megabyte logs rotate the whole history out within minutes
— the log is how this rig is diagnosed.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
import traceback

log = logging.getLogger("app.errors")

# One full traceback per distinct error per window; the rest are counted.
_REPEAT_WINDOW_S = 60.0

_lock = threading.Lock()
# (exception type, file, line) -> [window start, repeats since the last full log]
_seen: dict[tuple, list] = {}


def _signature(exc_type, tb) -> tuple:
    """Where the error came from: its type and the innermost frame."""
    frames = traceback.extract_tb(tb) if tb is not None else []
    if frames:
        return (exc_type.__qualname__, frames[-1].filename, frames[-1].lineno)
    return (exc_type.__qualname__, "?", 0)


def _due(sig) -> tuple[bool, int]:
    """Whether to log this one in full, and how many were counted since."""
    now = time.monotonic()
    with _lock:
        st = _seen.get(sig)
        if st is None or now - st[0] >= _REPEAT_WINDOW_S:
            repeats = st[1] if st is not None else 0
            _seen[sig] = [now, 0]
            return True, repeats
        st[1] += 1
        return False, 0


def _report(where: str, outcome: str, exc_type, exc, tb) -> None:
    full, repeats = _due(_signature(exc_type, tb))
    if not full:
        return
    text = "".join(traceback.format_exception(exc_type, exc, tb)).rstrip()
    again = (f" — it also happened {repeats} more time(s) since the last report"
             if repeats else "")
    log.error("UNHANDLED ERROR in %s — %s%s\n%s", where, outcome, again, text)


def _event_loop_running() -> bool:
    """True inside app.exec(): a slot or timer raised, and the app survives.
    False while starting up or shutting down, when Python exits after this."""
    try:
        from PyQt6.QtCore import QThread
        return QThread.currentThread().loopLevel() > 0
    except Exception:
        return False


def _on_main_error(exc_type, exc, tb) -> None:
    # Ctrl-C in a console keeps its usual behaviour.
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    try:
        if _event_loop_running():
            _report("the app", "logged, and the app carries on", exc_type, exc, tb)
        else:
            _report("the app", "while starting or closing, so the app stops",
                    exc_type, exc, tb)
    except Exception:
        # The hook itself must never raise: fall back to a plain traceback.
        sys.__excepthook__(exc_type, exc, tb)


def _on_thread_error(args) -> None:
    if issubclass(args.exc_type, SystemExit):
        return                                  # as the default hook does
    name = args.thread.name if args.thread is not None else "?"
    try:
        _report(f"thread '{name}'", "that thread has stopped; the app carries on",
                args.exc_type, args.exc_value, args.exc_traceback)
    except Exception:
        threading.__excepthook__(args)


def install() -> None:
    """Route unhandled errors to the log.  Call once, after logging is set up."""
    sys.excepthook = _on_main_error
    threading.excepthook = _on_thread_error
