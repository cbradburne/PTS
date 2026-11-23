@echo off
REM build_p4.bat - the P4 display through tools\build.bat, the way every other
REM board is built:   tools\build.bat flash p4display COM5
REM Kept so the command first given for it still works.  set CHIP=prev3 first
REM for a P4 older than v3.00.
if /i "%~1"=="flash" (
    call "%~dp0..\..\tools\build.bat" flash p4display %2
) else (
    call "%~dp0..\..\tools\build.bat" p4display
)
