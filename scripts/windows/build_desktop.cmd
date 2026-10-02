@echo off
rem Builds the Verity desktop app for Windows: desktop\target\release\Verity.exe (see build_desktop.ps1).
rem Examples:  build_desktop.cmd    build_desktop.cmd -Shortcut    build_desktop.cmd -DebugBuild
setlocal
call "%~dp0lib\preflight.cmd"
if "%ERRORLEVEL%"=="3" goto blocked
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_desktop.ps1" %*
set "RC=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %RC%
:blocked
if "%~1"=="" pause
exit /b 3
