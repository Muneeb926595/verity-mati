# Shared helpers for the Windows setup and system analysis: logging to console + file, running native commands with
# live output, resumable downloads verified by SHA256, zip extraction, and the pinned catalog.
# Windows PowerShell 5.1 compatible. ASCII only (5.1 reads BOM-less scripts as ANSI).

Set-StrictMode -Version 1.0     # 2.0 would throw on JSON objects that lack an optional property

$script:LogFile = $null
$script:StepTimer = $null
$script:Warnings = New-Object System.Collections.Generic.List[string]

function Initialize-Log {
    param([Parameter(Mandatory)] [string]$Root, [Parameter(Mandatory)] [string]$Name)
    $dir = Join-Path $Root 'logs'
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $script:LogFile = Join-Path $dir ("{0}-{1}.log" -f $Name, (Get-Date -Format 'yyyyMMdd-HHmmss'))
    try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
    Write-VrLog INFO ("Log file: {0}" -f $script:LogFile)
    Write-VrLog INFO ("PowerShell {0} on {1} ({2})" -f $PSVersionTable.PSVersion, [Environment]::OSVersion.VersionString, (Get-Arch))
    return $script:LogFile
}

function Write-VrLog {
    param([Parameter(Position = 0)] [ValidateSet('INFO', 'STEP', 'OK', 'WARN', 'FAIL', 'CMD', 'DATA')] [string]$Level = 'INFO',
          [Parameter(Position = 1)] [AllowEmptyString()] [string]$Message = '')
    $ts = Get-Date -Format 'HH:mm:ss'
    $line = "{0} [{1,-4}] {2}" -f $ts, $Level, $Message
    $color = switch ($Level) {
        'STEP' { 'Cyan' } 'OK' { 'Green' } 'WARN' { 'Yellow' } 'FAIL' { 'Red' } 'CMD' { 'DarkGray' } 'DATA' { 'White' }
        default { 'Gray' }
    }
    Write-Host $line -ForegroundColor $color
    if ($script:LogFile) {
        try { Add-Content -LiteralPath $script:LogFile -Value $line -Encoding UTF8 } catch { }
    }
    if ($Level -eq 'WARN') { $script:Warnings.Add($Message) | Out-Null }
}

function Start-Step {
    param([int]$Index, [int]$Total, [string]$Title)
    $script:StepTimer = [Diagnostics.Stopwatch]::StartNew()
    Write-VrLog STEP ''
    Write-VrLog STEP ("==== [{0}/{1}] {2} ====" -f $Index, $Total, $Title)
}

function Complete-Step {
    param([string]$Message = 'done')
    $s = if ($script:StepTimer) { [math]::Round($script:StepTimer.Elapsed.TotalSeconds, 1) } else { 0 }
    Write-VrLog OK ("{0} ({1} s)" -f $Message, $s)
}

function Get-Arch {
    $a = $env:PROCESSOR_ARCHITEW6432
    if (-not $a) { $a = $env:PROCESSOR_ARCHITECTURE }
    if ($a -match 'ARM64') { return 'arm64' }
    if ($a -match 'AMD64|x64') { return 'x64' }
    return 'x86'
}

function Format-Size {
    param([double]$Bytes)
    if ($Bytes -ge 1GB) { return ('{0:N1} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N0} MB' -f ($Bytes / 1MB)) }
    return ('{0:N0} KB' -f ($Bytes / 1KB))
}

function Read-Catalog {
    param([Parameter(Mandatory)] [string]$Root)
    $p = Join-Path $Root 'scripts\windows\catalog.json'
    if (-not (Test-Path -LiteralPath $p)) { throw "catalog not found: $p" }
    return (Get-Content -LiteralPath $p -Raw -Encoding UTF8 | ConvertFrom-Json)
}

function Enable-ModernTls {
    try {
        $p = [Net.SecurityProtocolType]::Tls12
        try { $p = $p -bor [Net.SecurityProtocolType]::Tls13 } catch { }
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor $p
    } catch { }
}

# Runs a native program, streaming every output line (stdout and stderr) to the console and the log as it happens.
# Returns the exit code; throws on a non-zero exit unless -AllowFail.
function Invoke-Native {
    param([Parameter(Mandatory)] [string]$FilePath, [string[]]$ArgumentList = @(), [string]$WorkingDirectory = '',
          [switch]$AllowFail, [string]$Label = '')
    $shown = if ($Label) { $Label } else { "{0} {1}" -f (Split-Path -Leaf $FilePath), ($ArgumentList -join ' ') }
    Write-VrLog INFO ("> {0}" -f $shown)
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'          # native stderr must not become a terminating error (PowerShell 5.1)
    $pushed = $false
    try {
        if ($WorkingDirectory) { Push-Location -LiteralPath $WorkingDirectory; $pushed = $true }
        & $FilePath @ArgumentList 2>&1 | ForEach-Object {
            $t = if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { "$_" }
            if ($t.Trim().Length -gt 0) { Write-VrLog CMD $t.TrimEnd() }
        }
        $code = $LASTEXITCODE
        if ($null -eq $code) { $code = 0 }
    } finally {
        if ($pushed) { Pop-Location }
        $ErrorActionPreference = $old
    }
    if ($code -ne 0 -and -not $AllowFail) { throw ("command failed (exit {0}): {1}" -f $code, $shown) }
    return $code
}

# Captures a native program's output as text (no live logging): for version checks and queries.
function Get-NativeOutput {
    param([Parameter(Mandatory)] [string]$FilePath, [string[]]$ArgumentList = @())
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $FilePath @ArgumentList 2>&1 | ForEach-Object { "$_" }
        return ($out -join "`n")
    } catch {
        return ''
    } finally {
        $ErrorActionPreference = $old
    }
}

function Get-Sha256 {
    param([Parameter(Mandatory)] [string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

# A verified file keeps a small marker (size + time + hash) so a 63 GB model is not hashed again on every run.
function Test-VerifiedFile {
    param([Parameter(Mandatory)] [string]$Path, [Parameter(Mandatory)] [string]$Sha256)
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $item = Get-Item -LiteralPath $Path
    $marker = "$Path.verified"
    $sig = "{0}|{1}|{2}" -f $item.Length, $item.LastWriteTimeUtc.Ticks, $Sha256.ToLowerInvariant()
    if ((Test-Path -LiteralPath $marker) -and ((Get-Content -LiteralPath $marker -Raw).Trim() -eq $sig)) { return $true }
    Write-VrLog INFO ("Checking SHA256 of {0} ({1})..." -f (Split-Path -Leaf $Path), (Format-Size $item.Length))
    if ((Get-Sha256 $Path) -eq $Sha256.ToLowerInvariant()) {
        Set-Content -LiteralPath $marker -Value $sig -Encoding ASCII
        return $true
    }
    return $false
}

function Get-CurlInfo {
    $c = Get-Command curl.exe -ErrorAction SilentlyContinue
    if (-not $c) { return $null }
    $v = Get-NativeOutput $c.Source @('--version')
    $m = [regex]::Match($v, 'curl (\d+)\.(\d+)')
    $modern = $m.Success -and (([int]$m.Groups[1].Value -gt 7) -or ([int]$m.Groups[1].Value -eq 7 -and [int]$m.Groups[2].Value -ge 71))
    return [pscustomobject]@{ Path = $c.Source; Modern = $modern }
}

# Downloads Url to Destination, resuming a partial *.part file, then checks the SHA256 before renaming it into place.
function Get-RemoteFile {
    param([Parameter(Mandatory)] [string]$Url, [Parameter(Mandatory)] [string]$Destination, [string]$Sha256 = '',
          [string]$Label = '', [double]$ExpectedBytes = 0)
    $name = if ($Label) { $Label } else { Split-Path -Leaf $Destination }
    if ($Sha256 -and (Test-VerifiedFile -Path $Destination -Sha256 $Sha256)) {
        Write-VrLog OK ("{0}: already downloaded and verified" -f $name)
        return
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
    $part = "$Destination.part"
    for ($attempt = 1; $attempt -le 2; $attempt++) {
        $size = if ($ExpectedBytes -gt 0) { " (" + (Format-Size $ExpectedBytes) + ")" } else { '' }
        Write-VrLog INFO ("Downloading {0}{1}`n           from {2}" -f $name, $size, $Url)
        $curl = Get-CurlInfo
        $sw = [Diagnostics.Stopwatch]::StartNew()
        if ($curl) {
            $a = @('-L', '--fail', '--retry', '10', '--retry-delay', '5', '-C', '-', '--progress-bar', '-o', $part, $Url)
            if ($curl.Modern) { $a = @('--retry-all-errors') + $a }
            $old = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            & $curl.Path @a                      # progress bar goes straight to the console
            $code = $LASTEXITCODE
            $ErrorActionPreference = $old
            if ($code -eq 33) {                  # server refused the resume range: start over
                Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue
                & $curl.Path @($a | Where-Object { $_ -ne '-C' -and $_ -ne '-' })
                $code = $LASTEXITCODE
            }
            if ($code -ne 0) { throw ("download failed (curl exit {0}): {1}" -f $code, $Url) }
        } else {
            Import-Module BitsTransfer -ErrorAction Stop
            Start-BitsTransfer -Source $Url -Destination $part -DisplayName "Verity setup" -Description $name
        }
        $mb = (Get-Item -LiteralPath $part).Length / 1MB
        Write-VrLog INFO ("Downloaded {0:N0} MB in {1:N0} s" -f $mb, $sw.Elapsed.TotalSeconds)
        if (-not $Sha256) {
            Move-Item -LiteralPath $part -Destination $Destination -Force
            return
        }
        Write-VrLog INFO ("Verifying SHA256 of {0}..." -f $name)
        $got = Get-Sha256 $part
        if ($got -eq $Sha256.ToLowerInvariant()) {
            Move-Item -LiteralPath $part -Destination $Destination -Force
            $item = Get-Item -LiteralPath $Destination
            Set-Content -LiteralPath "$Destination.verified" -Value ("{0}|{1}|{2}" -f $item.Length, $item.LastWriteTimeUtc.Ticks, $got) -Encoding ASCII
            Write-VrLog OK ("{0}: verified" -f $name)
            return
        }
        Write-VrLog WARN ("{0}: checksum mismatch (got {1}); downloading again from the start" -f $name, $got)
        Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue
    }
    throw ("{0}: the download does not match its published SHA256 twice in a row. A proxy or antivirus may be altering it." -f $name)
}

# Extracts a zip; when it holds a single top-level folder, its contents land directly in Destination.
function Expand-ZipTo {
    param([Parameter(Mandatory)] [string]$Zip, [Parameter(Mandatory)] [string]$Destination)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("verity-unzip-" + [guid]::NewGuid().ToString('N'))
    [IO.Compression.ZipFile]::ExtractToDirectory($Zip, $tmp)
    $items = @(Get-ChildItem -LiteralPath $tmp -Force)
    $src = $tmp
    if ($items.Count -eq 1 -and $items[0].PSIsContainer) { $src = $items[0].FullName }
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Get-ChildItem -LiteralPath $src -Force | ForEach-Object { Move-Item -LiteralPath $_.FullName -Destination $Destination -Force }
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

function Test-Url {
    param([Parameter(Mandatory)] [string]$Url, [int]$TimeoutSec = 10)
    try {
        $r = Invoke-WebRequest -Uri $Url -Method Head -UseBasicParsing -TimeoutSec $TimeoutSec -MaximumRedirection 5
        return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 400)
    } catch {
        return $false
    }
}

function Get-FreePort {
    $l = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
    $l.Start()
    $p = $l.LocalEndpoint.Port
    $l.Stop()
    return $p
}

function Get-WarningList { return $script:Warnings }
