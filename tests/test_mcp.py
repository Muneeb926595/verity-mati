"""MCP stdio server: handshake, tool listing, a tool call answered by the same engine."""
import json
import subprocess
import sys

from conftest import ROOT


def test_mcp_stdio(c_indexed, tmp_path):
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18",
                                                                          "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "verity_search",
                                                                          "arguments": {"path": str(c_indexed), "query": "CalculateHeading"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope", "arguments": {}}}]
    p = subprocess.run([sys.executable, "-m", "verity", "mcp"], cwd=str(ROOT), input="\n".join(json.dumps(m) for m in msgs) + "\n",
                       capture_output=True, text=True, timeout=120)
    out = {m["id"]: m for m in (json.loads(l) for l in p.stdout.splitlines() if l.strip())}
    assert out[1]["result"]["serverInfo"]["name"] == "verity"
    assert {t["name"] for t in out[2]["result"]["tools"]} >= {"verity_ask", "verity_index", "verity_search"}
    assert "CalculateHeading" in out[3]["result"]["content"][0]["text"]
    assert "error" in out[4]
