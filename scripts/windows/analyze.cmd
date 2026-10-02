@echo off
rem Verity system analysis for Windows: hardware, recommended llama.cpp build and AI model, optional stress test.
rem Examples:  analyze.cmd    analyze.cmd -StressTest -Apply    analyze.cmd -QualityCheck
setlocal
call "%~dp0lib\preflight.cmd"
if "%ERRORLEVEL%"=="3" goto blocked
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0analyze_system.ps1" %*
set "RC=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %RC%
:blocked
if "%~1"=="" pause
exit /b 3
