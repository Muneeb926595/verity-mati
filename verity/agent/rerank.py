"""Grounded re-ranking with the local model (design §20/§23, D-impl-23).

The recipes find candidate places in the code (checks, functions, call sites) and every fact about them is verified.
What heuristics cannot always do is judge MEANING: that "runs out of free blocks" is an allocator returning a no-space
code, or that "the heartbeat times out" is a consumer's timer check. Here the local model sees the question and the top
few candidates — each shown as the real code — and only picks one (or none). It cannot add a fact, a name or a line:
the answer is still built from the chosen candidate's verified facts, and the choice is disclosed in the answer.
"""
from __future__ import annotations

from typing import Any

SYSTEM = (
    "You help answer a question about a C or C# codebase. Static analysis found a few candidate places in the code; "
    "each candidate below is real code with its file and line. Pick the ONE candidate whose code best answers the "
    "question. Judge by what the code means and does — consider synonyms, abbreviations and the domain — not by how "
    "many words it shares with the question. If no candidate answers it, choose 0. Reply as JSON: "
    '{"choice": <candidate number or 0>, "why": "<one short sentence>"}.'
)
SCHEMA = {"type": "object", "properties": {"choice": {"type": "integer"}, "why": {"type": "string"}},
          "required": ["choice", "why"], "additionalProperties": False}
MAX_CANDIDATES = 11


def pick(llm: Any, question: str, candidates: list[str], timeout: float = 120.0) -> tuple[int | None, str]:
    """Index (0-based) of the candidate the model judges best, or None (no model, no choice, or 'none fits')."""
    if llm is None or len(candidates) < 2:
        return None, ""
    cands = candidates[:MAX_CANDIDATES]
    user = f"Question: {question}\n\nCandidates:\n" + "\n\n".join(f"[{i + 1}] {c}" for i, c in enumerate(cands))
    try:
        res = llm.chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}], schema=SCHEMA,
                       max_tokens=300, temperature=0.0, reasoning="low", timeout=timeout)
    except Exception:  # noqa: BLE001 — the deterministic order stands
        return None, ""
    data = res.get("json") or {}
    try:
        ch = int(data.get("choice", 0))
    except (TypeError, ValueError):
        return None, ""
    if 1 <= ch <= len(cands):
        return ch - 1, str(data.get("why") or "")[:240]
    return None, ""
