# Verity build — durable progress log & checklist

This file is the resume point for the overnight autonomous build. Each session appends to the log at the bottom and ticks items here.
Order of the sections: Phases 1–5 first (Phase 5, learning from corrections, is the latest work: 2026-10-03), then the
2026-10-02 sections (Windows setup and first run, prerequisites, the rename to Verity, the move to Git).
Spec: `docs/development/LOCAL_CODE_INTELLIGENCE_SYSTEM_DESIGN.html` (v0.3, 39 sections, read in full on 2026-09-30).

## Environment (probed 2026-09-30)
- Host: macOS 26.2, Apple M3 (8 cores), 24 GB unified memory, 82 GB free disk, ~4.3 MB/s download.
- Present: Python 3.12 (uv), Node 25, Apple clang 17 (+ libclang CLT), xcrun llvm-cov/llvm-profdata, lldb, sandbox-exec, cmake, java 17.
- Installed into ./tools: llama.cpp b11293 (Metal), .NET 10 SDK, Rust (Tauri), cppcheck (source build).
- Capability class for this machine: 24 GB unified → gpt-oss-20b (MXFP4, ~12 GB) is the strongest rung that fits; Qwen3-4B-Instruct-2507 is the fast/small rung (C1); embeddings Qwen3-Embedding-0.6B.

## Checklist (dependency order; [x] = integrated AND tested)
- [x] 0. Workspace skeleton, setup scripts, venv, toolchains, models (downloads in background)
- [x] 1. Sample C project `samples/navgps-c` (GPS → parser → validation → nav state → heading → vehicle) + reference harness gold
- [x] 2. Fact schema + SQLite store (WAL, FTS5, vectors-as-blobs) + per-file replacement
- [x] 3. Capability probe (hardware, tools, adapters, class) + model-ladder manifest
- [x] 4. Repository discovery (languages, build files, projects) + dialect packs + include resolution + stub synthesis (build reconstruction, fidelity F1–F4)
- [x] 5. C extractor (libclang): symbols, occurrences by role, calls (direct/fnptr/table/flow), value flow, guards/branches, constants, entry points, I/O sites
- [x] 6. cppcheck second parser (name-matched cross-check) + tree-sitter fallback (F4)
- [x] 7. C# extractor: Roslyn worker (.NET 10) → same schema (subagent) + sample `samples/navgps-cs`
- [x] 8. Graph layer: call graph, CHA/RTA/fnptr resolution, value-flow graph, slices, paths, reverse deps (change impact), entry-point reachability + path conditions
- [x] 9. Retrieval: identifier-aware FTS5, symbol cards, embeddings (llama-server), hybrid RRF, mention resolution
- [x] 10. LLM runtime: llama-server lifecycle, self-benchmark, JSON-schema output, graceful unavailability (C0)
- [x] 11. Agent: router (intents), recipes, evidence packs, bounded loop, LLM prose
- [x] 12. Verifier: claim schema, predicate checkers, identifier grounding, citation/blob checks, statuses, absence certificates, repair
- [x] 13. Renderer: plain-language answer + flow + evidence + expandable details
- [x] 14. Scenario execution: scenario parsing, slice-and-stub harness, sandbox adapter (macOS sandbox-exec / Linux bwrap), coverage + call trace + outputs → OBSERVED
- [x] 15. Engine daemon: JSON-RPC + SSE events, jobs, progress, cancellation, priorities, token auth, browser mode
- [x] 16. CLI (`verity index|ask|search|status|probe|serve|trace|bench|mcp`)
- [x] 17. Desktop app: Tauri 2 + SolidJS (simple UX: Open project → Index → Ask → Answer → Evidence)
- [x] 18. MCP server (stdio) over the same engine
- [x] 19. Known-answer benchmark + graders + results; fix loop
- [x] 20. Robustness: corrupted/interrupted index, cancellation, missing tools/models, huge files, stale index, invalid input, sandbox & offline guarantees
- [x] 21. Tests (pytest) green; end-to-end demos 1–7 verified
- [x] 22. Design doc: new final section "Implementation Status & Decision Log"; README

## Decisions / deviations (running)
- D-impl-1: libclang (Python, clang.cindex) for the C extractor instead of a C++ LibTooling binary — the design allows "prototype on libclang"; it is the only path buildable and testable in one night. Gaps (no CFG API) are covered by our own AST-level guard/branch extraction.
- D-impl-2: Vectors stored in SQLite as float32 blobs with numpy brute-force cosine (the sqlite-vec role at ≤100K LOC) — avoids loadable-extension issues across Python builds.
- D-impl-3: Engine transport = JSON-RPC 2.0 over HTTP + Server-Sent Events on 127.0.0.1 with a per-session bearer token (Tauri and browser mode both use it); named-pipe proxy left for later.
- D-impl-4: macOS isolation adapter = `sandbox-exec` profile (deny network, write only to the run dir); Linux = bubblewrap if present; otherwise execution disabled.
- D-impl-5: Main LLM on this machine = gpt-oss-20b (fits 24 GB); design default Qwen3.6-35B-A3B (~22 GB) does not fit alongside tools.

## Log
- 2026-09-30 22:15 Session start. Probed env, started model + toolchain downloads, read design doc in full.
- 2026-09-30 23:55 Items 0-2,4-6 done: c_clang extractor (USR keys, positional param keys, guards incl. early-exit, call-site-tagged flows,
  out-params, fnaddr tables), supervised worker pool (crash/timeout/startup-loop safe), incremental indexer (sha + header deps,
  per-TU stub preludes, free-macro variants +M/-M), derive (fnptr points-to: handle_rmc->ProcessGpsData res=flow; table
  dispatch -> handle_rmc/handle_gga res=table), io (heuristic + mmio), cppcheck cross-check 100%/100% on sample, cards+FTS.
  Sample index: 0.97 s full, 0.32 s no-op.
- 2026-10-01 01:20 Items 3,7-14 done. Graph: context-sensitive slices + flow_path (Dijkstra over (node, call-site stack)),
  external-function summaries, helper folding. 14 intent recipes; verifier (citations, predicate re-query, caps, prose grounding);
  renderer; Roslyn worker integrated (CHA/RTA/event bridges). Scenario harness (main() init replay, auto stubs, passing
  defaults, coverage branch outcomes via region entries, call trace via -finstrument-functions + nm slide, sandbox-exec):
  results equal bench/gold/reference_output.txt for scenarios A (337.5, TRACKING, steer 50), B (DEGRADED, 5), C (HOLD, 0, led 0).
  NEXT: engine daemon (15), CLI (16), MCP (18), Tauri UI (17), benchmark (19), robustness (20), tests (21), docs (22).
- 2026-10-01 02:10 Items 15,16,18,19,20,21 done. EngineCore shared by HTTP daemon (JSON-RPC+SSE, token, origin check),
  CLI (bin/verity) and MCP stdio server (protocol implemented directly, no SDK/otel). Benchmark bench/suite.json 31/31 Direct,
  30/31 LLM (gpt-oss-20b, 0 hallucinated names). Held-out bench/heldout.json: first run 5/15 (kept in
  bench/results/heldout/first-run-before-fixes.*), 15/15 after generic fixes (stemming/synonyms, routing, guards, arg sources,
  fnptr targets, else-branch rejection, var tracing). pytest: 71 passed (robustness: cancel/resume, crash containment,
  corruption, stale+shifted files, malformed/huge files, concurrency, path traversal, offline under sandbox).
  UI agent (Tauri+Solid) running in background. NEXT: large-repo perf, demos, README, setup script, design-doc section.
- 2026-10-01 03:40 Item 17 done (UI agent): ui/ (Solid, 26 KB gz JS) + desktop/ (Tauri 2, Verity.app 8.8 MB), 30/30 browser
  e2e checks vs real engine, screenshots in docs/screenshots. LLM prose: 'model' or 'model+facts' (summary + complete checked
  facts); LLM bench 31/31 + held-out 15/15, 0 hallucinated names. Perf: 48K-LOC generated repo full index 14 s, no-op 1 s.
  NEXT: README, design-doc section 39 + audit, final cleanup.
- 2026-10-01 04:30 Item 22 done: design doc §39 "Implementation status & decision log" (validate.sh PASS, mermaid renders),
  README.md, scripts/setup.sh (offline via vendor/wheels), scripts/verify.sh, docs/DEMOS.md (demos 1-7, gpt-oss wording).
  Final: pytest 71/71; bench 31/31 (Direct + LLM); held-out 15/15 (Direct + LLM; first run 5/15 on record);
  scenarios = gold A/B/C. Fixed last: CLI flags no longer persist into app settings (settings = auto + embeddings);
  aiohttp app-state deprecation; scenario run-folder retention (30).
  Known gaps (documented in §39 + README): Windows/MSVC untested, C#/Windows runtime scenarios not built, field-based
  C value flow, top-4 macro variants only, no free-form agent loop.

ALL DONE (phase 1)

## Phase 2 (resumed 2026-10-01): validate on code I did not write, fix what breaks
- [x] P2-1 MSVC-style uncompiled sample (windows.h/SAL/__declspec/WINAPI threads/ReadFile COM port/.vcxproj) + known answers
- [x] P2-2 TI C2000-style uncompiled sample (interrupt/cregister/EALLOW/PieVectTable ISR table/missing device headers) + answers
- [x] P2-3 Real open-source C codebases (lwIP, FreeRTOS-Kernel, minmea, + libmodbus, littlefs) in samples/realworld: index robustness + questions
- [x] P2-4 Real-world known-answer suites (answers checked by reading the code), run first, keep first-run result, fix generically; two held-out projects
- [x] P2-5 Regression (tests + all suites), update §39 / README / PROGRESS
- 2026-10-01 P2-1 done. Found: without windows.h the MSVC sample had ~40 errors/file (types as opaque structs, SAL as
  variables, locals cascading). Fixed generically: Win32 API knowledge-pack shims (verity/extract/shims/win32: types,
  SAL, constants, implemented Interlocked*/secure CRT, declared OS APIs), Windows SDK discovery on Windows, -fms-compatibility
  dropped off-Windows (va_list clash), usage-guided stub loop (int-like typedefs, member-chain register structs, SAL),
  API 'writes' pack (InterlockedExchange/ReadFile/memcpy write their pointee), canonical types for out-params, struct copies
  reach every field, memcpy backward summary, input-API outputs terminate slices, multi-origin answers, entries recipe,
  config-switch recipe, failure-side ranking, stubs from prototypes with success conventions, unit-aware inputs
  (85.5 °C -> temperatureCenti 8550), tree-sitter main() init replay. Result: 8/8 files F2, 0 errors; scenario RUNS on macOS.
- 2026-10-01 P2-2 done. TI sample (no device headers): float-like typedef stubs, types-first stub rounds, project-wide
  member synthesis (consistent register layouts across files), keyword ISR entries (interrupt/__interrupt/#pragma vector/
  ISR()), evaluated consequences (with g_pwmEnabled = 0 -> CurrentLoop no longer called), trigger inputs for failure runs,
  inputs bound to globals, per-file prelude compilation in the harness + one definition per stand-in object, lenient
  embedded-C flags, register writes in run results. bench/dialects.json 17/17 (tuned-on, labelled as such);
  tests/test_dialects.py added. All suites + tests green.
- 2026-10-01 P2-3 done. lwIP (173K lines C + 100 C#, full tree incl. ports/tests/docs): the build-switch detector read comment
  text on `#if` lines as macros ("build", "don", "t") -> comment stripping + platform predefines excluded; variant budget.
  Index 348.5 s / 1.9 GB -> ~2 min / ~0.5 GB. A Coverity model file and a test double overrode real definitions ->
  verity/pathkinds.py (tests/docs/examples/tool models = secondary; program definition wins, copies' body facts dropped,
  secondary code ranked last). Function-pointer targets must match the call's argument count. Operator kinds from the
  libclang AST (macro `++x` was a read). goto error exits narrated. C abbreviation table + inflection stems; acronyms in
  questions are words, not identifiers; digit-suffixed identifiers also yield their stem (crc16 -> crc). Thread-start
  wrappers (sys_thread_new -> pthread_create). Overlay flows (data buffer -> fields of the struct cast over it) and
  outer-field member flows. Failure ranking rebuilt: relevance x error-handling (rarity weights, concept coverage,
  failing property, outcome constants, log messages, state polarity, goto exits, side that opens with error handling).
  New recipes/paths: triggers ("what triggers X"), call path ("how does X reach f"), set-to-constant, verb-operation
  writes (decremented -> `- 1`), verification-check finder, I/O-aware function finder, error propagation through
  locals and function pointers, verified "There is no X in this code" statement.
- 2026-10-01 P2-4 done. First runs kept before any fixes: minmea+FreeRTOS 8/14 (bench/results/realworld/first-run-before-
  fixes.*), lwIP 5/15 (bench/results/realworld_lwip/first-run-before-fixes.*). HELD-OUT #1 libmodbus (added after the lwIP
  fixes): first run 5/14 (bench/results/realworld_heldout/first-run.*) -> errno modelled as a variable, "where is X set"
  routing, persistent-state ranking, timeout-by-outcome, debug-guarded prints hidden, I/O finder -> tuned-on. HELD-OUT #2
  littlefs (added after the libmodbus fixes): first run 9/12 (bench/results/realworld_heldout2/first-run.*); then local
  error-code writes, logging macros as diagnostics, match/compare abbreviations, exhaustion outcomes. Known miss kept:
  "runs out of free blocks" (NOSPC vs NOMEM needs domain knowledge). tests/test_realworld_patterns.py (10 tests).
- 2026-10-01 P2-5 done. Final regression (fresh indexes, Direct mode): pytest 101 passed; main 31/31; held-out 15/15;
  dialects 17/17; realworld (minmea+FreeRTOS) 14/14; lwIP 15/15; libmodbus 14/14; littlefs 11/12 (known miss above).
  Local LLM (gpt-oss-20b): main 31/31, 0 of 260 claims unsupported, 0 hallucinated names; held-out 15/15 (1 of 172 claims rejected by the verifier and removed before display, 0 hallucinated names).
  Design doc §39 extended with "Phase 2: validation on code the builder did not write" (projects table, what broke and
  the generic fixes, D-impl-12..19, caveats, updated verdict); README results/limitations; scripts/verify.sh runs every
  suite (--lwip, --llm). lwIP indexes in 120 s / ~0.5 GB.
ALL DONE (phase 2)

## Phase 3 (resumed 2026-10-01): keep measuring on never-seen code, close the gaps that can be closed here
- [x] P3-1 Held-out #3: an industrial C stack never run before (CANopenNode: uncompiled, target driver header missing);
      questions from verified source facts; first run kept before any fix
- [x] P3-2 Generic fixes from P3-1 (then CANopenNode is tuned-on)
- [x] P3-3 Held-out #4 (SOEM, never seen) to measure P3-2's generalisation; then its generic fixes (tuned-on)
- [x] P3-4 (investigated, not built) Windows-target fidelity on macOS: parse MSVC-style projects with the real MSVC target (x86_64-pc-windows-msvc,
      LLP64, _WIN32/_MSC_VER) where possible; measure errors/fidelity against the current profile
- [x] P3-5 Regression (tests, all suites, LLM, demos); update §39 / README / PROGRESS
- 2026-10-01 P3-1 done. CANopenNode first run 4/13 (bench/results/realworld_heldout3/first-run.*): worse than littlefs'
  9/12, so generalisation is not yet stable. Worst finding: "There is no crc16_ccitt / CO_fifo_write in this code" was
  FALSE — both exist but are compiled out by default feature options (CO_CONFIG_CRC16 = 0). The new absence statement
  made an untrue claim.
- 2026-10-01 P3-2 (fixes, CANopenNode now tuned-on): (1) configuration: recon.find_config_knobs finds overridable feature
  options (#ifndef X / #define X default, tested by #if); a third variant build "+ALL_FEATURES" switches them on (booleans
  -> 1, bitmasks -> their positive flags only: CO_CONFIG_CRC16_EXTERNAL would REMOVE code); answers about code that only
  exists there switch to that build and say so first, with the enabling #if conditions and the defaults; weak default
  answers retry on it; search sees such code only for those answers; absence claims now also check the source text
  (verifier too) and say "in the source but compiled out" instead. (2) Heartbeat/length/object abbreviations; timeout
  ranking by topic incl. function names; parameter/local names without their function prefix. (3) Failure ranking:
  each concept counted once (length/len/size), concept coverage weighted more, capped error-handling weight, words of the
  expressions a tested local came from (DLC = CO_CANrxMsg_readDLC(msg)), local next-state/error-code assignments as the
  side's outcome, consequences of the side's own state writes, named codes shown in handler calls, no single-word subject
  for "does not match". (4) "When is X sent/called/…" -> the call site doing it and its conditions. (5) Names shared by
  several structs answered per struct. (6) Multi-line statements quoted whole.
- 2026-10-01 P3-4 investigated: parsing with -target x86_64-pc-windows-msvc on macOS needs a stand-in C runtime (UCRT);
  mixing it with the macOS libc gives 56 errors (va_list conflicts) where today's profile (host target + Win32 shims +
  -fms-extensions + MSVC predefines) gives 0. On Windows the real SDK/UCRT are used. Not built: only LLP64 sizes and SEH
  keywords would differ, and a fake runtime would itself be unvalidated.
- 2026-10-01 P3-3: HELD-OUT #4 SOEM (EtherCAT master, Linux + win32/wpcap ports, generated ec_options.h missing): first
  run 5/12 (bench/results/realworld_heldout4/first-run.*). Held-out first runs so far: 5/14, 9/12, 4/13, 5/12 — about
  46% on never-seen code; that is the honest generalisation figure. Generic fixes from it (SOEM now tuned-on): loop
  conditions are extracted (while/do-while/for were stored empty), retry-until-timeout loops answered as loops (what is
  repeated, what ends it, the value returned when time runs out and its starting value), a caller retrying on a
  result is reported; plural stems (slaves -> slave); question words inside run-together names (slavecount, elist,
  nextmbxcnt) and segmentation of such names in the search index from the project's own vocabulary; conditions that
  only exist inside a macro expansion are ignored; words found only in comments are domain words, never "compiled-out
  code"; negation inside names (notEmpty); ring-buffer full/empty = equality of two ring indices (head == tail);
  concepts per question token (EtherCAT is one concept); state questions (full / zero / too much) weigh error handling
  less than error questions (bad / invalid / fails); "what does F return when …" states the value returned after the
  side; "who calls" lists every caller; thread lists put the entries that drive the most code first and mark port
  trampolines (thread_wrapper) — and a function is a thread-start wrapper only when its function-pointer parameter
  really becomes the thread (tcpip_init(initfunc) is not one); per-function occurrence index (failure questions on
  lwIP 19 s -> <3 s). tests: 104 passed.
- 2026-10-01 P3-5 done. Final regression (Direct): pytest 104 passed; main 31/31; held-out 15/15; dialects 17/17;
  realworld (minmea+FreeRTOS) 14/14; lwIP 15/15; libmodbus 14/14; littlefs 11/12 (known miss); CANopenNode 13/13;
  SOEM 12/12. Local LLM: main 31/31 (0 of 258 claims unsupported, 0 hallucinated names); held-out 15/15 (1 of 172 claims rejected by the verifier and hidden, 0 hallucinated names).
  Late fixes found by the regression's hallucination check: the internal build label leaked into a "configuration
  switch" sentence, and a dangling backtick in "may run `…`" made English read as code — both fixed; "…FromISR" APIs are
  not interrupt handlers. Indexing cost of the all-features build: lwIP 244 s / 866 MB (was 120 s / 494 MB).
  Design doc §39 gained "Phase 3: measuring on never-seen code, round after round" (held-out table, first-contact
  profile by question type, what was fixed, D-impl-20..22, costs); README results/limitations; verify.sh runs all
  suites.
ALL DONE (phase 3)

## Phase 4 (resumed 2026-10-01): use the local model and meaning search for first-contact accuracy, measured honestly
- [x] P4-1 Grounded re-ranking: the deterministic recipes keep finding and verifying candidate checks; the local model
      only picks which of the top candidates matches the question's meaning (no new facts; the choice is disclosed)
- [x] P4-2 Meaning search (embeddings) feeds candidate functions; benchmark flag to run with embeddings
- [x] P4-3 No regressions on the tuned suites in Direct and model modes
- [x] P4-4 Held-out #5 and #6 (never-seen projects): questions from the source first, then FIRST runs in three modes
      (keyword-only / + meaning search / + local-model re-ranking), all kept before any fix
- [x] P4-5 Regression, docs (§39, README, PROGRESS)
- [x] P4-6 Hand audit of the tuned answers of held-outs #5–#9 (the grader alone over-counts) and of every answer the
      fixes changed in the older suites

### Phase 4 log
- P4-1 `verity/agent/rerank.py` + `_rerank()` in recipes: used by failure/missing-check answers, `_find_check`
  and "where is X computed". The model sees each candidate rendered as real code (file:line, the condition, what the
  branch does) and returns one number; only the order changes. A changed choice adds an assumption ("The local model
  chose this check from N candidates…") and a footer line in the CLI/app. The failure pool is widened beyond the
  deterministic top 6 with candidates chosen by other signals: the best whose own outcome (error constant / log
  text) matches the question, and meaning-search matches. littlefs "runs out of free blocks" (the one known miss)
  is now answered correctly in model mode (lfs_alloc → LFS_ERR_NOSPC); Direct mode is unchanged.
- Fixed while testing: a re-ranking signal was computed before its inputs (UnboundLocalError) → the recipe crashed,
  the answer fell back to search results and the model's wording invented behaviour. Two guards: the signal is
  computed after its inputs, and search-only answers (`cx.fell_back`) are never reworded by the model.
- The app's answer footer said "no AI model used" for summary+facts answers; it now names the mode correctly.
- P4-2 `--embed` bench flag (index and search with Qwen3-Embedding); meaning-search functions join the candidate
  pool for failure questions (when no function is named) with a small relevance bonus.
- P4-3 Direct-mode regression after P4-1/P4-2: pytest 104, suite 31/31, held-out 15/15, dialects 17/17,
  realworld 14/14, libmodbus 14/14, littlefs 11/12 (the known miss, unchanged in Direct mode), CANopenNode 13/13,
  SOEM 12/12, lwIP 15/15 (`logs/regress4.status`).
- P4-4 held-out #5 uC/OS-II (Win32/Visual Studio port, ~15K lines) and #6 Paho MQTT Embedded-C (~7K lines) added
  and trimmed to one realistic configuration; 14 questions each written from verified source facts
  (`bench/realworld_heldout5.json`, `bench/realworld_heldout6.json`) before Verity was run on them. First runs in
  four modes (keyword / +embeddings / +model / +model+embeddings) → `bench/results/realworld_heldout{5,6}/first-run-*`.


### Phase 4 completed 2026-10-01 (evening)
Done before the pause (all generic, all tested):
- C#: `catch` blocks are decisions (Roslyn worker emits them), `throw` is an exit, `early_exit` ifs are ifs, catch
  propagation matches exception types, nested handlers narrated step by step; constructors shown as `new T()`; test
  folders `X.UnitTests/`, `TestApp/`; `contrib/`/third-party trees secondary.
- C: returning macros (`PB_RETURN_ERROR`) are exits; "otherwise" narration for else sides; `break`/`continue` worded;
  failure tails (success-first code) incl. nested ones; status-switch cases and the timeout recipe; `*perr = ERR`
  out-parameters; zero/null/negative/full ↔ conditions; acronyms; verb direction; success constants are not failures.
- Model: wording is a short summary ALWAYS followed by the checked facts; quoted code must exist in the facts; "not
  found" answers never reworded; failure reasons shown; orphaned model servers reaped.
- First runs of held-outs #5–#9 kept and strictly audited: #5+#6 15/16/17/17 of 28 (keyword / +embed / +model /
  +model+embed); #7 M2Mqtt 7/8 of 12 (keyword / +embed); #8+#9 final frozen round 9/9/14/13 of 24 (grader 12/12/18/18).

After resuming:
- Regression of the pause fixes found two misfiring rules (SOEM error list: "full" matched the `>` of `->`; libmodbus
  timeout: timer set-up taken for detection) — both narrowed. Design doc §39 "Phase 4" section written.
- P4-6 hand audit of the TUNED answers of #5–#9 (64): grader 55, strict 48. False sentences came from narration
  between facts. Fixed generically (tests/test_audit.py, 16 cases):
  compiled-out / brace-less returns taken as unconditional ("so the rest of pb_write() is skipped"); a returning macro
  worded as a write; failure tails running past `return` into the next `case`; decisions inside macro expansions (`?:`
  in PB_SET_ERROR, `do {} while (0)` in timeradd); the side of an equality the question names (NModbus ACKNOWLEDGE gave
  the throwing else side) — the value must be a real constant (macro, enum, `const` field), its specific last word
  must be in the question, bit tests do not count; follow-on calls from another catch handler or the other side of an
  if/else; a caller's test narrated on its success side for a failure (`err == OS_ERR_NONE`, `if (ParseFrame(…))` with
  a BOOL result, `wkc > 0` with EC_SLAVECOUNTEXCEEDED = -4 — the failing value is now evaluated, and when the failure
  side is empty the answer says what the caller skips); "returns" for a handler whose return is conditional; "where is
  errmsg set" answered by a function sharing a word (exact field names win; writes made by a macro are reported as the
  macro); unit tests answering for the program; `tests.c` files are test code; "more … than" prefers ordering tests;
  the module name covers its role (pb_decode.c = decoder); decode/encode direction; negative macro values (`- 4`) were
  not stored by the C extractor; a tested local is shown with the comparison that computed it (`readAgain = … ==
  Acknowledge`).
- Strict after the fixes (Direct, tuned): uC/OS-II 14/14, Paho 12/14, M2Mqtt 11/12, nanopb 10/12, NModbus 10/12 =
  57/64. Every answer of the older suites that the fixes changed was read; the changes were corrections (three older
  false caller statements fixed: msvc ParseFrame, SOEM detect_slaves, SOEM poperror) or wording only.
- Final regression (`logs/final14.status`, every project re-indexed, default configuration = local model + meaning
  search): pytest 146 passed; Direct: main 31/31, held-out 15/15, dialects 17/17, minmea+FreeRTOS 14/14, lwIP 15/15,
  libmodbus 14/14, littlefs 11/12, CANopenNode 13/13, SOEM 12/12, uC/OS-II 14/14, Paho 12/14, M2Mqtt 11/12, nanopb
  9/12, NModbus 10/12. Model + meaning search: main 31/31, held-out 15/15, dialects 17/17, minmea+FreeRTOS 14/14, lwIP 15/15 (model only), libmodbus 14/14, littlefs 12/12, CANopenNode 13/13, SOEM 10/12, uC/OS-II 14/14, Paho 13/14, M2Mqtt 11/12, nanopb 11/12, NModbus 9/12 (strict 10/12: the busy answer is right but lacks the constant's name). Strict model-mode count on #5–#9: 58/64. Two of 64 model summaries contradicted the facts under them (uC/OS-II mutex "raised to the waiting task's priority"; NModbus "the transport does not decide").
- After the final run (logs/final15.status): a failure side beyond a long function's first 40 steps was told as "runs
  lines a–b"; a failing value held in the returned local (`wkc = 0; … return wkc;`) is now passed to the callers;
  callers the question names are narrated first; "fact N" references are removed from model text; wrap detection no
  longer needs a `;` (`_code()` strips them — meaning search had let ecx_mbxrotatequeue beat ec_nextmbxcnt). Direct
  re-run: every score unchanged, 4 answers changed (SOEM SDO abort corrected, caller lists reordered). Model + meaning
  search re-run where it could matter: SOEM 11/12 (the error-list question is the model's own choice of the index
  wrap-around; model-only answers it right), M2Mqtt 11/12 (a timeout thrown in SendReceive is not traced into Ping's
  catch, which sits in an `#if DEBUG … #else` pair). pytest 148 passed.
ALL DONE (phase 4)

## Phase 5 (2026-10-03): learning from corrections — branch `phase5-learning-from-corrections`
Spec: `docs/development/VERITY_LEARNING_FROM_CORRECTIONS_ONE_PAGER.html`. Built on `windows-first-run-desktop-app`
after reviewing it (below). Nothing retrains the model: a correction only changes which verified candidate comes first.
- [x] P5-0 Measure top-1 vs top-3 (`verity/bench/run.py`: `topk_rank` per case, `summary.within_top3`).
  - Tuned suites (Direct): top-1 198/207, top-3 200/207. 2 of the 9 misses had the right place among the runners-up.
  - New never-seen project #10, **cJSON** v1.7.19 (`samples/realworld/cJSON`, 14 questions written from the source
    before any run, `bench/realworld_heldout10.json`; 10 paraphrases and 6 neighbours written at the same time,
    `bench/realworld_heldout10_followup.json`). First run, kept: Direct 4/14 (top-3 4/14); model + meaning search
    4/14 (top-3 5/14). Results: `bench/results/realworld_heldout10/first-run-*`.
  - Decision: the right place is rarely among the runners-up on first contact, so "pick a line" is the main way to
    correct; the alternatives are a shortcut shown when they are close (score ≥ 0.6 × the top, at most 2).
- [x] P5-1 Alternatives: `meta.learning.{candidates, alternatives, correctable}`; in the text answer ("Other places
  that could be the answer"), the CLI hint under each answer, `verity_ask` (id, alternatives) and the app's
  "Is this the right place?" card.
- [x] P5-2 Store `data/repos/<id>/learned.db` (`verity/learn/store.py`), never index.db. WAL; a damaged file is moved
  aside to `learned.db.corrupt-<time>` (never deleted) and a fresh one starts. RPC `answer.correct|confirm|unconfirm`,
  `corrections.*`, `glossary.*` (`docs/ENGINE_API.md`); CLI `verity correct|confirm|corrections|glossary`; MCP tool
  `verity_correct`.
- [x] P5-3 Apply (`verity/learn/apply.py`, hooks in the recipes failure_invalid, failure_missing, explain,
  where_computed, which_fn):
  - **pin**: the same question (normalised), the same intent with the same topic words, or the same meaning
    (similarity ≥ 0.80, only when the corrected function or line is named by words of the question; then the question
    is answered with the correction's intent). The corrected candidate comes first.
  - **hint**: the same intent with topic overlap ≥ 0.5, or similar meaning (≥ 0.70). It gives +50% to the corrected
    branch and +25% to its function. It can add the corrected candidate, but that candidate still has to win on its own
    score.
  - **glossary**: suggested on a correction (from the question's words and the identifiers at the line), applied only
    once accepted; `verity.toml [glossary]` is read too.
  - **disclosure**: `meta.learning.used`, an assumption line "Uses correction c-0003 (3 Oct 2026), recorded for this
    question …" (the text answer prints "Learned: uses correction c-0003 …"), and Undo.
  - Conflicting corrections on one question are flagged; the newest wins and both are named.
- [x] P5-4 Anchors (`verity/learn/anchor.py`): the line's code hash plus its function and branch.
  - After every index, lazily: a line that moved follows its code inside its function; changed or deleted code turns
    the correction **stale** (never applied, listed with the reason); it revives when the exact code returns.
  - Import re-anchors to the local copy and reports what is stale there.
- [x] P5-5 App:
  - Answer card "Is this the right place?" with **This one**, **✓ The answer was right** and **pick a line…**.
  - Pick mode in the source viewer: click a line, **Look elsewhere** (file chips and a function search), a note,
    **Use this line**. The question is re-asked at once.
  - A "learned" note with Undo at the top of answers that used a correction.
  - **Learned** panel (project bar, with counts and a stale dot): Corrections (filter, notes, Undo/Restore, Ask again),
    Glossary (accept, reject, add, remove), Share (export json / test suite / toml; import).
  - In the desktop app, export writes into Downloads.
- [x] P5-6 Proof on cJSON (`bench/learning_protocol.py`, on a private copy; results in
  `bench/results/realworld_heldout10_learning/`). The two numbers are kept apart:

  | Mode | Before (main · paraphrases · neighbours) | After one correction per miss: main · corrected asked again | Paraphrases of corrected | Neighbours (corrections used) | Stale after edit |
  |---|---|---|---|---|---|
  | Direct (no meaning search) | 4/14 · 2/10 · 2/6 | 13/14 · 9/9 (9 disclosed) | 2/6 (0 used one) | 2/6 (0) | OK |
  | Direct + meaning search | 4/14 · 2/10 · 2/6 | 12/14 · 8/8 (8 disclosed) | 4/6 (4 used one) | 2/6 (0) | OK |
  | Local model + meaning search | 4/14 · 2/10 · 3/6 | 12/14 · 8/9 (9 disclosed) | 4/7 (5 used one) | 3/6 (0) | OK |

  - Corrections not made: `cj-string-parser-callers` (a structural question with no single deciding line), and in
    the facts-only runs `cj-patch-unknown-op`: its answer already named the right line (cJSON_Utils.c:828) but not
    "status = 3", so Verity refused a correction to the same place and suggested confirming instead.
  - In the model run, the one corrected question still wrong is that same one: the pin was applied and disclosed,
    but the model's summary left out "status = 3".
  - **Below the spec's target for paraphrases (9 of 10).** Without meaning search, only word overlap links a
    paraphrase to a correction, which is not enough for reworded questions. With it, 4 of 6 paraphrases used the
    correction. The other two were a hint that lost to a stronger wrong candidate (it must win on its own score, by
    design) and a paraphrase at similarity 0.698 that was routed to a different kind of question.
  - The thresholds were chosen from the measured similarities on this project: paraphrases 0.60–0.89, neighbours
    0.45–0.76, unrelated ≤ 0.63. They were not tuned further, to avoid fitting the 6 paraphrases.
  - No neighbour or unrelated question used a correction in any run.
- Hardening after an independent read-only code review of the feature (each fix has a test):
  - picking an alternative that is a function (which_fn, explain, where_computed) failed; it now uses the function's
    definition line;
  - switch cases share the switch's line, so they were told apart only by side: candidates now carry the case's lines;
  - "…cannot be opened" pinned "…is opened" (the topic words drop negation): a negated question and one that names
    no trouble at all never count as the same question ("the closing quote never comes" and "…is missing" still do);
  - pins are ranked by how strongly they match (same words, then same topic, then same meaning), newest within each;
    correcting an answer replaces only a correction recorded for that very question; Undo clears "disagrees with";
  - import validates every field, checks each correction against this copy of the code before it becomes active,
    runs in one transaction, keeps glossary decisions made on this machine, and imports confirmed answers;
  - a hint is disclosed only if its place is still first after the local model's choice;
  - a locked or unreadable learned.db is refused, never moved aside as "damaged"; an answer never fails because of it;
  - a line in a file changed since the last index, or an alternative from before an edit, is refused;
  - re-anchoring also runs after a cancelled index, one at a time, and never overwrites an Undo made meanwhile;
    correction ids are taken in the same transaction as the insert (the app and the CLI can write at once);
  - an answer of a kind that does not hinge on one place (callers, flows, …) can no longer be "corrected", while
    every answer of a kind that does is correctable, also when it fell back to search (that is when pointing at the
    right place helps most); a correction matched as this question but not used by the recipe is always disclosed
    ("…could not be used here, so this answer comes from the analysis alone"); vectors from another embedding model
    are recomputed.
- Tests: `tests/test_learning.py` (30 tests), plus two in `tests/test_audit.py` (a macro's repeated call listed once;
  a unit test's helper never answering for the program) and one in `tests/test_engine_http.py` (a stale `ui/dist` is
  reported). They cover the store and its corruption recovery, pin and hint, an unrelated question left unaffected,
  every correctable intent, undo/restore, replacing a correction, the refusals, stale and revive, a moved line, an
  index.db reset, conflicts, the glossary (including verity.toml), an exported suite that passes, export/import round
  trip, the CLI and MCP, and every hardening fix above. Full suite: 202 passed, 1 skipped (PowerShell not installed).
- **Regression against `windows-first-run-desktop-app` (73972e7)** (`scripts/regress/capture.py`; each run indexes from
  scratch in its own data folder; the base branch ran from a worktree):
  - Direct, all 207 questions, two captures per side: 198/207 on both, **no pass/fail change**, and no index
    difference (the indexing code is byte-identical). The answers that differ beyond run-to-run variation were read by
    hand:
    - the intended macro wording (`rw-rtos-isrs`, `lfs-sync`, `lw-ip-checksum`, `lw-threads`);
    - `nm-wait-negative` (failing on both): before, a search fallback hiding the crash; now a wrong but visible place;
    - noise: `c-origin-lat` (hash seed, reproduced on both branches with fixed `PYTHONHASHSEED`), `lw-input-fn` /
      `lw-path-eth-tcp` (index write order), and `ucos-mutex-pcp` (evidence id).
  - Local model + meaning search, the six suites of the model baseline: **95/99 → 96/99**. The one change is
    `nm-wait-negative`, now right (read by hand: the setter's `value < 0` → `ArgumentException`, ModbusTransport.cs:70).
    The other 12 differing answers differ only in the model's summary paragraph; the checked facts are identical.
    0 invented names on both sides; rejected claims 11 → 12 (the extra one is in that answer: a call cited one line
    off, removed by the verifier).
  - The full record, with every compare output: `logs/phase5-regression.status`.
  - UI end-to-end 42/42 (screenshots regenerated; 19–21 new). Desktop self-test passes with no process left
    behind. Both design docs render (2/2 and 23/23 Mermaid diagrams).
- Note for long runs: background jobs started from a Claude Code session lose access to `~/Desktop` (macOS privacy)
  when the session ends ("Operation not permitted" on `.venv/pyvenv.cfg`). Two runs were hit this way (the base
  branch's NModbus model suite, and this branch's last three model suites); both were re-run, and only complete runs
  are reported.
- UI end-to-end (`ui/scripts/e2e-cdp.mjs`): the learning journey on a temporary copy of navgps-c. It picks a line in
  another file, the corrected answer comes back with the disclosure, then Undo, and the Learned panel. Screenshots
  19–21.
- Fixed on the way (found by the cJSON first run and the hand reading):
  - a crash (`TypeError`) when an indirect call had no known target, in failure answers (`nm-wait-negative`; with the
    crash gone, that answer then described a unit test's helper of the same name, so a function named by the question
    is now never a unit test's unless the question is about tests);
  - the wording "calls memset(FD_ZERO…)": it now reads "memset() (inside the macro FD_ZERO)";
  - repeated calls listed twice in "what happens next" answers;
  - the desktop app's fallback folder was a stale developer path: it now tries the folder it was built from;
  - setup and the desktop build scripts rebuilt the UI only when `ui/dist` was missing: after a pull or a branch
    checkout they served the old UI. They now also rebuild when any UI source is newer
    (`scripts/build_ui.sh --check`, `Test-UiStale` on Windows), and the engine prints a note when it serves a stale UI;
  - three messages still said the desktop app was macOS only or not ported to Windows (`setup.ps1`'s final summary,
    `check_prereqs.ps1`, and `check_prereqs.sh` on Linux);
  - the commands printed under each CLI answer ("correct it", "right?") were wrapped at the terminal width (80 columns
    in a file), which breaks them when copied: each is now one unwrapped line. The demo transcripts (`docs/DEMOS.md`)
    were regenerated with the local model; `run_demos.sh` now indexes with `--full` (every indexing phase is shown)
    and passes a relative project path (no local path in the transcripts).
- Open (Phase 5):
  - [ ] the pull request into `windows-first-run-desktop-app`, the user's manual test, and the merge;
  - [ ] Phase 5 on Windows: pytest, `bench/learning_protocol.py`, the UI end-to-end check (and inside the desktop app);
  - [ ] paraphrase matching below the spec's 9 of 10: measure any change on never-seen project #11, not on cJSON.
- Review of `windows-first-run-desktop-app` (before branching):
  - The CMake configure now runs in a private copy, with the generated twin mapped back. Shims (`stddef`, `stdint`,
    posix `pthread.h`, `sys/time.h`) resolve by subpath. Quoted includes fall back to system headers. The engine stops
    on stdin EOF (Windows desktop app). Plus the Windows desktop port and scripts.
  - Pass/fail is identical (198/207 Direct). libmodbus parse errors went from about 200 per file to about 0, and index
    times are unchanged. Verdict: an improvement with no regression.

## Windows setup (2026-10-02)
- [x] Engine made Windows-aware (`verity/osutil.py`):
  - executables with `.exe`; a liveness check that never kills (`os.kill(pid, 0)` terminates processes on Windows);
  - `taskkill` / `tasklist` instead of signals and `ps`; an `msvcrt` lock instead of `fcntl` (the indexer could not
    even import on Windows);
  - physical memory through `GlobalMemoryStatusEx` (the probe read 0 GB and would never pick a model);
  - cppcheck/dotnet/llama-server lookups; launchers `bin\verity.cmd` / `bin\verity.ps1` in UTF-8 mode.
- [x] Model choice from the system analysis: `models/selection.json`, read by the probe (wins only when its chat model
      is present, or when it says Direct mode); per-model llama-server options (e.g. `--n-cpu-moe`, `-ngl 0` for
      embeddings) reach the servers; ladder rung C3 = gpt-oss-120b.
- [x] `scripts/windows/setup.cmd|ps1`: 12 logged, resumable steps, no admin rights. Pinned and SHA256-verified: uv
      0.12.22, Python 3.12, llama.cpp b11293 (CUDA 13.4/12.4, Vulkan, CPU, ARM64; automatic fallback), .NET 10,
      cppcheck 2.22.0, models.
- [x] `scripts/windows/analyze.cmd|analyze_system.ps1`: hardware → build + model plan. `-StressTest` measures
      candidates the way Verity calls them (calibrated: 15 s for gpt-oss-20b on the M3 vs a real median of 12 s).
      `-Apply` writes the selection; `-QualityCheck` runs benchmark subsets.
- [x] `scripts/make_windows_bundle.sh`: one 65 MB zip of the code for the PC.
- [x] Checked without a Windows PC:
  - parse and the Windows PowerShell 5.1 compatibility lint (PSScriptAnalyzer);
  - 36 planning checks with simulated machines (`scripts/windows/tests/plan.tests.ps1`), which caught a null-array
    bug that would have passed "None" to llama-server;
  - real downloads, checksums and extraction;
  - all 16 URLs and sizes;
  - win_amd64 wheels for all 49 packages (win_arm64 lacks cryptography → x64 Python under emulation on ARM);
  - the stress-test code against a real llama-server;
  - the PowerShell-written selection read by the Python engine;
  - pytest 157 passed on macOS.
- [x] First real run on a Windows PC with a GPU (2026-10-02; a PC without a GPU is still to do)
- [x] Native desktop app on Windows (2026-10-02, below)
- [ ] Windows sandbox adapter for scenarios

### First real Windows run (2026-10-02)
PC: Windows 11 Pro 10.0.26200, NVIDIA RTX 5070 12 GB (driver too old for the CUDA 12.4 builds → Vulkan), 20 logical
CPUs, Visual Studio 2022 Build Tools with Windows SDK 10.0.26100, cmake 4.2.1 on PATH (from Miniconda), Rust 1.95.
- `setup.cmd`: every step OK in 53.7 min; 2 warnings: the CUDA driver, and pytest 10 failed / 141 passed / 13 skipped.
- **Engine bugs found and fixed** (all generic; none was visible on the Mac):
  - The API pack's `writes` loader dropped every name starting with `_` (meant for `_doc`), losing
    `_InterlockedExchange`, `_InterlockedIncrement`, `_snprintf` and `__atomic_store_n`. With the real Windows SDK,
    `InterlockedExchange` is a macro for `_InterlockedExchange`, so writes to `g_alarmActive` / `g_linkState` vanished
    (msvc-silent and msvc-alarm-flag failed). `c_clang._load_api_writes` now skips only non-list metadata.
  - A "quoted" include missing from the project never fell back to the system headers: it got an empty stand-in that
    hid the real header. libmodbus includes `"stdint.h"` under `_MSC_VER`, so `uint32_t` became an opaque stub type
    (20-32 errors per file, and under heavy load file parses ran into the 180 s timeout, so indexes differed from run
    to run). `plan_c_profile` now checks both include forms against the system headers.
  - POSIX headers that Windows lacks were empty stand-ins: `pthread_t t;` misparsed and pthread thread wrappers were
    missed (lwIP's unix port: 23 instead of 24 threads); SOEM's port header left `struct timespec` undefined. New
    knowledge-pack headers `verity/extract/shims/posix/pthread.h` and `sys/time.h` (used only when the host lacks
    them; `struct timeval` only when winsock has not defined it); `find_shim` now finds headers with a folder part.
  - The libc shims hard-coded 64-bit-Unix sizes (`size_t` = unsigned long …); on a Windows PC without Visual Studio
    every MSVC file failed to parse cleanly (typedef redefinition, F3). They now use the compiler's own
    `__SIZE_TYPE__`, `__PTRDIFF_TYPE__`, `__WCHAR_TYPE__`, `__INTPTR_TYPE__`, `__UINTPTR_TYPE__` (identical on the
    Mac/Linux); the libc `time.h` shim gained C11 `struct timespec`.
  - **The CMake capture ran `cmake` configure inside the user's project**, and configure scripts may write there:
    lwIP rewrote `doc/doxygen/lwip.Doxyfile` (CRLF on Windows). It now runs on a private copy in the work folder
    (`work/cmake-src`, VCS folders skipped, trees over 1 GiB not copied), maps the compile database back to the
    project, and keeps headers the configure generated reachable through the copy, after the project's own folders.
    On Windows the default Visual Studio generator exports no compile database, so there the capture adds nothing yet.
- **Tests made platform-aware:** `test_answers` (5 cases checked against a real run skip without a sandbox, as in
  `test_dialects`), the MSVC shim check (the real SDK is used when present), the orphaned-model-server test (no
  symlink to `/bin/sleep`), `test_offline` reads UTF-8. New tests: CMake capture never writes into the project, maps
  the copy back; POSIX knowledge-pack headers parse cleanly; engine stdin handling (below).
- **UI end-to-end script** (`ui/scripts/e2e-cdp.mjs`): asks the engine whether a sandbox exists and reports the run
  checks as SKIP without one (it aborted at 21/23 before); opens and indexes the sample itself (a fresh data folder
  has no recent projects); screenshots go to a temporary folder unless `--shots` names one (it overwrote
  `docs/screenshots`); `CDP_ATTACH=1` drives an already open page such as the desktop app's window; an answer that is
  back before the progress line could be seen is a SKIP, not a FAIL.
- **Results on this PC** (final code):
  - pytest: 152 passed, 18 skipped (sandbox-only), the same in a simulated PC without Visual Studio (a pytest plugin
    hiding the SDK).
  - Direct mode, all 14 suites (207 questions), two baselines of the old code (identical scores) against the new
    code: no question went from pass to fail; msvc-silent and msvc-alarm-flag now pass, soem-error-list-full too
    (SOEM 12/12, as on the Mac). Every differing answer was read: corrections that now match the Mac's recorded
    answers (lw-ttl-zero, lw-threads, msvc-threads, msvc-crc), order or count wording, internal fields only, and one
    equally valid alternative (mb-timeout names libmodbus's TCP select() timeout check, identical code to the RTU one
    the Mac names). Remaining Windows misses: 11 need the sandbox, 9 are the Mac's own known misses.
  - Local model + meaning search (gpt-oss-20b on Vulkan), six suites (99 questions), one baseline
    of the old code against the new code: 81 → 83/99; msvc-silent and msvc-alarm-flag now pass, nothing went from
    pass to fail; 0 invented names and 11 rejected claims in both runs (as on the Mac). Of the other 8 differing
    answers, 2 follow the changed MSVC facts and 6 are on projects whose index is identical (the model's wording
    varies between runs).
  - UI end-to-end: 29/29 with 2 skipped (sandbox) in Chrome, and 29/29 with 2 skipped inside the desktop app.
- Not fixed, noted: on Windows answers name `_InterlockedExchange()` (the SDK macro's target) where the Mac says
  `InterlockedExchange()`; libmodbus still has stand-ins for POSIX-only headers (`termios.h`, `sys/socket.h`), F3, and
  indexes in about 75 s against 6.5 s on the Mac (each file is parsed five times against the real SDK headers).

### Desktop app on Windows (2026-10-02)
- `desktop/src-tauri/src/main.rs`: `.venv\Scripts\python.exe`, started without a console window and in UTF-8 mode;
  stopped by closing its stdin pipe, then `taskkill /T /F` on what is left after 8 s; the `\\?\` prefix that
  `canonicalize()` adds is removed (the engine inherited `\\?\C:\...` as its folder and data folder); Unix signal and
  process-group code gated to Unix; `libc` is a Unix-only dependency. The Mac code paths are unchanged.
- Engine: `VERITY_STOP_ON_STDIN_EOF=1` stops it cleanly once its stdin pipe closes, which also happens when the app is
  killed (Windows has no SIGTERM, and `os.getppid()` never changes there). First built as a thread blocked reading
  stdin: on Windows that stalled the start of the parser worker processes, so the app never finished indexing. Found by
  the end-to-end test; the pipe is now polled every 2 s with `PeekNamedPipe` (select on Unix), and a test indexes a
  project through such an engine.
- `scripts\windows\build_desktop.cmd|ps1` (checks Rust, the C++ Build Tools, WebView2 and Node.js and says what is
  missing; `tauri build --no-bundle` → `desktop\target\release\Verity.exe`; `-Shortcut` adds Desktop and Start menu
  shortcuts) and `run_desktop.cmd`. First build 1 min 56 s, later ones about 30-40 s, 0 compiler warnings.
- Verified on this PC, on the final binary: the self-test (open, index, answer, quit) in 12 s; the full UI end-to-end
  check inside the app's own window (WebView2 DevTools); the native folder picker driven through the real Windows
  dialog (the chosen project opened, indexed and answered); closing the window, killing `Verity.exe` and an engine
  crash with **Try again** each leave no process behind (engine, model server, workers and web view gone in under
  2.5 s, engine shut down cleanly); the engine-cannot-start page; the Desktop shortcut and `run_desktop.cmd`; no
  console window at any point; an answer worded by the local model inside the app
  (msvc-telemetry, 66 s including the model start), after which both model servers stopped with the app.
- Not verified: the macOS build (a Tauri dependency compiles Objective-C, so it cannot be type-checked from Windows):
  run `bash scripts/build_desktop.sh` on the Mac once.

## Prerequisites check and setup from a bare copy (2026-10-02)
- [x] `scripts/check_prereqs.sh` (macOS/Linux, bash 3.2, no Python needed) and `scripts/windows/check_prereqs.cmd|ps1`
      (Windows PowerShell 5.1). Both install nothing and report five groups:
  - this machine: platform, memory and the model it can run, disk, internet;
  - what to install yourself;
  - what setup installs on its own (DONE/TODO, with sizes);
  - optional extras;
  - what is not needed (Ollama, Docker, system LLVM/Python, Xcode/Visual Studio, git, cloud, admin).

  They share the same JSON shape (`--json` / `-Json`). Exit 0 means nothing blocks setup.
- [x] The Mac setup works from a bare copy (the zip leaves out `tools/` and `models/`):
  - the helpers moved to `scripts/setup_toolchains.sh` and `scripts/download_models.sh`; the old paths are shims;
  - llama.cpp is picked per platform (Apple silicon even under Rosetta, Intel Mac, Linux x64/arm64);
  - cppcheck is pinned to 2.22.0 and skipped cleanly without the Xcode tools/cmake (no `git` call any more);
  - models are chosen by memory (`--model auto|direct|C1|C2|C3`, shared table `scripts/lib/models.sh`) and every
    download is checked against the pinned SHA256.
- [x] No "install developer tools" pop-ups on a Mac without Xcode tools: `osutil.mac_dev_tools()` guards the
      `/usr/bin` clang/cc/xcrun stubs in the probe, the recon sysroot lookup and the scenario runner.
- [x] The `.cmd` launchers share `lib\preflight.cmd`: it removes the downloaded mark, and it stops with a clear
      message if Group Policy enforces AllSigned/Restricted (no workaround attempted).
- [x] Verified:
  - simulated empty Mac (fresh bundle copy, empty HOME, system PATH only, Xcode tools hidden): the check reported
    exactly one blocker (uv/Python 3.12). With uv, `setup.sh --model direct` ran end to end (wheels only,
    `UV_NO_BUILD=1`; cppcheck skipped as designed). The check then said "already set up", and a facts-only answer
    on navgps-c was correct;
  - the Windows check on seven simulated PCs and a simulated finished install. This caught a bug where
    "Unrestricted" matched "Restricted";
  - the 5.1 lint (PSScriptAnalyzer) is clean; `tests/test_prereqs.py` has 7 tests.
- [ ] Linux: the check and setup are written for it but not run on Linux yet.

## Product name: Verity (2026-10-02)
- [x] The product is now called **Verity**, and the name was changed everywhere:
  - the Python package `verity/` and launchers `bin/verity`, `bin/verity.cmd`, `bin/verity.ps1`;
  - environment variables `VERITY_*`, the hints file `verity.toml`, and the MCP server and tools `verity_*`;
  - the C# worker `VerityRoslyn`, the desktop app `Verity.app` (`dev.verity.desktop`) and the Windows scripts;
  - the docs, tests, benchmark records and logs, and the internal `vr` prefix of generated identifiers.
- [x] New mark: a check mark in the same blue tile. It is used for the app icon, the UI header, the favicon and the
      start-up page.
- [x] Screenshots and demo transcripts regenerated from the renamed app. The two design documents (now in docs/development) were
      updated (the one-pager is now `VERITY_LEARNING_FROM_CORRECTIONS_ONE_PAGER.html`).
- [x] Regression-checked against a baseline recorded before the change:
  - pytest: 164 passed before and after;
  - all 207 benchmark questions in Direct mode: 198/207 before and after, the same per question;
  - 99 questions with the local model and meaning search: 95/99 before and after; 0 invented names and 11 rejected
    claims in every run;
  - the UI end-to-end check: 30/30;
  - the desktop self-test; the CLI and MCP smoke check;
  - the Windows lint and the 36 planning checks.

  Every answer either matched a baseline run word for word, or belonged to the answers that already vary between two
  runs of the unchanged code (the order of tied candidates, memory addresses in scenario output).
- [x] Found while checking, not caused by the rename (open):
  - A full re-index on an existing data folder keeps some rows from earlier runs: stored vectors, and on one sample
    244 symbols where a fresh index has 111. Answers were unaffected in the benchmark.
  - For nm-wait-negative, the verification notes contain a Python traceback (an exception in `recipes.py`
    `Ctx.D`). Fixed in Phase 5 (an indirect call with no known target).

## Moved to Git (2026-10-02)
- [x] The project now lives in the Git repository `verity-mati` (folder `~/Desktop/verity-mati` on the Mac), so work can
      continue on a Windows PC:
  - `.gitignore` leaves out what setup recreates (models, tools, `.venv`, vendor wheels, the C# worker build, `data/`,
    new setup logs);
  - `.gitattributes` turns off line-ending conversion (`.cmd` files are CRLF), so a Windows checkout is
    byte-identical;
  - the design documents moved into `docs/development/`.
- [x] A fresh clone from GitHub, with the C# worker built from source, gave pytest 163 passed + 1 skipped (pwsh
      absent), suite 31/31 and NModbus 10/12, the same as before.
- [x] Fixed: cppcheck built by `scripts/setup_toolchains.sh` has its install folder compiled in, so moving the folder
      broke it ("Failed to load std.cfg", and the cross-check silently returned nothing). Setup now adds a relative
      `bin/cfg` link, which cppcheck also searches.
- [x] Added the before/after evidence tools `scripts/regress/capture.py` and `compare.py`, and
      `ui/scripts/extra-shots.mjs` (screenshots 16-18).
