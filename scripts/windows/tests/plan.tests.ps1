# Tests for the planning logic with made-up machines (no Pester needed; runs on Windows PowerShell 5.1 and pwsh 7 on
# any OS):  powershell -ExecutionPolicy Bypass -File scripts\windows\tests\plan.tests.ps1
# Optional: -SelectionOut <path> writes the selection for one machine, for the Python cross-check.
param([string]$SelectionOut = '')

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $here '..\lib\common.ps1')
. (Join-Path $here '..\lib\plan.ps1')
$catalog = Get-Content -LiteralPath (Join-Path $here '..\catalog.json') -Raw | ConvertFrom-Json

$script:fails = 0
$script:count = 0
function Assert-That {
    param([bool]$Condition, [string]$Message)
    $script:count++
    if ($Condition) { Write-Host ("  ok   {0}" -f $Message) -ForegroundColor Green }
    else { $script:fails++; Write-Host ("  FAIL {0}" -f $Message) -ForegroundColor Red }
}

function New-Machine {
    param([double]$Ram, [object[]]$Gpus = @(), [string]$Arch = 'x64', [double]$Disk = 500)
    return [pscustomobject]@{ Os = 'test'; Arch = $Arch; RamGB = $Ram; RamFreeGB = $Ram / 2; DiskFreeGB = $Disk; DiskType = 'SSD'; OnBattery = $false
                              Cpu = [pscustomobject]@{ Name = 'Test CPU'; Cores = 8; Threads = 16; Avx2 = $true; Avx512 = $false }; Gpus = $Gpus }
}
function New-Gpu {
    param([string]$Name, [string]$Vendor, [double]$Vram, [string]$Cuda = '', [bool]$Integrated = $false)
    return [pscustomobject]@{ Name = $Name; Vendor = $Vendor; VramGB = $Vram; Integrated = $Integrated; Driver = '1'; CudaVersion = $Cuda }
}
function Get-Row { param($Plan, [string]$Rung) return ($Plan.Models | Where-Object { $_.Rung -eq $Rung } | Select-Object -First 1) }

Write-Host 'Laptop: 8 GB, Intel integrated graphics'
$p = Get-SetupPlan -Specs (New-Machine 8 @(New-Gpu 'Intel(R) UHD Graphics' 'Intel' 0.1 '' $true)) -Catalog $catalog
Assert-That ($p.Backend.Build -eq 'cpu-x64') 'CPU build first'
Assert-That (@($p.Backends | Where-Object { $_.Kind -eq 'igpu' }).Count -eq 1) 'Vulkan on the integrated GPU is offered as an alternative'
Assert-That ($p.Pick -and $p.Pick.Rung -eq 'C1' -and $p.Ctx -eq 8192) 'small model with an 8k context'
Assert-That ((Get-Row $p 'C2').Placement.Status -eq 'too large') 'gpt-oss-20b does not fit'

Write-Host 'Desktop: 4 GB'
$p = Get-SetupPlan -Specs (New-Machine 4) -Catalog $catalog
Assert-That ($p.Mode -eq 'direct' -and -not $p.Pick) 'Direct mode'
Assert-That (-not $p.Embed) 'no embeddings'
Assert-That (@($p.Warnings | Where-Object { $_ -like '*Direct*' }).Count -ge 1) 'warns about Direct mode'

Write-Host 'Desktop: 16 GB, no GPU'
$p = Get-SetupPlan -Specs (New-Machine 16) -Catalog $catalog
Assert-That ($p.Pick.Rung -eq 'C1' -and $p.Ctx -eq 16384) 'small model, full context'

Write-Host 'Workstation: 32 GB, no GPU'
$p = Get-SetupPlan -Specs (New-Machine 32) -Catalog $catalog
Assert-That ($p.Pick.Rung -eq 'C2' -and $p.Pick.Placement.Status -eq 'cpu') 'gpt-oss-20b on the CPU'
Assert-That (@($p.Pick.Placement.Args).Count -eq 0) 'no GPU options on a CPU build'

Write-Host 'Workstation: 96 GB, no GPU'
$p = Get-SetupPlan -Specs (New-Machine 96) -Catalog $catalog
Assert-That ((Get-Row $p 'C3').Placement.Status -eq 'cpu') 'gpt-oss-120b fits in memory'
Assert-That ($p.Pick.Rung -eq 'C2') 'but only the stress test may choose a 63 GB model on the CPU alone'

Write-Host 'Gaming PC: RTX 3060 12 GB + 32 GB'
$p = Get-SetupPlan -Specs (New-Machine 32 @(New-Gpu 'NVIDIA GeForce RTX 3060' 'NVIDIA' 12 '13.0')) -Catalog $catalog
Assert-That ($p.Backend.Build -eq 'cuda-12.4-x64') 'CUDA 12.4 build (driver reports CUDA 13.0)'
Assert-That ($p.Pick.Rung -eq 'C2' -and $p.Pick.Placement.Status -eq 'hybrid') 'gpt-oss-20b split between GPU and CPU'
Assert-That ($p.Pick.Placement.Args[0] -eq '--n-cpu-moe' -and [int]$p.Pick.Placement.Args[1] -ge 1 -and [int]$p.Pick.Placement.Args[1] -le 24) 'expert layers on the CPU within range'
Assert-That ($p.Pick.Placement.EmbedArgs -contains '-ngl') 'embeddings kept off the full GPU'

Write-Host 'Workstation: RTX 4090 24 GB + 64 GB, new driver'
$p = Get-SetupPlan -Specs (New-Machine 64 @(New-Gpu 'NVIDIA GeForce RTX 4090' 'NVIDIA' 24 '13.4')) -Catalog $catalog
Assert-That ($p.Backend.Build -eq 'cuda-13.4-x64') 'CUDA 13.4 build'
Assert-That ((Get-Row $p 'C2').Placement.Status -eq 'gpu') 'gpt-oss-20b fully on the GPU'
Assert-That ($p.Pick.Rung -eq 'C3' -and $p.Pick.Placement.Status -eq 'hybrid') 'gpt-oss-120b recommended (GPU + CPU)'

Write-Host 'Workstation: RTX 4090 24 GB + 32 GB'
$p = Get-SetupPlan -Specs (New-Machine 32 @(New-Gpu 'NVIDIA GeForce RTX 4090' 'NVIDIA' 24 '12.8')) -Catalog $catalog
Assert-That ($p.Pick.Rung -eq 'C2' -and $p.Pick.Placement.Status -eq 'gpu') 'gpt-oss-20b on the GPU; 120b too large'

Write-Host 'Old NVIDIA driver (CUDA 11.8)'
$p = Get-SetupPlan -Specs (New-Machine 32 @(New-Gpu 'NVIDIA GeForce GTX 1080' 'NVIDIA' 8 '11.8')) -Catalog $catalog
Assert-That ($p.Backend.Build -eq 'vulkan-x64') 'Vulkan instead of CUDA'
Assert-That (@($p.Warnings | Where-Object { $_ -like '*driver is too old*' }).Count -eq 1) 'tells the user to update the driver'

Write-Host 'AMD Radeon RX 7900 XTX 24 GB + 64 GB'
$p = Get-SetupPlan -Specs (New-Machine 64 @(New-Gpu 'AMD Radeon RX 7900 XTX' 'AMD' 24)) -Catalog $catalog
Assert-That ($p.Backend.Build -eq 'vulkan-x64' -and $p.Backend.Kind -eq 'gpu') 'Vulkan on the discrete AMD GPU'
Assert-That ($p.Pick.Rung -eq 'C3') 'gpt-oss-120b recommended'

Write-Host 'Windows on ARM, 16 GB'
$p = Get-SetupPlan -Specs (New-Machine 16 @() 'arm64') -Catalog $catalog
Assert-That ($p.Backend.Build -eq 'cpu-arm64') 'ARM64 CPU build'
Assert-That (@($p.Warnings | Where-Object { $_ -like '*ARM*' }).Count -eq 1) 'warns that ARM is untested'

Write-Host 'Low disk space'
$p = Get-SetupPlan -Specs (New-Machine 32 @() 'x64' 10) -Catalog $catalog
Assert-That (@($p.Warnings | Where-Object { $_ -like '*disk space*' }).Count -eq 1) 'warns about disk space'

Write-Host 'Forced choices'
$p = Get-SetupPlan -Specs (New-Machine 16) -Catalog $catalog -ForceRung 'C2'
Assert-That ($p.Pick.Rung -eq 'C2' -and @($p.Warnings | Where-Object { $_ -like '*requested*' }).Count -eq 1) '-Model C2 honoured with a warning'
$p = Get-SetupPlan -Specs (New-Machine 64) -Catalog $catalog -ForceRung 'direct'
Assert-That ($p.Mode -eq 'direct') '-Model direct honoured'
$p = Get-SetupPlan -Specs (New-Machine 32 @(New-Gpu 'NVIDIA GeForce RTX 3060' 'NVIDIA' 12 '13.0')) -Catalog $catalog -ForceBuild 'cpu-x64'
Assert-That ($p.Backend.Build -eq 'cpu-x64' -and $p.Pick.Placement.Status -eq 'cpu') '-Backend cpu-x64 plans for the CPU'

Write-Host 'Selection file'
$p = Get-SetupPlan -Specs (New-Machine 64 @(New-Gpu 'NVIDIA GeForce RTX 4090' 'NVIDIA' 24 '13.4')) -Catalog $catalog
$sel = New-SelectionObject -Plan $p
Assert-That ($sel.main.file -eq 'gpt-oss-120b-MXFP4.gguf' -and $sel.main.args[0] -eq '--n-cpu-moe') 'main model and its llama-server options'
Assert-That ($sel.embed.file -eq 'Qwen3-Embedding-0.6B-Q8_0.gguf' -and $sel.backend -eq 'cuda-13.4-x64') 'embedding model and build'
$m = [pscustomobject]@{ Args = @('--n-cpu-moe', '30'); Ctx = 8192 }
$sel2 = New-SelectionObject -Plan $p -Measured $m -Source 'measured'
Assert-That ($sel2.main.args[1] -eq '30' -and $sel2.main.ctx -eq 8192) 'measured options replace the estimate'
$cpuSel = New-SelectionObject -Plan (Get-SetupPlan -Specs (New-Machine 32) -Catalog $catalog)
$cpuJson = $cpuSel | ConvertTo-Json -Depth 6
Assert-That (@($cpuSel.main.args).Count -eq 0 -and $cpuJson -match '"args":\s*\[\s*\]' -and $cpuJson -notmatch 'null\s*\]') 'a CPU machine gets "args": [] (never [null])'
if ($SelectionOut) {
    [IO.File]::WriteAllText($SelectionOut, ($sel | ConvertTo-Json -Depth 6), (New-Object System.Text.UTF8Encoding($false)))
    Write-Host ("  wrote {0}" -f $SelectionOut)
}

Write-Host 'Helpers'
Assert-That ((ConvertTo-Version '13.0') -gt (ConvertTo-Version '12.4')) 'CUDA versions compare numerically'
Assert-That ((Format-Size 12109566624) -eq '11.3 GB') 'sizes are readable'

Write-Host ''
if ($script:fails) { Write-Host ("{0} of {1} checks failed" -f $script:fails, $script:count) -ForegroundColor Red; exit 1 }
Write-Host ("all {0} checks passed" -f $script:count) -ForegroundColor Green
exit 0
