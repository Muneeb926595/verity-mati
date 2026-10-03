# Verity engine API (for the desktop app and browser mode)

The engine is a local process: `.venv/bin/python -m verity.engine.server --port 0 --print-ready`.
With `--print-ready` it prints ONE JSON line on stdout once listening:
`{"ready": true, "port": 53211, "token": "…", "url": "http://127.0.0.1:53211/?token=…", "pid": 123}`
It also writes the same JSON to `data/engine.json`. It listens on 127.0.0.1 only. Set env `VERITY_EXIT_WITH_PARENT=1`
so it exits when the parent (the desktop app) dies. With `VERITY_STOP_ON_STDIN_EOF=1` it also shuts down cleanly once
its stdin pipe is closed, checked every 2 s without reading it: the Windows desktop app stops the engine this way,
and the pipe also closes when the app is killed (Windows has no SIGTERM, and `os.getppid()` never changes there).

Every request needs the token: header `Authorization: Bearer <token>` (or `?token=` for EventSource).
Cross-origin requests are rejected unless the Origin is `http://127.0.0.1…`, `http://localhost…`, `tauri://…`, or
`http(s)://tauri.localhost`.

## Transport
- `POST /rpc` — JSON-RPC 2.0: `{"jsonrpc":"2.0","id":1,"method":"project.status","params":{"path":"/abs/folder"}}`
  → `{"jsonrpc":"2.0","id":1,"result":…}` or `{"error":{"code":-32001,"message":"human readable"}}`.
  Error messages are written for end users — show them as-is.
- `GET /events?token=…` — Server-Sent Events, one JSON object per event (`event:` = its `type`). Keep one open.
- `GET /` — serves `ui/dist/index.html` (browser mode); `/assets/*` static.

## Methods
| method | params | result |
|---|---|---|
| `engine.info` | – | `{version, machine:[lines], llm:{mode, chat:{label,running,tok_per_s}, embed:{…}}, settings, offline:true}` |
| `settings.set` | `llm_mode?: "auto"\|"direct"\|"fast"`, `embeddings?: bool` | settings |
| `project.recent` | – | `[{path, name, opened, exists, label?, ready?, languages?}]` |
| `project.open` | `path` | project status (below); also adds to recent |
| `project.status` | `path` | `{path, name, id, state, label, ready, languages, counts, fidelity, stale:[files], indexed_at, summary, jobs:[job]}` |
| `project.forget` | `path` | `{ok}` (removes from the recent list only) |
| `index.start` | `path`, `full?` | job (starts or returns the running one) |
| `ask` | `path`, `question`, `use_llm?` | job (answer arrives in `job.done` event → `job.result`) |
| `job.get` | `job`, `with_result?` | job incl. `result` |
| `job.cancel` | `job` | job |
| `job.list` | – | recent jobs |
| `search` | `path`, `query`, `k?` | `[{key, name, kind, file, line, score, why:[…]}]` |
| `symbol.get` | `path`, `key?` or `name?` | symbol facts incl. `card`, callers, callees, writers, readers |
| `source.get` | `path`, `file`, `around?` or `start?/end?` | `{file, start, end, total, lines:[str], stale, lang}` (read-only, only inside the project) |
| `history.list` | `path`, `limit?` | `[{id, question, created}]` |
| `answer.get` | `path`, `id` | stored answer object |

### Learning from corrections (per project, stored in `data/repos/<project>/learned.db`)
| method | params | result |
|---|---|---|
| `answer.correct` | `path`, `answer_id`, and `pick` (an alternative's `rank`, 2 or 3) or `target: {file, line}`; `note?`, `author?` | `{correction, glossary_suggestions:[term], replaced:[ids], message}` |
| `answer.confirm` | `path`, `answer_id`, `note?` | `{confirmation, message}` (kept as a test case) |
| `answer.unconfirm` | `path`, `id` | `{ok}` |
| `corrections.list` | `path`, `status?: "active"\|"stale"\|"removed"\|"all"` | `{corrections:[correction], confirmations, glossary:[term], counts:{active, stale, removed, suggested, accepted, confirmations}, recovered_from, events}` |
| `corrections.summary` | `path` | `{counts, recovered_from}` (cheap; for the project bar) |
| `corrections.remove` / `corrections.restore` | `path`, `id` | `{correction, message}` (Undo / Restore) |
| `corrections.note` | `path`, `id`, `note` | `{correction}` |
| `corrections.export` | `path`, `format?: "json"\|"bench"\|"toml"`, `save?` | `{format, filename, data}`; with `save` the engine writes the file into Downloads (never overwriting) and returns `{format, filename, saved}` |
| `corrections.import` | `path`, `data` (a `json` export) | `{imported, duplicates, skipped:["c-0003 (why)"], stale, glossary, confirmations, message}`. Each correction is checked against this copy of the code first (active where its code is found, stale otherwise); one transaction, so a bad file changes nothing; glossary decisions made on this machine are kept |
| `glossary.list` | `path` | `[term]` |
| `glossary.accept` | `path`, `term`, `identifiers?` | `{term, message}` |
| `glossary.add` | `path`, `term`, `identifiers` | `{term, message}` |
| `glossary.reject` / `glossary.remove` | `path`, `term` | `{term, message}` / `{ok, message}` |

A correction: `{id:"c-0001", question, intent, kind:"pick"|"line", wrong:{fn, file, line, side, label},
target_fn, target_name, target_file, target_line, branch_line, side:"then"|"else"|"case"|null, target_label, note,
author, status:"active"|"stale"|"removed", status_reason, conflict:[ids], replaces, uses, last_used, created,
created_label, source:"local"|"import"}`. A glossary term: `{term, identifiers:[…], status:"suggested"|"accepted"|
"rejected", source (the correction id, "manual" or "import"; "verity.toml" with read_only:true), created, updated}`.
Refusals are `-32602` errors with a message for the user: a line outside the project or outside any function, a file
changed since the last index (or, for an alternative, since the answer), an answer that does not hinge on one place
(`meta.learning.correctable` false), the place the answer already gave, an unknown id.

`state` of a project: `new` (not indexed), `running`, `complete`, `cancelled`, `failed`. `label` is a ready-to-show
sentence ("Ready", "Ready — 3 file(s) changed since the last index", "Not indexed yet", "Indexing…"). `ready` = can ask.

A job: `{id, kind:"index"|"ask", project, state:"queued"|"running"|"done"|"failed"|"cancelled", progress, error, result?}`.

## Events
- `index.progress` — `{job, project, phase, label, phase_index (1-based), phase_total (7), state:"running"|"done"|"skipped"|"failed", detail, done?, total?, eta_s?, file?, elapsed_s}`.
  Phases in order: Looking at your project files · Working out how the code is built · Reading the C code ·
  Reading the C# code · Double-checking with a second parser · Connecting the pieces together · Preparing search.
  Friendly headline: "Understanding your C project… {completed} of 7 parts complete" where completed =
  phase_index − (state=="running" ? 1 : 0). "Show details" lists the phases with their `detail` text.
- `ask.progress` — `{job, step (1-based), steps (4), label, detail, elapsed_s}` labels: Understanding your question ·
  Looking through the code · Checking the answer against the code · Writing the answer.
- `job.queued`, `job.done`, `job.failed`, `job.cancelled`, `job.cancelling` — `{job: <job object>}`; `job.done` for an
  ask carries `job.result` = the answer object.
- `learning.changed` — `{project}` after any correction, confirmation, glossary or import change (refresh the
  Learned panel and counts). After an index, `job.result.learning = {moved, stale, revived}` (correction ids) when
  any correction followed its line, turned stale or came back.

## The answer object (see `ui/fixtures/answer_*.json` for real examples)
```
{ question, answer: [paragraph strings — inline `code` in backticks],
  flow: [{label, file, line, detail}],              // "How the data flows": render as a vertical arrow chain
  evidence: [{id, kind, file, line, end_line, text, note, status, status_label, sym, also?:[ids]}],
  status, status_label, status_counts: {CONFIRMED: n, …},
  sections: {
     reasoning: [{text, status, status_label, why, verified, check, ev:[ids]}],      // "Show reasoning / evidence"
     execution_path: [{label, file, line, status?, note?}],                          // "Show execution path"
     related: [{key, label, file, line, end_line}],                                  // "Show related code"
     assumptions: [str],                                                             // "Show assumptions"
     verification: {report:{claims, verified, rejected, evidence, evidence_ok, stale_files}, unknowns:[str],
                    notes:[str], rejected:[claims]} },                               // "Show verification"
  run: null | {ok, runs:[{label, inputs, calls:[fn names], branches:[{file,line,cond,taken,evaluated}], state:{k:v},
               hw_calls:[str], seconds}]},
  id,                                                // pass to answer.correct / answer.confirm
  meta: {seconds, prose:{mode:"direct"|"model"|"model+facts", model?, fallback?}, intent,
         learning: {correctable,                    // show "Is this the right place?" (true for every answer of a
                                                    // kind that uses corrections: failure_invalid, failure_missing,
                                                    // explain, purpose, where_computed, which_fn)
                    candidates:[{rank, fn, file, line, side, span, label, text, score, what}],
                                                    // the top places considered (up to 3); line is null for a
                                                    // function; span = a switch case's lines; what = check | function | symbol | assignment
                    alternatives:[{rank, fn, file, line, side, span, label}],  // close runners-up ("This one")
                    used:[{id, how:"pin"|"hint", match:"same"|"words"|"meaning"|"similar", changed, created, note,
                           target, question, conflicts, meaning?}],
                    glossary_used?,                 // accepted glossary terms the question used
                    rerouted?,                      // the kind of question taken from a same-meaning correction
                    pin_not_applied?: {id, target, why},   // a correction recorded for this question that this
                                                    // answer could not use (it comes from the analysis alone)
                    topic}} }                       // the question's topic words, as corrections are matched
```
An answer that used a correction also says so in `sections.assumptions` ("Uses correction c-0003 (2 Oct 2026),
recorded for this question: <note>"; for a match other than the same question, the recorded question is quoted). A
hint is listed only when its place is still first in the final ranking. A correction that matched but could not be
used adds "Correction c-0003 was recorded for this question, but … this answer comes from the analysis alone".
Status words for users: CONFIRMED "Confirmed in the code", OBSERVED "Observed by running the code",
INFERENCE "Inferred (likely, not certain)", RUNTIME-DEPENDENT "Depends on runtime values", UNKNOWN "Could not be determined".
