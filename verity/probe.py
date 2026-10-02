"""Capability probe (design §07/§09): what this machine can do — hardware, toolchains, isolation, models — and which
model-ladder rung and features follow from it. Cached; re-probed on demand."""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from . import config, osutil

LADDER = [
    # rung, label, file, approx resident GB (weights + KV at our context), role — strongest first
    {"rung": "C3", "label": "gpt-oss-120b (MXFP4)", "file": "gpt-oss-120b-MXFP4.gguf", "gb": 66.0, "ctx": 16384,
     "min_bytes": 60_000_000_000, "role": "main", "reasoning": True},
    {"rung": "C2", "label": "gpt-oss-20b (MXFP4)", "file": "gpt-oss-20b-MXFP4.gguf", "gb": 14.0, "ctx": 16384,
     "min_bytes": 11_500_000_000,
     "role": "main", "reasoning": True},
    {"rung": "C1", "label": "Qwen3-4B-Instruct-2507 (Q4_K_M)", "file": "Qwen3-4B-Instruct-2507-Q4_K_M.gguf", "gb": 4.0,
     "min_bytes": 2_400_000_000,
     "ctx": 16384, "role": "fast", "reasoning": False},
]
EMBED = {"label": "Qwen3-Embedding-0.6B (Q8_0)", "file": "Qwen3-Embedding-0.6B-Q8_0.gguf", "gb": 1.0,
         "min_bytes": 600_000_000}


def _run(cmd: list[str], timeout: float = 10) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def total_ram_gb() -> float:
    if osutil.IS_WINDOWS:
        return round(osutil.windows_memory()[0] / 2**30, 1)
    if platform.system() == "Darwin":
        v = _run(["sysctl", "-n", "hw.memsize"])
        return round(int(v) / 2**30, 1) if v.isdigit() else 0.0
    try:
        with open("/proc/meminfo") as f:
            for ln in f:
                if ln.startswith("MemTotal"):
                    return round(int(ln.split()[1]) / 2**20, 1)
    except OSError:
        pass
    return 0.0


def available_ram_gb() -> float:
    if osutil.IS_WINDOWS:
        return round(osutil.windows_memory()[1] / 2**30, 1)
    if platform.system() == "Darwin":
        out = _run(["vm_stat"])
        page = 16384
        free = 0
        for ln in out.splitlines():
            if "page size of" in ln:
                try:
                    page = int(ln.split("page size of")[1].split()[0])
                except Exception:
                    pass
            for key in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"):
                if ln.startswith(key):
                    free += int(ln.split(":")[1].strip().rstrip("."))
        return round(free * page / 2**30, 1)
    try:
        with open("/proc/meminfo") as f:
            for ln in f:
                if ln.startswith("MemAvailable"):
                    return round(int(ln.split()[1]) / 2**20, 1)
    except OSError:
        pass
    return 0.0


def gpu_info() -> dict[str, Any]:
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        chip = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
        return {"kind": "apple-metal", "name": chip, "unified_memory": True}
    smi = shutil.which("nvidia-smi")
    if not smi and osutil.IS_WINDOWS:
        q = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe"
        smi = str(q) if q.exists() else None
    if smi:
        out = _run([smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
        if out:
            name, mem = [x.strip() for x in out.splitlines()[0].split(",")[:2]]
            return {"kind": "cuda", "name": name, "vram_gb": round(float(mem) / 1024, 1), "unified_memory": False}
    return {"kind": "cpu", "name": platform.processor() or platform.machine(), "unified_memory": False}


def tools() -> dict[str, Any]:
    from .extract import crosscheck, csharp
    out: dict[str, Any] = {}
    try:
        import clang.cindex as ci
        out["libclang"] = {"ok": True, "version": ci.conf.lib.clang_getClangVersion and "18 (bundled)"}
    except Exception as e:  # noqa: BLE001
        out["libclang"] = {"ok": False, "why": str(e)}
    cc = (shutil.which("clang") or shutil.which("cc") or shutil.which("gcc")) if osutil.mac_dev_tools() else None
    out["c_compiler"] = {"ok": bool(cc), "path": cc, "version": _run([cc, "--version"]).splitlines()[0] if cc else ""}
    if platform.system() == "Darwin":
        llvm_cov = _run(["xcrun", "-f", "llvm-cov"]) if osutil.mac_dev_tools() else ""
    else:
        llvm_cov = shutil.which("llvm-cov") or ""
    out["coverage"] = {"ok": bool(llvm_cov), "llvm_cov": llvm_cov}
    ok, why = csharp.available()
    out["dotnet_worker"] = {"ok": ok, "why": why}
    cp = crosscheck.cppcheck_path()
    out["cppcheck"] = {"ok": bool(cp), "path": str(cp) if cp else None}
    ls = osutil.exe(config.TOOLS / "llama.cpp" / "llama-server")
    out["llama_server"] = {"ok": ls.exists(), "path": str(ls)}
    if platform.system() == "Darwin":
        out["sandbox"] = {"ok": bool(shutil.which("sandbox-exec")), "adapter": "sandbox-exec"}
    else:
        bw = shutil.which("bwrap")
        out["sandbox"] = {"ok": bool(bw), "adapter": "bubblewrap" if bw else None}
    return out


SELECTION = "selection.json"


def selection() -> dict[str, Any]:
    """The model choice written by the system analysis (scripts/windows/analyze_system.ps1 or by hand):
    {"mode": "auto"|"direct", "main": {...}, "fast": {...}, "embed": {...}}. Entries are ladder-shaped dicts
    (label, file, gb, ctx, reasoning, optional "args" for llama-server). Missing or unreadable: {}."""
    p = config.MODELS / SELECTION
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def catalog() -> list[dict[str, Any]]:
    """Every model the engine may use: the built-in ladder, the embedding model, and whatever the selection names."""
    out = list(LADDER) + [EMBED]
    known = {m["file"] for m in out}
    for role in ("main", "fast", "embed"):
        m = selection().get(role)
        if isinstance(m, dict) and m.get("file") and m["file"] not in known:
            out.append(dict(m, min_bytes=m.get("min_bytes", 100_000_000)))
            known.add(m["file"])
    return out


def models() -> dict[str, Any]:
    out = {}
    for m in catalog():
        p = config.MODELS / m["file"]
        size = p.stat().st_size if p.exists() else 0
        complete = p.exists() and size >= m.get("min_bytes", 100_000_000) and \
            not (config.MODELS / (m["file"] + ".part")).exists() and not _being_written(p)
        out[m["file"]] = {"present": complete, "bytes": size}
    return out


def _being_written(p: Path) -> bool:
    """A file still growing (download in progress) is not a usable model."""
    try:
        a = p.stat().st_size
        if time.time() - p.stat().st_mtime > 5:
            return False
        time.sleep(0.3)
        return p.stat().st_size != a
    except OSError:
        return True


def choose_rungs(ram_gb: float, gpu: dict, mdl: dict, sel: dict | None = None) -> dict[str, Any]:
    """Pick the strongest rung that fits with headroom for the engine, indexer and OS (design §09). A selection written
    by the system analysis (measured on this machine) wins over the estimate, as long as its files are present."""
    sel = selection() if sel is None else sel
    if sel:
        def present(m):
            return isinstance(m, dict) and mdl.get(m.get("file"), {}).get("present")
        # a selected model that is missing (deleted, still downloading) must not switch the AI off: then the
        # estimate below decides, as without a selection
        if sel.get("mode") == "direct" or present(sel.get("main")):
            main = sel.get("main") if present(sel.get("main")) and sel.get("mode") != "direct" else None
            fast = sel.get("fast") if present(sel.get("fast")) else None
            embed = sel.get("embed") if present(sel.get("embed")) else \
                (EMBED if mdl.get(EMBED["file"], {}).get("present") else None)
            return {"main": main, "fast": fast, "embed": embed, "budget_gb": None, "source": "selection",
                    "class": (main or {}).get("rung", "C?") if main else "C0"}
    budget = ram_gb * (0.70 if gpu.get("unified_memory") else 1.0)
    if gpu.get("kind") == "cuda":
        budget = max(budget, gpu.get("vram_gb", 0) + ram_gb * 0.3)
    main = None
    for m in LADDER:
        if mdl.get(m["file"], {}).get("present") and m["gb"] + EMBED["gb"] <= budget:
            main = m
            break
    fast = next((m for m in LADDER if m["role"] == "fast" and mdl.get(m["file"], {}).get("present")), None)
    return {"main": main, "fast": fast, "embed": EMBED if mdl.get(EMBED["file"], {}).get("present") else None,
            "budget_gb": round(budget, 1),
            "class": main["rung"] if main else "C0"}


def probe(refresh: bool = False) -> dict[str, Any]:
    cache = config.HOME / "probe.json"
    if cache.exists() and not refresh:
        try:
            data = json.loads(cache.read_text())
            if time.time() - data.get("at", 0) < 3600 and data.get("version") == 2:
                data["models"] = models()
                data["choice"] = choose_rungs(data["hardware"]["ram_gb"], data["hardware"]["gpu"], data["models"])
                return data
        except Exception:
            pass
    ram = total_ram_gb()
    gpu = gpu_info()
    mdl = models()
    disk = shutil.disk_usage(str(config.HOME if config.HOME.exists() else config.ROOT))
    data = {"version": 2, "at": time.time(),
            "hardware": {"os": f"{platform.system()} {platform.release()}", "arch": platform.machine(),
                         "cpus": os.cpu_count(), "ram_gb": ram, "ram_available_gb": available_ram_gb(), "gpu": gpu,
                         "disk_free_gb": round(disk.free / 2**30, 1)},
            "tools": tools(), "models": mdl}
    data["choice"] = choose_rungs(ram, gpu, mdl)
    config.HOME.mkdir(parents=True, exist_ok=True)
    try:
        cache.write_text(json.dumps(data, indent=1, default=str))
    except OSError:
        pass
    return data


def describe(p: dict[str, Any]) -> list[str]:
    hw = p["hardware"]
    ch = p["choice"]
    lines = [f"{hw['os']} · {hw['arch']} · {hw['cpus']} cores · {hw['ram_gb']} GB memory · {hw['gpu'].get('name', '')}"]
    lines.append(f"AI model: {ch['main']['label'] if ch['main'] else 'none (Direct mode: answers are built from facts only)'}")
    if ch.get("embed"):
        lines.append(f"Meaning search: {ch['embed']['label']}")
    t = p["tools"]
    lines.append("C analysis: libclang " + ("✓" if t["libclang"]["ok"] else "✗") +
                 " · cppcheck " + ("✓" if t["cppcheck"]["ok"] else "✗") +
                 " · C# compiler worker " + ("✓" if t["dotnet_worker"]["ok"] else "✗ (" + t["dotnet_worker"]["why"] + ")"))
    lines.append("Running code safely: " + ("✓ " + str(t["sandbox"]["adapter"]) if t["sandbox"]["ok"] else "✗ not available") +
                 " · coverage " + ("✓" if t["coverage"]["ok"] else "✗"))
    return lines
