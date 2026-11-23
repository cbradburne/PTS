"""One copy of the PC app at a time (pc_app/single_instance.py).

Two copies ran on the concert PC from 2026-11-20 20:36 to 2026-11-23: both on
the hub, both reading the joystick, both writing comms.log.  These run real
processes on this computer, as two launches would:

1. a second copy is refused while the first runs, and its doorbell brings the
   first to the front; the second exits quietly when it does;
2. a crash (SIGKILL) leaves nothing that keeps the next start out — not the
   lock, not the doorbell's socket file;
3. a copy that holds the claim but does not answer is reported, not ignored:
   "answered" means the running copy acted, not that a socket was there;
4. main.py claims before it opens comms.log, imports cv2 or builds the window,
   and only when it runs as the app, not when a test reads it.

The Windows branch (a named mutex) cannot run here; it is checked for the two
properties that matter — the error code it refuses on, and that a failure to
make the claim starts the app anyway.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, time, signal, pathlib, subprocess, tempfile, textwrap

REPO = pathlib.Path(__file__).resolve().parent.parent
APP = REPO / "pc_app"
NAME = f"pts-pc-app-test-{os.getpid()}"
ENV = dict(os.environ, QT_QPA_PLATFORM="offscreen", TEST_NAME=NAME,
           PYTHONPYCACHEPREFIX=tempfile.mkdtemp())

PRELUDE = textwrap.dedent(f"""
    import sys, os
    sys.path.insert(0, {str(APP)!r})
    import single_instance as si
    si.NAME = os.environ["TEST_NAME"]
    from PyQt6.QtCore import QCoreApplication, QTimer
""")

FIRST = PRELUDE + textwrap.dedent("""
    c = si.claim()
    print("CLAIMED" if c else "REFUSED", flush=True)
    if not c: sys.exit(3)
    app = QCoreApplication([])
    mode = sys.argv[1] if len(sys.argv) > 1 else "doorbell"
    if mode == "doorbell":
        d = si.Doorbell(lambda: print("BROUGHT FORWARD", flush=True))
    elif mode == "mute":
        # Holds the claim and listens, but never answers: a hung copy.
        from PyQt6.QtNetwork import QLocalServer
        QLocalServer.removeServer(si.NAME)
        srv = QLocalServer(); srv.listen(si.NAME)
    print("READY", flush=True)
    QTimer.singleShot(int(sys.argv[2]) if len(sys.argv) > 2 else 15000, app.quit)
    app.exec()
""")

SECOND = PRELUDE + textwrap.dedent("""
    c = si.claim()
    if c:
        print("CLAIMED", flush=True); sys.exit(0)
    app = QCoreApplication([])
    print("REFUSED, RANG " + ("ANSWERED" if si.ring() else "NO ANSWER"), flush=True)
""")

HAND_OVER = PRELUDE + textwrap.dedent("""
    from PyQt6.QtWidgets import QMessageBox
    QMessageBox.warning = staticmethod(lambda *a, **k: print("MESSAGE: " + a[1], flush=True))
    si.claim_or_hand_over()
    print("STARTED", flush=True)
""")


def start_first(mode="doorbell", life_ms=15000):
    p = subprocess.Popen([sys.executable, "-c", FIRST, mode, str(life_ms)], env=ENV,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    line = p.stdout.readline().strip()
    assert line == "CLAIMED", f"the first copy could not claim: {line!r} {p.stderr.read()[-300:]}"
    assert p.stdout.readline().strip() == "READY", "the first copy never got its doorbell up"
    return p


def run(script):
    r = subprocess.run([sys.executable, "-c", script], env=ENV, capture_output=True,
                       text=True, timeout=30)
    return r.returncode, r.stdout.strip(), r.stderr


def stop(p):
    p.kill()
    p.wait()


# ---- 1. a second copy ------------------------------------------------------------
print("1. a second launch while the app runs:")
first = start_first()
try:
    code, out, err = run(SECOND)
    assert out != "CLAIMED", "a second copy was let in while the first held the claim"
    assert out == "REFUSED, RANG ANSWERED", f"the second copy got {out!r}\n{err[-400:]}"
    assert first.stdout.readline().strip() == "BROUGHT FORWARD", \
        "the doorbell rang but the running copy never brought its window forward"
    print("   refused; the running copy was brought forward                OK")
    code, out, err = run(HAND_OVER)
    assert code == 0 and out == "", \
        f"claim_or_hand_over should exit quietly when the running copy answers: {code} {out!r}"
    assert first.stdout.readline().strip() == "BROUGHT FORWARD"
    print("   the second copy exits quietly, before starting anything        OK")
finally:
    stop(first)

code, out, _ = run(SECOND)
assert out == "CLAIMED", "after the first copy closed, the next start was still refused"
print("   once it has closed, the next start goes ahead                  OK")

# ---- 2. after a crash ---------------------------------------------------------------
print("\n2. after a crash:")
first = start_first()
os.kill(first.pid, signal.SIGKILL)
first.wait()
again = start_first()            # asserts it could claim AND listen again
try:
    code, out, _ = run(SECOND)
    assert out == "REFUSED, RANG ANSWERED", \
        f"after a crash the new copy's doorbell did not work: {out!r} (a stale socket file?)"
    again.stdout.readline()
    print("   a killed copy leaves no lock and no socket behind               OK")
finally:
    stop(again)

# ---- 3. a copy that does not answer ----------------------------------------------------
print("\n3. a running copy that does not answer:")
mute = start_first("mute")
try:
    code, out, _ = run(SECOND)
    assert out == "REFUSED, RANG NO ANSWER", \
        f"a hung copy was reported as answering: {out!r} — 'answered' must mean it acted"
    code, out, _ = run(HAND_OVER)
    assert code == 1 and out.startswith("MESSAGE: PC app already running"), \
        f"with the running copy not answering, nothing told the operator: {code} {out!r}"
    assert "STARTED" not in out, "the second copy went on and started anyway"
    print("   refused, and the operator is told it is not responding         OK")
finally:
    stop(mute)

# ---- 4. where main.py claims ---------------------------------------------------------------
print("\n4. main.py:")
src = (APP / "main.py").read_text(encoding="utf-8")
i_claim = src.index("single_instance.claim_or_hand_over()")
guard = src.rfind('if __name__ == "__main__":', 0, i_claim)
assert guard != -1 and src[guard:i_claim].count("\n") <= 3, \
    "the claim is not inside `if __name__ == \"__main__\"` — reading main.py in a test would claim"
for later in ("RotatingFileHandler(", "import cv2", "from PyQt6.QtWidgets import QApplication",
              "Bridge(", "JoystickHandler("):
    assert i_claim < src.index(later), f"main.py does '{later}' before it claims: a second copy would"
i_door = src.index("single_instance.Doorbell(")
assert src.index("window.show()") < i_door < src.index("app.exec()"), \
    "the doorbell is not set up between showing the window and running the app"
print("   claims before the log, cv2, Qt, the hub link and the joystick   OK")
print("   rings only when run as the app; doorbell after the window      OK")

# ---- 5. the Windows branch, read --------------------------------------------------------------
print("\n5. the Windows branch:")
si_src = (APP / "single_instance.py").read_text(encoding="utf-8")
win = si_src[si_src.index("def _claim_windows"):si_src.index("def _claim_flock")]
assert "ERROR_ALREADY_EXISTS = 183" in win and "if err == ERROR_ALREADY_EXISTS:" in win, \
    "the mutex refuses on something other than ERROR_ALREADY_EXISTS"
assert win.index("if not handle:") < win.index("if err == ERROR_ALREADY_EXISTS:"), \
    "a failed CreateMutexW is not handled before the already-exists test"
assert "starting anyway" in win[win.index("if not handle:"):win.index("if err == ERROR_ALREADY_EXISTS:")], \
    "a failure to make the claim does not start the app anyway"
print("   refuses only on ERROR_ALREADY_EXISTS; a failed claim starts anyway OK")

print("\nALL CHECKS PASSED")
