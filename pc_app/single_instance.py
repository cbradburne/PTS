"""
One copy of the PC app at a time.

From 2026-11-20 20:36 to 2026-11-23 two copies ran on the concert PC.  Both
connected to the hub and both read the joystick, so every stick movement went
out twice, to whichever camera each copy had selected.  Both wrote comms.log:
every report line twice, and the backups' numbers skipping, because each copy
renumbered the same files on its own count.

So the app claims the computer before it does anything else.  A second launch
finds the first, asks it to come to the front, and closes, before it has
opened comms.log, connected to the hub or touched the joystick.  Double-clicking
the app while it is running, minimised or hidden behind something, now shows
the copy that is running.  That is almost certainly how the second one started.

The claim is the operating system's: a named mutex on Windows, flock() on a
file elsewhere.  Either goes when the process does, however it ends, so a
crash or a power cut cannot leave a stale lock that keeps the next start out.
And if the claim itself cannot be made (an odd filesystem, an API failure), the
app starts anyway.  A broken lock must never stop an operator starting the app.

The doorbell to the running copy is a QLocalServer, and only a convenience.
When nothing answers it — the running copy is busy or hung — the second copy
says so and how to close the stuck one, instead of silently doing nothing.
"""
from __future__ import annotations

import getpass
import logging
import re
import sys
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


def _name() -> str:
    """Per user: one copy per person logged in, not one per computer."""
    try:
        user = getpass.getuser()
    except Exception:                       # no user name in this environment
        user = "user"
    return "pts-pc-app-" + (re.sub(r"[^A-Za-z0-9_-]", "_", user) or "user")


NAME = _name()


class Claim:
    """Held for the life of the process.  The OS releases it when it ends."""

    def __init__(self, handle=None, file=None, how: str = ""):
        self._handle = handle               # Windows mutex
        self._file = file                   # flock()ed file
        self.how = how                      # what made it, for the log


def claim() -> Claim | None:
    """This copy's claim on the computer, or None when another copy holds it."""
    if sys.platform == "win32":
        return _claim_windows()
    return _claim_flock()


def _claim_windows() -> Claim | None:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    ERROR_ALREADY_EXISTS = 183
    # "Local\" is this logon session: the copies that matter share a desktop,
    # because both read the same joystick and draw on the same screen.
    handle = k32.CreateMutexW(None, False, "Local\\" + NAME)
    err = ctypes.get_last_error()
    if not handle:
        return Claim(how=f"no claim (CreateMutexW failed, error {err}): starting anyway")
    if err == ERROR_ALREADY_EXISTS:
        k32.CloseHandle(handle)
        return None
    return Claim(handle=handle, how="Windows mutex Local\\" + NAME)


def _claim_flock() -> Claim | None:
    import fcntl

    path = Path(tempfile.gettempdir()) / (NAME + ".lock")
    try:
        f = open(path, "a")
    except OSError as e:
        return Claim(how=f"no claim ({path}: {e}): starting anyway")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    except OSError as e:
        return Claim(file=f, how=f"no claim (flock: {e}): starting anyway")
    return Claim(file=f, how=f"flock {path}")


# ---- the doorbell -------------------------------------------------------------

def ring(timeout_ms: int = 1500) -> bool:
    """From a second copy: ask the running one to come to the front.  True if
    it answered.  Needs a QCoreApplication."""
    from PyQt6.QtNetwork import QLocalSocket

    if sys.platform == "win32":
        # Windows lets only the foreground process bring a window forward; this
        # copy was just launched, so it is, and it passes that on.
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(-1)      # ASFW_ANY
        except Exception:
            pass
    s = QLocalSocket()
    s.connectToServer(NAME)
    if not s.waitForConnected(timeout_ms):
        return False
    s.write(b"show\n")
    s.flush()
    s.waitForBytesWritten(timeout_ms)
    # Wait for the running copy's answer, so "answered" means it acted, not
    # merely that its socket was there.
    ok = s.waitForReadyRead(timeout_ms) and bytes(s.readAll()).startswith(b"shown")
    s.disconnectFromServer()
    return ok


class Doorbell:
    """In the running copy: a later launch rings, and on_ring brings the
    window forward.  Keep a reference for the life of the app."""

    def __init__(self, on_ring):
        from PyQt6.QtNetwork import QLocalServer

        self._on_ring = on_ring
        self._server = QLocalServer()
        # A crash leaves a socket file behind on macOS and Linux.  This copy
        # holds the claim, so no live copy can own it: safe to clear.
        QLocalServer.removeServer(NAME)
        if not self._server.listen(NAME):
            log.warning("PC APP doorbell: could not listen (%s) — a second launch "
                        "will say the app is running, but cannot bring it forward",
                        self._server.errorString())
        self._server.newConnection.connect(self._answer)

    def _answer(self) -> None:
        while self._server.hasPendingConnections():
            s = self._server.nextPendingConnection()
            s.disconnected.connect(s.deleteLater)
            s.readyRead.connect(lambda s=s: self._read(s))

    def _read(self, s) -> None:
        if not bytes(s.readAll()).startswith(b"show"):
            return
        try:
            self._on_ring()
        finally:
            s.write(b"shown\n")
            s.flush()


def claim_or_hand_over() -> Claim:
    """At the very top of main.py: this copy's claim, or — when a copy is
    already running — bring that one forward and exit this one."""
    c = claim()
    if c is not None:
        return c
    from PyQt6.QtWidgets import QApplication, QMessageBox

    app = QApplication.instance() or QApplication(sys.argv)
    if ring():
        sys.exit(0)            # the running copy is now in front: nothing to say
    QMessageBox.warning(
        None, "PC app already running",
        "The PC app is already running on this computer, but it did not come "
        "to the front when asked.  It may be busy, or not responding.\n\n"
        "Use the copy that is running.  If it is stuck, close it first: Task "
        "Manager, then the Python process running main.py.  Then start the app "
        "again.")
    sys.exit(1)
