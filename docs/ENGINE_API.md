# Verity engine API (for the desktop app and browser mode)

The engine is a local process: `.venv/bin/python -m verity.engine.server --port 0 --print-ready`.
With `--print-ready` it prints ONE JSON line on stdout once listening:
`{"ready": true, "port": 53211, "token": "…", "url": "http://127.0.0.1:53211/?token=…", "pid": 123}`
It also writes the same JSON to `data/engine.json`. It listens on 127.0.0.1 only. Set env `VERITY_EXIT_WITH_PARENT=1`
so it exits when the parent (the desktop app) dies.

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
  meta: {seconds, prose:{mode:"direct"|"model", model?, fallback?}, intent} }
```
Status words for users: CONFIRMED "Confirmed in the code", OBSERVED "Observed by running the code",
INFERENCE "Inferred (likely, not certain)", RUNTIME-DEPENDENT "Depends on runtime values", UNKNOWN "Could not be determined".
