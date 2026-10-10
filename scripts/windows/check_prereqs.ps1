<#
.SYNOPSIS
    Lists everything Verity needs on this Windows PC, without installing or changing anything.

.DESCRIPTION
    Five groups, each item marked OK, DONE, TODO, WARN, MISSING, -- (absent, optional) or no (not needed):
      1  This machine: Windows version, memory (and the model it can run), disk, internet, power
      2  What must be true before setup: PowerShell, script policy, a writable folder, complete files
      3  What setup.cmd installs on its own inside the Verity folder: already there, or still to do
      4  Optional extras and what they add
      5  What is NOT needed: Ollama, Docker/WSL, a system Python, Visual Studio, git, cloud accounts, admin rights
    Exit code 0: nothing blocks setup (or everything is already set up). 1: something to fix first.

.PARAMETER Model
    auto (default: chosen from the hardware, as setup.cmd does), direct, C1, C2 or C3.
.PARAMETER Backend
    auto (default) or a llama.cpp build: cpu-x64, cpu-arm64, vulkan-x64, cuda-12.4-x64, cuda-13.4-x64.
.PARAMETER Offline
    Do not test the internet connection.
.PARAMETER Json
    Print the result as JSON instead of the report.
.PARAMETER SpecsFile
    Testing: a JSON file describing a made-up machine (RamGB, Arch, Gpus, DiskFreeGB, OsBuild, ...) instead of this one.
.PARAMETER AllowNonWindows
    Testing: run under PowerShell 7 on macOS or Linux (Windows-only checks are skipped or simulated).

.EXAMPLE
    scripts\windows\check_prereqs.cmd
.EXAMPLE
    scripts\windows\check_prereqs.cmd -Model C1 -Json
#>
[CmdletBinding()]
param(
    [string]$Root = '',
    [ValidateSet('auto', 'direct', 'C1', 'C2', 'C3')] [string]$Model = 'auto',
    [ValidateSet('auto', 'cpu-x64', 'cpu-arm64', 'vulkan-x64', 'cuda-12.4-x64', 'cuda-13.4-x64')] [string]$Backend = 'auto',
    [switch]$Offline,
    [switch]$Json,
    [string]$SpecsFile = '',
    [switch]$AllowNonWindows
)

# AppLocker / Device Guard can force "constrained language", where these scripts cannot work: say so plainly first
$langMode = "$($ExecutionContext.SessionState.LanguageMode)"
if ($langMode -ne 'FullLanguage') {
    Write-Host ("MISSING  PowerShell runs in {0} mode here (an AppLocker or Device Guard policy), so Verity's setup scripts cannot run." -f $langMode) -ForegroundColor Red
    Write-Host '         -> Ask your IT team to allow the scripts in scripts\windows (or to run setup for you).'
    exit 1
}

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$OnWindows = ($env:OS -eq 'Windows_NT')
if (-not $OnWindows -and -not $AllowNonWindows) {
    Write-Host 'This checker is for Windows. On macOS or Linux run:  bash scripts/check_prereqs.sh'
    exit 2
}
if (-not $Root) { $Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path }
foreach ($lib in @('common', 'plan', 'detect')) {
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot "lib\$lib.ps1"))) {
        Write-Host ("MISSING  Incomplete copy: scripts\windows\lib\{0}.ps1 is not there. Copy or extract the whole Verity folder again." -f $lib) -ForegroundColor Red
        exit 1
    }
}
. (Join-Path $PSScriptRoot 'lib\common.ps1')
. (Join-Path $PSScriptRoot 'lib\plan.ps1')
. (Join-Path $PSScriptRoot 'lib\detect.ps1')

# ------------------------------------------------------------------------------------------------ results
$Items = New-Object System.Collections.Generic.List[object]
$Todo = New-Object System.Collections.Generic.List[object]
function Add-Item {
    param([string]$Id, [string]$Group, [string]$Status, [string]$Text, [string]$Fix = '')
    $Items.Add([pscustomobject]@{ id = $Id; group = $Group; status = $Status; summary = $Text; fix = $Fix }) | Out-Null
}
function Add-Todo {      # something setup still has to install: disk GB, download GB, the sites it downloads from
    param([string]$Id, [double]$DiskGB, [double]$DownloadGB, [string[]]$Hosts)
    $Todo.Add([pscustomobject]@{ Id = $Id; Disk = $DiskGB; Download = $DownloadGB; Hosts = $Hosts }) | Out-Null
}
function Test-Reachable {  # any HTTP answer (even an error page) means the site is reachable; only no answer is a failure
    param([string]$HostName, [int]$TimeoutSec = 8)
    try {
        [void](Invoke-WebRequest -Uri ("https://{0}/" -f $HostName) -Method Head -UseBasicParsing -TimeoutSec $TimeoutSec)
        return $true
    } catch {
        $resp = $null
        try { $resp = $_.Exception.Response } catch { }
        return ($null -ne $resp)
    }
}
function Test-ModelPresent {
    param($Model)
    $f = Join-Path $Root ("models\" + $Model.file)
    if (-not (Test-Path -LiteralPath $f) -or (Test-Path -LiteralPath "$f.part")) { return $false }
    return ((Get-Item -LiteralPath $f).Length -ge 0.95 * [double]$Model.bytes)
}
function Get-Gb { param([double]$Bytes) return [math]::Round($Bytes / 1e9, 1) }

# ------------------------------------------------------------------------------------------------ the machine
if ($SpecsFile) {
    $Specs = Get-Content -LiteralPath $SpecsFile -Raw | ConvertFrom-Json
} elseif ($OnWindows) {
    $Specs = Get-SystemSpecs -Root $Root -NoNetwork
} else {
    $Specs = [pscustomobject]@{ Os = 'simulated Windows 11 (not a Windows PC)'; Arch = 'x64'; RamGB = 16; RamFreeGB = 8; Gpus = @()
                                Cpu = [pscustomobject]@{ Name = 'Simulated CPU'; Cores = 8; Threads = 16; Avx2 = $true; Avx512 = $false }
                                Drive = 'C:'; DiskFreeGB = 200; DiskType = 'SSD'; OnBattery = $false; Admin = $false; LongPaths = $false }
}
$OsBuild = 22631
if ($Specs.OsBuild) { $OsBuild = [int]$Specs.OsBuild } elseif ($OnWindows) { $OsBuild = [Environment]::OSVersion.Version.Build }
$Catalog = Read-Catalog -Root $Root
$forceRung = ''
if ($Model -ne 'auto') { $forceRung = $Model }
$forceBuild = ''
if ($Backend -ne 'auto') { $forceBuild = $Backend }
$Plan = Get-SetupPlan -Specs $Specs -Catalog $Catalog -ForceRung $forceRung -ForceBuild $forceBuild
$Tools = Join-Path $Root 'tools'

# ------------------------------------------------------------------------------------------------ 2. before setup
$psv = $PSVersionTable.PSVersion
if ($psv.Major -lt 5 -or ($psv.Major -eq 5 -and $psv.Minor -lt 1)) {
    Add-Item 'powershell' 'required' 'missing' ("PowerShell {0}: setup needs 5.1 or newer" -f $psv) 'Install Windows Management Framework 5.1 (Windows 10 and 11 already have it)'
} else {
    if ($psv.Major -eq 5) { Add-Item 'powershell' 'required' 'ok' ("Windows PowerShell {0} (built into Windows; nothing to install)" -f $psv) }
    else { Add-Item 'powershell' 'required' 'ok' ("PowerShell {0}; setup.cmd uses the Windows PowerShell 5.1 that comes with Windows" -f $psv) }
}

$gpo = @()
foreach ($scope in @('MachinePolicy', 'UserPolicy')) {
    try { $v = "$(Get-ExecutionPolicy -Scope $scope)"; if ($v -and $v -ne 'Undefined') { $gpo += ("{0} {1}" -f $scope, $v) } } catch { }
}
$blocking = @($gpo | Where-Object { $_ -match ' (AllSigned|Restricted)$' })     # (not 'Unrestricted')
$motw = $false
if ($OnWindows) {
    try { $motw = [bool](Get-Item -LiteralPath (Join-Path $PSScriptRoot 'setup.ps1') -Stream Zone.Identifier -ErrorAction SilentlyContinue) } catch { }
}
if ($blocking.Count) {
    Add-Item 'execution-policy' 'required' 'missing' ("Group Policy sets the PowerShell script policy ({0}): Verity's scripts are not signed, so setup cannot run" -f ($blocking -join ', ')) `
        'Ask your IT team to allow the scripts in scripts\windows. Do not try to work around the policy.'
} else {
    $t = 'No Group Policy blocks scripts; the .cmd files run them for this one process only (nothing changes system-wide)'
    if ($gpo.Count) { $t = ("Group Policy sets {0}: fine, the .cmd files remove the downloaded mark first" -f ($gpo -join ', ')) }
    if ($motw) { $t += '. The copied files still carry the "downloaded from the internet" mark; the .cmd files remove it' }
    Add-Item 'execution-policy' 'required' 'ok' $t
}

$writable = $false
try {
    $probe = Join-Path $Root (".vr-write-test-{0}" -f [guid]::NewGuid().ToString('N'))
    [IO.File]::WriteAllText($probe, 'x')
    Remove-Item -LiteralPath $probe -Force
    $writable = $true
} catch { }
if (-not $writable) {
    Add-Item 'folder' 'required' 'missing' ("Cannot write to {0}: setup installs everything inside this folder" -f $Root) 'Move the Verity folder somewhere you can write, such as C:\verity (not under Program Files)'
} elseif ($Root -match '\\OneDrive( - [^\\]+)?\\') {
    Add-Item 'folder' 'required' 'warn' ("{0} is inside OneDrive: it would try to upload several GB of tools and models, and syncing can lock files" -f $Root) 'Move the folder to a local path such as C:\verity'
} elseif ($Root.Length -gt 80) {
    Add-Item 'folder' 'required' 'warn' ("The folder path is {0} characters long; deep files may hit the 260-character limit" -f $Root.Length) 'Move the folder to a short path such as C:\verity'
} else {
    Add-Item 'folder' 'required' 'ok' ("{0}: writable, short path, not synced" -f $Root)
}

$absent = @()
foreach ($f in @('verity\__init__.py', 'verity\__main__.py', 'bin\verity.cmd', 'requirements.lock', 'scripts\windows\setup.ps1', 'scripts\windows\setup.cmd',
                 'scripts\windows\catalog.json', 'scripts\windows\lib\install.ps1', 'workers\roslyn\VerityRoslyn.csproj')) {
    if (-not (Test-Path -LiteralPath (Join-Path $Root $f))) { $absent += $f }
}
if ($absent.Count) {
    Add-Item 'setup-files' 'required' 'missing' ("Incomplete copy; missing: {0}" -f ($absent -join ', ')) 'Copy or extract the whole Verity folder again'
} else {
    Add-Item 'setup-files' 'required' 'ok' "Verity's code and setup scripts are complete"
}

$curl = Get-CurlInfo
$msi = Get-Command msiexec.exe -ErrorAction SilentlyContinue
if (-not $OnWindows) { $curl = $true; $msi = $true }       # simulation on macOS/Linux
if ($curl -and $msi) {
    Add-Item 'shell-tools' 'required' 'ok' 'curl.exe and msiexec.exe (both part of Windows): resumable downloads, SHA256-checked'
} else {
    $miss = @()
    if (-not $curl) { $miss += 'curl.exe (downloads fall back to BITS: slower, no live progress)' }
    if (-not $msi) { $miss += 'msiexec.exe (cppcheck will be skipped)' }
    Add-Item 'shell-tools' 'required' 'warn' ("Not found: {0}. Setup still works" -f ($miss -join '; '))
}

# ------------------------------------------------------------------------------------------------ 3. installed by setup
$uv = Join-Path $Tools 'uv\uv.exe'
if ((Test-Path -LiteralPath $uv) -and ((Get-NativeOutput $uv @('--version')) -match [regex]::Escape($Catalog.uv.version))) {
    Add-Item 'uv' 'auto' 'done' ("uv {0} in tools\uv (installs Python)" -f $Catalog.uv.version)
} else {
    Add-Todo 'uv' 0.05 0.02 @('github.com')
    Add-Item 'uv' 'auto' 'todo' ("Setup downloads uv {0} (SHA256-checked) into tools\uv; uv then installs Python 3.12" -f $Catalog.uv.version)
}

$py = Join-Path $Root '.venv\Scripts\python.exe'
$venvOk = $false
if (Test-Path -LiteralPath $py) {
    # Use a temp script file, not `python -c '...'`: PowerShell 5.1 strips the double quotes around "vr-ok" when
    # passing the argument to a native exe, so `python -c` sees print(vr-ok) (a NameError, no output) and this check
    # would always report the venv as missing. A script file passes through untouched.
    $checkFile = Join-Path $Root '.vr-prereq-check.py'
    $checkCode = "import clang.cindex as c`r`nc.Index.create()`r`nimport numpy, mcp`r`nprint('vr-ok')"
    [System.IO.File]::WriteAllText($checkFile, $checkCode, (New-Object System.Text.UTF8Encoding($false)))
    try { $venvOk = (Get-NativeOutput $py @($checkFile)) -match 'vr-ok' }
    finally { Remove-Item -LiteralPath $checkFile -Force -ErrorAction SilentlyContinue }
}
if ($venvOk) {
    Add-Item 'python-env' 'auto' 'done' 'Python environment (.venv) with the pinned packages; libclang (the C parser) loads'
} else {
    Add-Todo 'python-env' 0.8 0.2 @('github.com', 'pypi.org', 'files.pythonhosted.org')
    $t = 'Setup installs Python 3.12 into tools\python and creates .venv with the pinned packages (about 0.5 GB). libclang comes as a Python package: no LLVM or Visual Studio needed'
    if ($Specs.Arch -eq 'arm64') { $t += '. On ARM, Python runs as x64 under emulation' }
    Add-Item 'python-env' 'auto' 'todo' $t
}

$ldir = Join-Path $Tools 'llama.cpp'
$lmark = Join-Path $ldir '.verity-build.json'
$bk = $Plan.Backend
$fallbacks = @($Plan.Backends | Where-Object { $_.Build -ne $bk.Build } | ForEach-Object { $_.Build } | Select-Object -Unique)
if ((Test-Path -LiteralPath (Join-Path $ldir 'llama-server.exe')) -and (Test-Path -LiteralPath $lmark)) {
    $m = Get-Content -LiteralPath $lmark -Raw | ConvertFrom-Json
    $t = "llama.cpp {0}, {1} build, in tools\llama.cpp (runs the local models)" -f $m.tag, $m.build
    if ($m.build -ne $bk.Build) { $t += (". The preferred build here is {0}: analyze.cmd -StressTest -Apply compares them" -f $bk.Build) }
    Add-Item 'llama.cpp' 'auto' 'done' $t
} else {
    $spec = $Catalog.llama_cpp.builds.($bk.Build)
    $mb = 0.0
    if ($spec) { $mb = [double]$spec.mb; if ($spec.runtime) { $mb += [double]$spec.runtime.mb } }
    Add-Todo 'llama.cpp' ($mb * 2 / 1000) ($mb / 1000) @('github.com')
    $t = "Setup downloads llama.cpp {0}, the {1} build for {2} (about {3:N0} MB)" -f $Catalog.llama_cpp.tag, $bk.Build, $bk.Device, $mb
    if ($fallbacks.Count) { $t += ("; if it does not start, it tries {0}" -f ($fallbacks -join ', ')) }
    Add-Item 'llama.cpp' 'auto' 'todo' ($t + '. This replaces Ollama')
}

$dotnet = Join-Path $Tools 'dotnet\dotnet.exe'
$sdk = ''
if (Test-Path -LiteralPath $dotnet) { $sdk = ([regex]::Match((Get-NativeOutput $dotnet @('--list-sdks')), '(?m)^(10\.[\d.]+)')).Groups[1].Value }
if ($sdk) {
    Add-Item 'dotnet' 'auto' 'done' (".NET SDK {0} in tools\dotnet (runs the C# analyser; a system-wide .NET is not used)" -f $sdk)
} else {
    Add-Todo 'dotnet' 0.9 0.3 @('dot.net', 'builds.dotnet.microsoft.com')
    Add-Item 'dotnet' 'auto' 'todo' "Setup installs .NET SDK 10 (about 0.7 GB) into tools\dotnet with Microsoft's signed dotnet-install script. No admin"
}

$dll = Join-Path $Root 'workers\roslyn\bin\VerityRoslyn.dll'
if (Test-Path -LiteralPath $dll) {
    $wv = ''
    if ($sdk) { $wv = (Get-NativeOutput $dotnet @($dll, '--version')).Trim() }
    if ($wv) { Add-Item 'csharp-worker' 'auto' 'done' ("C# analyser worker built and running: {0}" -f $wv) }
    else { Add-Item 'csharp-worker' 'auto' 'done' 'C# analyser worker is prebuilt (workers\roslyn\bin); it runs once .NET is installed' }
} else {
    Add-Todo 'csharp-worker' 0.2 0.1 @('api.nuget.org')
    Add-Item 'csharp-worker' 'auto' 'todo' 'Setup builds the C# analyser worker with .NET (downloads its packages from nuget.org once)'
}

$cpp = Join-Path $Tools 'cppcheck\cppcheck.exe'
if (Test-Path -LiteralPath $cpp) {
    Add-Item 'cppcheck' 'auto' 'done' ("{0} in tools\cppcheck (optional second opinion on C code)" -f (Get-NativeOutput $cpp @('--version')).Trim())
} elseif ($msi) {
    Add-Todo 'cppcheck' 0.15 0.03 @('github.com')
    Add-Item 'cppcheck' 'auto' 'todo' ("Setup unpacks cppcheck {0} into tools\cppcheck (nothing installed system-wide); an optional second opinion on C code" -f $Catalog.cppcheck.version)
} else {
    Add-Item 'cppcheck' 'auto' 'warn' 'Setup will skip cppcheck (msiexec.exe not found). Verity works without it'
}

if (Test-Path -LiteralPath (Join-Path $Root 'ui\dist\index.html')) {
    Add-Item 'ui' 'auto' 'done' 'User interface is prebuilt (ui\dist): no Node.js needed'
} else {
    Add-Todo 'ui' 0.45 0.15 @('nodejs.org', 'registry.npmjs.org')
    Add-Item 'ui' 'auto' 'todo' 'Setup downloads a portable Node.js 22 into tools\node and builds the user interface'
}

$want = @()
if ($Plan.Embed) { $want += $Plan.Embed }
if ($Plan.Pick) { $want += $Plan.Pick.Model }
$haveM = @(); $getM = @(); $getBytes = 0.0
foreach ($m in $want) {
    if (Test-ModelPresent $m) { $haveM += $m.label } else { $getM += ("{0} {1:N1} GB" -f $m.label, (Get-Gb $m.bytes)); $getBytes += [double]$m.bytes }
}
$use = 'no chat model (Direct mode: answers built from checked facts)'
if ($Plan.Pick -and $Plan.Pick.Placement.Where) { $use = "{0}: {1}" -f $Plan.Pick.Label, $Plan.Pick.Placement.Where }
elseif ($Plan.Pick) { $use = "{0}, which does not fit this machine" -f $Plan.Pick.Label }
$fitWarn = @($Plan.Warnings | Where-Object { $_ -like '*was requested but*' })
if ($getM.Count -eq 0) {
    $t = "Models ready{0}. Verity will answer with {1}" -f $(if ($haveM.Count) { ': ' + ($haveM -join ', ') } else { '' }), $use
    if ($fitWarn.Count) { Add-Item 'models' 'auto' 'warn' ($t + '. ' + $fitWarn[0]) 'Use -Model auto (the models this machine can run)' }
    else { Add-Item 'models' 'auto' 'done' $t }
} else {
    $dlGb = Get-Gb $getBytes
    Add-Todo 'models' $dlGb $dlGb @('huggingface.co')
    $t = "Setup downloads {0:N1} GB (-Model {1}): {2}. Resumable and SHA256-checked. Verity will answer with {3}" -f $dlGb, $Model, ($getM -join ', '), $use
    if ($fitWarn.Count) { Add-Item 'models' 'auto' 'warn' ($t + '. ' + $fitWarn[0]) 'Use -Model auto (the models this machine can run)' }
    else { Add-Item 'models' 'auto' 'todo' $t }
}

# ------------------------------------------------------------------------------------------------ 1. this machine
$arch = "$($Specs.Arch)"
if ($arch -eq 'x86') {
    Add-Item 'platform' 'machine' 'missing' '32-bit Windows is not supported' 'Verity needs 64-bit Windows 10 (version 1809 or newer) or Windows 11'
} elseif ($OsBuild -lt 17763) {
    Add-Item 'platform' 'machine' 'missing' ("{0}: Verity needs Windows 10 version 1809 (build 17763) or newer" -f $Specs.Os) 'Update Windows'
} elseif ($arch -eq 'arm64' -and $OsBuild -lt 22000) {
    Add-Item 'platform' 'machine' 'missing' ("{0} on ARM: Windows 11 is needed (Python runs as x64 under its emulation)" -f $Specs.Os) 'Update to Windows 11'
} elseif ($arch -eq 'arm64') {
    Add-Item 'platform' 'machine' 'warn' ("{0} on ARM: should work, not tested yet. Python runs as x64 under emulation (slower indexing); llama.cpp runs natively" -f $Specs.Os)
} else {
    Add-Item 'platform' 'machine' 'ok' ("{0}, 64-bit, {1}" -f $Specs.Os, $Specs.Cpu.Name)
}

$c2 = $Plan.Models | Where-Object { $_.Rung -eq 'C2' } | Select-Object -First 1
$c3 = $Plan.Models | Where-Object { $_.Rung -eq 'C3' } | Select-Object -First 1
$fits = @('gpu', 'hybrid', 'cpu')
if ($c2 -and $fits -contains $c2.Placement.Status) {
    $t = "{0:N1} GB memory: enough for gpt-oss-20b, Verity's default model: {1}" -f $Specs.RamGB, $c2.Placement.Where
    if ($c3 -and @('gpu', 'hybrid') -contains $c3.Placement.Status) { $t += '. gpt-oss-120b fits too (-Model C3, 63 GB download, not yet benchmarked)' }
    Add-Item 'ram' 'machine' 'ok' $t
} elseif ($Plan.Pick) {
    Add-Item 'ram' 'machine' 'warn' ("{0:N1} GB memory: enough for {1}: {2}. gpt-oss-20b needs about 15 GB free for the model" -f $Specs.RamGB, $Plan.Pick.Label, $Plan.Pick.Placement.Where) `
        'Fine to use: setup picks the right model on its own. analyze.cmd -StressTest measures what really runs well'
} else {
    Add-Item 'ram' 'machine' 'warn' ("{0:N1} GB memory: too little for a local model; Verity answers in Direct mode (facts only)" -f $Specs.RamGB)
}

$need = 2.0
foreach ($x in $Todo) { $need += $x.Disk }
$need = [math]::Round($need, 1)
$free = [double]$Specs.DiskFreeGB
$dl = 0.0
foreach ($x in $Todo) { $dl += $x.Download }
$dl = [math]::Round($dl, 1)
$drive = "$($Specs.Drive)"
if ($Todo.Count -eq 0) {
    if ($free -ge 2) { Add-Item 'disk' 'machine' 'ok' ("{0:N1} GB free on {1}; nothing left to download (keep a few GB for project indexes)" -f $free, $drive) }
    else { Add-Item 'disk' 'machine' 'warn' ("{0:N1} GB free on {1}: low; indexing large projects needs a few GB" -f $free, $drive) 'Free some disk space' }
} elseif ($free -ge $need + 10) {
    Add-Item 'disk' 'machine' 'ok' ("{0:N1} GB free on {1}; setup needs about {2:N1} GB (including 2 GB for indexes)" -f $free, $drive, $need)
} elseif ($free -ge $need) {
    Add-Item 'disk' 'machine' 'warn' ("{0:N1} GB free on {1}; setup needs about {2:N1} GB: enough, but little to spare" -f $free, $drive, $need) 'Free more space, or use -Model C1 / -Model direct'
} else {
    Add-Item 'disk' 'machine' 'missing' ("{0:N1} GB free on {1}; setup needs about {2:N1} GB" -f $free, $drive, $need) 'Free disk space, or choose a smaller model: -Model C1 (2.5 GB) or -Model direct'
}

$hosts = @($Todo | ForEach-Object { $_.Hosts } | Sort-Object -Unique)
if ($Todo.Count -eq 0) {
    Add-Item 'internet' 'machine' 'ok' 'Not needed: everything is installed; Verity works offline'
} elseif ($Offline) {
    Add-Item 'internet' 'machine' 'warn' ("Not checked (-Offline). Setup still has to download about {0:N1} GB from: {1}" -f $dl, ($hosts -join ', '))
} else {
    Enable-ModernTls
    $bad = @($hosts | Where-Object { -not (Test-Reachable $_) })
    if ($bad.Count -eq 0) {
        Add-Item 'internet' 'machine' 'ok' ("Reachable: {0}. Setup downloads about {1:N1} GB; afterwards Verity works offline" -f ($hosts -join ', '), $dl)
    } else {
        Add-Item 'internet' 'machine' 'missing' ("Setup must download about {0:N1} GB, but these sites are unreachable: {1}" -f $dl, ($bad -join ', ')) `
            ("Connect to the internet, or ask IT to allow: {0}" -f ($hosts -join ', '))
    }
}

if ($Specs.OnBattery) { Add-Item 'power' 'machine' 'warn' 'Running on battery: laptops slow down on battery' 'Plug in before setup and before the stress test' }
else { Add-Item 'power' 'machine' 'ok' 'On mains power (or a desktop)' }

# ------------------------------------------------------------------------------------------------ 4. optional
$gpus = @($Specs.Gpus)
$oldNv = @($Plan.Warnings | Where-Object { $_ -like '*driver is too old*' })
if ($arch -eq 'arm64') {
    Add-Item 'gpu' 'optional' 'info' 'Windows on ARM: models run on the CPU (the ARM64 build)'
} elseif ($bk.Kind -eq 'gpu' -and $oldNv.Count) {
    Add-Item 'gpu' 'optional' 'warn' ("{0}: setup uses the Vulkan build because the driver is too old for CUDA" -f $bk.Device) 'Update the NVIDIA driver (CUDA 12.4 or newer) for the faster CUDA build, then run setup again'
} elseif ($bk.Kind -eq 'gpu') {
    Add-Item 'gpu' 'optional' 'ok' ("{0}: setup uses the {1} build ({2})" -f $bk.Device, $bk.Build, $bk.Why)
} elseif (@($gpus | Where-Object { $_.Integrated }).Count) {
    Add-Item 'gpu' 'optional' 'absent' 'Integrated graphics only: models run on the CPU (analyze.cmd -StressTest checks whether Vulkan on it helps)'
} else {
    Add-Item 'gpu' 'optional' 'absent' 'No supported GPU: models run on the CPU (slower answers; Direct mode is unaffected)'
}

$sdkHeaders = $false
if ($OnWindows) {
    $sdkHeaders = [bool]$env:INCLUDE -or (Test-Path -LiteralPath 'C:\Program Files (x86)\Windows Kits\10\Include')
}
if ($sdkHeaders) {
    Add-Item 'windows-sdk' 'optional' 'ok' 'Windows SDK headers found: C code written for MSVC is parsed against its real headers'
} else {
    Add-Item 'windows-sdk' 'optional' 'absent' 'No Windows SDK: C code is parsed with bundled standard headers (works; a little less precise for MSVC-specific code)' `
        'Optional, only for MSVC-heavy C code: the Windows SDK (or Visual Studio Build Tools) from Microsoft'
}

if ($Specs.LongPaths) { Add-Item 'long-paths' 'optional' 'ok' 'Long paths are enabled' }
else { Add-Item 'long-paths' 'optional' 'absent' 'Long paths are off: fine as long as the folder path is short, such as C:\verity' }

Add-Item 'sandbox' 'optional' 'info' "Not on Windows yet: Verity does not run project code here (no Windows sandbox adapter); questions about runtime behaviour are answered from the code"
Add-Item 'desktop-app' 'optional' 'info' 'The desktop app is built on this PC with scripts\windows\build_desktop.cmd (needs Rust and the Visual Studio C++ Build Tools); bin\verity.cmd ui shows the same pages in the browser without building anything'

# ------------------------------------------------------------------------------------------------ 5. not needed
$ollama = (Get-Command ollama.exe -ErrorAction SilentlyContinue) -or ($env:LOCALAPPDATA -and (Test-Path -LiteralPath (Join-Path $env:LOCALAPPDATA 'Programs\Ollama')))
if ($ollama) {
    Add-Item 'ollama' 'not_needed' 'info' "Not needed: Verity runs models with its own llama.cpp and model files. Ollama is installed here; it can stay, but don't run a big model in it while using Verity (memory)"
} else {
    Add-Item 'ollama' 'not_needed' 'info' 'Not needed: Verity runs models with its own llama.cpp (tools\) and model files (models\), not Ollama'
}
Add-Item 'docker' 'not_needed' 'info' 'Not needed: no Docker Desktop, WSL or Hyper-V'
Add-Item 'system-llvm' 'not_needed' 'info' 'Not needed: no LLVM or clang install; the C parser (libclang 18) comes with the Python packages'
Add-Item 'system-python' 'not_needed' 'info' 'Not needed: an installed Python. Setup gets its own Python 3.12 through uv (into tools\python); any other Python is ignored'
Add-Item 'visual-studio' 'not_needed' 'info' 'Not needed: Visual Studio or a C compiler. Every Python package installs ready-made'
$nodeTxt = 'Not needed: the user interface is prebuilt'
if (-not (Test-Path -LiteralPath (Join-Path $Root 'ui\dist\index.html'))) { $nodeTxt = 'Not needed to install yourself: setup downloads a portable Node.js to build the user interface' }
Add-Item 'node' 'not_needed' 'info' $nodeTxt
Add-Item 'git' 'not_needed' 'info' 'Not needed: setup downloads pinned releases and Verity never calls git'
Add-Item 'cloud' 'not_needed' 'info' 'Not needed: accounts, API keys or cloud services. After setup Verity works fully offline and sends nothing anywhere'
$adminTxt = "Not needed: administrator rights. Everything installs inside {0}; deleting the folder removes it all" -f $Root
if ($Specs.Admin) { $adminTxt += ' (this window runs as administrator; a normal window is enough)' }
Add-Item 'admin' 'not_needed' 'info' $adminTxt

# ------------------------------------------------------------------------------------------------ verdict
$nMissing = @($Items | Where-Object { $_.status -eq 'missing' }).Count
$nTodo = @($Items | Where-Object { $_.status -eq 'todo' }).Count
$nWarn = @($Items | Where-Object { $_.status -eq 'warn' }).Count
$mflag = ''
if ($Model -ne 'auto') { $mflag = " -Model $Model" }
if ($nMissing -gt 0) {
    $state = 'blocked'
    $verdict = "NOT READY: $nMissing thing(s) to fix first (marked MISSING)."
    $next = 'Fix what is marked MISSING, then run scripts\windows\check_prereqs.cmd again'
} elseif ($Todo.Count -gt 0) {
    $state = 'ready'
    $verdict = "READY FOR SETUP: setup installs the item(s) marked TODO by itself (about {0:N1} GB download, {1:N1} GB disk)." -f $dl, $need
    $next = "scripts\windows\setup.cmd$mflag"
} else {
    $state = 'installed'
    $verdict = 'ALREADY SET UP: everything Verity needs is installed.'
    $next = 'bin\verity.cmd probe   then   bin\verity.cmd ask samples\navgps-c "Explain how GPS data travels through this application."'
}
$order = @('machine', 'required', 'auto', 'optional', 'not_needed')
$sorted = @(foreach ($g in $order) { $Items | Where-Object { $_.group -eq $g } })

# ------------------------------------------------------------------------------------------------ output
if ($Json) {
    $keys = @()
    if ($Plan.Embed) { $keys += 'embed' }
    if ($Plan.Pick) { $keys += $Plan.Pick.Rung }
    $o = [ordered]@{
        tool = 'verity-check-prereqs'; schema = 1; platform = ("windows-{0}" -f $arch); root = $Root; model_choice = $Model
        ram_gb = [double]$Specs.RamGB; model_budget_gb = $null; models_selected = ($keys -join ' ')
        model_used = $(if ($Plan.Pick) { $Plan.Pick.Label } else { 'direct' }); backend = $bk.Build
        disk_free_gb = $free; disk_needed_gb = $need; download_gb = $dl
        state = $state; missing = $nMissing; todo = $nTodo; warnings = $nWarn; next = $next; items = $sorted
    }
    $o | ConvertTo-Json -Depth 5
} else {
    $w = 100
    try { $w = [int]$Host.UI.RawUI.WindowSize.Width } catch { }
    if ($w -lt 70) { $w = 70 }
    if ($w -gt 120) { $w = 120 }
    $tw = $w - 28
    function Split-Lines {   # word-wraps one paragraph, keeping its leading indent (commands stay on one line)
        param([string]$Text, [int]$Width)
        $lines = New-Object System.Collections.Generic.List[string]
        $indent = ([regex]::Match($Text, '^ *')).Value
        $cur = $indent
        foreach ($word in ($Text.Trim() -split ' ')) {
            if ($cur.Trim().Length -gt 0 -and ($cur.Length + 1 + $word.Length) -gt $Width) { $lines.Add($cur); $cur = $word }
            elseif ($cur.Trim().Length -gt 0) { $cur = "$cur $word" }
            else { $cur = "$indent$word" }
        }
        if ($cur.Trim().Length -gt 0) { $lines.Add($cur) }
        return $lines
    }
    $titles = @{ machine = '1. This machine'; required = '2. Must be true before setup'; auto = '3. Installed by setup on its own (inside this folder, no admin)'
                 optional = '4. Optional extras'; not_needed = '5. NOT needed' }
    $tags = @{ ok = @('  OK   ', 'Green'); done = @(' DONE  ', 'Green'); todo = @(' TODO  ', 'Cyan'); warn = @(' WARN  ', 'Yellow')
               missing = @('MISSING', 'Red'); absent = @('  --   ', 'DarkGray'); info = @('  no   ', 'DarkGray') }
    Write-Host ("Verity: what this PC needs   {0}" -f $Root) -ForegroundColor White
    Write-Host ("{0}, model choice: {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm'), $Model) -ForegroundColor DarkGray
    foreach ($g in $order) {
        Write-Host ''
        Write-Host $titles[$g] -ForegroundColor White
        foreach ($it in ($sorted | Where-Object { $_.group -eq $g })) {
            $tg = $tags[$it.status]
            $first = $true
            foreach ($ln in (Split-Lines $it.summary $tw)) {
                if ($first) {
                    Write-Host '  ' -NoNewline; Write-Host $tg[0] -ForegroundColor $tg[1] -NoNewline
                    Write-Host (" {0,-16} {1}" -f $it.id, $ln)
                    $first = $false
                } else {
                    Write-Host ("  {0,7} {1,-16} {2}" -f '', '', $ln)
                }
            }
            if ($it.fix) {
                foreach ($para in ($it.fix -split "`n")) {
                    $k = 0
                    foreach ($ln in (Split-Lines $para ($tw - 3))) {
                        $pre = '-> '
                        if ($k -gt 0) { $pre = '   ' }
                        Write-Host ("  {0,7} {1,-16} {2}{3}" -f '', '', $pre, $ln) -ForegroundColor Cyan
                        $k++
                    }
                }
            }
        }
    }
    Write-Host ''
    $vc = 'Green'
    if ($state -eq 'blocked') { $vc = 'Red' }
    Write-Host $verdict -ForegroundColor $vc
    if ($nWarn -gt 0) { Write-Host ("{0} warning(s): Verity works, with the limits noted." -f $nWarn) -ForegroundColor DarkGray }
    Write-Host ("Next: {0}" -f $next) -ForegroundColor White
}
if ($nMissing -gt 0) { exit 1 }
exit 0
