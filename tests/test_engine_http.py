"""The engine daemon: token auth, JSON-RPC, SSE progress, origin checks, path safety."""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from conftest import ROOT


@pytest.fixture(scope="module")
def server(c_indexed):
    p = subprocess.Popen([sys.executable, "-m", "verity.engine.server", "--port", "0", "--token", "tok123",
                          "--print-ready", "--llm", "direct"], cwd=str(ROOT), stdout=subprocess.PIPE, text=True)
    line = p.stdout.readline()
    info = json.loads(line)
    yield info
    p.terminate()
    try:
        p.wait(20)
    except subprocess.TimeoutExpired:
        p.kill()
        raise


def rpc(info, method, params=None, token="tok123", origin=None):
    req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/rpc", method="POST",
                                 data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode(),
                                 headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {}),
                                          **({"Origin": origin} if origin else {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def test_requires_token(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        rpc(server, "engine.info", token="wrong")
    assert e.value.code == 401


def test_rejects_foreign_origin(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        rpc(server, "engine.info", origin="https://evil.example.com")
    assert e.value.code == 403


def test_rpc_roundtrip_and_errors(server, c_indexed):
    r = rpc(server, "engine.info")
    assert r["result"]["offline"] is True
    r = rpc(server, "project.status", {"path": str(c_indexed)})
    assert r["result"]["ready"]
    r = rpc(server, "no.such.method")
    assert r["error"]["code"] == -32601
    r = rpc(server, "source.get", {"path": str(c_indexed), "file": "../../../../etc/hosts"})
    assert "outside the project" in r["error"]["message"]


def test_ask_over_http_with_sse_progress(server, c_indexed):
    ev_req = urllib.request.Request(f"http://127.0.0.1:{server['port']}/events?token=tok123")
    stream = urllib.request.urlopen(ev_req, timeout=60)
    job = rpc(server, "ask", {"path": str(c_indexed), "question": "Who calls CalculateHeading?"})["result"]
    types = []
    t0 = time.time()
    while time.time() - t0 < 60:
        line = stream.readline().decode()
        if line.startswith("data: "):
            ev = json.loads(line[6:])
            types.append(ev["type"])
            if ev["type"] == "job.done" and ev["job"]["id"] == job["id"]:
                assert "ProcessGpsData" in " ".join(ev["job"]["result"]["answer"])
                break
    assert "ask.progress" in types and "job.done" in types


def _engine(tmp_path, **env):
    import os
    e = {**os.environ, "VERITY_HOME": str(tmp_path), **env}
    p = subprocess.Popen([sys.executable, "-m", "verity.engine.server", "--port", "0", "--print-ready", "--llm", "direct"],
                         cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=e)
    assert json.loads(p.stdout.readline())["ready"]
    return p


def test_engine_stops_cleanly_when_the_desktop_app_closes_its_stdin(tmp_path):
    """The Windows desktop app stops the engine (and learns of its own death) through the stdin pipe it holds."""
    p = _engine(tmp_path, VERITY_STOP_ON_STDIN_EOF="1")
    assert (tmp_path / "engine.json").is_file()
    p.stdin.close()
    try:
        assert p.wait(30) == 0
    finally:
        if p.poll() is None:
            p.kill()
    assert not (tmp_path / "engine.json").exists()            # the clean shutdown path ran


def test_engine_ignores_stdin_unless_asked(tmp_path):
    p = _engine(tmp_path)
    try:
        p.stdin.close()
        time.sleep(3)
        assert p.poll() is None
    finally:
        p.kill()
        p.wait(20)


def test_engine_watching_its_stdin_still_starts_parser_workers(tmp_path, c_repo):
    """Windows: a thread blocked reading the stdin pipe stalled the parser workers' start, so a desktop-app engine
    never finished indexing. The pipe is polled instead."""
    p = _engine(tmp_path, VERITY_STOP_ON_STDIN_EOF="1")
    try:
        info = json.loads((tmp_path / "engine.json").read_text())
        info["port"], tok = info["port"], info["token"]
        assert rpc(info, "index.start", {"path": str(c_repo), "full": True}, token=tok)["result"]
        t0 = time.time()
        st = {}
        while time.time() - t0 < 90:
            st = rpc(info, "project.status", {"path": str(c_repo)}, token=tok)["result"]
            if st.get("state") in ("complete", "failed", "cancelled"):
                break
            time.sleep(0.5)
        assert st.get("state") == "complete" and st.get("ready"), st
    finally:
        p.stdin.close()
        try:
            p.wait(30)
        except subprocess.TimeoutExpired:
            p.kill()
            raise


def test_stale_ui_is_reported(tmp_path, monkeypatch):
    # after a pull or a branch checkout, ui/src is newer than the built ui/dist: the engine says so when it starts
    import os
    from verity.engine import server as srv
    (tmp_path / "src").mkdir()
    (tmp_path / "dist").mkdir()
    index, source = tmp_path / "dist" / "index.html", tmp_path / "src" / "App.tsx"
    index.write_text("<html></html>")
    source.write_text("export {}")
    monkeypatch.setattr(srv, "UI_DIR", tmp_path / "dist")
    os.utime(source, (time.time() - 100, time.time() - 100))
    assert not srv.ui_stale()
    os.utime(source, (time.time() + 100, time.time() + 100))
    assert srv.ui_stale()
    index.unlink()
    assert not srv.ui_stale()              # no build at all: nothing to compare (setup builds it)
