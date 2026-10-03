# Verity on Windows

Two scripts do the whole setup. Neither needs administrator rights, and everything goes inside the Verity folder
(`tools\`, `models\`, `.venv\`), so deleting the folder removes it all.

## Quick start

1. Get the code onto the PC, in a short local path that is not in OneDrive:
   - with Git (recommended): `git clone https://github.com/Muneeb926595/verity-mati.git C:\verity`; or
   - with a zip: on the Mac, `bash scripts/make_windows_bundle.sh` makes `~/Desktop/verity-windows-<date>.zip`
     (about 65 MB); extract it on the PC (`C:\` gives `C:\verity`).
2. Neither includes models, toolchains or the Python environment; setup downloads them. A Git clone also has no
   prebuilt C# worker, so setup builds it (about a minute).
3. Optional, first: double-click `C:\verity\scripts\windows\check_prereqs.cmd` to see what this PC still needs (see below).
4. Double-click `C:\verity\scripts\windows\setup.cmd`. Everything is logged on screen and in `logs\setup-<time>.log`.

To see what the hardware can run before installing anything, double-click `analyze.cmd`.

## check_prereqs.cmd: what this PC needs

It installs and changes nothing, and runs in a few seconds. It lists five groups, each item marked OK, DONE, TODO,
WARN, MISSING, `--` (optional, not present) or `no` (not needed):

| Group | What it checks |
|---|---|
| 1. This machine | Windows 10 1809+ or 11, 64-bit (ARM needs Windows 11); memory and the model it can run; disk space for what is still missing; whether the download sites are reachable; battery |
| 2. Must be true before setup | PowerShell 5.1; that Group Policy allows unsigned scripts; a writable folder with a short path, not in OneDrive; complete files; curl.exe and msiexec.exe |
| 3. Installed by setup on its own | uv, Python and `.venv`, the llama.cpp build for this GPU, .NET SDK 10, the C# worker, cppcheck, the UI, the models. Each is DONE or TODO, with sizes |
| 4. Optional extras | GPU and driver (CUDA 12.4+ for the CUDA builds), Windows SDK headers, long paths |
| 5. NOT needed | Ollama, Docker/WSL, an installed Python, Visual Studio, Node.js, git, cloud accounts, administrator rights |

On a normal Windows 10/11 PC nothing needs installing by hand. Only these block setup: no internet, too little
disk, a Group Policy that only allows signed scripts, or a folder you cannot write to. Each comes with what to do.

Options: `-Model auto|direct|C1|C2|C3` (the same choice as setup), `-Json` (machine-readable), `-Offline`.
The exit code is 0 when nothing blocks setup and 1 otherwise. macOS/Linux have the same check:
`bash scripts/check_prereqs.sh`.

All three `.cmd` files first remove the "downloaded from the internet" mark from the scripts. If Group Policy sets
the script policy to AllSigned or Restricted, they stop with a clear message: ask IT to allow the scripts. The
scripts do not try to get around such a policy.

## setup.cmd: what it does

| # | Step | What happens | If it fails |
|---|---|---|---|
| 1 | Preflight | Checks Windows, PowerShell, the folder, internet (GitHub, Hugging Face, PyPI, Microsoft) and removes the "downloaded" mark from the copied files | Stops and says why |
| 2 | System analysis | Reads CPU, memory, GPU (NVIDIA through `nvidia-smi`), disk and power; picks the llama.cpp build and the model | — |
| 3 | uv | Pinned `uv` 0.12.22, SHA256-checked | Stops |
| 4 | Python | Python 3.12 (managed by uv), `.venv`, the pinned packages; checks that libclang loads. Windows on ARM uses x64 Python under emulation | Stops |
| 5 | llama.cpp | Build b11293: CUDA 13.4 / 12.4 (NVIDIA, chosen from the driver), Vulkan (AMD, Intel, older NVIDIA drivers) or CPU. Confirms the GPU is visible | Tries the next build, ending with CPU |
| 6 | .NET SDK 10 | Microsoft's `dotnet-install.ps1` (signature checked) into `tools\dotnet` | Stops |
| 7 | C# worker | Uses the prebuilt worker if it runs; otherwise restores (locked) and publishes it | Stops |
| 8 | cppcheck | 2.22.0 MSI unpacked into `tools\cppcheck` (nothing installed system-wide) | Warns and continues: it is an optional cross-check |
| 9 | UI | Uses `ui\dist` when present and up to date; when it is missing or older than `ui\src` (after a pull), portable Node.js 22 builds it | Stops |
| 10 | Models | The embedding model and the chosen chat model, resumable, SHA256-checked; writes `models\selection.json` | Stops (disk space is checked first) |
| 11 | Stress test | Only with `-StressTest` (see below) | Warns, keeps the estimate |
| 12 | Verification | `verity probe`, the test suite, a facts-only answer, a model answer; `-QualityCheck` adds a benchmark run | Reported in the summary |

Re-running is safe: finished steps are detected and skipped, and partial downloads resume.

Options (pass them to `setup.cmd`):

| Option | Effect |
|---|---|
| `-Model auto\|direct\|C1\|C2\|C3` | `auto` picks from the hardware; `direct` installs no AI model |
| `-Backend auto\|cpu-x64\|vulkan-x64\|cuda-12.4-x64\|cuda-13.4-x64\|cpu-arm64` | Force a llama.cpp build |
| `-StressTest` | Measure the candidate models and keep the best |
| `-QualityCheck` | Run part of Verity's benchmark with the chosen model (about 10 minutes) |
| `-Yes` | Do not ask before downloads over 20 GB |
| `-SkipModels`, `-SkipTests`, `-SkipCppcheck` | Leave those parts out |

## analyze.cmd: which model for which machine

`analyze.cmd` on its own reads the hardware and prints, in about 10 seconds:
- the llama.cpp build with its fallbacks;
- every model, and where it would run: all on the GPU, split between GPU and CPU (the expert weights of a
  mixture-of-experts model stay in system memory), on the CPU, tight, or too large;
- the recommended model.

| Rung | Model | Download | Needs (with Verity's 16k context) | Notes |
|---|---|---|---|---|
| C0 | none (Direct mode) | — | — | Answers built from checked facts only |
| C1 | Qwen3-4B-Instruct-2507 Q4_K_M | 2.5 GB | about 5 GB (3.9 GB at 8k context) | Small and fast; weaker at choosing the right check |
| C2 | gpt-oss-20b MXFP4 | 12.1 GB | about 14 GB | Verity's default, fully benchmarked |
| C3 | gpt-oss-120b MXFP4 | 63.4 GB | about 66 GB (GPU + system memory) | Same family as C2; not yet benchmarked with Verity |

`analyze.cmd -StressTest` downloads the candidates (it asks first for anything over 20 GB) and runs each one the way
Verity uses it. Each test answer is a re-rank request plus a summary, about 1k tokens each, repeated three times. On
the 24 GB Mac this gives 15 s for gpt-oss-20b, where real Verity answers have a median of 12 s.

It reports:
- load time;
- prompt and generation speed (tokens per second);
- median answer time;
- peak memory;
- whether the speed held up over the run.

Verdicts: **good** means 20 s or less per answer; **usable** means 45 s or less, at least 8 tokens per second, and
15% of memory left for Windows. The highest rung that is good wins; otherwise the highest usable one; otherwise
Direct mode. With a GPU it also tries GPU+CPU splits and keeps the fastest that works.

Add `-Apply` to make the winner the model Verity uses. The choice goes to `models\selection.json`, with any
`llama-server` options it needs (for example `--n-cpu-moe 25`), and the winning build to `tools\llama.cpp`.

`analyze.cmd -QualityCheck` runs 12 questions on the sample projects and nanopb's 12 behavioural questions, each
facts-only and with the model. It prints the pass rate, seconds per question and invented code names, next to the
Mac reference.

Reports go to `logs\system-analysis-<time>.md` and `.json`.

## After setup

```
scripts\windows\run_desktop.cmd   the desktop app (builds it the first time, see below)
bin\verity.cmd ask samples\navgps-c "Explain how GPS data travels through this application."
bin\verity.cmd ask C:\code\your-project "What happens when the CRC check fails?"
bin\verity.cmd ui       the same app in your browser
bin\verity.cmd mcp      MCP server for AI assistants
```

## The desktop app (build_desktop.cmd, run_desktop.cmd)

The Verity window on Windows is the same Tauri 2 app as on the Mac: it starts this folder's engine, shows the UI in
its own window with the native Windows folder picker (**Open Project**), and stops the engine when you close it.

```
scripts\windows\build_desktop.cmd             builds desktop\target\release\Verity.exe (logs\desktop-build-*.log)
scripts\windows\build_desktop.cmd -Shortcut   ... and adds "Verity" to the Desktop and the Start menu
scripts\windows\run_desktop.cmd               starts the app (builds it first when it is not built yet)
```

- **Needs, for building only:** Rust (rustup, MSVC toolchain), the Visual Studio C++ Build Tools ("Desktop
  development with C++"; on a managed PC usually installed by IT), WebView2 (part of Windows 11) and Node.js
  (setup's `tools\node`). The script checks each one and says what is missing. The first build downloads the Rust
  crates and the Tauri CLI and takes a few minutes; later builds take under a minute. The finished app is offline.
- **After a `git pull` or a branch checkout:** run `build_desktop.cmd` again. It rebuilds `ui\dist` when any UI source
  is newer (`-BuildUI` forces it), so the app never shows an old UI.
- **Keep `Verity.exe` inside the Verity folder** (where the build puts it): it finds the engine by walking up from its
  own location. A shortcut can live anywhere. `VERITY_ROOT` overrides the folder.
- **Stopping:** closing the window, or the app being killed (Task Manager, a crash), stops the engine and any model
  server it started within a few seconds: the engine watches the pipe the app holds open
  (`VERITY_STOP_ON_STDIN_EOF`). No console windows appear.
- **If the engine stops unexpectedly**, the window shows the reason and **Try again**. App and engine messages go to
  `data\logs\desktop-engine.log`.
- **Self-test:** `set VERITY_DESKTOP_SELFTEST=quit` and `set VERITY_DESKTOP_SELFTEST_PROJECT=<folder>`, then start
  `Verity.exe`; it opens the project, asks a question, prints `SELFTEST {...}` lines and quits. The full browser check
  can also drive the app's own window: start it with
  `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222`, then run
  `node ui\scripts\e2e-cdp.mjs` with `CDP_ATTACH=1` and `VERITY_URL` set to the `url` in `data\engine.json`.

## Not on Windows yet

- **Running code for "what happens when" questions:** there is no Windows sandbox adapter, so those questions are
  answered by reading the code (marked as such). The tests that need a sandbox are skipped.
- **An installer for the desktop app:** `build_desktop.cmd` makes a plain `Verity.exe` inside this folder (plus
  optional shortcuts), not an MSI/NSIS installer, and setup does not build the app on its own.

**First real run (2026-10-02, Windows 11, RTX 5070, VS 2022 Build Tools):** setup, the system analysis, pytest
(152 passed, 18 skipped: sandbox-only), every benchmark suite and the desktop app were run on a real PC; the
findings and fixes are in `PROGRESS.md` (Windows section). Phase 5 (learning from corrections, 3 Oct 2026) has not
been run on Windows yet: run pytest, `.venv\Scripts\python bench\learning_protocol.py` and the UI check there.

## Troubleshooting

- **Company proxy or antivirus:** downloads failing their checksum twice usually means something rewrites them.
  Ask IT to allow github.com, huggingface.co, pypi.org, files.pythonhosted.org, dot.net and nodejs.org.
- **No GPU found:** update the GPU driver; CUDA builds need a driver for CUDA 12.4 or newer. The script falls back
  to Vulkan or CPU on its own.
- **Not enough disk:** run `setup.cmd -Model C1` (2.5 GB model) or `-Model direct`.
- **Long paths:** extract to `C:\` so paths stay short.
