# Install helpers shared by setup.ps1 and analyze_system.ps1: a pinned llama.cpp build, model files, and the model
# selection read by the engine (models\selection.json). Windows PowerShell 5.1 compatible. ASCII only.

Set-StrictMode -Version 1.0

# Quotes arguments for Start-Process (Windows PowerShell 5.1 joins an argument array without quoting spaces).
function Join-ProcessArgs {
    param([string[]]$Items)
    return (($Items | ForEach-Object {
        if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
    }) -join ' ')
}

# Installs one llama.cpp Windows build (verified zip, plus the CUDA runtime DLLs for CUDA builds) into Destination
# and checks that llama-server.exe starts. Returns the version line.
function Install-LlamaBuild {
    param([Parameter(Mandatory)] [string]$Root, [Parameter(Mandatory)] $Catalog, [Parameter(Mandatory)] [string]$Build,
          [Parameter(Mandatory)] [string]$Destination)
    $spec = $Catalog.llama_cpp.builds.$Build
    if (-not $spec) { throw "unknown llama.cpp build '$Build'" }
    $marker = Join-Path $Destination '.verity-build.json'
    $exe = Join-Path $Destination 'llama-server.exe'
    if ((Test-Path -LiteralPath $marker) -and (Test-Path -LiteralPath $exe)) {
        $m = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
        if ($m.build -eq $Build -and $m.tag -eq $Catalog.llama_cpp.tag) {
            Write-VrLog OK ("llama.cpp {0} ({1}) already installed in {2}" -f $Catalog.llama_cpp.tag, $Build, $Destination)
            return $m.version
        }
    }
    $dl = Join-Path $Root 'tools\downloads'
    $zip = Join-Path $dl $spec.zip
    Get-RemoteFile -Url ($Catalog.llama_cpp.base_url + $spec.zip) -Destination $zip -Sha256 $spec.sha256 -ExpectedBytes ([double]$spec.mb * 1MB)
    $rt = $null
    if ($spec.runtime) {
        $rt = Join-Path $dl $spec.runtime.zip
        Get-RemoteFile -Url ($Catalog.llama_cpp.base_url + $spec.runtime.zip) -Destination $rt -Sha256 $spec.runtime.sha256 -ExpectedBytes ([double]$spec.runtime.mb * 1MB) -Label ("CUDA runtime for {0}" -f $Build)
    }
    if (Test-Path -LiteralPath $Destination) { Remove-Item -LiteralPath $Destination -Recurse -Force }
    Write-VrLog INFO ("Extracting {0}..." -f $spec.zip)
    Expand-ZipTo -Zip $zip -Destination $Destination
    if ($rt) { Expand-ZipTo -Zip $rt -Destination $Destination }
    Get-ChildItem -LiteralPath $Destination -Recurse -File | Unblock-File -ErrorAction SilentlyContinue
    if (-not (Test-Path -LiteralPath $exe)) {
        $found = Get-ChildItem -LiteralPath $Destination -Recurse -Filter 'llama-server.exe' | Select-Object -First 1
        if (-not $found) { throw "llama-server.exe not found after extracting $($spec.zip)" }
        Get-ChildItem -LiteralPath $found.DirectoryName -Force | ForEach-Object { Move-Item -LiteralPath $_.FullName -Destination $Destination -Force }
    }
    $ver = (Get-NativeOutput $exe @('--version')) -split "`n" | Where-Object { $_ -match 'version' } | Select-Object -First 1
    if (-not $ver) { throw ("llama-server.exe from the {0} build does not start on this machine (missing driver or CPU feature?)" -f $Build) }
    $ver = $ver.Trim()
    ([ordered]@{ build = $Build; tag = $Catalog.llama_cpp.tag; version = $ver; installed = (Get-Date).ToString('s') } |
        ConvertTo-Json) | Set-Content -LiteralPath $marker -Encoding UTF8
    Write-VrLog OK ("llama.cpp ready: {0} [{1}]" -f $ver, $Build)
    return $ver
}

# Lists the devices a llama.cpp build can see (empty when only the CPU is usable).
function Get-LlamaDevices {
    param([Parameter(Mandatory)] [string]$Directory)
    $exe = Join-Path $Directory 'llama-server.exe'
    $out = Get-NativeOutput $exe @('--list-devices')
    return @(($out -split "`n") | Where-Object { $_ -match '^\s*(CUDA|Vulkan|SYCL|ROCm|OpenCL)\d*\s*:' } | ForEach-Object { $_.Trim() })
}

function Install-ModelFile {
    param([Parameter(Mandatory)] [string]$Root, [Parameter(Mandatory)] $Model)
    $dest = Join-Path $Root ("models\" + $Model.file)
    Get-RemoteFile -Url $Model.url -Destination $dest -Sha256 $Model.sha256 -ExpectedBytes ([double]$Model.bytes) -Label $Model.label
    return $dest
}

function Write-Selection {
    param([Parameter(Mandatory)] [string]$Root, [Parameter(Mandatory)] $Selection)
    $p = Join-Path $Root 'models\selection.json'
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $p) | Out-Null
    # UTF-8 without BOM: Python's json reader rejects a BOM unless told otherwise
    $json = $Selection | ConvertTo-Json -Depth 6
    [IO.File]::WriteAllText($p, $json, (New-Object System.Text.UTF8Encoding($false)))
    Write-VrLog OK ("Model selection written: {0}" -f $p)
    $main = if ($Selection.main) { "{0} {1}" -f $Selection.main.label, (($Selection.main.args) -join ' ') } else { 'none (Direct mode)' }
    Write-VrLog DATA ("  chat model : {0}" -f $main)
    Write-VrLog DATA ("  embeddings : {0}" -f $(if ($Selection.embed) { $Selection.embed.label } else { 'off' }))
    Write-VrLog DATA ("  llama.cpp  : {0} on {1}" -f $Selection.backend, $Selection.device)
}

function Write-Sha256Sums {
    param([Parameter(Mandatory)] [string]$Root)
    $dir = Join-Path $Root 'models'
    $lines = Get-ChildItem -LiteralPath $dir -Filter '*.gguf' | ForEach-Object {
        $mk = "$($_.FullName).verified"
        if (Test-Path -LiteralPath $mk) { "{0}  {1}" -f ((Get-Content -LiteralPath $mk -Raw).Trim() -split '\|')[2], $_.Name }
    }
    if ($lines) { [IO.File]::WriteAllText((Join-Path $dir 'SHA256SUMS.txt'), (($lines -join "`n") + "`n"), (New-Object System.Text.UTF8Encoding($false))) }
}
