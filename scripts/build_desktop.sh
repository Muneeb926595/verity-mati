#!/usr/bin/env bash
# Builds the macOS desktop app:  desktop/target/release/bundle/macos/Verity.app
#   bash scripts/build_desktop.sh           release build (default)
#   bash scripts/build_desktop.sh --debug   faster debug build → desktop/target/debug/bundle/macos/Verity.app
# Uses the project-local Rust toolchain (tools/cargo, tools/rustup) and npm cache; all build output stays in desktop/.
# Idempotent: re-running only rebuilds what changed. Set SKIP_UI=1 to skip rebuilding ui/dist.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export CARGO_HOME="$ROOT/tools/cargo"
export RUSTUP_HOME="$ROOT/tools/rustup"
export PATH="$ROOT/tools/cargo/bin:$PATH"
export CARGO_TARGET_DIR="$ROOT/desktop/target"
export npm_config_cache="$ROOT/tools/npm-cache"
export npm_config_update_notifier=false npm_config_fund=false npm_config_audit=false
export CI=true   # no interactive prompts from the Tauri CLI

PROFILE=release
TAURI_ARGS=(build --bundles app)
if [ "${1:-}" = "--debug" ]; then PROFILE=debug; TAURI_ARGS+=(--debug); fi

command -v cargo >/dev/null || { echo "Rust not found in $ROOT/tools/cargo (run scripts/setup_toolchains.sh)." >&2; exit 1; }

# the app shows the UI served by the engine from ui/dist, so make sure it is built
if [ "${SKIP_UI:-0}" != "1" ] || bash "$ROOT/scripts/build_ui.sh" --check; then
  bash "$ROOT/scripts/build_ui.sh"
fi

cd "$ROOT/desktop"
if [ ! -x node_modules/.bin/tauri ]; then
  echo "[desktop] installing the Tauri CLI…"
  npm ci --prefer-offline --no-audit --no-fund || npm install --prefer-offline --no-audit --no-fund
fi

echo "[desktop] building ($PROFILE)…"
npx --no-install tauri "${TAURI_ARGS[@]}"

APP="$CARGO_TARGET_DIR/$PROFILE/bundle/macos/Verity.app"
test -x "$APP/Contents/MacOS/Verity"
echo "[desktop] done → $APP"
