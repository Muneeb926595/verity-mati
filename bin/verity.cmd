@echo off
rem Verity launcher for Windows: runs the CLI from this folder's own Python environment
rem (created by scripts\windows\setup.cmd). Usage: bin\verity.cmd ask <folder> "question"
setlocal
set "HERE=%~dp0.."
set "PY=%HERE%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Verity is not set up on this machine yet. Run:  scripts\windows\setup.cmd
  exit /b 1
)
rem UTF-8 everywhere: the engine reads and writes UTF-8 files and prints symbols such as a check mark
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
chcp 65001 >nul
"%PY%" -m verity %*
exit /b %ERRORLEVEL%
