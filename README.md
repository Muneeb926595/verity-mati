# Verity — understand your C/C# codebase, fully offline

Point Verity at a C or C# codebase and ask questions in plain language, for example:
- "How does GPS data travel through this application?"
- "What happens if GPS becomes unavailable?"
- "What could be affected if I change `CalculateHeading()`?"

Every answer is built from compiler-grade facts about the code. Every statement is re-checked against the source, cited as `file:line`, and labelled with how certain it is. "What happens when…" questions are answered by **running the real code in a sandbox**. Nothing leaves the machine: there are no cloud APIs, no telemetry and no remote models.

The design is described in `docs/development/LOCAL_CODE_INTELLIGENCE_SYSTEM_DESIGN.html`. Section 39 of that document records what was implemented, the implementation decisions and an honest readiness assessment.

## Use it (this folder is already set up)

```bash
# Desktop app: starts the local engine and opens the UI
bash scripts/run_desktop.sh                       # macOS; builds with: bash scripts/build_desktop.sh
scripts\windows\run_desktop.cmd                  # Windows; builds with: scripts\windows\build_desktop.cmd [-Shortcut]

# Browser mode
bin/verity ui                                      # opens http://127.0.0.1:<port>/?token=…

# Command line
bin/verity index samples/navgps-c
bin/verity ask    samples/navgps-c "Explain how GPS data travels through this application."
bin/verity ask    samples/navgps-c "What happens when GPS sends heading 270, speed 80, valid true?" --details
bin/verity search samples/navgps-c "ProcessGpsData"
bin/verity source samples/navgps-c nav/navigation.c:59
bin/verity probe                                   # what this machine can do (models, compilers, sandbox)
```

In the app: **Open Project**, wait for "● Ready" (progress is shown as "Understanding your C project… 4 of 7 parts complete"), then ask. The answer shows **Your question · Answer · How the data flows · Evidence**, followed by five collapsed sections: reasoning/evidence, execution path, related code, assumptions and verification. Click any evidence row to see the source. Everything can be cancelled.

### Teach it when it picks the wrong place

Some answers hinge on one place in the code: "what happens if…", "where is X decided", "which function…". Under those
answers the app asks **Is this the right place?**
- **This one** picks one of the other places it considered. They are shown when they scored close to its choice.
- **pick a line…** opens the source. You click the line that really answers the question; **Look elsewhere** jumps to
  another file or function. Then add an optional note and choose **Use this line**.
- **✓ The answer was right** keeps the answer as a test case.

The question is answered again at once. From then on:
- **The same question, or the same question in other words** (matched by meaning search when it is on), goes straight
  to that line.
- **Similar questions** give that line a head start. It still has to win on its own evidence.

Every answer that used a correction says so ("Uses correction c-0003 (2 Oct 2026), recorded for this question …")
and offers **Undo**. **Learned** in
the project bar lists every correction, glossary word and confirmed answer. There you can add notes, undo and restore,
and export them or import them on another machine. When the code at a corrected line changes, the correction turns
**stale** and is no longer used. It comes back by itself if that code returns, and it follows the line when code above
it moves. Corrections live in `data/repos/<project>/learned.db`, next to the index, so a full re-index keeps them.

```bash
bin/verity ask         samples/navgps-c "What happens if the input data is invalid?"   # prints the answer id
bin/verity correct     samples/navgps-c <answer-id> --line gps/nmea_parser.c:26 --note "the checksum decides"
bin/verity correct     samples/navgps-c <answer-id> --pick 2                 # or one of the listed alternatives
bin/verity confirm     samples/navgps-c <answer-id>                         # right as it is: keep it as a test case
bin/verity corrections samples/navgps-c [list|undo c-0001|restore c-0001|note c-0001 "…"]
bin/verity corrections samples/navgps-c export --format bench -o my-cases.json   # json | bench | toml
bin/verity corrections samples/navgps-c import corrections.json
bin/verity glossary    samples/navgps-c [list|accept TERM|reject TERM|add TERM IDENT…|remove TERM]
```

When you correct an answer, Verity may suggest a **glossary** word: a word from your question tied to the
identifiers at the corrected line (for example "fix" → `valid`, `gps_fix_t`). Once you accept it, later questions
that use the word also search for those identifiers. A `[glossary]` table in `verity.toml` at the project root
works the same way and is read-only.

**MCP** (for AI assistants that speak the Model Context Protocol, stdio):
```json
{ "mcpServers": { "verity": { "command": "<Verity folder>/bin/verity", "args": ["mcp"] } } }
```
Tools: `verity_index`, `verity_status`, `verity_ask`, `verity_search`, `verity_symbol`, `verity_source`, `verity_correct`. They run on the same engine as the app and the CLI. `verity_ask` returns the answer id; `verity_correct` corrects that answer (an alternative or `file` + `line`), confirms it, or undoes a correction.

**On Windows:** `bash scripts/make_windows_bundle.sh` packs the code into one zip, about 65 MB (no models or toolchains). On the PC, extract it to `C:\` and double-click `scripts\windows\setup.cmd`. It installs everything inside the folder without administrator rights: Python, llama.cpp (CUDA, Vulkan or CPU, chosen for the hardware), .NET, cppcheck and the models. It then verifies the result, logging every step. `scripts\windows\analyze.cmd` says which model fits the machine, and `-StressTest` measures the candidates and keeps the best. Then use `bin\verity.cmd` the same way as `bin/verity`, or the desktop app (`scripts\windows\build_desktop.cmd`, which needs Rust and the Visual Studio C++ Build Tools to build it). Details and limits: `scripts/windows/README.md`.

## How it works

1. **Index** (incremental, cancellable, crash-safe), in seven friendly phases:
   - find files;
   - work out how the code is built (captured build, or reconstructed from dialect packs for MSVC/GCC/TI/IAR/Keil, with stand-ins for missing headers, and fidelity labels F1–F4);
   - read C with libclang (supervised worker processes) and C# with Roslyn;
   - cross-check C with cppcheck;
   - link function pointers, dispatch tables, virtual calls, events, entry points and I/O;
   - build search (FTS5 plus local embeddings).

   Facts live in SQLite (`data/repos/<project>/index.db`).
2. **Answer:**
   - A router picks one of 15 intent recipes: flow, origin, callers, impact, scenario, and so on.
   - Recipes query the call graph and a context-sensitive value-flow graph, producing claims plus evidence.
   - Scenario recipes compile and run the code in `sandbox-exec`, with no network and writes only to a run folder.
   - A verifier re-derives each claim from the index, checks every citation against the current file, and caps certainty.
   - The local model, if present, does two things only. It picks which of the verified candidate checks matches the question's meaning; a changed choice is disclosed in the answer. It writes a short summary (at most three sentences), which is always followed by the complete checked facts. Code it quotes must appear in those facts, otherwise its text is dropped. Search-only and "not found" answers are never reworded.
3. **Certainty labels:** CONFIRMED (compiler fact) · OBSERVED (seen in a run) · INFERENCE (an approximation was involved) · RUNTIME-DEPENDENT · UNKNOWN.

Models, all local, chosen by `bin/verity probe` from the hardware:
- gpt-oss-20b (MXFP4): main wording model on this 24 GB machine.
- Qwen3-4B-Instruct: fast/small rung.
- Qwen3-Embedding-0.6B: meaning search (on by default).

With no model the system still answers fully in Direct mode. The default configuration is the local model plus meaning search.

## Verified results (1 Oct 2026, Apple M3 / 24 GB, v0.5.0)

| Check | Result |
|---|---|
| Tests (`tests/`, pytest) | **157 passed** (on macOS; on Windows the sandbox-only tests are skipped): robustness (cancel/resume, crash containment, corruption, stale and shifted files, malformed/huge files, concurrency, path traversal), HTTP security, MCP, the pipeline under OS network denial, uncompiled MSVC/TI code without SDK or device headers, real-world C idioms, C# catch/throw, the local model's re-ranking and wording rules (with a fake model), orphaned model servers, 19 cases from the hand audit (`tests/test_audit.py`), and the cross-platform pieces the Windows setup relies on (`tests/test_windows_compat.py`) |
| Known-answer benchmark (`bench/suite.json`, 31) · held-out phrasings (15) · dialects (17) | **31/31 · 15/15 · 17/17** in Direct mode and with the local model + meaning search · citation precision 100% · 0 hallucinated names |
| Twelve real open-source projects **not written by Verity's author** (`samples/realworld`, tuned-on) | see the table below |
| **First contact with a never-seen project** (questions written from the source first, first run kept, read by hand) | Phase 3 (libmodbus, littlefs, CANopenNode, SOEM; 51 questions): 23/51. Phase 4 (uC/OS-II, Paho, M2Mqtt, nanopb, NModbus; 64 questions): 31/64 keyword-only; on the final round with the code frozen, **9/24 without and 14/24 with the local model**. Call paths, where a value is set, threads and names that do not exist are right from the start; "what happens when…" questions are the weak spot |
| Hand audit of the tuned answers (held-outs #5–#9, 64) | the grader said 55, a strict reading 48 (it found false connecting sentences); after generic fixes **57/64** in Direct mode and **58/64** with the model + meaning search |
| Real-code indexing (uncompiled) | lwIP 173K lines: about 4 min (≈0.87 GB) · SOEM 25K: 31 s (156 s with meaning search) · FreeRTOS: 83 s with meaning search · CANopenNode 27K: 8 s · libmodbus 8K: 6.5 s |
| Scenarios vs the independent gold harness (`bench/gold/`) | heading 270 → `currentHeading` 337.5, TRACKING, `motor_set_steering(50)`; 5 invalid fixes → DEGRADED; no GPS for 2.1 s → HOLD, steering 0, LED off — **all equal** |
| Performance | generated 48K-LOC C project: full index 14 s, no-op re-index 1 s, 126 MB; Direct answer ≈0.3 s; scenario ≈1–3 s. Local model + meaning search on real projects: median 12 s per answer, 90% within 24 s (model loaded; first load ≈20 s) |
| Learning from corrections (Phase 5, 3 Oct 2026, never-seen cJSON) | before any correction 4/14; after one correction per miss 13/14 facts-only and 12/14 with the model. Every corrected question discloses its correction. Paraphrases of a corrected question are right 4 of 6 with meaning search (2 of 6 without), and no neighbouring question used a correction. Existing suites: Direct 198/207 unchanged per question, model 95/99 → 96/99. Details: `PROGRESS.md`, "Phase 5" |
| Every surface, end to end | desktop app self-test (start, open a project, answer, quit) in 20 s; browser UI (index, cancel/resume, answers, source viewer, settings); CLI; MCP (all 6 tools and their errors); offline install from the vendored wheels under an OS network deny; sockets bound to localhost only |

Tuned results per project (the automatic grader's count; Phase 4 held-outs also read by hand, see above):

| Project (language, size) | Direct | Local model + meaning search |
|---|---|---|
| minmea + FreeRTOS (C) · lwIP (C, 173K) | 14/14 · 15/15 | 14/14 · 15/15 (lwIP: model only) |
| libmodbus · littlefs · CANopenNode · SOEM (C) | 14/14 · 11/12 · 13/13 · 12/12 | 14/14 · **12/12** · 13/13 · 11/12 |
| uC/OS-II · Paho MQTT (C) · M2Mqtt (C#) | 14/14 · 12/14 · 11/12 | 14/14 · 13/14 · 11/12 |
| nanopb (C) · NModbus (C#) | 9/12 · 10/12 | 11/12 · 9/12 |

The final demo transcripts (demos 1–7) are in `docs/DEMOS.md`, generated by `scripts/run_demos.sh`. UI screenshots are in `docs/screenshots/`.

Re-run everything with `bash scripts/verify.sh` (add `--llm` to include the model, `--lwip` for the 173K-line lwIP suite). The full regression log of this release is `logs/final14.status`.

## Limitations (read before relying on it)
- **Built on macOS; first real Windows run on 2026-10-02** (Windows 11, RTX 5070, Visual Studio 2022 Build Tools present): setup, the system analysis, pytest (152 passed, 18 sandbox-only tests skipped), every benchmark suite in Direct mode and the desktop app were run there, and the problems found were fixed (see `PROGRESS.md`, Windows section). The Windows runtime-scenario adapter is not built, so there scenarios fall back to reading the code (INFERENCE). Linux is still unrun.
- **Generalisation is measured, and not perfect.** On nine real projects Verity had never seen, every question was written from the source first and the first run was kept. Phase 3 (four projects, 51 questions): 23/51 right. Phase 4 (five projects, 64 questions, every answer read by hand): 31/64 keyword-only; on the final round with the code frozen, 9/24 without and 14/24 with the local model. Structural questions (call paths, where a value is set, threads, names that do not exist) are dependable from the start. Behavioural "what happens when…" questions often pick the wrong check on first contact when the meaning is domain knowledge the code does not spell out (an MQTT SUBACK failure code, priority inheritance, a nesting limit that is compiled out). A wrong pick is always visible: the answer names the function and line it chose.
- **The benchmark grader checks names, not meaning**, so it over-counts. All first runs, and the tuned answers of held-outs #5–#9, were also read by hand, and the strict counts are reported next to the grader's. The hand audit of the tuned answers found false connecting sentences (for example "so the rest is skipped", or a caller's success side described for a failure); those were fixed generically and are covered by `tests/test_audit.py`.
- **Configurable code:** code that is compiled out by default feature options is analysed in an extra "all optional features on" build and answered with that caveat; on option-heavy trees this roughly doubles indexing time (lwIP 120 s → about 4 min (244 s), ≈0.87 GB peak).
- C value flow is field-based (all instances of a struct share field nodes) and has no SSA or general pointer-alias analysis. Claims through struct fields are therefore marked INFERENCE.
- Configuration variants: the 4 never-defined macros guarding the most code, plus one build with every overridable feature option switched on; other combinations of options are not explored. The data-model sweep is not implemented.
- Scenario execution: C only (C# scenarios are not built). Hardware is replaced by logging stubs, and every default the question does not give is listed as an assumption.
- **The local model needs most of a 24 GB machine.** Run one Verity engine (app, CLI or MCP) with the model at a time; a second engine that cannot load it answers in facts-only mode and says so. Model answers take about 9–20 s (Direct: under a second).
- Meaning search (on by default) was measured on every suite except lwIP, which was indexed without it.
- The router is rule-based with a model fallback. Unusual phrasings can fall back to search; those answers are still grounded, but less targeted.
- Validated on the samples, a generated 48K-LOC project, two dialect samples and twelve real open-source projects (ten in C, two in C#; up to 173K lines), not yet on a customer's industrial codebase.

## Set up from scratch
First see what this machine still needs. The check installs nothing and takes a few seconds:
```bash
bash scripts/check_prereqs.sh            # macOS / Linux   (--model C1, --json, --offline)
scripts\windows\check_prereqs.cmd        # Windows          (-Model C1, -Json, -Offline)
```
It lists, item by item:
- **This machine:** platform, memory and the model it can run, disk space, internet.
- **What you install yourself.** On a Mac that is only `uv` (or Python 3.12):
  `curl -LsSf https://astral.sh/uv/install.sh | sh`. On Windows it is nothing.
- **What setup installs on its own**, inside this folder.
- **Optional extras.** For example, the Xcode Command Line Tools let Verity run code for "what happens when"
  questions.
- **What is not needed:** Ollama, Docker, a system LLVM, Xcode, Visual Studio, git, cloud accounts, admin rights.

Exit code 0 means nothing blocks setup. Then:
```bash
bash scripts/setup.sh                    # macOS / Linux   (--model auto|direct|C1|C2|C3)
scripts\windows\setup.cmd                # Windows          (see scripts/windows/README.md)
```
Everything goes into this folder:
- Python 3.12 venv (`requirements.lock`; from `vendor/wheels` when present).
- llama.cpp, .NET SDK, Rust and cppcheck in `tools/` (`scripts/setup_toolchains.sh`).
- Models in `models/` (`scripts/download_models.sh`, resumable, every file checked against its pinned SHA256):
  - `auto` picks what fits: gpt-oss-20b on a 24 GB Mac or larger (15.2 GB), Qwen3-4B on smaller ones (3.1 GB).
  - `direct` downloads only the 0.6 GB embedding model.
- The Roslyn worker, then the UI.

After setup no network is used.

## Layout
```
verity/            engine (Python)
  extract/           recon (build reconstruction), c_clang (libclang), csharp (Roslyn adapter),
                     crosscheck (cppcheck), ts_fallback (tree-sitter), pool (supervised workers), packs/
  store/ analysis/   fact store; derive (linking), graph (call + value-flow graphs)
  retrieval/         symbol cards, hybrid search
  agent/ verify/     router, recipes, answer orchestration; verifier
  render/            answer object + text rendering
  runtime_exec/      sandboxed scenario harness
  llm/ probe.py      local model runtime; capability probe and model ladder
  engine/            EngineCore + HTTP/SSE daemon
  cli.py  mcp_server.py  bench/
workers/roslyn/    C# fact extractor (.NET)
ui/  desktop/      SolidJS UI; Tauri 2 shell
samples/           navgps-c (GPS → parser → validation → navigation → heading → vehicle), navgps-cs,
                   msvc-telemetry, ti-motor-c2000 (dialects), realworld/ (C: minmea, FreeRTOS, lwIP, libmodbus, littlefs,
                   CANopenNode, SOEM, uC/OS-II, Paho MQTT, nanopb; C#: M2Mqtt, NModbus — sources and licences in its README)
bench/             suite.json, heldout.json, dialects.json, realworld*.json, gold/ (reference harness), results/, gen_large.py
tests/  scripts/  docs/  models/  tools/  vendor/  data/ (indexes, created at runtime)
```
`PROGRESS.md` is the build log and checklist.
