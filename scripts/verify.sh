#!/usr/bin/env bash
# Full verification: tests, every known-answer suite, the demos.
#   bash scripts/verify.sh            tests + main/held-out/dialect/real-world suites + demos (Direct mode)
#   bash scripts/verify.sh --llm      also the main suite with the local model (re-ranking + wording)
#   bash scripts/verify.sh --lwip     also lwIP (173K lines: ~2 min to index)
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
LLM=0; LWIP=0
for a in "$@"; do case "$a" in --llm) LLM=1;; --lwip) LWIP=1;; esac; done
.venv/bin/python -m pytest tests -q
.venv/bin/python -m verity.bench.run
.venv/bin/python -m verity.bench.run --no-reindex --suite bench/heldout.json --out bench/results/heldout
.venv/bin/python -m verity.bench.run --suite bench/dialects.json --out bench/results/dialects
.venv/bin/python -m verity.bench.run --suite bench/realworld.json --out bench/results/realworld
.venv/bin/python -m verity.bench.run --suite bench/realworld_heldout.json --out bench/results/realworld_heldout
.venv/bin/python -m verity.bench.run --suite bench/realworld_heldout2.json --out bench/results/realworld_heldout2
.venv/bin/python -m verity.bench.run --suite bench/realworld_heldout3.json --out bench/results/realworld_heldout3
.venv/bin/python -m verity.bench.run --suite bench/realworld_heldout4.json --out bench/results/realworld_heldout4
for n in 5 6 7 8 9; do     # Phase 4: uC/OS-II, Paho MQTT, M2Mqtt (C#), nanopb, NModbus (C#)
  .venv/bin/python -m verity.bench.run --suite bench/realworld_heldout$n.json --out bench/results/realworld_heldout$n
done
[ "$LWIP" = 1 ] && .venv/bin/python -m verity.bench.run --suite bench/realworld_lwip.json --out bench/results/realworld_lwip
[ "$LLM" = 1 ] && .venv/bin/python -m verity.bench.run --no-reindex --llm
bash scripts/run_demos.sh "$([ "$LLM" = 1 ] && echo --llm || echo --direct)"
