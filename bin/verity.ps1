# Verity launcher for Windows PowerShell: runs the CLI from this folder's own Python environment
# (created by scripts\windows\setup.cmd). Usage: .\bin\verity.ps1 ask <folder> "question"
$here = Split-Path -Parent $PSScriptRoot
$py = Join-Path $here '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    Write-Host 'Verity is not set up on this machine yet. Run:  scripts\windows\setup.cmd'
    exit 1
}
# UTF-8 everywhere: the engine reads and writes UTF-8 files and prints symbols such as a check mark
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
& $py -m verity @args
exit $LASTEXITCODE
