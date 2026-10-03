# Verity desktop app (Tauri 2)

A thin native shell for macOS and Windows. It starts the local engine, shows the Verity UI in a native window with a
native folder picker, and stops the engine when you quit.

## Build and run

```bash
bash scripts/build_desktop.sh            # release → desktop/target/release/bundle/macos/Verity.app
bash scripts/build_desktop.sh --debug    # quicker debug build → desktop/target/debug/bundle/macos/Verity.app
bash scripts/run_desktop.sh              # run it from the terminal (Ctrl-C, Cmd+Q or closing the window quits)
bash scripts/run_desktop.sh --open       # launch it like any Mac app
```

Windows (details in `scripts/windows/README.md`):

```
scripts\windows\build_desktop.cmd [-Shortcut] [-DebugBuild]   → desktop\target\release\Verity.exe (no installer)
scripts\windows\run_desktop.cmd                              # starts it (builds it first if needed)
```

It uses `tools\cargo` when present, else the user's rustup, plus the Visual Studio C++ Build Tools, WebView2 and
setup's `tools\node`; the build is `tauri build --no-bundle`, so `Verity.exe` stays inside the Verity folder.

On macOS the scripts use the Rust toolchain inside the project (`tools/cargo`, `tools/rustup`), the project npm cache
(`tools/npm-cache`), and `CARGO_TARGET_DIR=desktop/target`. The Tauri CLI is the prebuilt npm package
`@tauri-apps/cli`. `build_desktop.sh` also rebuilds `ui/dist` first (`SKIP_UI=1` skips that only while `ui/dist` is
up to date with the UI sources). On Windows, `build_desktop.ps1` rebuilds it when it is missing or stale, and
`-BuildUI` forces it. The network is
needed only for the first build, to download crates and packages. The finished app is fully offline.

## How it works

1. **Find the Verity folder (ROOT).** The shell uses `VERITY_ROOT` if it is set and valid. Otherwise it walks up
   from the executable until it finds a folder that contains both `verity/` and `.venv/` (on Windows without the
   `\\?\` prefix that path resolution adds, so the engine sees ordinary `C:\...` paths). Then it tries the folder it
   was built from (two levels above `desktop/src-tauri`), and as a last resort `/opt/verity` (Windows: `C:\verity`).
2. **Start the engine.** It runs `ROOT/.venv/bin/python -m verity.engine.server --port 0 --print-ready` with
   `VERITY_EXIT_WITH_PARENT=1` in its own process group, then reads the one-line ready JSON (`{port, token, …}`)
   from the engine's stdout, waiting at most 60 s. A bundled start-up page (`desktop/splash/`) says "Starting the
   engine…" in the meantime. If the engine cannot start, that page shows the reason and the engine's last output,
   plus a **Try again** button. On Windows the engine is `ROOT\.venv\Scripts\python.exe`, started without a console
   window, in UTF-8 mode (as `bin\verity.cmd` does), with its stdin connected to a pipe the app holds and
   `VERITY_STOP_ON_STDIN_EOF=1`.
3. **Show the UI.** The window is pointed at `http://127.0.0.1:<port>/?token=<token>`, the UI that the engine serves
   from `ui/dist`. The page and the engine share an origin, so there is no CORS to configure, and the engine's
   Origin check already allows `127.0.0.1`. The folder picker (`dialog:allow-open`) and the shell's own commands
   are granted to that page by a `remote` capability for `http://127.0.0.1:*`
   (`src-tauri/capabilities/engine-ui.json`). Nothing else is exposed.
4. **Stop the engine.** When the app exits (window closed, Cmd+Q, or `app.exit`), the shell sends SIGTERM to the
   engine's process group, waits up to 8 s for a clean shutdown, then sends SIGKILL to anything left, such as a
   model server the engine started. SIGINT and SIGTERM sent to the app do the same. If the app is killed outright,
   the engine notices that its parent is gone and exits within about 2 s (`VERITY_EXIT_WITH_PARENT`).
   **Windows** has neither signals nor a parent check that works there (`os.getppid()` never changes, and the venv
   launcher sits between app and engine): the shell closes the engine's stdin pipe, which also happens on its own
   when the app is killed or crashes. The engine polls that pipe every 2 s without reading it (a read blocked in a
   thread stalls the start of the parser worker processes on Windows), shuts down cleanly, and stops its model
   server. After 8 s the shell ends whatever is left of the engine's process tree (`taskkill /T /F`).
5. **If the engine crashes** while the app is open, the window goes back to the start-up page and offers
   **Try again**.

Engine output goes to `data/logs/desktop-engine.log`.

## Self-test

`VERITY_DESKTOP_SELFTEST=1|close|quit` makes the shell run a small script in the loaded UI. The script reports
whether the UI rendered, whether the Tauri bridge is present, and whether the folder-picker permission is granted.
The report is printed as a `SELFTEST {...}` line. With `VERITY_DESKTOP_SELFTEST_PROJECT=<folder>` it also opens that
project, asks a question and reports the answer's sections (including **Is this the right place?**);
`VERITY_DESKTOP_SNAPSHOT_DIR=<dir>` saves a picture of each stage (macOS). With `close` or `quit`, the app then closes
its window or quits, which exercises the shutdown path.

```bash
VERITY_DESKTOP_SELFTEST=close desktop/target/release/bundle/macos/Verity.app/Contents/MacOS/Verity
pgrep -f verity.engine.server    # → nothing left behind
```

On Windows the same works with `Verity.exe` (no web-view snapshots there). The full browser check can also drive the
app's own window over the Chrome DevTools protocol of WebView2: start `Verity.exe` with
`WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222`, then run
`CDP_ATTACH=1 VERITY_URL=<url from data/engine.json> node ui/scripts/e2e-cdp.mjs`.

## Files

| path | what |
|---|---|
| `src-tauri/src/main.rs` | engine start/stop, ROOT resolution, commands (`engine_status`, `restart_engine`, `pick_folder`, `selftest_report`) |
| `src-tauri/tauri.conf.json` | window (title "Verity", 1200×800, min 760×540), bundle settings |
| `src-tauri/capabilities/` | `splash` (the local start-up page) and `engine-ui` (the page served on 127.0.0.1) |
| `src-tauri/icons/` | app icons (generated from `icon-source.png` with `tauri icon`) |
| `splash/index.html` | the start-up and error page bundled into the app |
