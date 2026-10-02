#!/usr/bin/env bash
# Resumable model download for Verity (run once, while online). Files are fetched to *.part, checked against the
# pinned SHA256 and only then renamed, so a partial or corrupted download is never mistaken for a model.
#   bash scripts/download_models.sh [auto|direct|C1|C2|C3]       (or VERITY_MODEL=...; default auto)
#   auto    the models this machine can run: gpt-oss-20b when about 15 GB fits (a 24 GB Mac or larger), else Qwen3-4B
#   direct  only the embedding model (answers from checked facts, no chat model)
#   C1/C2   up to Qwen3-4B / gpt-oss-20b, whatever the memory;  C3  gpt-oss-120b (63 GB download, needs about 66 GB)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/lib/models.sh"
CHOICE="${1:-${VERITY_MODEL:-auto}}"
KEYS=$(vr_models_for "$CHOICE") || { echo "[models] unknown choice '$CHOICE' (use auto, direct, C1, C2 or C3)" >&2; exit 2; }
mkdir -p "$HERE/../models"; cd "$HERE/../models"     # models/ does not exist on a fresh copy
echo "[models] $CHOICE (memory budget $(vr_budget_gb) GB): $KEYS"

sha256() { if command -v shasum >/dev/null; then shasum -a 256 "$1"; else sha256sum "$1"; fi | awk '{ print $1 }'; }
dl() { # key
  local f url want got
  f=$(vr_model "$1" file); url=$(vr_model "$1" url); want=$(vr_model "$1" sha)
  if [ -f "$f" ] && [ ! -f "$f.part" ]; then echo "[models] $f already present"; return; fi
  echo "[models] $f ($(vr_gb "$(vr_model "$1" bytes)") GB)"
  curl -L --fail --retry 20 --retry-delay 5 -C - -o "$f.part" "$url"
  echo "[models] checking SHA256 of $f"
  got=$(sha256 "$f.part")
  if [ "$got" != "$want" ]; then
    mv -f "$f.part" "$f.bad"
    echo "[models] $f: checksum mismatch (got $got, want $want); kept as $f.bad. Run again to re-download." >&2
    exit 1
  fi
  mv "$f.part" "$f"
}
for k in $KEYS; do dl "$k"; done

# the pinned checksums of the models present (each new download was verified against them above)
: > SHA256SUMS.txt
for k in embed C1 C2 C3; do f=$(vr_model "$k" file); [ -f "$f" ] && echo "$(vr_model "$k" sha)  $f" >> SHA256SUMS.txt; done
echo "[models] done"
