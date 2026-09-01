"""Plugging a controller into a running app does not kill it.

Start the PC app with nothing connected, plug in a DualSense, and the app died.
Start it with the controller already there and it was fine.

Two things made that a crash rather than a log line.

JoystickHandler.init() opened the device with no guard, and a device that has
just appeared is not necessarily openable yet — a DualSense over Bluetooth
enumerates in stages, so get_count() can report it a moment before SDL will
hand it over and Joystick(0) raises "Invalid joystick device number".

And init() runs from CommandDispatcher._tick, which is a QTimer slot. PyQt6
routes an unhandled exception in a slot to qFatal(), so it does not print a
warning and carry on — it aborts the process. That is not obvious from reading
the code, so section 4 carries the stack it produces; the crash report from the
rig shows the same frames.

Nothing pumped the SDL event queue while unplugged either — poll() returns
before its pump when there is no joystick, and the dispatcher returns before
poll() — and SDL only notices a device arriving while its queue is pumped.

Run directly, or via tools/run_tests.sh with the rest.
"""
import os, sys, pathlib

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pc_app"))

import pygame
from motion.joystick import JoystickHandler

SRC = (REPO / "pc_app/motion/joystick.py").read_text()


class FakeSDL:
    """Stands in for the joystick corner of pygame, driven like the real thing."""

    def __init__(self):
        self.count = 0
        self.openable = True
        self.pumps = 0
        self.opened = []

    def install(self):
        pygame.event.pump = lambda: setattr(self, "pumps", self.pumps + 1)
        pygame.joystick.get_count = lambda: self.count
        pygame.joystick.Joystick = self._open

    def _open(self, index):
        if index >= self.count:
            raise pygame.error("Invalid joystick device number")
        if not self.openable:
            # What a half-enumerated Bluetooth pad does: counted, not yet given.
            raise pygame.error("Invalid joystick device number")
        self.opened.append(index)
        return FakeJoystick()


class FakeJoystick:
    def init(self):        pass
    def get_name(self):    return "Sony DualSense Wireless Controller"
    def get_axis(self, n): return 0.0


SDL = FakeSDL()
SDL.install()

# ---- 1. nothing plugged in --------------------------------------------------
print("1. running with no controller:")
joy = JoystickHandler()
joy._initialised = True          # pygame.init() already ran for this process
for _ in range(5):
    assert joy.init() is False, "init() claims a joystick with none connected"
assert not joy.connected
assert SDL.pumps >= 5, \
    f"the event queue was pumped {SDL.pumps} times in 5 ticks. SDL only notices " \
    "a\n    device arriving while its queue is pumped, and nothing else pumps it " \
    "while\n    we are unplugged — so a controller plugged in now might never " \
    "be seen"
print(f"   {SDL.pumps} pumps in 5 ticks, so a hot-plug is seen    OK")

# ---- 2. the moment it appears, before it is ready --------------------------
# This is the tick that used to kill the app: counted but not yet openable.
print("\n2. the controller appears but is not ready:")
SDL.count, SDL.openable = 1, False
for _ in range(3):
    try:
        got = joy.init()
    except BaseException as e:
        raise AssertionError(
            f"init() raised {type(e).__name__}: {e}\n"
            "    It runs in a QTimer slot, and PyQt6 turns that into an abort — "
            "this is\n    the app dying the instant the controller is plugged in."
        ) from None
    assert got is False, "init() reports success on a device it could not open"
assert not joy.connected, "a device that would not open became the live handle"
print("   three ticks, no exception, still not connected    OK")

# ---- 3. and then it is ------------------------------------------------------
print("\n3. enumeration finishes:")
SDL.openable = True
assert joy.init() is True, "the controller never connects once it is openable"
assert joy.connected, "connected is still False after a successful open"
assert SDL.opened == [0], f"opened {SDL.opened}, expected device 0 exactly once"
assert joy.init() is True and SDL.opened == [0], \
    "a later tick reopens the device instead of keeping the one it has"
print("   opens once, and stays open                        OK")

# Unplugged again mid-session: poll() must not raise either, for the same reason.
def boom(*_a, **_k):
    raise pygame.error("Invalid joystick device number")


joy._joystick.get_axis = boom
try:
    joy.poll()
except BaseException as e:
    raise AssertionError(f"poll() raised {type(e).__name__} on unplug: {e}") from None
assert not joy.connected, "unplugging did not clear the handle"
assert joy.axes.is_zero(), "the axes kept their last values after an unplug"
print("   unplugging mid-session zeroes and lets go         OK")

# The pump moved inside that guard — before it, an error from the pump escaped.
body = SRC[SRC.index("    def poll(self)"):]
body = body[:body.index("\n    @property")]
assert body.index("try:") < body.index("pygame.event.pump()"), \
    "poll() pumps outside its guard — a pygame error from the pump would abort\n" \
    "    the app exactly as the open used to"
print("   and poll() pumps inside its guard                 OK")

# ---- 4. why any of this matters --------------------------------------------
# An unhandled exception in a Qt slot is not a traceback and a carry-on, it is
# SIGABRT. This was demonstrated once, by raising in a QTimer slot: exit -6,
# and macOS raised a crash report for it. The report is not repeated here —
# a suite that pops a system crash dialog every run is worse than one that
# writes the finding down — and the operator's own report of the DualSense
# crash carries the same stack, which is the evidence that matters:
#
#     5  QtCore          QMessageLogger::fatal(char const*, ...)
#     6  QtCore.abi3.so  pyqt6_err_print() + 888
#     7  QtCore.abi3.so  PyQtSlotProxy::unislot(void**)
#     10 QtCore          QTimer::timerEvent(QTimerEvent*)
#     Termination Reason: SIGNAL 6, Abort trap: 6
#
# So the guards are not defensive tidying. They are the difference between a
# log line and the app going away.
print("\n4. the guards that keep it out of qFatal:")
for fn in ("init", "poll"):
    body = SRC[SRC.index(f"    def {fn}(self)"):]
    body = body[:body.index("\n    def ", 1)] if "\n    def " in body[1:] else body
    assert "except pygame.error" in body, \
        f"{fn}() has no pygame.error guard. It runs in a QTimer slot, and PyQt6 " \
        "sends\n    an unhandled exception there to qFatal() — the app aborts " \
        "rather than logs."
print("   init() and poll() both catch pygame.error         OK")

# ---- 5. the dispatcher's unplugged path ------------------------------------
# _tick is the slot itself. Its no-joystick branch is one call to init(), so it
# inherits everything above — but check the branch is still that shape, because
# an unguarded call added beside it would land straight back in qFatal().
print("\n5. the tick that runs while unplugged:")
DISP = (REPO / "pc_app/motion/command_dispatcher.py").read_text()
tick = DISP[DISP.index("    def _tick(self) -> None:"):]
tick = tick[:tick.index("\n\n")]
assert "if not self._joy.connected:" in tick and "self._joy.init()" in tick, \
    "the unplugged branch changed shape"
assert tick.count("self._joy.") == 2, \
    f"the unplugged branch touches the joystick {tick.count('self._joy.')} times; " \
    "everything it\n    calls has to be non-raising, because this is the slot"
print("   nothing but init(), which cannot raise            OK")

print("\nALL CHECKS PASSED")
