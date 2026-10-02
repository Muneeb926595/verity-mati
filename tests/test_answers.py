"""End-to-end answers: the known-answer benchmark cases, as tests (Direct mode, no model needed)."""
import json
from pathlib import Path

import pytest

from conftest import ROOT, ask, needs_sandbox
from verity.bench.run import grade, vocabulary

SUITE = json.loads((ROOT / "bench" / "suite.json").read_text())
RUNS = ("run_state", "run_state_any", "run_state_absent", "hw_calls")      # cases checked against a real run


@pytest.mark.parametrize("case", [pytest.param(c, marks=needs_sandbox) if any(k in c for k in RUNS) else c
                                  for c in SUITE["cases"]], ids=[c["id"] for c in SUITE["cases"]])
def test_case(core, c_indexed, cs_indexed, case):
    repo = c_indexed if case["project"] == "c" else cs_indexed
    ans = ask(core, repo, case["q"])
    g = grade(case, ans, vocabulary(core.project(str(repo)).store))
    failing = {k: v for k, v in g["checks"].items() if not k.startswith("_") and not v["ok"]}
    assert g["passed"], (failing, g["checks"]["_hallucinated_names"], ans["answer"])


def test_every_answer_has_verified_evidence(core, c_indexed):
    ans = ask(core, c_indexed, "What does ProcessGpsData() do?")
    assert ans["evidence"] and all(e["file"] for e in ans["evidence"] if e["kind"] != "run")
    rep = ans["sections"]["verification"]["report"]
    assert rep["rejected"] == 0 and rep["verified"] == rep["claims"] > 5
