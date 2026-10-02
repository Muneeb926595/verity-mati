"""Local-only guarantees: the whole index → ask → scenario pipeline works with the network denied by the OS, and
runtime code contains no remote endpoints."""
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT


@pytest.mark.skipif(platform.system() != "Darwin" or not shutil.which("sandbox-exec"), reason="macOS sandbox only")
def test_pipeline_runs_with_network_denied(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(ROOT / "samples" / "navgps-c", repo)
    home = tmp_path / "home"
    prof = "(version 1)(allow default)(deny network-outbound (remote ip))(deny network-inbound (remote ip))"
    code = ("import sys; sys.path.insert(0, %r)\n"
            "from verity.engine.core import EngineCore\n"
            "if __name__ == '__main__':\n"
            "    c = EngineCore(llm_mode='direct', embeddings=False)\n"
            "    j = c.start_index(%r); assert c.wait(j['id'], 600)['state'] == 'done'\n"
            "    a = c.ask(%r, 'Explain how GPS data travels through this application.')['result']\n"
            "    print('FLOW', ' > '.join(s['label'] for s in a['flow']))\n"
            "    c.shutdown()\n") % (str(ROOT), str(repo), str(repo))
    script = tmp_path / "run.py"
    script.write_text(code)
    r = subprocess.run(["sandbox-exec", "-p", prof, sys.executable, str(script)], capture_output=True, text=True,
                       timeout=600, env={"VERITY_HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin"})
    assert "FLOW" in r.stdout and "motor_set_steering" in r.stdout and "USART2_IRQHandler" in r.stdout, r.stderr[-2000:]
    # (scenario runs use their own OS sandbox, which cannot be nested inside this one; they deny network themselves —
    #  see test_scenario.py::test_runs_are_sandboxed_and_leave_repo_untouched)


def test_no_remote_endpoints_in_runtime_code():
    bad = []
    for f in (ROOT / "verity").rglob("*.py"):
        if "bench" in f.parts:
            continue
        for m in re.finditer(r"https?://([A-Za-z0-9.-]+)", f.read_text(encoding="utf-8")):
            host = m.group(1)
            if host not in ("127.0.0.1", "localhost", "tauri.localhost"):
                bad.append(f"{f.relative_to(ROOT)}: {m.group(0)}")
    assert not bad, bad


def test_llm_servers_bind_localhost_only():
    src = (ROOT / "verity" / "llm" / "runtime.py").read_text(encoding="utf-8")
    assert '"--host", "127.0.0.1"' in src and "--api-key" in src
