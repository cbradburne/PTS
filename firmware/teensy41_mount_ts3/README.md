# Mount firmware on TeensyStep 3 (test build)

The Teensy 4.1 mount firmware built on **TeensyStep 3.0.0** instead of our
TeensyStep4 copy, for trying on the bench before anything changes on the rig.
If it doesn't work out, delete this folder, `libraries/TeensyStep` and the
`teensyts3` target.

## What is in it

| File | Against production (`firmware/teensy41_mount`) |
|---|---|
| `MountMotion.cpp` | the same file, unchanged |
| `MountMotion.h` | the same, except that it includes `ts4_on_ts3.h` instead of `<teensystep4.h>` |
| `teensy41_mount_ts3.ino` | the same, plus the lines marked `(TS3)`: a note in the header, a second banner line, and TeensyStep 3's reports in `loop()` |
| `ts4_on_ts3.h` | **the port**: TeensyStep4's interface, as MountMotion uses it, on TeensyStep 3. Each call's mapping is described at its top |
| `protocol.h`, `shared` | the same |

So the motion code on the bench is the rig's; only the library under it
differs.  `tools/test_teensy_ts3.py` keeps it that way: a change to the
production files fails it until it is copied here.  It also checks the
mapping against a stand-in for TeensyStep 3.

## Build and flash

```bash
tools/build.sh flash teensyts3
```

On Windows: `tools\build.bat flash teensyts3 COMxx`.  Back to production:
`tools/build.sh flash teensy`.  Either is a Teensy flash like any other:
re-home and set ref 0/0 afterwards, as usual.

The build uses the repo's `libraries/TeensyStep` (3.0.0).  The Arduino IDE
would use whichever TeensyStep it has installed — 2.3.4 on this Mac — and
fail, so build it with the script.

On the USB serial monitor (115200) the banner reads `TeensyStep 3 test build
(firmware/teensy41_mount_ts3)`, and anything TeensyStep 3 reports prints as
`[TS3] … error(s), last: …` or `[TS3] … warning(s), last: …`, at most once a
second.

## What is different on the bench

Kept as production does it, so that a difference is the library's:
- how `setMaxSpeed()`, `setAcceleration()` and the speed override behave, the
  acceleration limit of v²/2 included;
- a rotation's stop, which takes effect at once (see below).

TeensyStep 3's own, the things to watch:
- **Pull-in speed: TeensyStep 3's default, 100 steps/s.**  Below it a motor
  starts, stops and reverses without a ramp.  TeensyStep4 started every move at
  200 steps/s.  Shows in the slowest moves: the start and end of a slow pan or
  tilt, CV tracking at low speed, the zoom.
- **The ramp** is decided again at every step from the current speed and the
  goal: speed changes during a move, reversals, the GOTO loop's braking.
- **STEP pulse and DIR setup time: 5 µs each**, TeensyStep 3's defaults, made
  by the timer rather than waited out inside the interrupt.  (Ours were cut
  from 8 and 5 µs to 1 µs because that waiting starved the interrupt.)
- **The step interrupts run at priority 48**, ahead of the serial port to the
  ESP32 and the StallGuard pins.  Watch for lost commands and for limit finds.
- **Timers** are taken at the start of a move and given back at its end by
  TeensyStep 3 itself; none of our timer fixes are needed.
- Memory: about 9 KB more code and 32 KB more RAM than production; 336 KB of
  RAM is still free.

## A finding: a rotation stops dead in production

Our TeensyStep4's rotation interrupt stops a motor the moment its target speed
is 0.  So `stopAsync()` on a rotating axis never decelerates.  The acceleration
MountMotion sets just before it (`JOG_STOP_ACCEL_SCALE`, "Soft stop —
decelerate gently") is never used.  A joystick hides this, because the PC app
and the displays ease the speed down before they send the stop.  But a soft
limit or the jog watchdog stops the axis from full speed.

`TS3_SOFT_STOP`, at the top of `ts4_on_ts3.h`, chooses:
- **0, the default:** at once, as production;
- **1:** decelerate at that acceleration, as MountMotion's comments intend.  A
  soft-limit stop then runs past the limit by v²/2a, so try it with the limits
  in mind.

The Teensy toolchain takes no extra compiler flags from the build script, so
this is a change to the header, not an environment variable.

## What to try, on one mount

- **Jog** each axis, slow and fast: reversals and letting go of the stick, from
  the PC app, a display's controller and the web app.
- **GOTO** between stored positions on slow and fast presets.  Does it land
  where it was stored?
- **Runs**: positions, and look-at.
- **Look-at**: slides with and without a subject, switches mid-move, CV
  tracking at low speed.
- **Limit finds and homing** (slider, zoom): StallGuard trips as before; the
  `LIMIT FIND` lines in the PC log.
- **E-STOP.**
- **A long session**: return to a stored position afterwards and compare, for
  lost steps.
