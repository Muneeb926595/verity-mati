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
