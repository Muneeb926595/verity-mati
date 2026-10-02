@echo off
rem One-command Verity setup for Windows (no administrator rights needed).
rem Runs setup.ps1 with the execution policy relaxed for this one process only; nothing changes system-wide.
rem Options pass through, for example:  setup.cmd -StressTest -QualityCheck -Yes
setlocal
call "%~dp0lib\preflight.cmd"
if "%ERRORLEVEL%"=="3" goto blocked
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
set "RC=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %RC%
:blocked
if "%~1"=="" pause
exit /b 3
