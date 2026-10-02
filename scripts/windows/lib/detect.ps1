# Hardware detection and the human-readable report, shared by setup.ps1 and analyze_system.ps1.
# Windows PowerShell 5.1 compatible. ASCII only.

Set-StrictMode -Version 1.0

# ------------------------------------------------------------------------------------------------ hardware detection
function Test-CpuFeature {
    param([int]$Feature)
    try {
        if (-not ('Vr.Native' -as [type])) {
            Add-Type -Namespace Vr -Name Native -MemberDefinition '[DllImport("kernel32.dll")] public static extern bool IsProcessorFeaturePresent(uint f);'
        }
        return [Vr.Native]::IsProcessorFeaturePresent([uint32]$Feature)
    } catch {
        return $false
    }
}

function Find-NvidiaSmi {
    $c = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    if ($env:ProgramFiles) {
        $p = Join-Path $env:ProgramFiles 'NVIDIA Corporation\NVSMI\nvidia-smi.exe'
        if (Test-Path -LiteralPath $p) { return $p }
    }
    return $null
}

function Get-GpuList {
    $regMem = @{}
    $cls = 'HKLM:\SYSTEM\ControlSet001\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}'
    try {
        Get-ChildItem -LiteralPath $cls -ErrorAction Stop | Where-Object { $_.PSChildName -match '^\d{4}$' } | ForEach-Object {
            $p = Get-ItemProperty -LiteralPath $_.PSPath -ErrorAction SilentlyContinue
            if ($p -and $p.DriverDesc) {
                $q = $p.'HardwareInformation.qwMemorySize'
                if (-not $q) {
                    $q = $p.'HardwareInformation.MemorySize'
                    if ($q -is [byte[]]) { $q = [BitConverter]::ToUInt32($q, 0) }
                }
                if ($q) { $regMem[[string]$p.DriverDesc] = [double]$q }
            }
        }
    } catch { }
    $list = New-Object System.Collections.Generic.List[object]
    foreach ($v in @(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue)) {
        $name = ([string]$v.Name).Trim()
        if (-not $name -or $name -match 'Microsoft Basic|Remote Display|Hyper-V|Parsec|Virtual') { continue }
        $comp = [string]$v.AdapterCompatibility
        $vendor = 'Other'
        if ($name -match 'NVIDIA|GeForce|Quadro|RTX|Tesla' -or $comp -match 'NVIDIA') { $vendor = 'NVIDIA' }
        elseif ($name -match 'AMD|Radeon' -or $comp -match 'Advanced Micro|AMD') { $vendor = 'AMD' }
        elseif ($name -match 'Intel' -or $comp -match 'Intel') { $vendor = 'Intel' }
        $bytes = if ($regMem.ContainsKey($name)) { $regMem[$name] } else { [double]$v.AdapterRAM }
        $vram = [math]::Round($bytes / 1GB, 1)
        $integrated = $false
        if ($vendor -eq 'Intel' -and $name -notmatch 'Arc') { $integrated = $true }
        if ($vendor -eq 'AMD' -and $name -match 'Radeon\(TM\) Graphics|Radeon Graphics|Vega \d+ Graphics|780M|760M|680M|890M|880M') { $integrated = $true }
        if ($vram -lt 1) { $integrated = $true }
        $list.Add([pscustomobject]@{ Name = $name; Vendor = $vendor; VramGB = $vram; Integrated = $integrated; Driver = [string]$v.DriverVersion; CudaVersion = '' })
    }
    $smi = Find-NvidiaSmi
    if ($smi) {
        $head = Get-NativeOutput $smi @()
        $cuda = ([regex]::Match($head, 'CUDA Version:\s*([\d.]+)')).Groups[1].Value
        $rows = (Get-NativeOutput $smi @('--query-gpu=name,memory.total,driver_version', '--format=csv,noheader,nounits')) -split "`n"
        foreach ($r in $rows) {
            $f = $r -split ','
            if ($f.Count -lt 3) { continue }
            $n = $f[0].Trim(); $mem = [double]($f[1].Trim()) / 1024; $drv = $f[2].Trim()
            $g = $list | Where-Object { $_.Vendor -eq 'NVIDIA' -and ($_.Name -eq $n -or $n -like "*$($_.Name)*" -or $_.Name -like "*$n*") } | Select-Object -First 1
            if ($g) { $g.VramGB = [math]::Round($mem, 1); $g.CudaVersion = $cuda; $g.Driver = $drv; $g.Integrated = $false }
            else { $list.Add([pscustomobject]@{ Name = $n; Vendor = 'NVIDIA'; VramGB = [math]::Round($mem, 1); Integrated = $false; Driver = $drv; CudaVersion = $cuda }) }
        }
    }
    return $list
}

function Get-SystemSpecs {
    param([Parameter(Mandatory)] [string]$Root, [switch]$NoNetwork)
    $os = Get-CimInstance Win32_OperatingSystem
    $cs = Get-CimInstance Win32_ComputerSystem
    $cpus = @(Get-CimInstance Win32_Processor)
    $cores = ($cpus | Measure-Object -Property NumberOfCores -Sum).Sum
    $threads = ($cpus | Measure-Object -Property NumberOfLogicalProcessors -Sum).Sum
    $drive = (Split-Path -Qualifier $Root)
    $ld = Get-CimInstance Win32_LogicalDisk -Filter ("DeviceID='{0}'" -f $drive) -ErrorAction SilentlyContinue
    $diskType = 'Unknown'
    try {
        $part = Get-Partition -DriveLetter $drive.TrimEnd(':') -ErrorAction Stop
        $pd = Get-PhysicalDisk -ErrorAction Stop | Where-Object { [string]$_.DeviceId -eq [string]$part.DiskNumber } | Select-Object -First 1
        if ($pd -and $pd.MediaType -and "$($pd.MediaType)" -ne 'Unspecified') { $diskType = "$($pd.MediaType)" }
    } catch { }
    $bat = Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue | Select-Object -First 1
    $admin = $false
    try { $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) } catch { }
    $longPaths = $false
    try { $longPaths = ((Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -ErrorAction Stop).LongPathsEnabled -eq 1) } catch { }
    $net = [ordered]@{ GitHub = $null; HuggingFace = $null; PyPI = $null }
    if (-not $NoNetwork) {
        Enable-ModernTls
        $net.GitHub = Test-Url 'https://github.com'
        $net.HuggingFace = Test-Url 'https://huggingface.co'
        $net.PyPI = Test-Url 'https://pypi.org/simple/pip/'
    }
    return [pscustomobject]@{
        Os = ("{0} (build {1})" -f $os.Caption, $os.BuildNumber); Arch = (Get-Arch)
        Cpu = [pscustomobject]@{ Name = ([string]$cpus[0].Name).Trim(); Cores = [int]$cores; Threads = [int]$threads; MHz = [int]$cpus[0].MaxClockSpeed
                                 Avx2 = (Test-CpuFeature 40); Avx512 = (Test-CpuFeature 41) }
        RamGB = [math]::Round([double]$cs.TotalPhysicalMemory / 1GB, 1)
        RamFreeGB = [math]::Round([double]$os.FreePhysicalMemory * 1KB / 1GB, 1)
        Gpus = @(Get-GpuList)
        Drive = $drive; DiskFreeGB = $(if ($ld) { [math]::Round([double]$ld.FreeSpace / 1GB, 1) } else { 0 }); DiskType = $diskType
        OnBattery = [bool]($bat -and $bat.BatteryStatus -eq 1)
        Admin = $admin; LongPaths = $longPaths; PowerShell = "$($PSVersionTable.PSVersion)"
        Network = [pscustomobject]$net
    }
}

# ------------------------------------------------------------------------------------------------ report
function Show-Specs {
    param($Specs)
    Write-VrLog DATA ("Windows   : {0}, {1}" -f $Specs.Os, $Specs.Arch)
    Write-VrLog DATA ("CPU       : {0} - {1} cores / {2} threads{3}{4}" -f $Specs.Cpu.Name, $Specs.Cpu.Cores, $Specs.Cpu.Threads, $(if ($Specs.Cpu.Avx2) { ', AVX2' } else { '' }), $(if ($Specs.Cpu.Avx512) { ', AVX-512' } else { '' }))
    Write-VrLog DATA ("Memory    : {0:N1} GB total, {1:N1} GB free now" -f $Specs.RamGB, $Specs.RamFreeGB)
    if (@($Specs.Gpus).Count -eq 0) { Write-VrLog DATA 'GPU       : none found' }
    foreach ($g in @($Specs.Gpus)) {
        Write-VrLog DATA ("GPU       : {0} - {1}{2}{3}" -f $g.Name, $(if ($g.Integrated) { 'integrated (shares system memory)' } else { ("{0:N1} GB dedicated" -f $g.VramGB) }), $(if ($g.CudaVersion) { ", CUDA $($g.CudaVersion)" } else { '' }), $(if ($g.Driver) { ", driver $($g.Driver)" } else { '' }))
    }
    Write-VrLog DATA ("Disk {0}   : {1:N0} GB free ({2})" -f $Specs.Drive, $Specs.DiskFreeGB, $Specs.DiskType)
    Write-VrLog DATA ("Power     : {0}" -f $(if ($Specs.OnBattery) { 'on battery' } else { 'mains / desktop' }))
    if ($null -ne $Specs.Network.GitHub) {
        Write-VrLog DATA ("Internet  : GitHub {0}, Hugging Face {1}, PyPI {2}" -f $(if ($Specs.Network.GitHub) { 'ok' } else { 'BLOCKED' }), $(if ($Specs.Network.HuggingFace) { 'ok' } else { 'BLOCKED' }), $(if ($Specs.Network.PyPI) { 'ok' } else { 'BLOCKED' }))
    }
    Write-VrLog DATA ("Session   : PowerShell {0}, administrator: {1}, long paths: {2}" -f $Specs.PowerShell, $Specs.Admin, $Specs.LongPaths)
}

function Show-Plan {
    param($Plan)
    Write-VrLog STEP ''
    Write-VrLog STEP 'llama.cpp build for this machine'
    foreach ($b in $Plan.Backends) {
        $tag = if ($b.Build -eq $Plan.Backend.Build -and $b.Kind -eq $Plan.Backend.Kind) { 'use     ' } else { 'fallback' }
        Write-VrLog DATA ("  {0} {1,-14} {2} - {3}" -f $tag, $b.Build, $b.Device, $b.Why)
    }
    Write-VrLog STEP ''
    Write-VrLog STEP 'Local AI models (largest first)'
    foreach ($r in $Plan.Models) {
        $pl = $r.Placement
        Write-VrLog DATA ("  {0}  {1,-34} {2,8}  {3,-9} {4}" -f $r.Rung, $r.Label, (Format-Size $r.Bytes), $pl.Status, $(if ($pl.Where) { $pl.Where + '; ' + $pl.Expect } else { $pl.Why }))
        if ($pl.Where) { Write-VrLog DATA ("                 {0}" -f $pl.Why) }
    }
    Write-VrLog STEP ''
    Write-VrLog STEP 'Recommendation'
    if ($Plan.Pick) {
        Write-VrLog OK ("Chat model : {0} ({1}) - {2}" -f $Plan.Pick.Label, $Plan.Pick.Placement.Where, $Plan.Pick.Model.tested)
        if (@($Plan.Pick.Placement.Args).Count) { Write-VrLog DATA ("             llama-server options: {0}" -f (@($Plan.Pick.Placement.Args) -join ' ')) }
    } else {
        Write-VrLog OK 'Chat model : none - Direct mode (answers built from checked facts only; nothing to download)'
    }
    Write-VrLog OK ("Embeddings : {0}" -f $(if ($Plan.Embed) { $Plan.Embed.label + ' (meaning search)' } else { 'off (too little memory)' }))
    Write-VrLog OK ("Context    : {0} tokens; downloads about {1}" -f $Plan.Ctx, (Format-Size $Plan.DownloadBytes))
    $bigger = $Plan.Models | Where-Object { $_.Placement.Status -eq 'cpu' -and $_.Rung -eq 'C3' } | Select-Object -First 1
    if ($bigger) { Write-VrLog INFO ("{0} also fits in memory but runs on the CPU only; run with -StressTest to see whether it is fast enough here." -f $bigger.Label) }
    foreach ($w in $Plan.Warnings) { Write-VrLog WARN $w }
}

function Save-Report {
    param($Root, $Specs, $Plan, $Results, $Winner)
    $ts = Get-Date -Format 'yyyyMMdd-HHmmss'
    $base = Join-Path $Root ("logs\system-analysis-{0}" -f $ts)
    $obj = [ordered]@{
        at = (Get-Date).ToString('s'); specs = $Specs; backend = $Plan.Backend; backends = $Plan.Backends
        models = @($Plan.Models | ForEach-Object { [ordered]@{ rung = $_.Rung; label = $_.Label; bytes = $_.Bytes; status = $_.Placement.Status; where = $_.Placement.Where; args = $_.Placement.Args; expect = $_.Placement.Expect; why = $_.Placement.Why } })
        recommendation = [ordered]@{ main = $(if ($Plan.Pick) { $Plan.Pick.Label } else { 'Direct mode' }); embed = $(if ($Plan.Embed) { $Plan.Embed.label } else { $null }); ctx = $Plan.Ctx }
        stress_test = $Results; winner = $Winner; warnings = $Plan.Warnings
    }
    ($obj | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath "$base.json" -Encoding UTF8
    $md = New-Object System.Collections.Generic.List[string]
    $md.Add('# Verity system analysis'); $md.Add('')
    $md.Add(("{0} - {1}, {2:N1} GB memory, {3}" -f (Get-Date), $Specs.Cpu.Name, $Specs.RamGB, (@($Specs.Gpus | ForEach-Object { $_.Name }) -join ', '))); $md.Add('')
    $md.Add('| Rung | Model | Download | Fits as | Expected |'); $md.Add('|---|---|---|---|---|')
    foreach ($r in $Plan.Models) { $md.Add(("| {0} | {1} | {2} | {3} | {4} |" -f $r.Rung, $r.Label, (Format-Size $r.Bytes), $r.Placement.Status, $(if ($r.Placement.Where) { $r.Placement.Expect } else { $r.Placement.Why }))) }
    if ($Results) {
        $md.Add(''); $md.Add('## Stress test'); $md.Add('')
        $md.Add('| Model | Build | Options | Load s | Prompt tok/s | Gen tok/s | Answer s | Peak memory GB | Verdict |'); $md.Add('|---|---|---|---|---|---|---|---|---|')
        foreach ($x in $Results) { $md.Add(("| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} | {8} |" -f $x.Label, $x.Build, (@($x.Args) -join ' '), $x.LoadS, $x.PromptTps, $x.GenTps, $x.AnswerS, $x.PeakGB, $x.Verdict)) }
    }
    if ($Winner) { $md.Add(''); $md.Add(("**Chosen:** {0}" -f $Winner)) }
    if ($Plan.Warnings.Count) { $md.Add(''); $md.Add('## Warnings'); foreach ($w in $Plan.Warnings) { $md.Add("- $w") } }
    [IO.File]::WriteAllText("$base.md", ($md -join "`n") + "`n", (New-Object System.Text.UTF8Encoding($false)))
    Write-VrLog INFO ("Report: {0}.md (and .json)" -f $base)
}
