#!/usr/bin/env bash
# Packs what a Windows machine needs into ONE zip: the code, tests, scripts, benchmarks, docs, the trimmed sample
# projects, the prebuilt UI and C# worker, and the design documents. No models, toolchains, Python environment or
# indexes (scripts\windows\setup.cmd downloads and builds those on the Windows side).
#   bash scripts/make_windows_bundle.sh [output.zip]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$HOME/Desktop/verity-windows-$(date +%Y%m%d).zip}"
PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY=python3

"$PY" - "$ROOT" "$OUT" <<'EOF'
import hashlib, os, sys, zipfile
from pathlib import Path

root, out = Path(sys.argv[1]), Path(sys.argv[2])
include = ["verity", "tests", "scripts", "bench", "docs", "samples", "ui", "workers", "desktop", "bin",
           "README.md", "PROGRESS.md", "HANDOFF.md", "CLAUDE.md", "requirements.lock", "models/SHA256SUMS.txt"]
skip_dirs = {"node_modules", "target", "__pycache__", ".pytest_cache", "obj", ".git", "gen"}
skip_files = {".DS_Store"}
files = []
for item in include:
    p = root / item
    if p.is_file():
        files.append((p, item))
    elif p.is_dir():
        for dp, dn, fn in os.walk(p):
            dn[:] = [d for d in dn if d not in skip_dirs]
            for f in fn:
                if f in skip_files or f.endswith(".pyc"):
                    continue
                full = Path(dp) / f
                files.append((full, full.relative_to(root).as_posix()))
out.parent.mkdir(parents=True, exist_ok=True)
total = 0
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for full, arc in files:
        z.write(full, f"verity/{arc}")
        total += full.stat().st_size
h = hashlib.sha256(out.read_bytes()).hexdigest()
Path(str(out) + ".sha256").write_text(f"{h}  {out.name}\n")
print(f"  {len(files)} files, {total / 2**20:.0f} MB uncompressed -> {out} ({out.stat().st_size / 2**20:.0f} MB)")
print(f"  SHA256 {h}")
EOF

cat <<MSG

Next, on the Windows PC:
  1. Copy the zip, right-click > Extract All, preferably to a short path such as C:\\ (gives C:\\verity).
  2. Optional: C:\\verity\\scripts\\windows\\check_prereqs.cmd lists what this PC still needs (installs nothing).
  3. Double-click  C:\\verity\\scripts\\windows\\setup.cmd   (or run it with options, e.g. -StressTest -Yes).
  4. Optional: C:\\verity\\scripts\\windows\\analyze.cmd shows what this hardware can run before installing.
On another Mac or Linux machine:  bash scripts/check_prereqs.sh   then   bash scripts/setup.sh
MSG
