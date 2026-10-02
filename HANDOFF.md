# Verity: handoff for the next Claude Code session

Written 2026-10-02 at the end of a long session on the original Mac (folder `~/Desktop/mati`). Read all of this
before acting. It replaces the previous session's memory, which does not travel to another machine. The full build log
is `PROGRESS.md`; how to use the product is in `README.md`.

## 0. Where things stand

- **Verity** (v0.5.0) is an offline code-intelligence tool for C and C# codebases. It indexes a project and answers
  plain-language questions with checked facts, citing file:line.
- It is pilot-ready on macOS (Apple silicon) and fully built and tested there.
- **Windows:** first real run done on 2026-10-02 (Windows 11, RTX 5070, VS 2022 Build Tools): setup, pytest, every
  benchmark suite and the **desktop app, now ported to Windows**. The bugs it found are fixed (`PROGRESS.md`, "First
  real Windows run" and "Desktop app on Windows"). Still to do on Windows: a PC without a GPU / without Visual Studio
  for real (it was simulated), and the sandbox adapter. **Linux:** the scripts support it, but it has never been run.
- **Name:** the product was renamed to Verity on 2026-10-02 because the user found the earlier working name
  unprofessional. Every identifier, file, doc, icon and screenshot was changed and regression-tested (section 6).
  Never reintroduce the earlier name anywhere.
- **Next feature:** Phase 5, "learning from corrections". The spec is done; implementation has not started (section 7).

## 1. What Verity is

- **Engine:** Python (`verity/`).
  - It parses C with libclang (pip wheel `libclang==18.1.1`) and C# with a Roslyn worker (`workers/roslyn`, .NET 10).
  - It stores facts in SQLite and derives call and value-flow graphs.
  - It answers through rule-based recipes, then a deterministic verifier with 5 statuses, then a renderer.
- **Two answer modes:**
  - **Direct:** facts only, no model, under a second.
  - **Default:** a local model through llama.cpp `llama-server`, which re-ranks candidates and words the answer,
    plus meaning search (embeddings).
  - Model answers are checked for names and quotes.
- **Runtime scenarios:** "what happens when X" questions on C can be answered by compiling and running the real code
  in a sandbox (macOS `sandbox-exec`, Linux bubblewrap), with hardware replaced by logging stubs. This is not
  available on Windows yet.
- **Ways in:**
  - CLI: `bin/verity` (`bin\verity.cmd` on Windows).
  - Browser UI: `bin/verity ui`.
  - Desktop app (Tauri 2 + SolidJS): `Verity.app` on macOS, `desktop\target\release\Verity.exe` on Windows
    (`scripts\windows\build_desktop.cmd [-Shortcut]`, `run_desktop.cmd`), id `dev.verity.desktop`.
  - MCP stdio server: `bin/verity mcp`, tools `verity_index`, `verity_status`, `verity_ask`, `verity_search`,
    `verity_symbol`, `verity_source`.
- **Configuration:**
  - Environment variables: `VERITY_HOME` (data folder, default `data/`), `VERITY_MODELS`, `VERITY_TOOLS`,
    `VERITY_MODEL` (setup's model choice).
  - Optional per-project hints file: `verity.toml`.
  - The internal abbreviation used in generated code and scripts is `vr` (for example `Write-VrLog`, `__VR_ENTER`,
    `vr_harness.c`).
- **Design (generic by the user's explicit request):** hardware-independent; it detects, then assumes or sweeps, then
  labels, with optional hints. Never ask the user for chip, toolchain or Windows-edition specifics; they cannot
  provide them.
  - The model is picked from the hardware: C0 is Direct mode, C1 Qwen3-4B, C2 gpt-oss-20b (default), C3 gpt-oss-120b.
  - C dialect packs (MSVC, TI C2000, ...) are detected automatically.
  - Builds are reconstructed for code that does not build (fidelity levels F1-F4).

## 2. The user's standing rules (follow them)

1. **Product name:** the name is **Verity**. Never reintroduce the earlier name in code, docs, configs, comments,
   tests or file names.
2. **Company security policy:** never help bypass company security controls. The office Mac blocks USB disks
   (device-control policy), and Group Policy may enforce AllSigned PowerShell. Point the user to their IT team
   instead, and treat credentials as confidential.
3. **Changes must be regression-free and fully tested**, with before/after evidence (section 5). The user asks for
   this explicitly.
4. **Automate:** the user wants scripts that do the whole job on their own and log what they do as they go. Remove
   manual steps where possible.
5. **Time estimates:** the user often asks how much time is left. Give honest estimates up front and status lines
   during long runs.
6. **Hand audits:** audit answers by hand, tuned ones too. The benchmark grader checks names, not meaning, so it
   over-counts.
7. **Held-out discipline:**
   - Write a new project's questions from its source before running anything, and keep the first run.
   - Measure generalisation only on the next never-seen project.
8. **Docs:** the design documents are in `docs/development/`. They use the user's own single-file HTML design
   system (sidebar, chapters, Mermaid diagrams); keep its structure and CSS when editing. Each doc has a unique
   `data-doc` id: the design doc is d97, the one-pager d98. After editing, check that every Mermaid diagram still
   renders (open the file in a browser).
9. **Progress log:** record progress in `PROGRESS.md` (checklist plus dated log).

## 3. Repository map

```
verity/            engine (Python 3.12)
  agent/             router, recipes (question types), answer orchestration, rerank, evidence
  analysis/          derive (linking), graph (call + value-flow)
  extract/           recon (build reconstruction), c_clang, csharp (Roslyn adapter), crosscheck (cppcheck),
                     ts_fallback (tree-sitter), pool (workers), packs/ (dialects), shims/ (libc + win32 headers)
  retrieval/         symbol cards, hybrid keyword + meaning search
  runtime_exec/      sandboxed scenario harness (C)
  verify/ render/    verifier; answer rendering
  llm/runtime.py     llama-server lifecycle (orphan reaping by pid files in data/run)
  engine/            EngineCore (shared by CLI, HTTP daemon, MCP); server.py (JSON-RPC + SSE, token)
  probe.py           hardware/tool probe and model ladder; reads models/selection.json (Windows analysis)
  osutil.py          OS differences (Windows liveness without os.kill, msvcrt lock, memory, Xcode-stub guard)
  bench/run.py       known-answer benchmark runner
workers/roslyn/    C# worker (VerityRoslyn); build.sh restores (locked) and publishes to bin/
ui/                SolidJS UI (prebuilt in ui/dist); ui/scripts/e2e-cdp.mjs = end-to-end check + screenshots
desktop/           Tauri 2 shell (macOS); src-tauri/src/main.rs has a self-test mode
bin/               verity, verity.cmd, verity.ps1 launchers
scripts/           setup.sh, setup_toolchains.sh, download_models.sh, check_prereqs.sh, verify.sh, run_demos.sh,
                   build_ui.sh, build_desktop.sh, make_windows_bundle.sh, lib/models.sh (model pins + fit rule)
scripts/windows/   setup.cmd|ps1, analyze.cmd|analyze_system.ps1, check_prereqs.cmd|ps1, catalog.json (pins),
                   lib/ (common, detect, plan, install, preflight.cmd), tests/plan.tests.ps1, README.md
bench/             suites (suite, heldout, dialects, realworld*.json), gold/, results/ (history)
samples/           navgps-c, navgps-cs, msvc-telemetry, ti-motor-c2000 (ours); realworld/ (12 open-source projects)
tests/             pytest (14 test files + conftest.py, 164 tests)
docs/              ENGINE_API.md, FACT_SCHEMA.md, DEMOS.md + demos/, screenshots/ (25 images)
data/              settings.json, repos/<project-id>/ (indexes), logs/, run/   (not in the Windows zip)
models/, tools/, .venv/   downloaded/built by setup (not in the Windows zip)
```

## 4. Getting it running on this machine

The project lives in the Git repository `github.com/Muneeb926595/verity-mati` (branch `main`). The repository holds
the code, tests, samples, benchmark records, docs (including the design documents in `docs/development/`) and the
prebuilt UI (`ui/dist`, so no Node.js is needed). `.gitignore` leaves out everything setup downloads or builds:
models, toolchains (`tools/`), `.venv/`, the C# worker build (`workers/roslyn/bin`), indexes and settings (`data/`),
and new setup logs. Setup creates all of these inside the folder, with no admin rights.
`.gitattributes` turns off line-ending conversion, so a Windows checkout is byte-identical to the Mac (`.cmd` files
are CRLF). `bash scripts/make_windows_bundle.sh` still makes an equivalent zip if Git is not an option.

**Windows** (10 1809+ or 11, 64-bit; ARM needs 11):
1. Clone (or extract) to a short local path such as `C:\verity`, not in OneDrive.
2. `scripts\windows\check_prereqs.cmd` lists what is missing and installs nothing. Add `-Json` for
   machine-readable output.
3. `scripts\windows\setup.cmd`: 12 resumable, logged steps. It installs uv, then Python 3.12 and `.venv`, the
   llama.cpp build chosen for the GPU (CUDA/Vulkan/CPU, with fallback), .NET 10, the worker, cppcheck and the models.
   It ends with a verification step.
   - Options: `-Model auto|direct|C1|C2|C3`, `-StressTest`, `-QualityCheck`, `-Yes`. Logs go to `logs\setup-*.log`.
4. `scripts\windows\analyze.cmd [-StressTest -Apply]`: hardware, then the recommended model; it measures and
   writes `models\selection.json`.
   - Desktop app: `scripts\windows\build_desktop.cmd -Shortcut` (needs Rust and the Visual Studio C++ Build Tools,
     only for building), then the Desktop shortcut or `scripts\windows\run_desktop.cmd`.
5. Try it:
   - `bin\verity.cmd probe`
   - `bin\verity.cmd ask samples\navgps-c "Explain how GPS data travels through this application."`

**macOS / Linux:**
1. `bash scripts/check_prereqs.sh` lists what is missing. On a Mac, you only need uv or Python 3.12:
   `curl -LsSf https://astral.sh/uv/install.sh | sh`.
2. `bash scripts/setup.sh [--model auto|direct|C1|C2|C3]`. `auto` picks gpt-oss-20b when the memory budget is at
   least 15 GB, otherwise Qwen3-4B. The budget is 70% of memory on Apple silicon; elsewhere it is RAM, or VRAM plus
   30% of RAM.
3. `bin/verity probe`, then `bin/verity ask samples/navgps-c "..."`.

Models (pinned SHA256s are in `scripts/windows/catalog.json` and `scripts/lib/models.sh`):

| Rung | Model | Download | Needs | Status |
|---|---|---|---|---|
| embed | Qwen3-Embedding-0.6B Q8_0 | 0.64 GB | about 1 GB | used for meaning search |
| C1 | Qwen3-4B-Instruct-2507 Q4_K_M | 2.5 GB | about 4-5 GB | small/fast, weaker re-ranking |
| C2 | gpt-oss-20b MXFP4 | 12.1 GB | about 14 GB | default, fully benchmarked |
| C3 | gpt-oss-120b MXFP4 | 63.4 GB | about 66 GB | same family, not benchmarked yet |

Pinned tool versions:
- llama.cpp b11293;
- .NET SDK 10;
- cppcheck 2.22.0;
- uv 0.12.22;
- Node 20.19+ / 22.12+, needed only to rebuild the UI. Windows setup fetches a portable Node itself.

## 5. How to test (and what "unchanged" looks like)

- **Unit tests:** `.venv/bin/python -m pytest tests -q` (Windows: `.venv\Scripts\python -m pytest tests -q`).
  Always pass `tests`: a bare `pytest` collects cppcheck's own tests under `tools/`. Expect **170 tests** on the
  Mac (1 skipped where PowerShell is not installed); on Windows **152 passed, 18 skipped** (the sandbox-only ones).
  To simulate a Windows PC without Visual Studio, load a pytest plugin that sets
  `verity.extract.recon.windows_sdk_include_args = lambda: []` in `pytest_configure` (after conftest has set a private
  `VERITY_HOME`; importing `verity` earlier makes the tests write into the real `data/`).
- **Full regression (macOS):** `bash scripts/verify.sh [--llm] [--lwip]` runs pytest, every suite in Direct mode, then
  the demos. Note that it rewrites `docs/demos` in Direct mode; to restore the model-worded transcripts afterwards,
  run `bash scripts/run_demos.sh`.
- **One suite:** `.venv/bin/python -m verity.bench.run --suite bench/<suite>.json --out bench/results/<name>`.
  Add `--llm`, `--embed` or `--no-reindex` as needed.
- **Baseline: Direct mode, fresh index, 198/207**

  | Suite | Score |
  |---|---|
  | suite (navgps-c/-cs) | 31/31 |
  | heldout | 15/15 |
  | dialects (MSVC, TI C2000) | 17/17 |
  | realworld (minmea, FreeRTOS) | 14/14 |
  | libmodbus | 14/14 |
  | littlefs | 11/12 |
  | CANopenNode | 13/13 |
  | SOEM | 12/12 |
  | uC/OS-II | 14/14 |
  | Paho MQTT | 12/14 |
  | M2Mqtt (C#) | 11/12 |
  | nanopb | 9/12 |
  | NModbus (C#) | 10/12 |
  | lwIP | 15/15 |

- **Baseline: with gpt-oss-20b and meaning search, 95/99** on suite, heldout, dialects, M2Mqtt, nanopb and NModbus
  (31, 15, 17, 11, 11, 10). There were 0 invented names and 11 rejected claims in every run.
- **Results vary between runs. Compare pass/fail per question, not raw text.**
  - Even unchanged code differs in about 35 of 207 Direct answers between runs. The causes are the order of tied
    candidates, memory addresses (ASLR) in scenario output, and internal ids.
  - Model wording differs in about 20 of 99 answers.
  - Use a fresh `VERITY_HOME` for each compared run. A full re-index into an existing data folder keeps stale rows
    (section 8).
- **Before/after evidence for a change** (the tools used to prove the rename changed nothing):
  1. Before the change, record two baselines. Each run indexes from scratch in its own temporary data folder:
     - `python scripts/regress/capture.py --out ../caps/before-1`
     - `python scripts/regress/capture.py --out ../caps/before-2`

     Add `--mode llm-embed --suites ...` for model runs.
  2. Make the change, then capture `../caps/after` the same way.
  3. `python scripts/regress/compare.py ../caps/after ../caps/before-1 ../caps/before-2` (add `--text-only` for
     model runs). It reports pass/fail changes, index differences, and answers that differ beyond the normal
     run-to-run variation.
- **UI end-to-end:**
  1. Start the engine: `.venv/bin/python -m verity.engine.server --port 8765 --token testtoken`.
  2. Start Chrome: `--headless=new --remote-debugging-port=9222 --user-data-dir=<tmp>`.
  3. Run `node ui/scripts/e2e-cdp.mjs [--shots docs/screenshots]`. Expect **30/30**; it also writes screenshots
     01-15.

  Screenshots 16-18 come from `ui/scripts/extra-shots.mjs`; its header shows how to run it:
  - 16: the engine is stopped while the page is open ("Reconnecting...");
  - 17 and 18: a copy of navgps-c, with a line appended to `nav/navigation.c` after indexing ("1 file changed", and
    the source-viewer warning).
- **Desktop app on Windows:** `VERITY_DESKTOP_SELFTEST=quit` plus `VERITY_DESKTOP_SELFTEST_PROJECT=<folder>` on
  `Verity.exe` (it prints `SELFTEST {...}` lines when its stdout is redirected). The full UI check runs inside the app:
  start it with `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9223`, then
  `CDP=http://127.0.0.1:9223 CDP_ATTACH=1 VERITY_URL=<url in data\engine.json> node ui/scripts/e2e-cdp.mjs`. Check
  that closing the window, killing `Verity.exe` and killing the engine leave no `verity.engine.server` or
  `llama-server` process behind. Use a fresh `VERITY_HOME` with `{"llm_mode": "direct"}` while a model benchmark runs.
- **Desktop self-test (macOS):**
  - Basic run: `VERITY_DESKTOP_SELFTEST=quit VERITY_DESKTOP_SNAPSHOT_DIR=<dir> desktop/target/release/bundle/macos/Verity.app/Contents/MacOS/Verity`.
  - Add `VERITY_DESKTOP_SELFTEST_PROJECT=<abs path to samples/navgps-c>` for the answer screenshot.
  - Use `VERITY_ROOT=<folder with empty verity/ and .venv/>` for the engine-error screenshot.
  - It prints `SELFTEST {...}` lines.
- **Windows scripts:**
  - `scripts/windows/tests/plan.tests.ps1` (36 checks).
  - PSScriptAnalyzer with `PSUseCompatibleSyntax` (target 5.1) and `PSUseCompatibleCommands` (profile
    `win-8_x64_10.0.17763.0_5.1.17763.316_x64_4.0.30319.42000_framework`).
  - `check_prereqs.ps1 -AllowNonWindows -SpecsFile <made-up machine.json>` simulates a PC.
  - Keep `.ps1` files ASCII-only and `.cmd` files CRLF; a test enforces both.

## 6. What was done, in order

- **2026-09-30 Design:** the design doc v0.3 (generic, hardware-independent), with a Tauri UI section, then section 39
  "Implementation status & decision log" (D-impl-1..29).
- **2026-09-30 to 10-01, Phases 1-4:**
  - The whole system was built: engine, worker, UI, desktop app, CLI, MCP, benchmark and robustness tests.
  - It was validated on code not written by us: 12 real open-source projects (ten C, two C#, up to 173K lines).
  - Measured first-contact accuracy honestly:
    - Phase 3: 23/51;
    - Phase 4: 31/64 keyword-only;
    - final frozen round: 9/24 without the model, 14/24 with it.
  - Hand audit of tuned answers: grader 55; strict 48 before fixes; strict 57 (Direct) / 58 (model) after.
    The fixes are covered by `tests/test_audit.py`.
- **2026-10-01, Phase 5 spec:** the "learning from corrections" one-pager. Its decisions:
  - corrections live in a separate `learned.db` (`index.db` is rebuildable);
  - they only reorder or pin verified candidates;
  - they are disclosed, undoable, anchored, marked stale when the code changes, and kept per project;
  - do P5-0 (measure top-1 against top-3) first.
- **2026-10-02, USB investigation:** the Mac cannot mount USB disks because of a company device-control policy. The
  user was told to ask IT and was given no bypass.
- **2026-10-02, Windows support:**
  - `verity/osutil.py`: liveness check without `os.kill`, `msvcrt` locks, memory, taskkill/tasklist.
  - The probe reads `models/selection.json`; C3 was added to the ladder.
  - The setup and analyze scripts, with a stress test calibrated on the Mac. Each test answer is a re-rank plus a
    summary, three times; 15 s for gpt-oss-20b against a real median of 12 s. "Good" means 20 s or less;
    "usable" means 45 s or less, at least 8 tokens/s and 15% memory headroom.
  - The bundle script.
- **2026-10-02, prerequisites check:**
  - `check_prereqs` for both platforms (same JSON shape, `tests/test_prereqs.py`).
  - Mac setup made self-sufficient from a bare copy: the helpers moved into `scripts/`, llama.cpp is picked per
    platform, cppcheck is pinned and skipped cleanly without the Xcode tools, models are chosen by memory and
    SHA256-checked.
  - `osutil.mac_dev_tools()` stops the Xcode install pop-ups.
  - Group Policy pre-check in `lib\preflight.cmd`.
  - A simulated empty Mac ran setup end to end successfully.
- **2026-10-02, rename to Verity:**
  - The package, launchers, environment variables, hints file, MCP tools, C# worker, desktop app id, UI title,
    Windows scripts, docs, logs and benchmark records were all renamed, along with the internal `vr` prefix.
  - New icon and mark: a white check mark on the blue tile.
  - The 25 screenshots and the demo transcripts were regenerated, and the design docs updated.
  - Verified against a recorded baseline: pytest 164/164; Direct 198/207 identical per question; model run 95/99;
    UI 30/30; desktop self-test, CLI/MCP and the Windows checks all pass. Every remaining text difference was shown
    to be ordinary run-to-run variation.

## 7. Open items, roughly in priority order

1. **On the Mac, rebuild and self-test the desktop app** (`bash scripts/build_desktop.sh`, then the self-test in
   section 5): `main.rs` was changed on Windows, and the macOS target cannot be type-checked there. Also run pytest and
   a Direct suite there (the include fallback, CMake copy and libc shim changes run on every platform).
   Windows follow-ups: a PC without a GPU and one without Visual Studio for real (both were only simulated); a POSIX
   header pack for `sys/socket.h`, `unistd.h`, `termios.h` (POSIX-only code such as libmodbus is F3 on Windows);
   a CMake generator that exports compile commands on Windows (Ninja, inside a VS developer environment); a Windows
   installer for the desktop app (today a plain `Verity.exe` inside the folder, plus shortcuts).
2. **Phase 5, learning from corrections:** checklist P5-0..P5-6 at the end of `PROGRESS.md`, spec in the one-pager.
   Start with P5-0, measured on a NEW never-seen project (#10).
3. Windows sandbox adapter for runtime scenarios (C), and C# scenarios.
4. Linux: run `check_prereqs.sh` and `setup.sh` on a real Linux machine.
5. Benchmark gpt-oss-120b (C3) with `-QualityCheck` on a machine that can hold it.
6. Validation on a customer's industrial codebase. This has not been done yet.

## 8. Known issues (pre-existing, not caused by the rename)

- **Windows wording:** answers name `_InterlockedExchange()` (what the SDK's `InterlockedExchange` macro expands to)
  where the Mac says `InterlockedExchange()`. Left as it is: the name check only accepts indexed names.
- **Windows speed on POSIX code:** libmodbus indexes in about 75 s on Windows (6.5 s on the Mac): every file is parsed
  five times (default + 4 configuration switches) against the real Windows SDK headers.

- **Traceback in an answer:** in `bench/realworld_heldout9.json` case `nm-wait-negative`, the verification notes
  contain a Python traceback from an exception in `verity/agent/recipes.py` `Ctx.D` (`self.g.display(key)`).
- **Stale rows after re-index:** a full re-index into an existing data folder keeps rows from earlier runs: stored
  vectors, and on msvc-telemetry 244 symbols where a fresh index has 111. Benchmark answers were unaffected; fresh
  folders give the numbers above.
- Benchmark misses that remain are listed in `bench/results/<suite>/latest-direct.md`. Behavioural "what happens
  when" questions on unfamiliar domains are the weak spot.

## 9. Gotchas and lessons

- **Code text:** `_code()` strips semicolons, so regexes over code text must not require `;`.
- **Model servers:** never run two engines with the 20B model on a 24 GB machine at once. The second one falls back
  to Direct mode without saying so loudly.
- **Long runs:** runs over 2 hours exceed the background-task cap; launch them with `nohup`.
- **Benchmarks under load:** never run several captures, or a capture and pytest, at once: overloaded parser workers
  hit the 180 s per-file timeout and the indexes (and answers) change. One at a time, and a baseline must be a full
  copy of the install (a git worktree lacks the git-ignored C# worker in `workers/roslyn/bin`, so C# falls back).
- **Windows processes:** `.venv\Scripts\python.exe` is a launcher that starts the real interpreter as a child; kill
  the tree, not the launcher. A thread blocked reading a pipe stalls child-process start on Windows: poll instead.
- **PowerShell 5.1:**
  - keep scripts ASCII-only;
  - an if-expression that yields `@()` stores `$null`;
  - dot-sourcing a script that has a `param()` block clobbers the caller's variables;
  - for stderr from native tools, set `$ErrorActionPreference='Continue'`;
  - do not name a function `Write-Log` (it collides with a built-in);
  - quote `Start-Process` arguments.
- **Execution policy:** `-ExecutionPolicy Bypass` cannot override Group Policy; the launchers detect that and stop
  with a message.
- **macOS stubs:** without the Command Line Tools, `/usr/bin/clang`, `git` and `xcrun` are stubs that open an
  install dialog. Check `xcode-select -p` first.
- **Python worker processes:** the engine's workers re-import `__main__`, so any script that drives `EngineCore`
  needs an `if __name__ == "__main__":` guard.
- **Windows on ARM:** `cryptography` has no win_arm64 wheel, so setup uses x64 Python under emulation; llama.cpp
  stays native.

## 10. The original machine (for context)

- MacBook with Apple M3, 24 GB, macOS 26.2.
- The project folder was `~/Desktop/verity-mati` (earlier `~/Desktop/mati`), pushed to GitHub on 2026-10-02 so work
  could continue on a Windows PC.
- The design docs are in `docs/development/`: `LOCAL_CODE_INTELLIGENCE_SYSTEM_DESIGN.html` (d97) and
  `VERITY_LEARNING_FROM_CORRECTIONS_ONE_PAGER.html` (d98).

## 11. Suggested way to resume

Start the new session in this folder and say, for example: "Read HANDOFF.md and PROGRESS.md, then run the
prerequisites check and tell me what this PC needs." Then:
1. run `check_prereqs`;
2. run setup;
3. run pytest and a Direct suite (expect section 5's numbers);
4. report back with time estimates before long steps.
