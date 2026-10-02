"""The prerequisites checkers (scripts/check_prereqs.sh, scripts/windows/check_prereqs.ps1): they run, report the
same items in the same JSON shape, agree with the pinned model list, and never block on something setup installs."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from verity import probe

ROOT = Path(__file__).resolve().parent.parent
SH = ROOT / "scripts" / "check_prereqs.sh"
PS = ROOT / "scripts" / "windows" / "check_prereqs.ps1"
STATUSES = {"ok", "done", "todo", "warn", "missing", "absent", "info"}
GROUPS = ["machine", "required", "auto", "optional", "not_needed"]
# what both checkers must report (each platform adds its own extras, e.g. execution-policy on Windows)
COMMON = {"platform", "ram", "disk", "internet", "setup-files", "shell-tools", "python-env", "llama.cpp", "dotnet",
          "csharp-worker", "cppcheck", "ui", "models", "gpu", "sandbox", "desktop-app", "ollama", "docker",
          "system-llvm", "system-python", "git", "cloud", "admin"}
KEYS = {"tool", "schema", "platform", "root", "model_choice", "ram_gb", "model_budget_gb", "models_selected",
        "model_used", "backend", "disk_free_gb", "disk_needed_gb", "download_gb", "state", "missing", "todo",
        "warnings", "next", "items"}


def _bash() -> str:
    return "/bin/bash" if Path("/bin/bash").exists() else (shutil.which("bash") or "bash")   # macOS: bash 3.2


def _run_sh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([_bash(), str(SH), *args], capture_output=True, text=True, timeout=120)


def _check_report(d: dict, rc: int) -> None:
    assert KEYS <= set(d), KEYS - set(d)
    assert d["tool"] == "verity-check-prereqs" and d["schema"] == 1
    ids = [i["id"] for i in d["items"]]
    assert len(ids) == len(set(ids)), "duplicate item ids"
    assert COMMON <= set(ids), COMMON - set(ids)
    for i in d["items"]:
        assert i["status"] in STATUSES, i
        assert i["group"] in GROUPS, i
        assert i["summary"].strip(), i
        if i["status"] == "missing":
            assert i["fix"].strip(), f"{i['id']}: a MISSING item must say how to fix it"
    order = [GROUPS.index(i["group"]) for i in d["items"]]
    assert order == sorted(order), "items are listed group by group"
    assert all(i["status"] == "info" for i in d["items"] if i["group"] == "not_needed")
    # nothing that setup installs by itself may block it (the UI is the one exception: no ui/dist and no Node.js)
    assert not [i for i in d["items"] if i["group"] == "auto" and i["status"] == "missing" and i["id"] != "ui"]
    n_missing = sum(i["status"] == "missing" for i in d["items"])
    assert d["missing"] == n_missing
    assert (rc == 0) == (n_missing == 0)
    assert d["state"] == ("blocked" if n_missing else "ready" if d["todo"] else "installed")


@pytest.mark.skipif(os.name == "nt", reason="the bash checker is for macOS/Linux")
def test_bash_checker_json_report():
    p = _run_sh("--json", "--offline")
    assert p.returncode in (0, 1), p.stderr
    d = json.loads(p.stdout)
    _check_report(d, p.returncode)
    by = {i["id"]: i for i in d["items"]}
    assert "Ollama" in by["ollama"]["summary"] and by["ollama"]["group"] == "not_needed"
    assert d["model_choice"] == "auto" and d["models_selected"].split()[0] == "embed"


@pytest.mark.skipif(os.name == "nt", reason="the bash checker is for macOS/Linux")
def test_bash_checker_text_report_and_options():
    p = _run_sh("--offline")
    assert p.returncode in (0, 1), p.stderr
    for title in ("1. This machine", "2. Install yourself before setup", "3. Installed by setup", "4. Optional",
                  "5. NOT needed"):
        assert title in p.stdout
    assert re.search(r"^(NOT READY|READY FOR SETUP|ALREADY SET UP)", p.stdout, re.M)
    assert "\033[" not in p.stdout                                   # no colours when not a terminal
    d = json.loads(_run_sh("--json", "--offline", "--model", "direct").stdout)
    assert d["models_selected"] == "embed"           # (a chat model already in models/ would still be used)
    bad = _run_sh("--model", "C9")
    assert bad.returncode == 2 and "auto, direct, C1, C2 or C3" in bad.stderr


@pytest.mark.skipif(os.name == "nt", reason="the bash checker is for macOS/Linux")
def test_bash_checker_on_an_empty_copy(tmp_path):
    """A fresh copy with no tools, models or .venv: everything setup installs is TODO, never MISSING."""
    copy = tmp_path / "verity"
    for item in ("verity", "bin", "scripts", "workers/roslyn/VerityRoslyn.csproj", "workers/roslyn/build.sh",
                 "requirements.lock"):
        src, dst = ROOT / item, copy / item
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "node_modules"))
        else:
            shutil.copy2(src, dst)
    (copy / "ui" / "dist").mkdir(parents=True)
    (copy / "ui" / "dist" / "index.html").write_text("<html></html>")
    p = subprocess.run([_bash(), str(copy / "scripts" / "check_prereqs.sh"), "--json", "--offline"],
                       capture_output=True, text=True, timeout=120)
    d = json.loads(p.stdout)
    _check_report(d, p.returncode)
    st = {i["id"]: i["status"] for i in d["items"]}
    for k in ("python-env", "llama.cpp", "dotnet", "csharp-worker", "rust", "models"):
        assert st[k] == "todo", (k, st[k])
    assert st["ui"] == "done"
    assert d["download_gb"] > 0.5 and d["disk_needed_gb"] > d["download_gb"]
    p = subprocess.run([_bash(), str(copy / "scripts" / "check_prereqs.sh"), "--json", "--offline", "--model", "direct"],
                       capture_output=True, text=True, timeout=120)
    direct = json.loads(p.stdout)
    assert direct["models_selected"] == "embed" and direct["model_used"] == "direct"
    assert direct["download_gb"] < d["download_gb"]


def _ids(path: Path, pattern: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in re.finditer(pattern, path.read_text(encoding="utf-8"), re.M)}


def test_both_checkers_report_the_same_core_items():
    sh = _ids(SH, r"\badd ([a-z.\-]+) (machine|required|auto|optional|not_needed) ")
    ps = _ids(PS, r"Add-Item '([a-z.\-]+)' '(machine|required|auto|optional|not_needed)'")
    assert COMMON <= set(sh), COMMON - set(sh)
    assert COMMON <= set(ps), COMMON - set(ps)
    same_group = {"platform", "ram", "disk", "internet", "setup-files", "python-env", "llama.cpp", "dotnet",
                  "csharp-worker", "cppcheck", "ui", "models", "gpu", "ollama", "docker", "cloud", "admin"}
    for k in same_group:
        assert sh[k] == ps[k], (k, sh[k], ps[k])


def test_model_pins_agree_everywhere():
    """scripts/lib/models.sh (macOS/Linux), scripts/windows/catalog.json and verity/probe.py name the same files,
    sizes, checksums and memory needs, so the checker's advice matches what setup downloads and what Verity runs."""
    rows = {}
    text = (ROOT / "scripts" / "lib" / "models.sh").read_text(encoding="utf-8")
    block = re.search(r"VR_MODELS='(.*?)'", text, re.S).group(1)
    for line in block.strip().splitlines():
        key, file, size, sha, gb, _label, url = line.split("|")
        rows[file] = (key, int(size), sha, float(gb), url)
    cat = json.loads((ROOT / "scripts" / "windows" / "catalog.json").read_text(encoding="utf-8"))
    for m in cat["models"]:
        key, size, sha, gb, url = rows[m["file"]]
        assert (size, sha, url) == (m["bytes"], m["sha256"], m["url"]), m["file"]
        assert key == m.get("rung", "embed")
    for m in probe.LADDER + [probe.EMBED]:
        assert rows[m["file"]][3] == m["gb"], m["file"]
        assert rows[m["file"]][1] >= m["min_bytes"], m["file"]


def test_windows_scripts_are_ascii_and_launchers_crlf():
    for f in list((ROOT / "scripts" / "windows").rglob("*.ps1")) + list((ROOT / "scripts" / "windows").rglob("*.cmd")):
        data = f.read_bytes()
        assert all(b < 128 for b in data), f"{f}: Windows PowerShell 5.1 reads BOM-less scripts as ANSI; keep ASCII"
        if f.suffix == ".cmd":
            assert b"\n" not in data.replace(b"\r\n", b""), f"{f}: cmd.exe needs CRLF line endings"
    for name in ("setup.cmd", "analyze.cmd", "check_prereqs.cmd"):
        assert 'call "%~dp0lib\\preflight.cmd"' in (ROOT / "scripts" / "windows" / name).read_text(), name


@pytest.mark.skipif(not (os.name == "nt" or shutil.which("pwsh")), reason="needs PowerShell (Windows or pwsh)")
def test_powershell_checker_json_report():
    exe = "powershell.exe" if os.name == "nt" else "pwsh"
    args = [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PS), "-Json", "-Offline"]
    if os.name != "nt":
        args.append("-AllowNonWindows")
    p = subprocess.run(args, capture_output=True, text=True, timeout=300)
    assert p.returncode in (0, 1), p.stderr
    _check_report(json.loads(p.stdout), p.returncode)
