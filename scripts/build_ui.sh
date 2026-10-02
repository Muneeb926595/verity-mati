#!/usr/bin/env bash
# Builds the Verity UI into ui/dist/ (index.html + assets/). The engine serves it at "/" in browser mode and the
# desktop app shows the same pages. Idempotent; uses the project-local npm cache. Network is only needed the first
# time (to download packages); afterwards it works offline.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export npm_config_cache="$ROOT/tools/npm-cache"
export npm_config_update_notifier=false npm_config_fund=false npm_config_audit=false
cd "$ROOT/ui"

command -v node >/dev/null || { echo "Node.js is required to build the UI (not needed to run it)." >&2; exit 1; }
# (re)install packages only when missing or when the lockfile changed
if [ ! -d node_modules ] || [ ! -f node_modules/.package-lock.json ] || [ package-lock.json -nt node_modules/.package-lock.json ]; then
  echo "[ui] installing packages…"
  npm ci --prefer-offline --no-audit --no-fund || npm install --prefer-offline --no-audit --no-fund
fi

echo "[ui] type-checking…"
npx --no-install tsc --noEmit -p .
echo "[ui] building…"
npx --no-install vite build
test -f dist/index.html && test -d dist/assets
echo "[ui] done → $ROOT/ui/dist"
