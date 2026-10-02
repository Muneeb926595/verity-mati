# Verity desktop app (Tauri 2)

A thin native shell for macOS. It starts the local engine, shows the Verity UI in a native window with a native
folder picker, and stops the engine when you quit.

## Build and run

```bash
bash scripts/build_desktop.sh            # release → desktop/target/release/bundle/macos/Verity.app
bash scripts/build_desktop.sh --debug    # quicker debug build → desktop/target/debug/bundle/macos/Verity.app
bash scripts/run_desktop.sh              # run it from the terminal (Ctrl-C, Cmd+Q or closing the window quits)
bash scripts/run_desktop.sh --open       # launch it like any Mac app
```

The scripts use the Rust toolchain inside the project (`tools/cargo`, `tools/rustup`), the project npm cache
(`tools/npm-cache`), and `CARGO_TARGET_DIR=desktop/target`. The Tauri CLI is the prebuilt npm package
`@tauri-apps/cli`. `build_desktop.sh` also rebuilds `ui/dist` first (skip that with `SKIP_UI=1`). The network is
needed only for the first build, to download crates and packages. The finished app is fully offline.

## How it works

1. **Find the Verity folder (ROOT).** The shell uses `VERITY_ROOT` if it is set and valid. Otherwise it walks up
   from the executable until it finds a folder that contains both `verity/` and `.venv/`. As a last resort it uses
   `/Users/emumba/Desktop/mati`.
2. **Start the engine.** It runs `ROOT/.venv/bin/python -m verity.engine.server --port 0 --print-ready` with
   `VERITY_EXIT_WITH_PARENT=1` in its own process group, then reads the one-line ready JSON (`{port, token, …}`)
   from the engine's stdout, waiting at most 60 s. A bundled start-up page (`desktop/splash/`) says "Starting the
   engine…" in the meantime. If the engine cannot start, that page shows the reason and the engine's last output,
   plus a **Try again** button.
3. **Show the UI.** The window is pointed at `http://127.0.0.1:<port>/?token=<token>`, the UI that the engine serves
   from `ui/dist`. The page and the engine share an origin, so there is no CORS to configure, and the engine's
   Origin check already allows `127.0.0.1`. The folder picker (`dialog:allow-open`) and the shell's own commands
   are granted to that page by a `remote` capability for `http://127.0.0.1:*`
   (`src-tauri/capabilities/engine-ui.json`). Nothing else is exposed.
4. **Stop the engine.** When the app exits (window closed, Cmd+Q, or `app.exit`), the shell sends SIGTERM to the
   engine's process group, waits up to 8 s for a clean shutdown, then sends SIGKILL to anything left, such as a
   model server the engine started. SIGINT and SIGTERM sent to the app do the same. If the app is killed outright,
   the engine notices that its parent is gone and exits within about 2 s (`VERITY_EXIT_WITH_PARENT`).
5. **If the engine crashes** while the app is open, the window goes back to the start-up page and offers
   **Try again**.

Engine output goes to `data/logs/desktop-engine.log`.

## Self-test

`VERITY_DESKTOP_SELFTEST=1|close|quit` makes the shell run a small script in the loaded UI. The script reports
whether the UI rendered, whether the Tauri bridge is present, and whether the folder-picker permission is granted.
The report is printed as a `SELFTEST {...}` line. With `close` or `quit`, the app then closes its window or quits,
which exercises the shutdown path.

```bash
VERITY_DESKTOP_SELFTEST=close desktop/target/release/bundle/macos/Verity.app/Contents/MacOS/Verity
pgrep -f verity.engine.server    # → nothing left behind
```

## Files

| path | what |
|---|---|
| `src-tauri/src/main.rs` | engine start/stop, ROOT resolution, commands (`engine_status`, `restart_engine`, `pick_folder`, `selftest_report`) |
| `src-tauri/tauri.conf.json` | window (title "Verity", 1200×800, min 760×540), bundle settings |
| `src-tauri/capabilities/` | `splash` (the local start-up page) and `engine-ui` (the page served on 127.0.0.1) |
| `src-tauri/icons/` | app icons (generated from `icon-source.png` with `tauri icon`) |
| `splash/index.html` | the start-up and error page bundled into the app |
