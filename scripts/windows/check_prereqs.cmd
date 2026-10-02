@echo off
rem Lists everything Verity needs on this PC, without installing or changing anything: what is missing, what
rem setup.cmd installs by itself, what is optional, and what is NOT needed (Ollama, Docker, Python, Visual Studio...).
rem Examples:  check_prereqs.cmd    check_prereqs.cmd -Model C1    check_prereqs.cmd -Json
setlocal
call "%~dp0lib\preflight.cmd"
if "%ERRORLEVEL%"=="3" goto blocked
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0check_prereqs.ps1" %*
set "RC=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %RC%
:blocked
if "%~1"=="" pause
exit /b 3
