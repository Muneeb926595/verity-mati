"""Cross-platform pieces the Windows setup relies on: executable names, process checks that never kill, the
exclusive index lock, and the model selection written by the system analysis (scripts/windows/analyze_system.ps1)."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from verity import osutil, probe


def test_exe_suffix_only_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(osutil, "IS_WINDOWS", True)
    assert osutil.exe(tmp_path / "llama-server").name == "llama-server.exe"
    assert osutil.exe(tmp_path / "VerityRoslyn.dll").name == "VerityRoslyn.dll"      # explicit suffix kept
    (tmp_path / "dotnet").write_text("")
    assert osutil.exe(tmp_path / "dotnet").name == "dotnet"                            # an existing file wins
    monkeypatch.setattr(osutil, "IS_WINDOWS", False)
    assert osutil.exe(tmp_path / "llama-server").name == "llama-server"


def test_pid_alive_tells_the_truth_without_killing():
    import os
    assert osutil.pid_alive(os.getpid())
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert not osutil.pid_alive(p.pid)
    assert not osutil.pid_alive(0) and not osutil.pid_alive(-5)


def test_index_lock_is_exclusive(tmp_path):
    f1 = open(tmp_path / "index.lock", "w")
    osutil.lock_exclusive(f1)
    f2 = open(tmp_path / "index.lock", "w")
    with pytest.raises(OSError):
        osutil.lock_exclusive(f2)
    osutil.unlock(f1)
    osutil.lock_exclusive(f2)
    osutil.unlock(f2)
    f1.close()
    f2.close()


def _present(*files):
    return {f: {"present": True, "bytes": 1} for f in files}


def test_selection_wins_when_its_files_are_present():
    sel = {"mode": "auto",
           "main": {"rung": "C3", "label": "gpt-oss-120b (MXFP4)", "file": "gpt-oss-120b-MXFP4.gguf", "ctx": 8192,
                    "reasoning": True, "args": ["--n-cpu-moe", "24"]},
           "embed": dict(probe.EMBED)}
    ch = probe.choose_rungs(16.0, {"kind": "cpu"}, _present("gpt-oss-120b-MXFP4.gguf", probe.EMBED["file"]), sel)
    assert ch["source"] == "selection" and ch["main"]["file"] == "gpt-oss-120b-MXFP4.gguf" and ch["class"] == "C3"
    # the selected model is missing: the estimate decides as before (16 GB fits gpt-oss-20b, not 120b)
    ch2 = probe.choose_rungs(24.0, {"kind": "cpu"}, _present("gpt-oss-20b-MXFP4.gguf", probe.EMBED["file"]), sel)
    assert ch2.get("source") != "selection" and ch2["main"]["file"] == "gpt-oss-20b-MXFP4.gguf"


def test_direct_mode_selection_means_no_chat_model():
    sel = {"mode": "direct", "embed": dict(probe.EMBED)}
    ch = probe.choose_rungs(64.0, {"kind": "cpu"}, _present("gpt-oss-20b-MXFP4.gguf", probe.EMBED["file"]), sel)
    assert ch["main"] is None and ch["class"] == "C0" and ch["embed"]["file"] == probe.EMBED["file"]


def test_ladder_prefers_the_largest_model_that_fits():
    files = _present("gpt-oss-120b-MXFP4.gguf", "gpt-oss-20b-MXFP4.gguf", probe.EMBED["file"])
    assert probe.choose_rungs(128.0, {"kind": "cpu"}, files, {})["main"]["rung"] == "C3"
    assert probe.choose_rungs(32.0, {"kind": "cpu"}, files, {})["main"]["rung"] == "C2"


def test_selected_server_arguments_reach_llama_server():
    from verity.llm.runtime import Runtime
    rt = Runtime(choice={"main": {"label": "x", "file": "x.gguf", "ctx": 8192, "args": ["--n-cpu-moe", 24]},
                         "fast": None, "embed": None})
    srv = rt.chat_server()
    assert srv.extra == ["--n-cpu-moe", "24"] and srv.ctx == 8192


def test_selection_file_is_read_from_models(monkeypatch, tmp_path):
    from verity import config
    monkeypatch.setattr(config, "MODELS", tmp_path)
    assert probe.selection() == {}
    (tmp_path / "selection.json").write_text(json.dumps({"mode": "direct"}), encoding="utf-8")
    assert probe.selection() == {"mode": "direct"}
    (tmp_path / "selection.json").write_text("{broken", encoding="utf-8")
    assert probe.selection() == {}


def test_windows_launchers_exist():
    root = Path(__file__).resolve().parent.parent
    for f in ("bin/verity.cmd", "bin/verity.ps1", "scripts/windows/setup.ps1", "scripts/windows/analyze_system.ps1",
              "scripts/windows/catalog.json"):
        assert (root / f).is_file(), f
