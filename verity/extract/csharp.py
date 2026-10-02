"""C# extraction: the Roslyn worker (workers/roslyn, .NET) emits FACT_SCHEMA JSONL; this module runs it under
supervision (timeout, cancellation, crash) and falls back to tree-sitter (F4) when .NET or the worker is missing."""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .. import config, osutil
from . import ts_fallback

WORKER_DLL = config.ROOT / "workers" / "roslyn" / "bin" / "VerityRoslyn.dll"


def dotnet_path() -> Path | None:
    p = osutil.exe(config.TOOLS / "dotnet" / "dotnet")
    if p.exists():
        return p
    for d in os.environ.get("PATH", "").split(os.pathsep):
        q = osutil.exe(Path(d) / "dotnet")
        if q.exists():
            return q
    return None


def available() -> tuple[bool, str]:
    dn = dotnet_path()
    if dn is None:
        return False, ".NET runtime not found (run scripts/setup_toolchains.sh, or scripts\\windows\\setup.cmd on Windows)"
    if not WORKER_DLL.exists():
        return False, "C# worker not built (run workers/roslyn/build.sh)"
    return True, "ok"


def extract(root: Path, scan, work: Path, cancel: threading.Event | None = None,
            progress: Callable[[int, int, str], None] | None = None, timeout: float = 1800.0) -> dict[str, Any]:
    cancel = cancel or threading.Event()
    progress = progress or (lambda d, t, m: None)
    ok, why = available()
    if not ok:
        recs = ts_fallback.extract_csharp(root, scan.cs_files)
        for rel in scan.cs_files:
            recs.append({"t": "file", "path": rel, "lang": "csharp", "fidelity": "F4", "errors": 0, "stubs": [],
                         "profile": "tree-sitter"})
        return {"records": recs, "mode": "syntax-only", "fidelity": "F4",
                "warning": f"C# was read syntax-only: {why}. Answers about C# are less certain.",
                "detail": f"Read {len(scan.cs_files)} C# file(s) syntax-only ({why})"}
    work.mkdir(parents=True, exist_ok=True)
    out_path = work / "roslyn.jsonl"
    err_path = work / "roslyn.stderr.log"
    env = dict(os.environ, DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_NOLOGO="1", DOTNET_SKIP_FIRST_TIME_EXPERIENCE="1",
               DOTNET_ROOT=str(dotnet_path().parent), NUGET_PACKAGES=str(config.TOOLS / "nuget-packages"))
    cmd = [str(dotnet_path()), str(WORKER_DLL), str(root), "--out", str(out_path), "--progress",
           "--timeout", str(int(timeout))]
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env, text=True)
    total = len(scan.cs_files)
    progress(0, total, "Compiling the C# code")
    err_tail: list[str] = []
    try:
        assert proc.stderr is not None
        for line in proc.stderr:        # "PROGRESS <done>/<total> <file>"
            if cancel.is_set():
                proc.kill()
                return {"records": None}
            m = re.match(r"PROGRESS (\d+)/(\d+) (.*)", line.strip())
            if m:
                progress(int(m.group(1)), int(m.group(2)), f"Read {m.group(3)}")
            else:
                err_tail = (err_tail + [line.rstrip()])[-40:]
            if time.monotonic() - t0 > timeout + 30:
                proc.kill()
                break
        proc.wait(timeout=60)
    except Exception:
        proc.kill()
    try:
        err_path.write_text("\n".join(err_tail))
    except OSError:
        pass
    if proc.returncode not in (0, 2) or not out_path.exists():
        tail = ""
        try:
            tail = err_path.read_text()[-600:]
        except OSError:
            pass
        recs = ts_fallback.extract_csharp(root, scan.cs_files)
        for rel in scan.cs_files:
            recs.append({"t": "file", "path": rel, "lang": "csharp", "fidelity": "F4", "errors": 1, "stubs": [],
                         "profile": "tree-sitter"})
        return {"records": recs, "mode": "syntax-only", "fidelity": "F4",
                "warning": f"The C# compiler worker failed (exit {proc.returncode}); C# was read syntax-only. {tail.strip()[:300]}",
                "detail": f"Read {len(scan.cs_files)} C# file(s) syntax-only (compiler worker failed)"}
    recs = []
    with open(out_path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    nfiles = sum(1 for r in recs if r.get("t") == "file")
    fid = {}
    for r in recs:
        if r.get("t") == "file":
            fid[r.get("fidelity", "F2")] = fid.get(r.get("fidelity", "F2"), 0) + 1
    out = {"records": recs, "mode": "roslyn", "fidelity": max(fid, key=fid.get) if fid else "F2",
           "detail": f"Read {nfiles or len(scan.cs_files)} C# file(s) with the C# compiler (Roslyn)"}
    if proc.returncode == 2:
        out["warning"] = "C# analysis hit its time limit; some C# facts are partial."
    if fid.get("F3"):
        out["detail"] += f"; {fid['F3']} file(s) have unresolved references (missing packages)"
    return out
