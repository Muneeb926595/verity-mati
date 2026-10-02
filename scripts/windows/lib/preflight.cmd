@echo off
rem Called by setup.cmd, analyze.cmd and check_prereqs.cmd before they start PowerShell.
rem 1. Removes the "downloaded from the internet" mark from the scripts in scripts\windows (a RemoteSigned policy
rem    would refuse them otherwise).
rem 2. Exits with code 3 and a clear message when Group Policy only allows signed scripts (AllSigned or Restricted).
rem    The launchers' "-ExecutionPolicy Bypass" cannot override a Group Policy setting, and Verity does not try to.
setlocal
set "VRDIR=%~dp0.."
powershell.exe -NoProfile -Command "Get-ChildItem -LiteralPath $env:VRDIR -Recurse -Filter *.ps1 -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue; $g = @('MachinePolicy','UserPolicy') | ForEach-Object { [string](Get-ExecutionPolicy -Scope $_) } | Where-Object { $_ -eq 'AllSigned' -or $_ -eq 'Restricted' } | Select-Object -First 1; if ($g) { Write-Host ''; Write-Host ('MISSING  Group Policy sets the PowerShell script policy to ' + $g + ', and Verity''s scripts are not signed, so they cannot run on this PC.') -ForegroundColor Red; Write-Host '         Ask your IT team to allow the scripts in the Verity folder (scripts\windows). Do not try to work around the policy.'; exit 3 }"
exit /b %ERRORLEVEL%
