#!/usr/bin/env bash
# Runs the Verity desktop app (builds it first if needed).
#   bash scripts/run_desktop.sh          run in this terminal; engine logs go to data/logs/desktop-engine.log.
#                                        Closing the window, Cmd+Q or Ctrl-C quits and stops the engine.
#   bash scripts/run_desktop.sh --open   launch it like a normal Mac app (returns immediately)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
find_app() {
  for p in release debug; do
    local a="$ROOT/desktop/target/$p/bundle/macos/Verity.app"
    if [ -x "$a/Contents/MacOS/Verity" ]; then echo "$a"; return 0; fi
  done
  return 1
}
APP="${VERITY_APP:-$(find_app || true)}"
if [ -z "$APP" ]; then
  echo "Verity.app has not been built yet — building it now…"
  bash "$ROOT/scripts/build_desktop.sh"
  APP="$(find_app)"
fi
if bash "$ROOT/scripts/build_ui.sh" --check; then bash "$ROOT/scripts/build_ui.sh"; fi
export VERITY_ROOT="${VERITY_ROOT:-$ROOT}"
echo "Starting $APP"
if [ "${1:-}" = "--open" ]; then
  open "$APP"
else
  exec "$APP/Contents/MacOS/Verity"
fi
