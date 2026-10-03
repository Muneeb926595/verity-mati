<#
.SYNOPSIS
    One-command Verity setup for Windows: installs everything Verity needs inside its own folder and checks that it
    works end to end. No administrator rights and no system-wide installs are needed.

.DESCRIPTION
    Steps (each one is skipped when already done, so re-running resumes where it stopped):
       1  Preflight: Windows, PowerShell, folder, internet, disk space
       2  System analysis: hardware -> llama.cpp build and local AI model (scripts\windows\analyze_system.ps1)
       3  uv (fetches Python and the Python packages)
       4  Python 3.12 + .venv + the pinned packages (requirements.lock)
       5  llama.cpp (CUDA / Vulkan / CPU build chosen for this machine; falls back automatically)
       6  .NET SDK 10
       7  C# compiler worker (Roslyn)
       8  cppcheck (second C parser for cross-checking; optional)
       9  User interface (only built when ui\dist is missing or older than ui\src)
      10  Local AI models (verified by SHA256) and models\selection.json
      11  Stress test (optional, -StressTest): measures the candidate models and keeps the best
      12  Verification: capability probe, test suite, a facts-only answer and a model answer
    Everything goes to the console and to logs\setup-<time>.log.

.PARAMETER Model
    auto (default: chosen from the hardware), direct (no AI model), C1, C2 or C3.
.PARAMETER Backend
    auto (default) or a llama.cpp build: cpu-x64, cpu-arm64, vulkan-x64, cuda-12.4-x64, cuda-13.4-x64.
.PARAMETER StressTest
    After installing, measure the candidate models on this machine and keep the best one.
.PARAMETER QualityCheck
    After installing, run part of Verity's benchmark with the chosen model (about 10 minutes).
.PARAMETER Yes
    Do not ask before large downloads (over 20 GB).

.EXAMPLE
    scripts\windows\setup.cmd
.EXAMPLE
    scripts\windows\setup.cmd -StressTest -QualityCheck -Yes
.EXAMPLE
    scripts\windows\setup.cmd -Model direct -SkipTests
#>
[CmdletBinding()]
param(
    [string]$Root = '',
    [ValidateSet('auto', 'direct', 'C1', 'C2', 'C3')] [string]$Model = 'auto',
    [ValidateSet('auto', 'cpu-x64', 'cpu-arm64', 'vulkan-x64', 'cuda-12.4-x64', 'cuda-13.4-x64')] [string]$Backend = 'auto',
    [switch]$StressTest,
    [switch]$QualityCheck,
    [switch]$SkipModels,
    [switch]$SkipTests,
    [switch]$SkipCppcheck,
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'          # Invoke-WebRequest is many times slower with its progress bar
if (-not $Root) { $Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path }
. (Join-Path $PSScriptRoot 'lib\common.ps1')
. (Join-Path $PSScriptRoot 'lib\plan.ps1')
. (Join-Path $PSScriptRoot 'lib\install.ps1')
. (Join-Path $PSScriptRoot 'lib\detect.ps1')

$Total = 12
$Tools = Join-Path $Root 'tools'
$Downloads = Join-Path $Tools 'downloads'
$Py = Join-Path $Root '.venv\Scripts\python.exe'
$Summary = New-Object System.Collections.Generic.List[object]

function Add-Result {
    param([string]$Item, [ValidateSet('OK', 'WARN', 'FAIL', 'SKIPPED')] [string]$State, [string]$Detail = '')
    $Summary.Add([pscustomobject]@{ Item = $Item; State = $State; Detail = $Detail }) | Out-Null
}

function Set-VerityEnv {
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $Tools 'python'
    $env:UV_CACHE_DIR = Join-Path $Tools 'uv-cache'
    $env:UV_PYTHON_PREFERENCE = 'only-managed'     # never pick up some other Python on PATH
    $env:UV_LINK_MODE = 'copy'
    $env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
    $env:DOTNET_NOLOGO = '1'
    $env:DOTNET_SKIP_FIRST_TIME_EXPERIENCE = '1'
    $env:DOTNET_GENERATE_ASPNET_CERTIFICATE = 'false'
    $env:DOTNET_ROOT = Join-Path $Tools 'dotnet'
    $env:NUGET_PACKAGES = Join-Path $Tools 'nuget-packages'
    $env:npm_config_cache = Join-Path $Tools 'npm-cache'
    $env:npm_config_update_notifier = 'false'
    $env:npm_config_fund = 'false'
    $env:npm_config_audit = 'false'
}

# ---------------------------------------------------------------------------------------------------------- steps
function Step-Preflight {
    Start-Step 1 $Total 'Preflight'
    if ($env:OS -ne 'Windows_NT') { throw 'This script is for Windows. On macOS/Linux use: bash scripts/setup.sh' }
    if ($PSVersionTable.PSVersion.Major -lt 5) { throw 'Windows PowerShell 5.1 or newer is required.' }
    foreach ($need in @('verity', 'requirements.lock', 'scripts\windows\catalog.json')) {
        if (-not (Test-Path -LiteralPath (Join-Path $Root $need))) { throw "This does not look like the Verity folder (missing $need): $Root" }
    }
    Write-VrLog INFO ("Verity folder: {0}" -f $Root)
    if ($Root.Length -gt 80) { Write-VrLog WARN ("The folder path is {0} characters long; deep files may hit Windows' 260-character limit. A short path such as C:\verity is safer." -f $Root.Length) }
    Write-VrLog INFO 'Removing the "downloaded from the internet" mark from the copied files...'
    Get-ChildItem -LiteralPath $Root -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notlike (Join-Path $Root 'tools\*') } | Unblock-File -ErrorAction SilentlyContinue
    Enable-ModernTls
    $sites = [ordered]@{ 'GitHub' = 'https://github.com'; 'Hugging Face' = 'https://huggingface.co'; 'PyPI' = 'https://pypi.org/simple/pip/'; 'Microsoft .NET' = 'https://dot.net/v1/dotnet-install.ps1' }
    $bad = @()
    foreach ($k in $sites.Keys) {
        if (Test-Url $sites[$k]) { Write-VrLog OK ("reachable: {0}" -f $k) } else { Write-VrLog WARN ("not reachable: {0} ({1})" -f $k, $sites[$k]); $bad += $k }
    }
    if ($bad.Count -eq $sites.Count) { throw 'No internet access. Setup downloads Python packages, tools and models once; connect and run it again.' }
    if (-not (Get-CurlInfo)) { Write-VrLog INFO 'curl.exe not found: downloads use BITS (slower, no live progress)' }
    $script:Catalog = Read-Catalog -Root $Root
    New-Item -ItemType Directory -Force -Path $Tools, $Downloads | Out-Null
    Set-VerityEnv
    Add-Result 'Preflight' 'OK' $(if ($bad.Count) { 'unreachable: ' + ($bad -join ', ') } else { '' })
    Complete-Step
}

function Step-Analysis {
    Start-Step 2 $Total 'System analysis'
    $script:Specs = Get-SystemSpecs -Root $Root -NoNetwork
    Show-Specs $script:Specs
    $forceRung = if ($Model -eq 'auto') { '' } else { $Model }
    $forceBuild = if ($Backend -eq 'auto') { '' } else { $Backend }
    $script:Plan = Get-SetupPlan -Specs $script:Specs -Catalog $script:Catalog -ForceRung $forceRung -ForceBuild $forceBuild
    Show-Plan $script:Plan
    Add-Result 'System analysis' 'OK' ("{0}; {1}" -f $script:Plan.Backend.Build, $(if ($script:Plan.Pick) { $script:Plan.Pick.Label } else { 'Direct mode' }))
    Complete-Step
}

function Step-Uv {
    Start-Step 3 $Total 'uv (Python installer)'
    $uv = Join-Path $Tools 'uv\uv.exe'
    $want = $script:Catalog.uv.version
    if ((Test-Path -LiteralPath $uv) -and ((Get-NativeOutput $uv @('--version')) -match [regex]::Escape($want))) {
        Write-VrLog OK ("uv {0} already installed" -f $want)
    } else {
        $arch = if ($script:Specs.Arch -eq 'arm64') { 'arm64' } else { 'x64' }
        $spec = $script:Catalog.uv.$arch
        $zip = Join-Path $Downloads (Split-Path -Leaf $spec.url)
        Get-RemoteFile -Url $spec.url -Destination $zip -Sha256 $spec.sha256 -Label ("uv {0}" -f $want)
        $dir = Join-Path $Tools 'uv'
        if (Test-Path -LiteralPath $dir) { Remove-Item -LiteralPath $dir -Recurse -Force }
        Expand-ZipTo -Zip $zip -Destination $dir
        Write-VrLog OK (Get-NativeOutput $uv @('--version'))
    }
    $script:Uv = $uv
    Add-Result 'uv' 'OK' $want
    Complete-Step
}

function Step-Python {
    Start-Step 4 $Total 'Python 3.12, virtual environment and packages'
    $ver = $script:Catalog.python
    $request = $ver
    if ($script:Specs.Arch -eq 'arm64') {
        # some pinned packages (cryptography) have no Windows-on-ARM wheels; x64 Python runs under Windows 11's
        # emulation and gets ready-made wheels (llama.cpp stays a native ARM64 program)
        $request = "cpython-$ver-windows-x86_64-none"
        Write-VrLog INFO 'Windows on ARM: using x64 Python (emulated) so every package installs without a compiler'
    }
    [void](Invoke-Native -FilePath $script:Uv -ArgumentList @('python', 'install', $request))
    $ok = $false
    if (Test-Path -LiteralPath $Py) { $ok = (Get-NativeOutput $Py @('--version')) -match ("Python " + [regex]::Escape($ver)) }
    if (-not $ok) {
        $venv = Join-Path $Root '.venv'
        if (Test-Path -LiteralPath $venv) { Write-VrLog INFO 'Replacing an unusable .venv'; Remove-Item -LiteralPath $venv -Recurse -Force }
        [void](Invoke-Native -FilePath $script:Uv -ArgumentList @('venv', '--python', $request, $venv) -WorkingDirectory $Root)
    }
    Write-VrLog INFO (Get-NativeOutput $Py @('--version'))
    [void](Invoke-Native -FilePath $script:Uv -ArgumentList @('pip', 'install', '--python', $Py, '-r', (Join-Path $Root 'requirements.lock')) -WorkingDirectory $Root)
    $check = 'import clang.cindex as ci, numpy, aiohttp, mcp, tree_sitter, tree_sitter_c, tree_sitter_c_sharp; ci.Index.create(); import verity.cli; print("imports ok, libclang loads")'
    [void](Invoke-Native -FilePath $Py -ArgumentList @('-c', $check) -WorkingDirectory $Root -Label 'python: import check')
    Add-Result 'Python + packages' 'OK' (Get-NativeOutput $Py @('--version'))
    Complete-Step
}

function Step-Llama {
    Start-Step 5 $Total 'llama.cpp (runs the local AI models)'
    $dest = Join-Path $Tools 'llama.cpp'
    $order = New-Object System.Collections.Generic.List[string]
    $order.Add($script:Plan.Backend.Build)
    foreach ($b in $script:Plan.Backends) { if (-not $order.Contains($b.Build)) { $order.Add($b.Build) } }
    $done = $null
    foreach ($b in $order) {
        try {
            [void](Install-LlamaBuild -Root $Root -Catalog $script:Catalog -Build $b -Destination $dest)
            if ($b -notlike 'cpu*') {
                $dev = Get-LlamaDevices -Directory $dest
                if ($dev.Count -eq 0) { throw ("the {0} build sees no GPU (driver missing or too old)" -f $b) }
                Write-VrLog OK ("GPU found: {0}" -f ($dev -join '; '))
            }
            $done = $b
            break
        } catch {
            Write-VrLog WARN ("{0}: {1} - trying the next build" -f $b, $_.Exception.Message)
            if (Test-Path -LiteralPath (Join-Path $dest '.verity-build.json')) { Remove-Item -LiteralPath (Join-Path $dest '.verity-build.json') -Force }
        }
    }
    if (-not $done) { throw 'No llama.cpp build runs on this machine.' }
    if ($done -ne $script:Plan.Backend.Build) {
        $bk = $script:Plan.Backends | Where-Object { $_.Build -eq $done } | Select-Object -First 1
        if (-not $bk) { $bk = [pscustomobject]@{ Build = $done; Kind = 'cpu'; VramGB = 0; Device = 'CPU'; Why = 'fallback' } }
        Write-VrLog WARN ("Using {0} instead of {1}; re-planning the model for it" -f $done, $script:Plan.Backend.Build)
        $forceRung = if ($Model -eq 'auto') { '' } else { $Model }
        $script:Plan = Get-SetupPlan -Specs $script:Specs -Catalog $script:Catalog -ForceRung $forceRung -ForceBuild $done
    }
    Add-Result 'llama.cpp' 'OK' ("{0} {1}" -f $script:Catalog.llama_cpp.tag, $done)
    Complete-Step
}

function Step-Dotnet {
    Start-Step 6 $Total '.NET SDK (for the C# compiler worker)'
    $dotnet = Join-Path $Tools 'dotnet\dotnet.exe'
    $ch = $script:Catalog.dotnet.channel
    if ((Test-Path -LiteralPath $dotnet) -and ((Get-NativeOutput $dotnet @('--list-sdks')) -match ("(?m)^" + [regex]::Escape($ch.Split('.')[0]) + '\.'))) {
        Write-VrLog OK (".NET SDK {0} already installed" -f $ch)
    } else {
        $inst = Join-Path $Downloads 'dotnet-install.ps1'
        Get-RemoteFile -Url $script:Catalog.dotnet.install_script -Destination $inst -Label 'dotnet-install.ps1'
        $sig = Get-AuthenticodeSignature -LiteralPath $inst
        if ($sig.Status -ne 'Valid') { Write-VrLog WARN ("dotnet-install.ps1 signature: {0}" -f $sig.Status) } else { Write-VrLog OK ("dotnet-install.ps1 signed by {0}" -f $sig.SignerCertificate.Subject) }
        [void](Invoke-Native -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $inst, '-Channel', $ch, '-InstallDir', (Join-Path $Tools 'dotnet'), '-NoPath'))
    }
    $script:Dotnet = $dotnet
    Add-Result '.NET SDK' 'OK' ((Get-NativeOutput $dotnet @('--version')).Trim())
    Complete-Step
}

function Step-Roslyn {
    Start-Step 7 $Total 'C# compiler worker (Roslyn)'
    $wdir = Join-Path $Root 'workers\roslyn'
    $dll = Join-Path $wdir 'bin\VerityRoslyn.dll'
    $fresh = $false
    if (Test-Path -LiteralPath $dll) {
        $newest = Get-ChildItem -LiteralPath (Join-Path $wdir 'src') -Recurse -File | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
        $fresh = (-not $newest) -or ((Get-Item -LiteralPath $dll).LastWriteTimeUtc -ge $newest.LastWriteTimeUtc)
        if ($fresh) { $fresh = (Get-NativeOutput $script:Dotnet @($dll, '--version')) -match '\d' }
    }
    if ($fresh) {
        Write-VrLog OK 'C# worker already built and up to date'
    } else {
        $lock = @()
        if (Test-Path -LiteralPath (Join-Path $wdir 'packages.lock.json')) { $lock = @('--locked-mode') }
        [void](Invoke-Native -FilePath $script:Dotnet -ArgumentList (@('restore', 'VerityRoslyn.csproj') + $lock) -WorkingDirectory $wdir)
        $bin = Join-Path $wdir 'bin'
        if (Test-Path -LiteralPath $bin) { Remove-Item -LiteralPath $bin -Recurse -Force }
        [void](Invoke-Native -FilePath $script:Dotnet -ArgumentList @('publish', 'VerityRoslyn.csproj', '-c', 'Release', '--no-restore', '-o', $bin,
            '-p:UseAppHost=false', '-p:DebugType=none', '-p:GenerateDocumentationFile=false') -WorkingDirectory $wdir)
    }
    $v = (Get-NativeOutput $script:Dotnet @($dll, '--version')).Trim()
    if (-not $v) { throw 'the C# worker does not run' }
    Write-VrLog OK ("C# worker: {0}" -f $v)
    Add-Result 'C# worker' 'OK' $v
    Complete-Step
}

function Step-Cppcheck {
    Start-Step 8 $Total 'cppcheck (optional cross-check of the C parser)'
    if ($SkipCppcheck) { Add-Result 'cppcheck' 'SKIPPED' '-SkipCppcheck'; Complete-Step 'skipped'; return }
    $want = $script:Catalog.cppcheck.version
    $exe = Join-Path $Tools 'cppcheck\cppcheck.exe'
    try {
        if ((Test-Path -LiteralPath $exe) -and ((Get-NativeOutput $exe @('--version')) -match [regex]::Escape($want))) {
            Write-VrLog OK ("cppcheck {0} already installed" -f $want)
        } else {
            $msi = Join-Path $Downloads (Split-Path -Leaf $script:Catalog.cppcheck.url)
            Get-RemoteFile -Url $script:Catalog.cppcheck.url -Destination $msi -Sha256 $script:Catalog.cppcheck.sha256 -Label ("cppcheck {0}" -f $want)
            $stage = Join-Path $Tools 'cppcheck-msi'
            if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
            $mlog = Join-Path $Root 'logs\cppcheck-msi.log'
            Write-VrLog INFO 'Unpacking the cppcheck installer into tools (an administrative image: nothing is installed system-wide)'
            $p = Start-Process -FilePath 'msiexec.exe' -ArgumentList (Join-ProcessArgs @('/a', $msi, '/qn', "TARGETDIR=$stage", '/L*v', $mlog)) -Wait -PassThru
            if ($p.ExitCode -ne 0) { throw ("msiexec exit {0} (see {1})" -f $p.ExitCode, $mlog) }
            $found = Get-ChildItem -LiteralPath $stage -Recurse -Filter 'cppcheck.exe' | Select-Object -First 1
            if (-not $found) { throw 'cppcheck.exe not found in the installer' }
            $dst = Join-Path $Tools 'cppcheck'
            if (Test-Path -LiteralPath $dst) { Remove-Item -LiteralPath $dst -Recurse -Force }
            New-Item -ItemType Directory -Force -Path $dst | Out-Null
            Copy-Item -Path (Join-Path $found.DirectoryName '*') -Destination $dst -Recurse -Force
            Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
        }
        $v = (Get-NativeOutput $exe @('--version')).Trim()
        Write-VrLog OK $v
        Add-Result 'cppcheck' 'OK' $v
        Complete-Step
    } catch {
        Write-VrLog WARN ("cppcheck not installed: {0}. Verity works without it (the cross-check is skipped)." -f $_.Exception.Message)
        Add-Result 'cppcheck' 'WARN' $_.Exception.Message
        Complete-Step 'continuing without cppcheck'
    }
}

function Step-Ui {
    Start-Step 9 $Total 'User interface'
    $ui = Join-Path $Root 'ui'
    if (-not (Test-UiStale -Ui $ui)) {
        Write-VrLog OK 'ui\dist is present and up to date (the engine serves it; Node.js is not needed)'
        Add-Result 'User interface' 'OK' 'prebuilt'
        Complete-Step
        return
    }
    $arch = if ($script:Specs.Arch -eq 'arm64') { 'arm64' } else { 'x64' }
    $idx = Invoke-RestMethod -Uri $script:Catalog.node.index -UseBasicParsing
    $rel = $idx | Where-Object { $_.lts -and $_.version -like ("v{0}.*" -f $script:Catalog.node.major) -and $_.files -contains ("win-{0}-zip" -f $arch) } | Select-Object -First 1
    if (-not $rel) { throw ("no Node.js {0} LTS for win-{1}" -f $script:Catalog.node.major, $arch) }
    $name = "node-{0}-win-{1}.zip" -f $rel.version, $arch
    $base = "https://nodejs.org/dist/{0}/" -f $rel.version
    $sums = (Invoke-WebRequest -Uri ($base + 'SHASUMS256.txt') -UseBasicParsing).Content
    $sha = ([regex]::Match($sums, '([0-9a-f]{64})\s+' + [regex]::Escape($name))).Groups[1].Value
    $zip = Join-Path $Downloads $name
    Get-RemoteFile -Url ($base + $name) -Destination $zip -Sha256 $sha -Label ("Node.js {0}" -f $rel.version)
    $node = Join-Path $Tools 'node'
    if (-not (Test-Path -LiteralPath (Join-Path $node 'node.exe'))) { Expand-ZipTo -Zip $zip -Destination $node }
    $env:PATH = "$node;$env:PATH"
    $npm = Join-Path $node 'npm.cmd'
    $npx = Join-Path $node 'npx.cmd'
    [void](Invoke-Native -FilePath $npm -ArgumentList @('ci', '--prefer-offline', '--no-audit', '--no-fund') -WorkingDirectory $ui)
    [void](Invoke-Native -FilePath $npx -ArgumentList @('--no-install', 'tsc', '--noEmit', '-p', '.') -WorkingDirectory $ui)
    [void](Invoke-Native -FilePath $npx -ArgumentList @('--no-install', 'vite', 'build') -WorkingDirectory $ui)
    if (-not (Test-Path -LiteralPath (Join-Path $ui 'dist\index.html'))) { throw 'the UI build did not produce ui\dist\index.html' }
    Add-Result 'User interface' 'OK' ("built with Node.js {0}" -f $rel.version)
    Complete-Step
}

function Step-Models {
    Start-Step 10 $Total 'Local AI models'
    if ($SkipModels) { Add-Result 'Models' 'SKIPPED' '-SkipModels: Direct mode until models are added'; Complete-Step 'skipped'; return }
    $want = New-Object System.Collections.Generic.List[object]
    if ($script:Plan.Embed) { $want.Add($script:Plan.Embed) }
    if ($script:Plan.Pick) { $want.Add($script:Plan.Pick.Model) }
    $missing = 0.0
    foreach ($m in $want) { if (-not (Test-Path -LiteralPath (Join-Path $Root ("models\" + $m.file)))) { $missing += [double]$m.bytes } }
    $free = (Get-CimInstance Win32_LogicalDisk -Filter ("DeviceID='{0}'" -f (Split-Path -Qualifier $Root))).FreeSpace
    if ($missing -gt 0 -and ($missing + 2GB) -gt $free) {
        throw ("The models need {0} but the drive has {1} free. Free some space, or run with -Model C1 / -Model direct." -f (Format-Size ($missing + 2GB)), (Format-Size $free))
    }
    if ($missing -gt 20GB -and -not $Yes) {
        $a = Read-Host ("About to download {0} of models. Continue? [Y/n]" -f (Format-Size $missing))
        if ($a -match '^(n|no)$') { throw 'Stopped before the model download. Re-run with -Model C1 (smaller) or -Model direct.' }
    }
    foreach ($m in $want) { [void](Install-ModelFile -Root $Root -Model $m) }
    $selPath = Join-Path $Root 'models\selection.json'
    $keep = $false
    if ((Test-Path -LiteralPath $selPath) -and $Model -eq 'auto') {
        try {
            $old = Get-Content -LiteralPath $selPath -Raw | ConvertFrom-Json
            $keep = ($old.written_by -like '*measured*') -and (-not $old.main -or (Test-Path -LiteralPath (Join-Path $Root ("models\" + $old.main.file))))
        } catch { }
    }
    if ($keep) { Write-VrLog OK 'Keeping the model selection measured by an earlier stress test' }
    else { Write-Selection -Root $Root -Selection (New-SelectionObject -Plan $script:Plan) }
    Write-Sha256Sums -Root $Root
    Add-Result 'Models' 'OK' (($want | ForEach-Object { $_.label }) -join ', ')
    Complete-Step
}

function Step-StressTest {
    Start-Step 11 $Total 'Stress test'
    if (-not $StressTest) { Write-VrLog INFO 'Skipped (add -StressTest to measure the candidate models on this machine).'; Add-Result 'Stress test' 'SKIPPED' ''; Complete-Step 'skipped'; return }
    $a = @{ Root = $Root; StressTest = $true; Apply = $true; Yes = [bool]$Yes; LogFile = $script:LogFile }
    & (Join-Path $PSScriptRoot 'analyze_system.ps1') @a
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { Write-VrLog WARN 'The stress test reported an error; keeping the estimated selection.'; Add-Result 'Stress test' 'WARN' 'see log' }
    else { Add-Result 'Stress test' 'OK' 'models\selection.json updated from measurements' }
    Complete-Step
}

function Step-Verify {
    Start-Step 12 $Total 'Verification'
    [void](Invoke-Native -FilePath $Py -ArgumentList @('-m', 'verity', 'probe') -WorkingDirectory $Root -AllowFail -Label 'verity probe')
    if ($SkipTests) {
        Add-Result 'Test suite' 'SKIPPED' '-SkipTests'
    } else {
        Write-VrLog INFO 'Running the test suite (a few minutes; tests that need a code sandbox are skipped on Windows)'
        $code = Invoke-Native -FilePath $Py -ArgumentList @('-m', 'pytest', 'tests', '-q', '-p', 'no:cacheprovider') -WorkingDirectory $Root -AllowFail -Label 'pytest tests'
        $last = Get-Content -LiteralPath $script:LogFile -Tail 40 | Where-Object { $_ -match '\d+ (passed|failed)' } | Select-Object -Last 1
        $txt = if ($last) { ($last -replace '^.*\[CMD \]\s*', '').Trim() } else { "exit $code" }
        if ($code -eq 0) { Add-Result 'Test suite' 'OK' $txt } else { Write-VrLog WARN ("Some tests failed: {0}" -f $txt); Add-Result 'Test suite' 'WARN' $txt }
    }
    $q = 'Explain how GPS data travels through this application.'
    $code = Invoke-Native -FilePath $Py -ArgumentList @('-m', 'verity', 'ask', 'samples\navgps-c', $q, '--direct') -WorkingDirectory $Root -AllowFail -Label ("verity ask (facts only): {0}" -f $q)
    if ($code -eq 0) { Add-Result 'Facts-only answer' 'OK' 'samples\navgps-c' } else { Add-Result 'Facts-only answer' 'FAIL' "exit $code" }
    if ($script:Plan.Pick -and -not $SkipModels) {
        $q2 = 'What happens if GPS becomes unavailable?'
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $code = Invoke-Native -FilePath $Py -ArgumentList @('-m', 'verity', 'ask', 'samples\navgps-c', $q2, '--llm') -WorkingDirectory $Root -AllowFail -Label ("verity ask (local model): {0}" -f $q2)
        $s = [math]::Round($sw.Elapsed.TotalSeconds, 0)
        if ($code -eq 0) { Add-Result 'Model answer' 'OK' ("{0} s including model load" -f $s) } else { Add-Result 'Model answer' 'FAIL' "exit $code (see data\logs\chat-server.log)" }
    }
    if ($QualityCheck) {
        $a = @{ Root = $Root; QualityCheck = $true; LogFile = $script:LogFile }
        & (Join-Path $PSScriptRoot 'analyze_system.ps1') @a
        Add-Result 'Quality check' 'OK' 'see the table above'
    }
    Complete-Step
}

function Show-Summary {
    param([bool]$Failed)
    Write-VrLog STEP ''
    Write-VrLog STEP '==== Summary ===='
    foreach ($r in $Summary) {
        $lvl = switch ($r.State) { 'OK' { 'OK' } 'WARN' { 'WARN' } 'FAIL' { 'FAIL' } default { 'INFO' } }
        Write-Host ("  {0,-8} {1,-20} {2}" -f $r.State, $r.Item, $r.Detail) -ForegroundColor $(switch ($lvl) { 'OK' { 'Green' } 'WARN' { 'Yellow' } 'FAIL' { 'Red' } default { 'Gray' } })
        Add-Content -LiteralPath $script:LogFile -Value ("  {0,-8} {1,-20} {2}" -f $r.State, $r.Item, $r.Detail) -Encoding UTF8
    }
    if ($Failed) {
        Write-VrLog FAIL 'Setup did not finish. Fix the problem above and run scripts\windows\setup.cmd again: finished steps are skipped.'
    } else {
        Write-VrLog STEP ''
        Write-VrLog OK 'Verity is ready. Try:'
        Write-VrLog DATA '  bin\verity.cmd ask samples\navgps-c "Explain how GPS data travels through this application."'
        Write-VrLog DATA '  bin\verity.cmd ask C:\path\to\your\project "What happens when the CRC check fails?"'
        Write-VrLog DATA '  bin\verity.cmd ui            (the app in your browser; the desktop app: scripts\windows\build_desktop.cmd -Shortcut)'
        Write-VrLog DATA '  bin\verity.cmd mcp           (MCP server for AI assistants)'
        Write-VrLog DATA '  scripts\windows\analyze.cmd -StressTest -Apply    (measure and re-pick the model on this machine)'
    }
    $w = Get-WarningList
    if ($w.Count) { Write-VrLog WARN ("{0} warning(s) above; all are in the log." -f $w.Count) }
    Write-VrLog INFO ("Full log: {0}" -f $script:LogFile)
}

# ---------------------------------------------------------------------------------------------------------- main
[void](Initialize-Log -Root $Root -Name 'setup')
Write-VrLog INFO ("Options: Model={0} Backend={1} StressTest={2} QualityCheck={3} SkipModels={4} SkipTests={5} Yes={6}" -f $Model, $Backend, [bool]$StressTest, [bool]$QualityCheck, [bool]$SkipModels, [bool]$SkipTests, [bool]$Yes)
$failed = $false
$t0 = [Diagnostics.Stopwatch]::StartNew()
try {
    Step-Preflight
    Step-Analysis
    Step-Uv
    Step-Python
    Step-Llama
    Step-Dotnet
    Step-Roslyn
    Step-Cppcheck
    Step-Ui
    Step-Models
    Step-StressTest
    Step-Verify
} catch {
    $failed = $true
    Write-VrLog FAIL $_.Exception.Message
    if ($_.InvocationInfo) { Write-VrLog FAIL ("at {0}" -f $_.InvocationInfo.PositionMessage.Split("`n")[0]) }
}
Write-VrLog INFO ("Total time: {0:N1} min" -f $t0.Elapsed.TotalMinutes)
Show-Summary -Failed $failed
if ($failed) { exit 1 }
exit 0
