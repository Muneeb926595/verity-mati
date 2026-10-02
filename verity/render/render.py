"""Answer renderer (design §11/§18): one answer object that every client shows the same way —
Your question · Answer · How the data flows · Evidence, plus five expandable sections (reasoning/evidence, execution
path, related code, assumptions, verification)."""
from __future__ import annotations

from typing import Any

from ..agent.evidence import PLAIN_STATUS, Pack


def render(question: str, route: dict, pack: Pack, paragraphs: list[str], report: dict, meta: dict) -> dict[str, Any]:
    ev_used: list[str] = []
    for c in pack.claims:
        if c.verified:
            for e in c.ev:
                if e not in ev_used:
                    ev_used.append(e)
    for st in pack.flow:
        if st.get("ev") and st["ev"] not in ev_used and st["ev"] in pack.evidence:
            ev_used.append(st["ev"])
    evidence = []
    seen_loc: dict[tuple, dict] = {}
    for eid in ev_used:
        e = pack.evidence.get(eid)
        if not e:
            continue
        if e.file and (e.file, e.line) in seen_loc:
            seen_loc[(e.file, e.line)].setdefault("also", []).append(eid)
            continue
        d = e.as_dict()
        d["status_label"] = PLAIN_STATUS.get(e.status, e.status)
        evidence.append(d)
        if e.file:
            seen_loc[(e.file, e.line)] = d
    evidence.sort(key=lambda d: (d["kind"] == "run", d["file"] or "", d["line"] or 0))
    flow = []
    for st in pack.flow:
        if flow and flow[-1]["label"] == st["label"]:
            continue
        flow.append({"label": st["label"], "file": st.get("file"), "line": st.get("line"), "detail": st.get("detail", "")})
    reasoning = [dict(c.as_dict(), status_label=PLAIN_STATUS.get(c.status, c.status)) for c in pack.claims]
    counts: dict[str, int] = {}
    for c in pack.claims:
        if c.verified:
            counts[c.status] = counts.get(c.status, 0) + 1
    overall = pack.overall()
    return {
        "question": question,
        "answer": paragraphs,
        "flow": flow,
        "evidence": evidence,
        "status": overall,
        "status_label": PLAIN_STATUS.get(overall, overall),
        "status_counts": counts,
        "sections": {
            "details": meta.get("template") if meta.get("prose", {}).get("mode") == "model" else [],   # model+facts: already inline
            "reasoning": reasoning,
            "execution_path": pack.path,
            "related": _dedupe(pack.related),
            "assumptions": list(dict.fromkeys(pack.assumptions)),
            "verification": {"report": report, "unknowns": pack.unknowns, "notes": pack.notes,
                             "rejected": [c.as_dict() for c in pack.claims if c.verified is False]},
        },
        "run": pack.run,
        "route": route,
        "meta": meta,
    }


def _dedupe(items: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for it in items:
        k = (it.get("key"), it.get("file"), it.get("line"))
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def to_text(ans: dict[str, Any], details: bool = False, width: int = 100) -> str:
    """Plain-text rendering for the CLI."""
    import textwrap
    out = [f"Your question\n  {ans['question']}", "", "Answer"]
    for p in ans["answer"]:
        out += ["  " + ln for ln in textwrap.wrap(p, width - 2)] + [""]
    if ans["flow"]:
        out.append("How the data flows")
        for i, st in enumerate(ans["flow"]):
            loc = f"  ({st['file']}:{st['line']})" if st.get("file") else ""
            out.append(f"  {'↓ ' if i else '  '}{st['label']}{loc}")
        out.append("")
    if ans["evidence"]:
        out.append("Evidence")
        for e in ans["evidence"][:25]:
            if e["kind"] == "run":
                note = e["note"] or ""
                out.append(f"  [{e['id']}] " + (note if note.startswith("sandboxed run") else f"sandboxed run — {note}"))
                continue
            out.append(f"  [{e['id']}] {e['file']}:{e['line']}  {e['text'][:90]}")
        out.append("")
    out.append(f"Certainty: {ans['status_label']}  " + ", ".join(f"{k.lower()}: {v}" for k, v in ans["status_counts"].items()))
    if details:
        s = ans["sections"]
        if s.get("details"):
            out += ["", "Detailed answer (checked facts, before AI wording)"]
            for p in s["details"]:
                out += ["  " + ln for ln in textwrap.wrap(p, width - 2)] + [""]
        if s["execution_path"]:
            out += ["", "Execution path"] + [f"  → {p['label']}" + (f"  ({p['file']}:{p['line']})" if p.get("file") else "")
                                             for p in s["execution_path"]]
        if s["assumptions"]:
            out += ["", "Assumptions"] + [f"  • {a}" for a in s["assumptions"]]
        v = s["verification"]
        out += ["", "Verification",
                f"  {v['report'].get('verified', 0)} of {v['report'].get('claims', 0)} statements re-checked against the code"
                + (f"; {v['report'].get('rejected')} removed" if v['report'].get('rejected') else "")]
        for c in s["reasoning"]:
            mark = "✓" if c["verified"] else "✗"
            out.append(f"  {mark} [{c['status']}] {c['text']}" + (f"  — {c['why']}" if c["why"] else ""))
        for u in v["unknowns"] + v["notes"]:
            out.append(f"  • {u}")
    return "\n".join(out)
