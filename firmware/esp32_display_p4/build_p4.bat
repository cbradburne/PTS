@echo off
REM build_p4.bat - compile, and optionally flash, the P4 display firmware (Windows).
REM
REM   firmware\esp32_display_p4\build_p4.bat                 compile
REM   firmware\esp32_display_p4\build_p4.bat flash COM5      compile + upload
REM
REM Flash through the board's "UART" Type-C (the CH343P USB-serial chip).
REM For a chip that reports v3.00 or newer, first:   set CHIP=v3
REM The other options (DISPLAY_FLIP, TOUCH_MIRROR_XY, PAD_USB) are not passed
REM from here - cmd mangles compiler flags; change their defaults in board_p4.h
REM and esp32_display_p4.ino, or use build_p4.sh on the Mac.

setlocal enabledelayedexpansion
for %%i in ("%~dp0..\..") do set "REPO=%%~fi"
set "SKETCH=%REPO%\firmware\esp32_display_p4"
set "VARIANT=prev3"
if /i "%CHIP%"=="v3" set "VARIANT=postv3"
set "FQBN=esp32:esp32:esp32p4:ChipVariant=%VARIANT%,PSRAM=enabled,FlashSize=32M,PartitionScheme=app5M_fat24M_32MB,USBMode=hwcdc,CDCOnBoot=default"
set "OUT=%REPO%\.build\display-p4-%VARIANT%"

echo -- display-p4  %FQBN%
arduino-cli compile --fqbn %FQBN% --libraries "%REPO%\libraries" --build-path "%OUT%" --warnings default "%SKETCH%"
if errorlevel 1 (
    echo COMPILE FAILED
    exit /b 1
)
if /i not "%~1"=="flash" exit /b 0
if "%~2"=="" (
    echo no port given.  Attached boards:
    arduino-cli board list
    exit /b 1
)
arduino-cli upload --fqbn %FQBN% --input-dir "%OUT%" -p %~2 "%SKETCH%"
if errorlevel 1 (
    echo UPLOAD FAILED - is a serial monitor holding %~2?
    exit /b 1
)
echo Flashed.  To watch it boot:  arduino-cli monitor -p %~2 -c baudrate=115200
