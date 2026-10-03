# Builds the Verity desktop app for Windows (Tauri 2):  desktop\target\release\Verity.exe
# The app starts this folder's engine (.venv) and shows the Verity UI in its own window, with a native folder picker.
#   scripts\windows\build_desktop.cmd              release build (the first one downloads Rust crates and the Tauri CLI)
#   scripts\windows\build_desktop.cmd -DebugBuild  quicker debug build (desktop\target\debug\Verity.exe, with a console)
#   scripts\windows\build_desktop.cmd -Shortcut    also put a "Verity" shortcut on the Desktop and in the Start menu
# Needs: Rust (rustup, MSVC toolchain), the Visual Studio C++ Build Tools (the linker), WebView2 (part of Windows 11),
# and Node.js (setup's tools\node, or one on PATH). Re-running only rebuilds what changed. Log: logs\desktop-build-*.log
# Windows PowerShell 5.1 compatible. ASCII only.
param(
    [switch]$DebugBuild,
    [switch]$Shortcut,
    [switch]$BuildUI,
    [string]$Root = ''
)
$ErrorActionPreference = 'Stop'
if (-not $Root) { $Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path }
. (Join-Path $PSScriptRoot 'lib\common.ps1')
[void](Initialize-Log -Root $Root -Name 'desktop-build')
$Tools = Join-Path $Root 'tools'
$profileName = if ($DebugBuild) { 'debug' } else { 'release' }
$total = 5

function Stop-WithHelp {
    param([string]$Message, [string[]]$Help = @())
    Write-VrLog FAIL $Message
    foreach ($h in $Help) { Write-VrLog INFO ("  " + $h) }
    exit 1
}

try {
    # ---------------------------------------------------------------------------------------------- 1. prerequisites
    Start-Step 1 $total 'Prerequisites'
    if (-not (Test-Path -LiteralPath (Join-Path $Root '.venv\Scripts\python.exe'))) {
        Stop-WithHelp 'Verity is not set up in this folder yet (no .venv).' @('Run scripts\windows\setup.cmd first.')
    }
    # Rust: a project-local toolchain (tools\cargo, tools\rustup) wins, else the user's rustup
    $localCargo = Join-Path $Tools 'cargo\bin\cargo.exe'
    if (Test-Path -LiteralPath $localCargo) {
        $env:CARGO_HOME = Join-Path $Tools 'cargo'
        $env:RUSTUP_HOME = Join-Path $Tools 'rustup'
        $env:PATH = (Join-Path $Tools 'cargo\bin') + ';' + $env:PATH
    } elseif (-not (Get-Command cargo.exe -ErrorAction SilentlyContinue)) {
        $userCargo = Join-Path $env:USERPROFILE '.cargo\bin'
        if (Test-Path -LiteralPath (Join-Path $userCargo 'cargo.exe')) { $env:PATH = $userCargo + ';' + $env:PATH }
    }
    $cargo = Get-Command cargo.exe -ErrorAction SilentlyContinue
    if (-not $cargo) {
        Stop-WithHelp 'Rust was not found (cargo.exe).' @(
            'Install it with rustup from https://rustup.rs (choose the default MSVC toolchain; no administrator rights needed),',
            'then run this script again.')
    }
    Write-VrLog OK ("Rust: {0}" -f (Get-NativeOutput -FilePath 'rustc.exe' -ArgumentList @('--version')).Trim())
    # the MSVC linker comes with the Visual Studio C++ Build Tools
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    $vc = ''
    if (Test-Path -LiteralPath $vswhere) {
        $vc = (Get-NativeOutput -FilePath $vswhere -ArgumentList @('-latest', '-products', '*', '-requires',
            'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-property', 'installationPath')).Trim()
    }
    if (-not $vc) {
        Stop-WithHelp 'The Visual Studio C++ Build Tools were not found (Rust needs their linker on Windows).' @(
            'Install "Build Tools for Visual Studio" with the "Desktop development with C++" workload.',
            'On a managed PC this usually needs your IT team.')
    }
    Write-VrLog OK ("C++ Build Tools: {0}" -f $vc)
    # WebView2 shows the app's window content; Windows 11 includes it
    $wv = $null
    foreach ($k in @('HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
                     'HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
                     'HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}')) {
        try { $wv = (Get-ItemProperty -LiteralPath $k -ErrorAction Stop).pv; if ($wv) { break } } catch { }
    }
    if ($wv) { Write-VrLog OK ("WebView2 runtime: {0}" -f $wv) }
    else { Write-VrLog WARN 'WebView2 runtime not found; the app needs it to show its window (Windows 11 includes it).' }
    # Node.js for the Tauri CLI: setup's portable copy, else one on PATH
    $nodeDir = Join-Path $Tools 'node'
    if (Test-Path -LiteralPath (Join-Path $nodeDir 'node.exe')) { $env:PATH = $nodeDir + ';' + $env:PATH }
    $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npm) { Stop-WithHelp 'Node.js was not found.' @('Run scripts\windows\setup.cmd (it installs a portable Node.js into tools\node).') }
    Write-VrLog OK ("Node.js: {0}" -f (Get-NativeOutput -FilePath 'node.exe' -ArgumentList @('--version')).Trim())
    Complete-Step

    # ---------------------------------------------------------------------------------------------- 2. user interface
    Start-Step 2 $total 'User interface (ui\dist)'
    $env:npm_config_cache = Join-Path $Tools 'npm-cache'
    $env:npm_config_update_notifier = 'false'
    $env:npm_config_fund = 'false'
    $env:npm_config_audit = 'false'
    $env:CI = 'true'                                   # no interactive prompts from the Tauri CLI
    $ui = Join-Path $Root 'ui'
    if ($BuildUI -or (Test-UiStale -Ui $ui)) {
        [void](Invoke-Native -FilePath $npm.Source -ArgumentList @('ci', '--prefer-offline', '--no-audit', '--no-fund') -WorkingDirectory $ui)
        [void](Invoke-Native -FilePath $npm.Source -ArgumentList @('run', 'build') -WorkingDirectory $ui)
        Complete-Step 'built'
    } else {
        Complete-Step 'already built and up to date (the app shows the UI the engine serves from ui\dist; -BuildUI rebuilds it)'
    }

    # ---------------------------------------------------------------------------------------------- 3. Tauri CLI
    Start-Step 3 $total 'Tauri CLI'
    $desktop = Join-Path $Root 'desktop'
    if (-not (Test-Path -LiteralPath (Join-Path $desktop 'node_modules\.bin\tauri.cmd'))) {
        [void](Invoke-Native -FilePath $npm.Source -ArgumentList @('ci', '--prefer-offline', '--no-audit', '--no-fund') -WorkingDirectory $desktop)
        Complete-Step 'installed'
    } else {
        Complete-Step 'already installed'
    }

    # ---------------------------------------------------------------------------------------------- 4. build
    Start-Step 4 $total ("Build the app ({0}; the first build compiles about 400 Rust crates, several minutes)" -f $profileName)
    $env:CARGO_TARGET_DIR = Join-Path $desktop 'target'
    $tauriArgs = @('build', '--no-bundle')            # a plain Verity.exe inside this folder, where it finds the engine
    if ($DebugBuild) { $tauriArgs += '--debug' }
    [void](Invoke-Native -FilePath (Join-Path $desktop 'node_modules\.bin\tauri.cmd') -ArgumentList $tauriArgs -WorkingDirectory $desktop)
    $exe = Join-Path $desktop ("target\{0}\Verity.exe" -f $profileName)
    if (-not (Test-Path -LiteralPath $exe)) { throw ("the build finished but {0} is missing" -f $exe) }
    Complete-Step ("built: {0} ({1})" -f $exe, (Format-Size (Get-Item -LiteralPath $exe).Length))

    # ---------------------------------------------------------------------------------------------- 5. shortcut
    Start-Step 5 $total 'Shortcut'
    if ($Shortcut) {
        $shell = New-Object -ComObject WScript.Shell
        $places = @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))
        foreach ($dir in $places) {
            $lnk = $shell.CreateShortcut((Join-Path $dir 'Verity.lnk'))
            $lnk.TargetPath = $exe
            $lnk.WorkingDirectory = $Root
            $lnk.IconLocation = "$exe,0"
            $lnk.Description = 'Verity: understand your C/C# codebase'
            $lnk.Save()
            Write-VrLog OK ("shortcut: {0}" -f (Join-Path $dir 'Verity.lnk'))
        }
        Complete-Step
    } else {
        Complete-Step 'skipped (add -Shortcut for a Desktop and Start menu shortcut)'
    }
    Write-VrLog OK ("Start it with:  scripts\windows\run_desktop.cmd   (or double-click {0})" -f $exe)
    exit 0
} catch {
    Write-VrLog FAIL ("{0}" -f $_.Exception.Message)
    Write-VrLog INFO ("Full log: {0}" -f $script:LogFile)
    exit 1
}
