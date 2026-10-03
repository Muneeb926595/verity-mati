#!/usr/bin/env bash
# Verity one-time setup. Everything is installed INSIDE this folder (no system-wide changes).
# Works offline when vendor/wheels, tools/ and models/ are already populated (as shipped); otherwise it downloads
# the missing pieces once (models 3-15 GB depending on memory, toolchains about 2 GB). After setup, Verity never
# uses the network. To see what this machine still needs first:  bash scripts/check_prereqs.sh
#   bash scripts/setup.sh [--model auto|direct|C1|C2|C3]     (default auto: the models this machine can run)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
say() { printf '\n\033[1m[setup] %s\033[0m\n' "$*"; }
while [ $# -gt 0 ]; do
  case "$1" in
    --model) VERITY_MODEL="${2:-}"; shift 2 ;;
    --model=*) VERITY_MODEL="${1#*=}"; shift ;;
    *) echo "usage: bash scripts/setup.sh [--model auto|direct|C1|C2|C3]" >&2; exit 2 ;;
  esac
done
export VERITY_MODEL="${VERITY_MODEL:-auto}"
# uv's installer puts uv in ~/.local/bin, which a terminal opened before installing it does not have on PATH yet
for d in "$HOME/.local/bin" "$HOME/.cargo/bin"; do [ -x "$d/uv" ] && PATH="$d:$PATH"; done

say "Python environment (.venv, Python 3.12)"
if [ ! -x .venv/bin/python ]; then
  if command -v uv >/dev/null; then uv venv --python 3.12 .venv
  elif command -v python3.12 >/dev/null; then python3.12 -m venv .venv
  else echo "Python 3.12 is required. Easiest: install uv with  curl -LsSf https://astral.sh/uv/install.sh | sh  (see bash scripts/check_prereqs.sh)"; exit 1; fi
fi
OFFLINE_WHEELS=0; [ -d vendor/wheels ] && OFFLINE_WHEELS=1     # shipped wheels when present, else PyPI
if command -v uv >/dev/null; then
  { [ "$OFFLINE_WHEELS" = 1 ] && uv pip install --python .venv/bin/python --no-index --find-links vendor/wheels -r requirements.lock; } \
    || uv pip install --python .venv/bin/python -r requirements.lock
else
  .venv/bin/python -m ensurepip -q || true
  { [ "$OFFLINE_WHEELS" = 1 ] && .venv/bin/python -m pip install -q --no-index --find-links vendor/wheels -r requirements.lock; } \
    || .venv/bin/python -m pip install -q -r requirements.lock
fi

say "Toolchains (llama.cpp, .NET SDK, Rust, cppcheck) into tools/"
bash scripts/setup_toolchains.sh

say "Local AI models into models/ ($VERITY_MODEL)"
bash scripts/download_models.sh "$VERITY_MODEL"

say "C# compiler worker (Roslyn)"
if [ ! -f workers/roslyn/bin/VerityRoslyn.dll ]; then bash workers/roslyn/build.sh; else echo "already built"; fi

say "User interface"
if [ -f scripts/build_ui.sh ] && bash scripts/build_ui.sh --check; then bash scripts/build_ui.sh; else echo "ui/dist present and up to date"; fi

say "What this machine can do"
bin/verity probe
say "Done. Try:  bin/verity ask samples/navgps-c \"Explain how GPS data travels through this application.\""
