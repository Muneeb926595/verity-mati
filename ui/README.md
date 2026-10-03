# Verity UI

The user interface for Verity: a small SolidJS + TypeScript single-page app with hand-written CSS (light and dark
follow the system setting). It does no heavy work itself. Everything comes from the local engine.

## Build

```bash
bash scripts/build_ui.sh          # → ui/dist/index.html + ui/dist/assets/*
bash scripts/build_ui.sh --check  # builds nothing; exit 0 = ui/dist is missing or older than the sources
```

`ui/dist` is not in Git. Setup, `scripts/run_desktop.sh` and the desktop build scripts rebuild it when it is missing
or older than any source file (after a pull or a branch checkout), and the engine prints a note when it serves a stale
build.

The script uses the project-local npm cache (`tools/npm-cache`). It needs the network only the first time, to
download packages. The built app never loads anything from the network: no CDNs and no web fonts (it uses the
system font stack).

Bundle size: about 34 KB of gzipped JS plus 8 KB of CSS (3 Oct 2026; the budget is 150 KB).

## Run (browser mode)

```bash
.venv/bin/python -m verity.engine.server --port 8765 --token testtoken
# open http://127.0.0.1:8765/?token=testtoken
```

The engine serves `ui/dist/index.html` at `/` and `ui/dist/assets/*` at `/assets/*`. Only those two paths are
served, which is why Vite is configured with `base: "/"`, no `public/` folder, and an inline favicon. Rebuilding the
UI while the engine runs is fine: reload the page. You do need to restart the engine if `ui/dist/assets` did not
exist when the engine started.

For live development, run `npm run dev` inside `ui/`. The Vite dev server proxies `/rpc` and `/events` to an engine
on port 8765. Open `http://localhost:5173/?token=testtoken`.

## How it connects to the engine

- **Where:** `window.__VERITY__ = {port, token}` is used if a shell injected it. Otherwise the UI uses the page's
  own origin and the `?token=` query parameter. The desktop app loads the engine-served page, so it takes the
  second path too.
- **Calls:** JSON-RPC 2.0 over `POST /rpc` with `Authorization: Bearer <token>` (see `docs/ENGINE_API.md`).
  Engine error messages are shown to the user as they are.
- **Progress:** a single `EventSource` on `/events?token=…` receives `index.progress`, `ask.progress`, `job.*` and
  `learning.changed` (which refreshes the Learned counts and panel).
  It reconnects with backoff, shows a small "Reconnecting…" pill while the stream is down, and re-syncs when it
  comes back. Every running job is also polled with `job.get` if its events go quiet, so a dropped event can never
  leave the UI stuck. Bursts of index progress are merged into one update per animation frame.
- **Folder picker:** inside the desktop app it uses `@tauri-apps/plugin-dialog` (with a shell-command fallback).
  In a browser it shows a "Folder path" text field.

## Layout of `src/`

| file | what |
|---|---|
| `api.ts` | connection, `rpc()`, the SSE stream, the job store (progress, results, waiters, safety polling) |
| `router.ts` | two screens: Home and `#/project/<path>` (a reload keeps you in the project) |
| `components/Home.tsx` | title, Open Project (native picker or path field), recent projects |
| `components/ProjectView.tsx` | status pill + Update, indexing card (7 phases, cancel), question box + examples, ask progress, recent questions; the **Learned** button (counts, stale dot), correcting and re-asking, the Undo banner |
| `components/AnswerView.tsx` | question, answer, data-flow chain, evidence, certainty line, the five "Show …" sections, sandbox-run table; the "learned" note (with Undo) on answers that used a correction, and the **Is this the right place?** card (This one · The answer was right · pick a line…) |
| `components/SourceViewer.tsx` | read-only source panel (side panel on wide screens, a modal on narrow ones), with a tiny C/C# highlighter (`highlight.ts`); pick mode for corrections (click a line, **Look elsewhere**: file chips and a function search, a note, **Use this line**) |
| `components/LearningPanel.tsx` | the Learned panel: Corrections (filter, notes, Undo/Restore, Ask again, confirmed answers), Glossary (accept, reject, add, remove), Share (export json / test suite / toml, import) |
| `types.ts`, `util.ts` | the engine's payload types (answer, correction, glossary term…); small helpers such as day-month-year dates |
| `components/Settings.tsx` | answer wording mode (Auto / Facts only / Fast), machine and model info |
| `fixtures/` | real engine payloads the UI was built against |

## End-to-end check

`ui/scripts/e2e-cdp.mjs` drives a real Chrome over the DevTools Protocol against a real engine. It walks the whole
journey (home → open → index with progress and cancel → ask → flow and evidence → source viewer → five sections →
scenario run → history → settings → phone width → dark mode → learning: on a temporary copy of navgps-c, pick a line
in another file, the corrected answer with its disclosure, Undo, the Learned panel) and runs 42 checks. Screenshots go
to a temporary folder unless `--shots docs/screenshots` is given:

```bash
.venv/bin/python -m verity.engine.server --port 8765 --token testtoken &
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --remote-debugging-port=9222 \
    --user-data-dir=/tmp/verity-chrome-profile &
node ui/scripts/e2e-cdp.mjs [--shots docs/screenshots]
```
