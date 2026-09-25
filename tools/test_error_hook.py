"""An error in a button handler or timer is logged, and the PC app carries on.

Under PyQt6 an exception that escapes a slot ends in qFatal() and the app
aborts: measured on PyQt6 6.11, exit code 134.  pc_app/error_hook.py replaces
sys.excepthook, so PyQt calls it instead and the app survives, and it logs the
error to comms.log where it can be read.

WHAT THIS TEST IS PROTECTING.

  it survives            a raising timer callback no longer kills the process,
                         and the control run without the hook still does — so
                         this test is measuring the hook, not PyQt's mood
  it says so             "UNHANDLED ERROR" with the full traceback, in the log
  it does not flood      the same error in a fast timer is logged once a minute
                         with a count, not a traceback per tick
  it tells them apart    a different error still gets its own report
  threads too            a background thread's error is logged, by name
  honest wording         an error before the event loop runs does stop the app,
                         and the log says so rather than "carries on"
  Ctrl-C is untouched    KeyboardInterrupt goes to Python's own hook
  it never raises        a failure inside the hook falls back to a traceback
  it is installed early  main.py installs it before the window is built

Run directly, or via tools/run_tests.sh with the rest.
"""
import io
import logging
import os
import pathlib
import subprocess
import sys
import threading
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
APP = REPO / "pc_app"
sys.path.insert(0, str(APP))

import error_hook  # noqa: E402


def run_qt(body: str) -> subprocess.CompletedProcess:
    """A small PyQt app in its own process — the unprotected case aborts."""
    script = f"""
import os, sys, logging
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, {str(APP)!r})
logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(levelname)s %(message)s")
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer
import error_hook
{body}
"""
    return subprocess.run([sys.executable, "-c", script], capture_output=True,
                          text=True, timeout=60)


# ---- 1. a raising slot no longer kills the app ------------------------------
print("1. an error in a timer callback:")
LOOP = """
app = QApplication([])
def boom():
    raise KeyError("a bug in a slot")
QTimer.singleShot(0, boom)
QTimer.singleShot(300, lambda: (print("STILL RUNNING"), app.quit()))
print("EXIT", app.exec())
"""
r = run_qt("error_hook.install()\n" + LOOP)
out = r.stdout + r.stderr
assert r.returncode == 0 and "STILL RUNNING" in out, \
    f"the app died on an error in a slot (exit {r.returncode}):\n{out[-800:]}"
assert "UNHANDLED ERROR in the app — logged, and the app carries on" in out, out[-800:]
assert "KeyError: 'a bug in a slot'" in out and "in boom" in out, \
    f"the log does not carry the traceback:\n{out[-800:]}"
control = run_qt(LOOP)
assert control.returncode != 0 and "STILL RUNNING" not in control.stdout, \
    "without the hook the app SURVIVED — PyQt no longer aborts on a slot error, " \
    "so this test's premise has changed; re-read error_hook.py's docstring"
print(f"   with the hook: carries on and logs it; without: exit {control.returncode}   OK")

# ---- 2. the same error in a fast timer does not flood the log ---------------
print("\n2. repeats:")
r = run_qt("""
error_hook.install()
app = QApplication([])
t = QTimer(); t.setInterval(1)
def tick():
    raise ValueError("the same bug, every tick")
t.timeout.connect(tick); t.start()
def other():
    raise RuntimeError("a different bug")
QTimer.singleShot(150, other)
QTimer.singleShot(400, lambda: (print("STILL RUNNING"), app.quit()))
app.exec()
""")
out = r.stdout + r.stderr
n_same = out.count("ValueError: the same bug, every tick")
n_other = out.count("RuntimeError: a different bug")
assert r.returncode == 0 and "STILL RUNNING" in out, f"exit {r.returncode}:\n{out[-600:]}"
assert n_same == 1, f"a 1 ms timer's error was logged in full {n_same} times — it floods the log"
assert n_other == 1, "a different error was swallowed by the repeat limit"
print("   hundreds of ticks -> one traceback; a different error -> its own   OK")

# In-process, with a short window: the next report carries the count.
buf = io.StringIO()
h = logging.StreamHandler(buf)
error_hook.log.addHandler(h)
error_hook.log.propagate = False
error_hook._REPEAT_WINDOW_S = 0.2


def raise_here():
    try:
        raise LookupError("counted")
    except LookupError:
        return sys.exc_info()


for _ in range(5):
    error_hook._on_main_error(*raise_here())
time.sleep(0.25)
error_hook._on_main_error(*raise_here())
logged = buf.getvalue()
assert logged.count("LookupError: counted") == 2, logged
assert "it also happened 4 more time(s) since the last report" in logged, logged
print("   after the window, the next report says how many were counted   OK")

# ---- 3. background threads ----------------------------------------------------
print("\n3. a thread that raises:")
error_hook.install()
buf.truncate(0); buf.seek(0)


def worker():
    raise OSError("the camera feed went away")


th = threading.Thread(target=worker, name="cv-worker")
th.start(); th.join()
logged = buf.getvalue()
assert "UNHANDLED ERROR in thread 'cv-worker' — that thread has stopped; the app carries on" in logged, logged
assert "OSError: the camera feed went away" in logged, logged
print("   logged by name, with its traceback   OK")

# ---- 4. an error before the event loop runs is not "carries on" -----------
print("\n4. an error while starting:")
r = run_qt("""
error_hook.install()
app = QApplication([])
raise KeyError("the window could not be built")
""")
out = r.stdout + r.stderr
assert r.returncode != 0, "a startup error did not stop the app"
assert "while starting or closing, so the app stops" in out and \
    "carries on" not in out, f"a startup error is reported as survivable:\n{out[-600:]}"
print("   reported as stopping the app, which it does   OK")

# ---- 5. Ctrl-C, and a failure inside the hook ------------------------------
print("\n5. what it leaves alone:")
calls = []
real = sys.__excepthook__
sys.__excepthook__ = lambda *a: calls.append(a[0])
try:
    buf.truncate(0); buf.seek(0)
    error_hook._on_main_error(KeyboardInterrupt, KeyboardInterrupt(), None)
    assert calls == [KeyboardInterrupt] and buf.getvalue() == "", \
        "Ctrl-C was swallowed into the log instead of going to Python's own hook"

    # Logging itself fails — the hook must still not raise.
    def log_fails(*a, **k):
        raise RuntimeError("the log could not be written")

    error_hook.log.error = log_fails
    error_hook._seen.clear()
    calls.clear()
    try:
        raise ZeroDivisionError("in a slot")
    except ZeroDivisionError:
        error_hook._on_main_error(*sys.exc_info())      # must not raise
    assert calls == [ZeroDivisionError], \
        "when logging failed, the hook raised or dropped the error instead of " \
        "falling back to a plain traceback"
finally:
    sys.__excepthook__ = real
    del error_hook.log.error
print("   Ctrl-C goes to Python; a hook that cannot log still prints   OK")

# ---- 6. main.py installs it before the window exists -------------------------
print("\n6. the app installs it:")
src = (APP / "main.py").read_text(encoding="utf-8")
assert "error_hook.install()" in src, "main.py never installs the hook"
i_hook = src.index("error_hook.install()")
assert src.index("logging.basicConfig(") < i_hook < src.index("def main()"), \
    "the hook is installed after the window is built, or before logging exists " \
    "— an error in between is either fatal or goes nowhere"
print("   after logging is set up, before main() builds the window   OK")

print("\nALL CHECKS PASSED")
