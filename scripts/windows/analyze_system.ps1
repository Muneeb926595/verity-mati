<#
.SYNOPSIS
    Verity system analysis for Windows: reads this machine's hardware, recommends the llama.cpp build and the local AI
    model that fit it, and (optionally) stress-tests the candidates to pick the one that really performs here.

.DESCRIPTION
    Without switches it only reads the hardware and prints a recommendation (no downloads, about 10 seconds).
      -StressTest   downloads the candidate models and the llama.cpp build(s), then runs each candidate the way Verity
                    uses it (a ~3k-token re-rank request plus a summary, three times) and measures load time, prompt
                    and generation speed, answer time, peak memory and stability.
      -Apply        makes the result the model Verity uses (models\selection.json + tools\llama.cpp).
      -QualityCheck after setup: runs part of Verity's own benchmark with the chosen model and with facts only, so
                    you see answer quality and speed on this machine.
    Everything is logged to logs\system-analysis-<time>.log and summarised in .json and .md reports.

.EXAMPLE
    scripts\windows\analyze.cmd
.EXAMPLE
    scripts\windows\analyze.cmd -StressTest -Apply
.EXAMPLE
    scripts\windows\analyze.cmd -StressTest -Test C2,C3 -Yes
.EXAMPLE
    scripts\windows\analyze.cmd -QualityCheck
#>
[CmdletBinding()]
param(
    [string]$Root = '',
    [switch]$StressTest,
    [switch]$Apply,
    [switch]$QualityCheck,
    [switch]$Yes,
    [ValidateSet('', 'direct', 'C1', 'C2', 'C3')] [string]$Model = '',
    [ValidateSet('', 'cpu-x64', 'cpu-arm64', 'vulkan-x64', 'cuda-12.4-x64', 'cuda-13.4-x64')] [string]$Backend = '',
    [string[]]$Test = @(),
    [int]$Cycles = 3,
    [string]$LogFile = ''          # append to an existing log (used when setup.ps1 runs the stress test)
)

$ErrorActionPreference = 'Stop'
if (-not $Root) { $Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path }
. (Join-Path $PSScriptRoot 'lib\common.ps1')
. (Join-Path $PSScriptRoot 'lib\plan.ps1')
. (Join-Path $PSScriptRoot 'lib\install.ps1')
. (Join-Path $PSScriptRoot 'lib\detect.ps1')

# ------------------------------------------------------------------------------------------------ stress test
# Deterministic Verity-like facts text (~4 characters per token), different for each cycle so no prompt cache helps.
function New-FactsText {
    param([int]$Chars, [int]$Seed)
    $rnd = New-Object System.Random($Seed)
    $fns = 'pb_write', 'pb_read', 'decode_field', 'OSMemGet', 'OSQPost', 'keepalive', 'readPacket', 'ecx_pusherror', 'lfs_alloc', 'modbus_reply', 'UnicastMessage', 'CO_SYNC_receive'
    $conds = 'count > 0 && stream->callback != NULL', 'pmem->OSMemNFree > 0u', 'rem_len > (c->readbuf_size - len)', 'context->elist.head == context->elist.tail', 'wkc > 0', 'DLC == 0U', 'request.FunctionCode != response.FunctionCode', 'lfs->lookahead.ckpoint <= 0'
    $acts = 'returns through PB_RETURN_ERROR(stream, "stream full")', 'reports *perr = OS_ERR_MEM_NO_FREE_BLKS to its caller and returns NULL', 'records rc = BUFFER_OVERFLOW and jumps to exit', 'calls Logger.Warning() and Sleep(WaitToRetryMilliseconds)', 'sets SYNC->receiveError = DLC | 0x40U', 'returns LFS_ERR_NOSPC'
    $sb = New-Object System.Text.StringBuilder
    $i = 1
    while ($sb.Length -lt $Chars) {
        $f = $fns[$rnd.Next($fns.Count)]; $c = $conds[$rnd.Next($conds.Count)]; $a = $acts[$rnd.Next($acts.Count)]
        [void]$sb.AppendLine(("[{0}] {1}() checks ``{2}`` (src/{1}.c:{3}); when that is true it {4}. It is called by {5}() at line {6}, which then returns the error to its caller." -f $i, $f, $c, $rnd.Next(20, 3000), $a, $fns[$rnd.Next($fns.Count)], $rnd.Next(20, 3000)))
        $i++
    }
    return $sb.ToString()
}

function Invoke-ChatRequest {
    param([int]$Port, [string]$Key, [hashtable]$Body, [int]$TimeoutSec = 600)
    $json = $Body | ConvertTo-Json -Depth 12 -Compress
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $r = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/v1/chat/completions" -f $Port) -Method Post -ContentType 'application/json' `
        -Headers @{ Authorization = "Bearer $Key" } -Body ([Text.Encoding]::UTF8.GetBytes($json)) -TimeoutSec $TimeoutSec
    $sec = $sw.Elapsed.TotalSeconds
    $t = $r.timings
    $pp = if ($t -and $t.prompt_per_second) { [double]$t.prompt_per_second } else { 0 }
    $tg = if ($t -and $t.predicted_per_second) { [double]$t.predicted_per_second } else { 0 }
    if (-not $tg -and $r.usage -and $r.usage.completion_tokens) { $tg = [double]$r.usage.completion_tokens / [math]::Max(0.1, $sec) }
    return [pscustomobject]@{ Seconds = $sec; PromptTps = $pp; GenTps = $tg; PromptTokens = $(if ($r.usage) { $r.usage.prompt_tokens } else { 0 }); Text = [string]$r.choices[0].message.content }
}

function Measure-ModelRun {
    param([string]$Root, $Model, [string]$Build, [string]$BuildDir, [string[]]$ExtraArgs, [int]$Ctx, [int]$Cycles, $Targets, [double]$RamGB,
          [int]$FactsChars = 3000)      # ~1k tokens per request: on the 24 GB Mac this gives 15 s for gpt-oss-20b, real median 12 s
    $exe = Join-Path $BuildDir 'llama-server.exe'
    $mpath = [IO.Path]::Combine($Root, 'models', $Model.file)
    $port = Get-FreePort
    $key = [guid]::NewGuid().ToString('N')
    $ts = Get-Date -Format 'HHmmss'
    $log = [IO.Path]::Combine($Root, 'logs', ("stress-{0}-{1}-{2}.log" -f $Model.rung, $Build, $ts))
    $argList = @('-m', $mpath, '--host', '127.0.0.1', '--port', "$port", '--api-key', $key, '-c', "$Ctx", '-np', '1', '-ngl', '999', '--no-webui', '--jinja', '-fa', 'auto') + @($ExtraArgs)
    $res = [ordered]@{ Rung = $Model.rung; Label = $Model.label; Build = $Build; Args = @($ExtraArgs); Ctx = $Ctx; LoadS = $null; PromptTps = $null; GenTps = $null; AnswerS = $null; PeakGB = $null; VramGB = $null; Stable = $null; Verdict = 'failed'; Error = '' }
    Write-VrLog INFO ("Starting {0} on {1} {2}" -f $Model.label, $Build, (@($ExtraArgs) -join ' '))
    $p = Start-Process -FilePath $exe -ArgumentList (Join-ProcessArgs $argList) -WorkingDirectory $BuildDir -NoNewWindow -PassThru -RedirectStandardOutput $log -RedirectStandardError "$log.err"
    try {
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $limit = 90 + 4 * [double]$Model.resident_gb
        $ready = $false
        while ($sw.Elapsed.TotalSeconds -lt $limit) {
            if ($p.HasExited) { break }
            try {
                $h = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/health" -f $port) -TimeoutSec 2
                if ($h.status -eq 'ok') { $ready = $true; break }
            } catch { }
            Start-Sleep -Milliseconds 700
        }
        if (-not $ready) {
            $tail = ''
            foreach ($f in @("$log.err", $log)) { if (Test-Path -LiteralPath $f) { $tail += ((Get-Content -LiteralPath $f -Tail 6) -join ' | ') } }
            $res.Error = $(if ($p.HasExited) { "the server stopped while loading: $tail" } else { "not ready within $([int]$limit) s" })
            Write-VrLog WARN ("{0} on {1}: {2}" -f $Model.label, $Build, $res.Error)
            return [pscustomobject]$res
        }
        $res.LoadS = [math]::Round($sw.Elapsed.TotalSeconds, 1)
        Write-VrLog INFO ("  loaded in {0} s; warming up" -f $res.LoadS)
        $reason = [bool]$Model.reasoning
        $base = @{ temperature = 0.1; top_p = 0.9; seed = 7; cache_prompt = $true }
        $warm = $base.Clone(); $warm.messages = @(@{ role = 'user'; content = 'Say ok.' }); $warm.max_tokens = 8
        if ($reason) { $warm.reasoning_effort = 'low'; $warm.chat_template_kwargs = @{ reasoning_effort = 'low' } }
        [void](Invoke-ChatRequest -Port $port -Key $key -Body $warm -TimeoutSec 300)
        $schema = @{ type = 'object'; properties = @{ choice = @{ type = 'integer' }; why = @{ type = 'string' } }; required = @('choice', 'why'); additionalProperties = $false }
        $cyc = New-Object System.Collections.Generic.List[object]
        $maxWs = 0
        for ($k = 1; $k -le $Cycles; $k++) {
            $facts = New-FactsText -Chars $FactsChars -Seed (1000 * $k + [int][char]$Model.rung[1])
            $rr = $base.Clone()
            $rr.messages = @(@{ role = 'system'; content = 'You choose which numbered candidate check answers the question. Reply with JSON only.' },
                             @{ role = 'user'; content = "Question: What happens when the error list is full?`n`nCandidates:`n$facts" })
            $rr.max_tokens = 200
            $rr.response_format = @{ type = 'json_schema'; json_schema = @{ name = 'answer'; schema = $schema; strict = $true } }
            if ($reason) { $rr.reasoning_effort = 'low'; $rr.chat_template_kwargs = @{ reasoning_effort = 'low' } }
            $a = Invoke-ChatRequest -Port $port -Key $key -Body $rr
            $su = $base.Clone()
            $su.messages = @(@{ role = 'system'; content = 'Summarise the checked facts in at most three sentences. Do not add anything they do not say.' },
                             @{ role = 'user'; content = "Checked facts:`n$facts" })
            $su.max_tokens = 220
            if ($reason) { $su.reasoning_effort = 'low'; $su.chat_template_kwargs = @{ reasoning_effort = 'low' } }
            $b = Invoke-ChatRequest -Port $port -Key $key -Body $su
            $p.Refresh()
            if ($p.WorkingSet64 -gt $maxWs) { $maxWs = $p.WorkingSet64 }
            $one = [pscustomobject]@{ AnswerS = $a.Seconds + $b.Seconds; PromptTps = ($a.PromptTps + $b.PromptTps) / 2; GenTps = ($a.GenTps + $b.GenTps) / 2 }
            $cyc.Add($one)
            Write-VrLog INFO ("  cycle {0}/{1}: answer {2:N1} s (prompt {3:N0} tok/s, generation {4:N1} tok/s, {5} prompt tokens)" -f $k, $Cycles, $one.AnswerS, $one.PromptTps, $one.GenTps, $a.PromptTokens)
        }
        $p.Refresh()
        $sorted = @($cyc | Sort-Object AnswerS)
        $mid = $sorted[[int][math]::Floor($sorted.Count / 2)]
        $res.AnswerS = [math]::Round($mid.AnswerS, 1)
        $res.PromptTps = [math]::Round(($cyc | Measure-Object PromptTps -Average).Average, 0)
        $res.GenTps = [math]::Round(($cyc | Measure-Object GenTps -Average).Average, 1)
        $peak = [math]::Max([double]$p.PeakWorkingSet64, [double]$maxWs)       # Peak is 0 where the OS does not report it
        $res.PeakGB = [math]::Round($peak / 1GB, 1)
        $smi = Find-NvidiaSmi
        if ($smi -and $Build -like 'cuda*') {
            $used = (Get-NativeOutput $smi @('--query-gpu=memory.used', '--format=csv,noheader,nounits')) -split "`n" | Select-Object -First 1
            if ($used -match '^\s*\d+') { $res.VramGB = [math]::Round([double]$used / 1024, 1) }
        }
        $first = $cyc[0].GenTps; $last = $cyc[$cyc.Count - 1].GenTps
        $res.Stable = (-not ($first -gt 0 -and $last -lt 0.75 * $first))
        $memOk = $res.PeakGB -le (1 - [double]$Targets.memory_headroom) * $RamGB
        if ($res.AnswerS -le [double]$Targets.good_answer_s -and $res.GenTps -ge [double]$Targets.min_gen_tps -and $memOk) { $res.Verdict = 'good' }
        elseif ($res.AnswerS -le [double]$Targets.max_answer_s -and $res.GenTps -ge [double]$Targets.min_gen_tps -and $memOk) { $res.Verdict = 'usable' }
        else { $res.Verdict = 'too slow' }
        if (-not $memOk) { $res.Error = ("peak memory {0} GB leaves too little for Windows" -f $res.PeakGB) }
        if (-not $res.Stable) { $res.Error = (($res.Error + ' speed dropped over the run (heat or memory pressure)').Trim()) }
        Write-VrLog OK ("  {0}: median answer {1} s, generation {2} tok/s, peak memory {3} GB -> {4}" -f $Model.label, $res.AnswerS, $res.GenTps, $res.PeakGB, $res.Verdict.ToUpper())
    } catch {
        $res.Error = $_.Exception.Message
        Write-VrLog WARN ("{0} on {1} failed: {2}" -f $Model.label, $Build, $res.Error)
    } finally {
        if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; $p.WaitForExit(15000) | Out-Null }
    }
    return [pscustomobject]$res
}

function Measure-Embeddings {
    param([string]$Root, $Embed, [string]$BuildDir, [string[]]$ExtraArgs)
    $exe = Join-Path $BuildDir 'llama-server.exe'
    $port = Get-FreePort; $key = [guid]::NewGuid().ToString('N')
    $log = [IO.Path]::Combine($Root, 'logs', ("stress-embed-{0}.log" -f (Get-Date -Format 'HHmmss')))
    $argList = @('-m', [IO.Path]::Combine($Root, 'models', $Embed.file), '--host', '127.0.0.1', '--port', "$port", '--api-key', $key, '-c', '8192', '-np', '2', '-ngl', '999', '--no-webui', '--embedding', '--pooling', 'last', '-ub', '2048', '-b', '2048') + @($ExtraArgs)
    $p = Start-Process -FilePath $exe -ArgumentList (Join-ProcessArgs $argList) -WorkingDirectory $BuildDir -NoNewWindow -PassThru -RedirectStandardOutput $log -RedirectStandardError "$log.err"
    try {
        $sw = [Diagnostics.Stopwatch]::StartNew()
        while ($sw.Elapsed.TotalSeconds -lt 90 -and -not $p.HasExited) {
            try { if ((Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/health" -f $port) -TimeoutSec 2).status -eq 'ok') { break } } catch { }
            Start-Sleep -Milliseconds 500
        }
        $texts = 1..32 | ForEach-Object { "function_$_ checks a buffer length against its capacity and returns an error code when the frame is too long or the checksum does not match." }
        $sw = [Diagnostics.Stopwatch]::StartNew()
        foreach ($i in 1..2) {
            $body = @{ input = $texts; model = 'embed' } | ConvertTo-Json -Compress
            [void](Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/v1/embeddings" -f $port) -Method Post -ContentType 'application/json' -Headers @{ Authorization = "Bearer $key" } -Body $body -TimeoutSec 300)
        }
        $rate = [math]::Round(64 / $sw.Elapsed.TotalSeconds, 1)
        Write-VrLog OK ("Embeddings: {0} texts per second (indexing a 50k-line project needs roughly {1:N0} min of embedding)" -f $rate, (6000 / [math]::Max(0.1, $rate) / 60))
        return $rate
    } catch {
        Write-VrLog WARN ("Embedding test failed: {0}" -f $_.Exception.Message)
        return $null
    } finally {
        if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    }
}

function Invoke-StressTest {
    param($Root, $Catalog, $Specs, $Plan, [string[]]$Rungs, [switch]$Yes, [int]$Cycles)
    Write-VrLog STEP ''
    Write-VrLog STEP '==== Stress test: running each candidate the way Verity uses it ===='
    # builds: the preferred one, plus the integrated-GPU alternative when there is one
    $builds = @($Plan.Backend.Build)
    $alt = $Plan.Backends | Where-Object { $_.Kind -eq 'igpu' } | Select-Object -First 1
    if ($alt -and $builds -notcontains $alt.Build) { $builds += $alt.Build }
    $buildDirs = @{}
    foreach ($b in $builds) {
        $dir = Join-Path $Root ("tools\llama.cpp-builds\" + $b)
        try {
            [void](Install-LlamaBuild -Root $Root -Catalog $Catalog -Build $b -Destination $dir)
            $buildDirs[$b] = $dir
            $dev = Get-LlamaDevices -Directory $dir
            Write-VrLog INFO ("{0} sees: {1}" -f $b, $(if ($dev.Count) { $dev -join '; ' } else { 'CPU only' }))
        } catch {
            Write-VrLog WARN ("{0}: {1}" -f $b, $_.Exception.Message)
        }
    }
    if ($buildDirs.Count -eq 0 -and $Plan.Backend.Build -ne 'cpu-x64' -and $Specs.Arch -eq 'x64') {
        $dir = Join-Path $Root 'tools\llama.cpp-builds\cpu-x64'
        [void](Install-LlamaBuild -Root $Root -Catalog $Catalog -Build 'cpu-x64' -Destination $dir)
        $buildDirs['cpu-x64'] = $dir; $builds = @('cpu-x64')
    }
    # candidates: every model that may fit (largest first), or the rungs asked for
    $cands = if ($Rungs.Count) { @($Plan.Models | Where-Object { $Rungs -contains $_.Rung }) } else { @($Plan.Models | Where-Object { $_.Placement.Status -ne 'too large' }) }
    $cands = @($cands | Select-Object -First 3)
    if ($cands.Count -eq 0) { Write-VrLog WARN 'No model fits this machine: Verity will run in Direct mode.'; return @() }
    foreach ($c in $cands) {
        $path = Join-Path $Root ("models\" + $c.File)
        if (Test-Path -LiteralPath $path) { continue }
        $gb = $c.Bytes / 1GB
        $free = (Get-CimInstance Win32_LogicalDisk -Filter ("DeviceID='{0}'" -f (Split-Path -Qualifier $Root))).FreeSpace / 1GB
        if ($gb + 3 -gt $free) { Write-VrLog WARN ("Skipping {0}: needs {1:N0} GB, only {2:N0} GB free" -f $c.Label, $gb, $free); continue }
        if ($gb -gt 20 -and -not $Yes) {
            $ans = Read-Host ("Download {0} ({1:N0} GB) to test it? [y/N]" -f $c.Label, $gb)
            if ($ans -notmatch '^(y|yes)$') { Write-VrLog INFO ("Skipped {0}" -f $c.Label); continue }
        }
        [void](Install-ModelFile -Root $Root -Model $c.Model)
    }
    $embed = $Catalog.models | Where-Object { $_.role -eq 'embed' } | Select-Object -First 1
    $results = New-Object System.Collections.Generic.List[object]
    foreach ($c in $cands) {
        if (-not (Test-Path -LiteralPath (Join-Path $Root ("models\" + $c.File)))) { continue }
        foreach ($b in $builds) {
            if (-not $buildDirs.ContainsKey($b)) { continue }
            $bk = $Plan.Backends | Where-Object { $_.Build -eq $b } | Select-Object -First 1
            if (-not $bk) { $bk = $Plan.Backend }
            $pl = Get-Placement -Specs $Specs -Model $c.Model -Backend $bk -EmbedGB ([double]$embed.resident_gb)
            $variants = New-Object System.Collections.Generic.List[object]
            $variants.Add(@($pl.Args))
            if ($pl.Status -eq 'gpu' -and [bool]$c.Model.moe) { $variants.Add(@('--n-cpu-moe', [string][math]::Ceiling([int]$c.Model.layers / 3))) }
            if ($pl.Status -eq 'hybrid') {
                $n = [int]$pl.Args[1]; $layers = [int]$c.Model.layers
                $variants.Add(@('--n-cpu-moe', [string]([math]::Min($layers, $n + 4))))
                $variants.Add(@('--n-cpu-moe', [string]$layers))
            }
            $ctx = [int]$pl.Ctx
            foreach ($v in $variants) {
                $r = Measure-ModelRun -Root $Root -Model $c.Model -Build $b -BuildDir $buildDirs[$b] -ExtraArgs $v -Ctx $ctx -Cycles $Cycles -Targets $Catalog.targets -RamGB $Specs.RamGB
                $results.Add($r)
                if ($r.Verdict -eq 'good' -and $pl.Status -ne 'hybrid') { break }          # first full-GPU/CPU success is enough
                if ($r.Verdict -ne 'failed' -and $pl.Status -eq 'gpu') { break }
            }
        }
    }
    if ($embed -and (Test-Path -LiteralPath (Join-Path $Root ("models\" + $embed.file)))) {
        $eb = $builds[0]
        if ($buildDirs.ContainsKey($eb)) { [void](Measure-Embeddings -Root $Root -Embed $embed -BuildDir $buildDirs[$eb] -ExtraArgs @()) }
    }
    return $results
}

function Select-Winner {
    param($Results)
    $ok = @($Results | Where-Object { $_.Verdict -eq 'good' -or $_.Verdict -eq 'usable' })
    if ($ok.Count -eq 0) { return $null }
    # highest rung that is 'good'; otherwise the highest 'usable'; ties go to the faster run
    $good = @($ok | Where-Object { $_.Verdict -eq 'good' } | Sort-Object @{ Expression = { $_.Rung }; Descending = $true }, @{ Expression = { $_.AnswerS }; Ascending = $true })
    if ($good.Count) { return $good[0] }
    return @($ok | Sort-Object @{ Expression = { $_.Rung }; Descending = $true }, @{ Expression = { $_.AnswerS }; Ascending = $true })[0]
}

# ------------------------------------------------------------------------------------------------ quality check
function Invoke-QualityCheck {
    param($Root)
    $py = Join-Path $Root '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $py)) { throw 'Verity is not set up yet (no .venv). Run scripts\windows\setup.cmd first.' }
    $env:PYTHONUTF8 = '1'; $env:PYTHONIOENCODING = 'utf-8'
    $ids = 'c-explain-process,c-flow-gps,c-origin-lat,c-where-heading,c-impact-calc,c-callers-calc,c-macro-timeout,c-trap-nonexistent,c-dead-reckoning,cs-callers-process,cs-writers-heading,cs-event-chain'
    $ts = Get-Date -Format 'yyyyMMdd-HHmmss'
    $runs = @(
        @{ Name = 'facts only, sample projects'; Suite = 'bench\suite.json'; Only = $ids; Llm = $false },
        @{ Name = 'local model, sample projects'; Suite = 'bench\suite.json'; Only = $ids; Llm = $true },
        @{ Name = 'facts only, nanopb (behavioural)'; Suite = 'bench\realworld_heldout8.json'; Only = ''; Llm = $false },
        @{ Name = 'local model, nanopb (behavioural)'; Suite = 'bench\realworld_heldout8.json'; Only = ''; Llm = $true }
    )
    $rows = New-Object System.Collections.Generic.List[object]
    foreach ($r in $runs) {
        $out = Join-Path $Root ("logs\quality-{0}\{1}" -f $ts, ($r.Name -replace '[^a-z0-9]+', '-'))
        $a = @('-m', 'verity.bench.run', '--suite', $r.Suite, '--out', $out)
        if ($r.Only) { $a += @('--only', $r.Only) }
        if ($r.Llm) { $a += '--llm' }
        Write-VrLog STEP ("Quality check: {0}" -f $r.Name)
        [void](Invoke-Native -FilePath $py -ArgumentList $a -WorkingDirectory $Root -AllowFail)
        $j = Get-ChildItem -LiteralPath $out -Filter 'latest-*.json' -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($j) {
            $s = (Get-Content -LiteralPath $j.FullName -Raw -Encoding UTF8 | ConvertFrom-Json).summary
            $rows.Add([pscustomobject]@{ Run = $r.Name; Passed = ("{0}/{1}" -f $s.passed, $s.cases); Seconds = $s.mean_seconds
                                         Invented = $s.hallucinated_names_in_answers.cases })
        } else {
            $rows.Add([pscustomobject]@{ Run = $r.Name; Passed = 'error'; Seconds = $null; Invented = $null })
        }
    }
    Write-VrLog STEP ''
    Write-VrLog STEP 'Quality check results (automatic grader; reference on the 24 GB Mac: all sample questions pass, nanopb 9/12 facts only and 11/12 with the model)'
    foreach ($x in $rows) {
        Write-VrLog DATA ("  {0,-36} {1,-7} {2,-22} invented code names: {3}" -f $x.Run, $x.Passed, $(if ($x.Seconds) { ("{0:N1} s per question" -f [double]$x.Seconds) } else { '' }), $x.Invented)
    }
    return $rows
}

# ------------------------------------------------------------------------------------------------ main
function Invoke-Analysis {
    if ($LogFile) { $script:LogFile = $LogFile } else { [void](Initialize-Log -Root $Root -Name 'system-analysis') }
    $catalog = Read-Catalog -Root $Root
    if ($QualityCheck) { [void](Invoke-QualityCheck -Root $Root); return }
    Write-VrLog STEP '==== This machine ===='
    $specs = Get-SystemSpecs -Root $Root
    Show-Specs $specs
    $plan = Get-SetupPlan -Specs $specs -Catalog $catalog -ForceRung $Model -ForceBuild $Backend
    Show-Plan $plan
    $results = $null; $winner = $null
    if ($StressTest) {
        $results = @(Invoke-StressTest -Root $Root -Catalog $catalog -Specs $specs -Plan $plan -Rungs $Test -Yes:$Yes -Cycles $Cycles)
        Write-VrLog STEP ''
        Write-VrLog STEP 'Stress test results'
        foreach ($x in $results) {
            Write-VrLog DATA ("  {0,-30} {1,-13} {2,-16} load {3,5} s  prompt {4,6} tok/s  gen {5,5} tok/s  answer {6,6} s  peak {7,5} GB  {8} {9}" -f $x.Label, $x.Build, (@($x.Args) -join ' '), $x.LoadS, $x.PromptTps, $x.GenTps, $x.AnswerS, $x.PeakGB, $x.Verdict.ToUpper(), $x.Error)
        }
        $w = Select-Winner $results
        if ($w) {
            $winner = ("{0} on {1} {2} - median answer {3} s, {4} tok/s" -f $w.Label, $w.Build, (@($w.Args) -join ' '), $w.AnswerS, $w.GenTps)
            Write-VrLog OK ("Best model for this machine: {0}" -f $winner)
            $pick = $plan.Models | Where-Object { $_.Rung -eq $w.Rung } | Select-Object -First 1
            $plan.Pick = $pick; $plan.Mode = 'auto'
            $bk = $plan.Backends | Where-Object { $_.Build -eq $w.Build } | Select-Object -First 1
            if ($bk) { $plan.Backend = $bk }
            if ($Apply) {
                $sel = New-SelectionObject -Plan $plan -Measured $w -Source 'analyze_system.ps1 -StressTest (measured)'
                $dst = Join-Path $Root 'tools\llama.cpp'
                [void](Install-LlamaBuild -Root $Root -Catalog $catalog -Build $w.Build -Destination $dst)
                Write-Selection -Root $Root -Selection $sel
            }
        } else {
            Write-VrLog WARN 'No candidate met the speed and memory targets: Direct mode (facts only) is the best choice here.'
            if ($Apply) {
                $plan.Pick = $null; $plan.Mode = 'direct'
                Write-Selection -Root $Root -Selection (New-SelectionObject -Plan $plan -Source 'analyze_system.ps1 -StressTest (measured)')
            }
        }
    } elseif ($Apply) {
        Write-Selection -Root $Root -Selection (New-SelectionObject -Plan $plan)
    }
    Save-Report -Root $Root -Specs $specs -Plan $plan -Results $results -Winner $winner
    if (-not $StressTest) {
        Write-VrLog INFO 'Next: run with -StressTest to measure the candidates on this machine, or scripts\windows\setup.cmd to install everything.'
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        Invoke-Analysis
    } catch {
        Write-VrLog FAIL $_.Exception.Message
        Write-VrLog FAIL ("Details are in {0}" -f $script:LogFile)
        exit 1
    }
}
