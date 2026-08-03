@echo off
REM build.bat - compile and flash firmware with arduino-cli on Windows.
REM
REM   tools\build.bat hubeth              compile one target
REM   tools\build.bat flash hubeth COM13  compile + upload
REM
REM The Windows counterpart of build.sh, which is sh-only and will not run in
REM cmd.  The FQBNs below are copied from build.sh and MUST stay identical: a
REM board setting that differs between the two produces a different binary from
REM the same commit, which is exactly the kind of difference that is invisible
REM until something crashes and the ELF no longer matches.
REM
REM Every successful compile copies the .elf and .bin into
REM .build\keep\<target>-<commit>\ .  A crash backtrace can only be read against
REM the ELF of the build that was actually running, and the next compile
REM overwrites .build\<target>\ - so the kept copy, keyed by the commit it came
REM from, is the one that survives.
REM
REM NOT SUPPORTED HERE: naming a satellite (SAT_NAME).  Passing a quoted string
REM through cmd, arduino-cli and gcc needs escaping that differs from sh, and
REM shipping an untested version of it would silently bake the quote characters
REM into the SSID.  Use tools\build.sh on macOS, or uncomment the SAT_NAME define
REM in esp32_satellite.ino.

setlocal enabledelayedexpansion

REM Resolved to a full path, and arduino-cli is given an ABSOLUTE --build-path,
REM exactly as build.sh does.  A relative one is resolved against the working
REM directory - a difference from the sh script that buys nothing and is one
REM more thing to suspect when a link fails.
for %%i in ("%~dp0..") do set "REPO=%%~fi"
pushd "%REPO%" || (echo cannot find repo root & exit /b 1)
set "OUT=%REPO%\.build"

set "DO_FLASH="
set "TARGET=%~1"
set "PORT="
if /i "%~1"=="flash" (
    set "DO_FLASH=1"
    set "TARGET=%~2"
    set "PORT=%~3"
)

if "%TARGET%"=="" (
    echo usage: tools\build.bat ^<target^>
    echo        tools\build.bat flash ^<target^> ^<port^>
    echo.
    echo targets: hub hubeth display amoled sat teensy
    goto :fail
)

call :resolve "%TARGET%"
if "%FQBN%"=="" (
    echo unknown target: %TARGET%
    goto :fail
)

echo -- %TARGET%  %FQBN%
arduino-cli compile --fqbn %FQBN% --libraries "%REPO%\libraries" --build-path "%OUT%\%TARGET%" --warnings default "%REPO%\%SKETCH%"
if errorlevel 1 (
    echo.
    echo COMPILE FAILED: %TARGET%
    echo.
    echo If that was "undefined reference to app_main" or "to millis", the core
    echo archive is missing from the link - a stale build cache, not your sketch.
    echo Clear both caches and run this again:
    echo.
    echo     arduino-cli cache clean
    echo     rmdir /s /q "%OUT%\%TARGET%"
    goto :fail
)

REM ---- preserve the ELF, keyed by the commit it was built from --------------
REM Keyed by commit rather than a timestamp because this machine tracks GitHub:
REM the commit is what identifies which source produced the binary, and it is
REM the thing worth quoting when reporting a crash.  A dirty tree reuses the
REM parent commit's name, so commit before flashing if you have edited anything.
set "SHA=nogit"
for /f "delims=" %%i in ('git rev-parse --short HEAD 2^>nul') do set "SHA=%%i"
set "KEEP=%OUT%\keep\%TARGET%-%SHA%"
if not exist "%KEEP%" mkdir "%KEEP%"
copy /y "%OUT%\%TARGET%\%SKETCHNAME%.elf" "%KEEP%\" >nul 2>&1
copy /y "%OUT%\%TARGET%\%SKETCHNAME%.bin" "%KEEP%\" >nul 2>&1
echo    kept ELF + bin in %KEEP%

if not defined DO_FLASH goto :done

if "%PORT%"=="" (
    echo.
    echo no port given.  Attached boards:
    arduino-cli board list
    echo.
    echo     tools\build.bat flash %TARGET% COM13
    goto :fail
)

echo -- uploading to %PORT%
arduino-cli upload --fqbn %FQBN% --input-dir "%OUT%\%TARGET%" -p %PORT% "%REPO%\%SKETCH%"
if errorlevel 1 (
    echo UPLOAD FAILED - is the PC app or a serial monitor holding %PORT%?
    goto :fail
)

echo.
echo Flashed.  To watch it boot:
echo     arduino-cli monitor -p %PORT% -c baudrate=921600

:done
popd
endlocal
exit /b 0

:fail
popd
endlocal
exit /b 1

REM ---------------------------------------------------------------------------
:resolve
set "FQBN="
set "SKETCH="
set "SKETCHNAME="
if /i "%~1"=="hub" (
    set "FQBN=esp32:esp32:XIAO_ESP32S3:USBMode=default,CDCOnBoot=default,PartitionScheme=default_8MB,FlashSize=8M"
    set "SKETCH=firmware\esp32_hub"
    set "SKETCHNAME=esp32_hub.ino"
)
if /i "%~1"=="hubeth" (
    set "FQBN=esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc,FlashSize=16M,PartitionScheme=default_8MB,PSRAM=disabled"
    set "SKETCH=firmware\esp32_hub_eth"
    set "SKETCHNAME=esp32_hub_eth.ino"
)
if /i "%~1"=="display" (
    set "FQBN=esp32:esp32:esp32s3:CDCOnBoot=cdc,FlashSize=16M,PartitionScheme=default_8MB,PSRAM=opi"
    set "SKETCH=firmware\esp32_display"
    set "SKETCHNAME=esp32_display.ino"
)
if /i "%~1"=="amoled" (
    set "FQBN=esp32:esp32:esp32s3:CDCOnBoot=cdc,FlashSize=16M,PartitionScheme=default_8MB,PSRAM=opi"
    set "SKETCH=firmware\esp_mount_amoled175"
    set "SKETCHNAME=esp_mount_amoled175.ino"
)
if /i "%~1"=="sat" (
    set "FQBN=esp32:esp32:esp32s3:CDCOnBoot=cdc,FlashSize=16M,PartitionScheme=default_8MB,PSRAM=disabled"
    set "SKETCH=firmware\esp32_satellite"
    set "SKETCHNAME=esp32_satellite.ino"
)
if /i "%~1"=="teensy" (
    set "FQBN=teensy:avr:teensy41:usb=serial,speed=600,opt=o2std"
    set "SKETCH=firmware\teensy41_mount"
    set "SKETCHNAME=teensy41_mount.ino"
)
exit /b 0
