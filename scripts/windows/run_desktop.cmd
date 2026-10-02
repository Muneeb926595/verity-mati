@echo off
rem Starts the Verity desktop app: it starts this folder's engine, shows the Verity UI in its own window and stops the
rem engine again when you close it. Builds the app first when it is not built yet (build_desktop.cmd).
setlocal
set "ROOT=%~dp0..\.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
set "EXE=%ROOT%\desktop\target\release\Verity.exe"
if not exist "%EXE%" if exist "%ROOT%\desktop\target\debug\Verity.exe" set "EXE=%ROOT%\desktop\target\debug\Verity.exe"
if exist "%EXE%" goto run
echo The Verity desktop app has not been built yet. Building it now (the first build takes several minutes)...
call "%~dp0lib\preflight.cmd"
if "%ERRORLEVEL%"=="3" goto blocked
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_desktop.ps1"
if errorlevel 1 goto failed
:run
set "VERITY_ROOT=%ROOT%"
echo Starting %EXE%
start "" "%EXE%"
exit /b 0
:failed
echo The build failed; the log is in %ROOT%\logs (desktop-build-*.log).
pause
exit /b 1
:blocked
pause
exit /b 3
