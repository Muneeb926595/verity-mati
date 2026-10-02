# Pure planning logic: machine specs -> which llama.cpp build to use, where each model fits, and which model to pick.
# No side effects, so it can be tested with made-up machines (see scripts/windows/tests/plan.tests.ps1).
# Windows PowerShell 5.1 compatible. ASCII only.

Set-StrictMode -Version 1.0

function ConvertTo-Version {
    param([string]$Text)
    $m = [regex]::Match("$Text", '(\d+)\.(\d+)')
    if (-not $m.Success) { return [version]'0.0' }
    return [version]("{0}.{1}" -f $m.Groups[1].Value, $m.Groups[2].Value)
}

# Ordered llama.cpp builds to use on this machine (first = preferred), each with the device it targets.
function Get-BackendPlan {
    param([Parameter(Mandatory)] $Specs)
    $out = New-Object System.Collections.Generic.List[object]
    if ($Specs.Arch -eq 'arm64') {
        $out.Add([pscustomobject]@{ Build = 'cpu-arm64'; Kind = 'cpu'; VramGB = 0; Device = 'CPU (ARM64)'; Why = 'Windows on ARM: CPU build' })
        return $out
    }
    $gpus = @($Specs.Gpus)
    $nv = $gpus | Where-Object { $_.Vendor -eq 'NVIDIA' -and -not $_.Integrated -and $_.VramGB -ge 4 } | Sort-Object VramGB -Descending | Select-Object -First 1
    $other = $gpus | Where-Object { $_.Vendor -ne 'NVIDIA' -and -not $_.Integrated -and $_.VramGB -ge 4 } | Sort-Object VramGB -Descending | Select-Object -First 1
    $igpu = $gpus | Where-Object { $_.Integrated } | Select-Object -First 1
    if ($nv) {
        $cv = ConvertTo-Version $nv.CudaVersion
        $dev = "{0} ({1:N0} GB)" -f $nv.Name, $nv.VramGB
        if ($cv -ge [version]'13.4') {
            $out.Add([pscustomobject]@{ Build = 'cuda-13.4-x64'; Kind = 'gpu'; VramGB = $nv.VramGB; Device = $dev; Why = "NVIDIA driver supports CUDA $($nv.CudaVersion)" })
        }
        if ($cv -ge [version]'12.4') {
            $out.Add([pscustomobject]@{ Build = 'cuda-12.4-x64'; Kind = 'gpu'; VramGB = $nv.VramGB; Device = $dev; Why = "NVIDIA driver supports CUDA $($nv.CudaVersion)" })
        }
        $out.Add([pscustomobject]@{ Build = 'vulkan-x64'; Kind = 'gpu'; VramGB = $nv.VramGB; Device = $dev; Why = 'Vulkan works on any recent GPU driver' })
    } elseif ($other) {
        $out.Add([pscustomobject]@{ Build = 'vulkan-x64'; Kind = 'gpu'; VramGB = $other.VramGB; Device = ("{0} ({1:N0} GB)" -f $other.Name, $other.VramGB); Why = 'AMD/Intel discrete GPU: Vulkan build' })
    }
    $out.Add([pscustomobject]@{ Build = 'cpu-x64'; Kind = 'cpu'; VramGB = 0; Device = ("CPU ({0})" -f $Specs.Cpu.Name); Why = 'always works' })
    if (-not $nv -and -not $other -and $igpu) {
        $out.Add([pscustomobject]@{ Build = 'vulkan-x64'; Kind = 'igpu'; VramGB = 0; Device = ("{0} (shares system memory)" -f $igpu.Name); Why = 'integrated GPU: may speed up prompt reading; the stress test compares it with the CPU build' })
    }
    return $out
}

# Where a model can run, for the preferred build: gpu (all on GPU), hybrid (MoE experts on CPU), cpu, tight, too large.
function Get-Placement {
    param([Parameter(Mandatory)] $Specs, [Parameter(Mandatory)] $Model, [Parameter(Mandatory)] $Backend, [double]$EmbedGB = 1.0)
    $full = Get-PlacementAt -Specs $Specs -Model $Model -Backend $Backend -EmbedGB $EmbedGB -Need ([double]$Model.resident_gb) -Ctx ([int]$Model.ctx)
    if ($full.Status -in @('tight', 'too large') -and $Model.resident_gb_8k) {
        $small = Get-PlacementAt -Specs $Specs -Model $Model -Backend $Backend -EmbedGB $EmbedGB -Need ([double]$Model.resident_gb_8k) -Ctx 8192
        if ($small.Status -notin @('tight', 'too large')) { $small.Why = $small.Why + ' (with an 8k-token context)'; return $small }
    }
    return $full
}

function Get-PlacementAt {
    param($Specs, $Model, $Backend, [double]$EmbedGB, [double]$Need, [int]$Ctx)
    $need = $Need
    $reserve = [math]::Max(3.0, 0.25 * [double]$Specs.RamGB)        # Windows, the engine and the indexer
    $ram = [math]::Max(0.0, [double]$Specs.RamGB - $reserve)
    $vram = if ($Backend.Kind -eq 'gpu') { [math]::Max(0.0, [double]$Backend.VramGB - 0.75) } else { 0.0 }
    $isMoe = [bool]$Model.moe
    $layers = if ($Model.layers) { [int]$Model.layers } else { 32 }
    $p = [ordered]@{ Status = 'too large'; Where = ''; Args = @(); EmbedArgs = @(); Expect = ''; Why = ''; Ctx = $(if ($Ctx) { $Ctx } else { 16384 }) }
    if ($vram -gt 0 -and $need -le $vram) {
        $p.Status = 'gpu'; $p.Where = 'all on the GPU'
        if ($need + $EmbedGB -gt $vram) { $p.EmbedArgs = @('-ngl', '0') }        # embeddings on the CPU, VRAM is full
        $p.Expect = 'fast (typically under 10 s per answer)'
        $p.Why = ("needs {0:N0} GB, GPU has {1:N1} GB usable" -f $need, $vram)
        return [pscustomobject]$p
    }
    if ($vram -ge 4 -and $isMoe -and $need -le ($vram + 0.9 * $ram)) {
        $onGpu = [math]::Max(0.05, ($vram - 2.0) / $need)
        $n = [int][math]::Ceiling($layers * (1.0 - $onGpu))
        $n = [math]::Min($layers, [math]::Max(1, $n))
        $p.Status = 'hybrid'; $p.Where = ("GPU + CPU (expert weights of {0} of {1} layers in system memory)" -f $n, $layers)
        $p.Args = @('--n-cpu-moe', "$n"); $p.EmbedArgs = @('-ngl', '0')
        $p.Expect = 'medium (typically 15-40 s per answer)'
        $p.Why = ("needs {0:N0} GB: {1:N1} GB GPU + {2:N0} GB usable system memory" -f $need, $vram, $ram)
        return [pscustomobject]$p
    }
    $cpuArgs = @()                                   # (an if-expression yielding @() would store $null)
    if ($Backend.Kind -eq 'gpu') { $cpuArgs = @('-ngl', '0') }
    if ($need + $EmbedGB -le $ram) {
        $p.Status = 'cpu'; $p.Where = 'system memory (CPU)'; $p.Args = $cpuArgs; $p.EmbedArgs = $cpuArgs
        $p.Expect = if ($need -le 6) { 'fast (typically under 15 s per answer)' } elseif ($need -le 20) { 'medium (typically 15-40 s per answer)' } else { 'slow (often over 45 s per answer); the stress test decides' }
        $p.Why = ("needs {0:N0} GB, {1:N0} GB of system memory usable" -f ($need + $EmbedGB), $ram)
        return [pscustomobject]$p
    }
    if ($need + $EmbedGB -le 1.15 * $ram) {
        $p.Status = 'tight'; $p.Where = 'system memory, with little to spare'; $p.Args = $cpuArgs; $p.EmbedArgs = $cpuArgs
        $p.Expect = 'may run if other programs are closed'
        $p.Why = ("needs {0:N0} GB, only {1:N0} GB usable" -f ($need + $EmbedGB), $ram)
        return [pscustomobject]$p
    }
    $p.Why = ("needs {0:N0} GB; this machine has {1:N0} GB usable{2}" -f ($need + $EmbedGB), $ram, $(if ($vram -gt 0) { (" + {0:N1} GB GPU" -f $vram) } else { '' }))
    return [pscustomobject]$p
}

# The full plan: build, every model's placement, the recommendation, downloads and warnings.
function Get-SetupPlan {
    param([Parameter(Mandatory)] $Specs, [Parameter(Mandatory)] $Catalog, [string]$ForceRung = '', [string]$ForceBuild = '')
    $backends = @(Get-BackendPlan $Specs)
    $primary = $backends[0]
    if ($ForceBuild) {
        $f = $backends | Where-Object { $_.Build -eq $ForceBuild } | Select-Object -First 1
        if ($f) { $primary = $f } else { $primary = [pscustomobject]@{ Build = $ForceBuild; Kind = $(if ($ForceBuild -like 'cpu*') { 'cpu' } else { 'gpu' }); VramGB = 0; Device = 'as requested'; Why = 'chosen with -Backend' } }
    }
    $embed = $Catalog.models | Where-Object { $_.role -eq 'embed' } | Select-Object -First 1
    $chat = @($Catalog.models | Where-Object { $_.rung } | Sort-Object { $_.rung } -Descending)          # C3, C2, C1
    $rows = foreach ($m in $chat) {
        $pl = Get-Placement -Specs $Specs -Model $m -Backend $primary -EmbedGB ([double]$embed.resident_gb)
        [pscustomobject]@{ Rung = $m.rung; Label = $m.label; File = $m.file; Bytes = [double]$m.bytes; Model = $m; Placement = $pl }
    }
    $warn = New-Object System.Collections.Generic.List[string]
    $pick = $null
    if ($ForceRung -and $ForceRung -ne 'direct') {
        $pick = $rows | Where-Object { $_.Rung -eq $ForceRung } | Select-Object -First 1
        if ($pick -and $pick.Placement.Status -in @('tight', 'too large')) { $warn.Add(("{0} was requested but {1}: {2}" -f $pick.Label, $pick.Placement.Status, $pick.Placement.Why)) }
    } elseif (-not $ForceRung) {
        foreach ($r in $rows) {
            $st = $r.Placement.Status
            # a 63 GB model on the CPU alone is usually too slow for interactive answers: only the stress test may pick it
            if ($st -eq 'gpu' -or $st -eq 'hybrid' -or ($st -eq 'cpu' -and $r.Rung -ne 'C3')) { $pick = $r; break }
        }
    }
    $ramUsable = [math]::Max(0.0, [double]$Specs.RamGB - [math]::Max(3.0, 0.25 * [double]$Specs.RamGB))
    $useEmbed = $ramUsable -ge 1.5
    $mode = if ($pick) { 'auto' } else { 'direct' }
    $ctx = if ($pick) { [int]$pick.Placement.Ctx } else { 16384 }
    $downloads = 0.0
    if ($useEmbed) { $downloads += [double]$embed.bytes }
    if ($pick) { $downloads += $pick.Bytes }
    if ([double]$Specs.DiskFreeGB -gt 0 -and ($downloads / 1GB + 6) -gt [double]$Specs.DiskFreeGB) {
        $warn.Add(("Not enough free disk space: the tools and models need about {0:N0} GB, the drive has {1:N0} GB free" -f ($downloads / 1GB + 6), $Specs.DiskFreeGB))
    }
    if ([double]$Specs.RamGB -lt 8) { $warn.Add('Less than 8 GB of memory: Verity runs in facts-only (Direct) mode; answers are complete but no AI model chooses or summarises') }
    if ($Specs.OnBattery) { $warn.Add('Running on battery: plug in before the stress test, laptops slow down on battery') }
    if ($Specs.DiskType -eq 'HDD') { $warn.Add('The drive is a hard disk: loading a model takes much longer than from an SSD') }
    $nvOld = @($Specs.Gpus) | Where-Object { $_.Vendor -eq 'NVIDIA' -and -not $_.Integrated -and (ConvertTo-Version $_.CudaVersion) -lt [version]'12.4' } | Select-Object -First 1
    if ($nvOld) { $warn.Add(("{0}: the NVIDIA driver is too old for the CUDA builds (needs CUDA 12.4+); using Vulkan. Update the driver for best speed." -f $nvOld.Name)) }
    if ($Specs.Arch -eq 'arm64') { $warn.Add('Windows on ARM is untested: Python runs as x64 under emulation (slower indexing); llama.cpp runs natively') }
    return [pscustomobject]@{
        Backends = $backends; Backend = $primary; Models = $rows; Pick = $pick; Embed = $(if ($useEmbed) { $embed } else { $null })
        Mode = $mode; Ctx = $ctx; DownloadBytes = $downloads; Warnings = $warn
    }
}

# models\selection.json content (read by verity/probe.py). Measured results, when given, replace the estimates.
function New-SelectionObject {
    param([Parameter(Mandatory)] $Plan, $Measured = $null, [string]$Source = 'analyze_system.ps1 (estimate)')
    $main = $null
    if ($Plan.Pick) {
        $m = $Plan.Pick.Model
        $margs = @($Plan.Pick.Placement.Args | Where-Object { $null -ne $_ })
        $ctx = $Plan.Ctx
        if ($Measured) { $margs = @($Measured.Args | Where-Object { $null -ne $_ }); if ($Measured.Ctx) { $ctx = $Measured.Ctx } }
        $main = [ordered]@{ rung = $m.rung; label = $m.label; file = $m.file; gb = [double]$m.resident_gb; ctx = $ctx
                            reasoning = [bool]$m.reasoning; args = $margs; min_bytes = [long]([double]$m.bytes * 0.98) }
    }
    $embed = $null
    if ($Plan.Embed) {
        $e = $Plan.Embed
        $eargs = @()
        if ($Plan.Pick) { $eargs = @($Plan.Pick.Placement.EmbedArgs | Where-Object { $null -ne $_ }) }
        $embed = [ordered]@{ label = $e.label; file = $e.file; gb = [double]$e.resident_gb; args = $eargs; min_bytes = [long]([double]$e.bytes * 0.98) }
    }
    $fast = $null
    if ($main -and $main.rung -eq 'C1') { $fast = $main }
    return [ordered]@{
        written_by = $Source; at = (Get-Date).ToString('s'); mode = $Plan.Mode; backend = $Plan.Backend.Build
        device = $Plan.Backend.Device; main = $main; fast = $fast; embed = $embed
    }
}
