"""Known-answer benchmark (design §22). Runs every case in bench/suite.json through the real engine and grades:

  symbol identification   expected identifiers named in the answer (must_mention recall)
  call paths              expected call/flow chains appear in order (flow_contains, trace_contains)
  file/line references    expected citations present (must_cite recall) + every cited line really contains the
                          symbol it is cited for (citation precision)
  data-flow paths         ordered subsequence match of the "How the data flows" chain
  scenario results        observed final values / outputs / branch outcomes equal the gold harness
  unsupported claims      claims the verifier rejected, and final-answer sentences using ungrounded names
  hallucinations          code-like names in the answer that do not exist in the codebase; claims that a
                          non-existent symbol exists
  missing evidence        answer paragraphs with no evidence behind them
  false positives         names that must NOT appear (wrong callers etc.)
  execution trace         observed call sequence contains the expected functions in order
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from .. import config

CODE_TOKEN = re.compile(r"`([^`]+)`|\b([A-Za-z_][A-Za-z0-9_]*\(\)|[a-z]+_[a-z0-9_]+|[a-z]+[A-Z][A-Za-z0-9]*|[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*|[A-Z][A-Z0-9]*_[A-Z0-9_]+)\b")


def subseq(needles: list[str], hay: list[str]) -> tuple[bool, int]:
    """Ordered subsequence match (case-insensitive substring per item); returns (all found, how many in order)."""
    i = 0
    for h in hay:
        if i < len(needles) and needles[i].lower() in h.lower():
            i += 1
    return i == len(needles), i


def answer_text(ans: dict) -> str:
    parts = list(ans.get("answer", []))
    parts += [s["label"] + " " + (s.get("detail") or "") for s in ans.get("flow", [])]
    parts += [p.get("label", "") for p in ans.get("sections", {}).get("execution_path", [])]
    return "\n".join(parts)


def vocabulary(store) -> set[str]:
    v = set()
    for r in store.q("SELECT name, qual FROM symbol"):
        v.add(r["name"])
        for part in re.split(r"[.:]", r["qual"] or ""):
            v.add(part)
    root = store.meta_get("repo_root")
    for r in store.q("SELECT path FROM file"):
        v.add(r["path"])
        v.add(Path(r["path"]).name)
        v.add(Path(r["path"]).stem)
        v |= set(Path(r["path"]).parent.parts)
        # anything that appears in the project's own source text is not invented
        if root:
            try:
                v |= set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", (Path(root) / r["path"]).read_text(errors="replace")))
            except OSError:
                pass
    for r in store.q("SELECT DISTINCT sym FROM occ WHERE sym LIKE 'c:macro@%'"):
        v.add(r["sym"][8:])
    # library / framework APIs the compiler resolved calls to (`_handle.Set()` -> System.Threading.EventWaitHandle.Set):
    # facts from the compiler, not invented names
    for r in store.q("SELECT DISTINCT callee FROM call"):
        for part in re.split(r"[.:#()@,\[\]` ]+", r["callee"] or ""):
            if part:
                v.add(part)
    return v


def hallucinated_tokens(text: str, vocab: set[str], question: str) -> list[str]:
    bad = []
    low = {x.lower() for x in vocab}
    for m in CODE_TOKEN.finditer(text):
        if m.group(1):
            toks = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", m.group(1))
        else:
            toks = [m.group(2).rstrip("()")]
        for t in toks:
            if len(t) < 3 or t.lower() in low or t in question:
                continue
            if t.lower() in ("true", "false", "null", "return", "int", "double", "float", "void", "char", "unsigned",
                             "const", "sizeof", "static", "struct", "uint8_t", "uint16_t", "uint32_t", "int16_t",
                             "int32_t", "size_t", "else", "while", "for", "break", "string", "bool", "var", "new",
                             "this", "base", "object", "event", "get", "set", "value", "args"):
                continue
            bad.append(t)
    return sorted(set(bad))


def grade(case: dict, ans: dict, vocab: set[str]) -> dict:
    text = answer_text(ans)
    ev = ans.get("evidence", [])
    cited = {f"{e['file']}:{e['line']}" for e in ev if e.get("file")}
    for e in ev:
        if e.get("file") and e.get("end_line"):
            for ln in range(e["line"], (e["end_line"] or e["line"]) + 1):
                if e["kind"] == "def":
                    cited.add(f"{e['file']}:{ln}") if ln == e["line"] else None
    g: dict = {"id": case["id"], "category": case["category"], "q": case["q"], "intent": ans.get("route", {}).get("intent"),
               "checks": {}, "status": ans.get("status"), "seconds": ans.get("meta", {}).get("seconds")}
    c = g["checks"]
    if case.get("intent"):
        c["intent"] = {"ok": ans.get("route", {}).get("intent") == case["intent"], "expected": case["intent"],
                       "got": ans.get("route", {}).get("intent")}
    if case.get("must_mention"):
        hits = [m for m in case["must_mention"] if m.lower() in text.lower()]
        c["symbols"] = {"ok": len(hits) == len(case["must_mention"]), "score": len(hits) / len(case["must_mention"]),
                        "missing": [m for m in case["must_mention"] if m not in hits]}
    if case.get("must_cite"):
        hits = [x for x in case["must_cite"] if x in cited]
        c["citations"] = {"ok": len(hits) == len(case["must_cite"]), "score": len(hits) / len(case["must_cite"]),
                          "missing": [x for x in case["must_cite"] if x not in hits]}
    if case.get("flow_contains"):
        labels = [s["label"] for s in ans.get("flow", [])]
        ok, n = subseq(case["flow_contains"], labels)
        c["dataflow"] = {"ok": ok, "score": n / len(case["flow_contains"]), "got": labels}
    run = ans.get("run") or {}
    runs = run.get("runs") or []
    if case.get("run_state") or case.get("run_state_any") or case.get("run_state_absent"):
        if not runs:
            c["scenario"] = {"ok": False, "score": 0.0, "why": "no run"}
        else:
            final = runs[-1].get("state") or {}
            exp = case.get("run_state") or case.get("run_state_any") or {}
            ok_items = [k for k, v in exp.items() if any(str((r.get("state") or {}).get(k, "")).startswith(v) for r in
                                                        (runs if case.get("run_state_any") else [runs[-1]]))]
            absent_ok = all(k not in final for k in case.get("run_state_absent", []))
            total = len(exp) + (1 if case.get("run_state_absent") else 0)
            got = len(ok_items) + (1 if case.get("run_state_absent") and absent_ok else 0)
            c["scenario"] = {"ok": got == total, "score": got / max(1, total), "final": final}
    if case.get("hw_calls"):
        allhw = [h for r in runs for h in (r.get("hw_calls") or [])]
        hits = [h for h in case["hw_calls"] if any(x == h or x.startswith(h) for x in allhw)]
        c["outputs"] = {"ok": len(hits) == len(case["hw_calls"]), "score": len(hits) / len(case["hw_calls"]), "got": allhw}
    if case.get("hw_calls_absent"):
        last = runs[-1].get("hw_calls") if runs else []
        bad = [h for h in (last or []) if any(h.startswith(x) for x in case["hw_calls_absent"])]
        c["outputs_absent"] = {"ok": bool(runs) and not bad, "score": 1.0 if runs and not bad else 0.0, "got": last}
    if case.get("trace_contains"):
        trace = runs[0].get("calls", []) if runs else []
        ok, n = subseq(case["trace_contains"], trace)
        c["trace"] = {"ok": ok, "score": n / len(case["trace_contains"]), "got": trace[:30]}
    if case.get("branches"):
        brs = {f"{b['file']}:{b['line']}": b.get("taken") for r in runs[:1] for b in r.get("branches", [])}
        hits = [k for k, v in case["branches"].items() if brs.get(k) == v]
        c["branches"] = {"ok": len(hits) == len(case["branches"]), "score": len(hits) / len(case["branches"]),
                         "got": {k: brs.get(k) for k in case["branches"]}}
    if case.get("must_not_mention"):
        fp = [m for m in case["must_not_mention"] if re.search(rf"\b{re.escape(m)}\b", text)]
        c["false_positives"] = {"ok": not fp, "score": 0.0 if fp else 1.0, "found": fp}
    if case.get("must_not_claim_defined"):
        bad = []
        for name in case["must_not_claim_defined"]:
            for cl in ans.get("sections", {}).get("reasoning", []):
                if name in cl["text"] and cl.get("verified") and not re.search(rf"\b{re.escape(name)}\b is not defined",
                                                                               cl["text"]):
                    bad.append(cl["text"])          # a verified claim ABOUT it (saying it is absent is the right answer)
            if any(name in (e.get("text") or "") for e in ev):
                bad.append(f"evidence mentions {name}")
            # a clause that names it and says it is defined / called — unless the clause denies it ("There is no
            # function named X in this code; the closest routine is Y, which is called by Z" is the right answer)
            for clause in re.split(r"[.;]\s", " ".join(ans.get("answer", []))):
                if re.search(rf"\b{name}\b[^.;]*\b(is defined|defined in|is called by|calls)\b", clause) and \
                        not re.search(r"\b(no|not|never|nothing|none)\b|n't\b", clause.split(name, 1)[0] + " " +
                                      clause.split(name, 1)[1][:40]):
                    bad.append("answer asserts it exists")
                    break
        c["no_invention"] = {"ok": not bad, "score": 0.0 if bad else 1.0, "found": bad}
    # always-on quality metrics
    rej = ans.get("sections", {}).get("verification", {}).get("report", {})
    claims = rej.get("claims", 0)
    c["_unsupported_claims"] = {"rejected": rej.get("rejected", 0), "claims": claims}
    halluc = hallucinated_tokens(" ".join(ans.get("answer", [])), vocab, case["q"])
    c["_hallucinated_names"] = {"names": halluc}
    # precision of citations: each evidence line contains the symbol it was captured for (verifier ran it)
    reasoning = ans.get("sections", {}).get("reasoning", [])
    c["_citation_precision"] = {"verified": sum(1 for r in reasoning if r.get("verified")), "total": len(reasoning)}
    tmpl = ans.get("meta", {}).get("template") or ans.get("answer", [])
    c["_missing_evidence"] = {"paragraphs": len(ans.get("answer", [])), "without_evidence": 0 if ev else len(ans.get("answer", []))}
    scored = [v for k, v in c.items() if not k.startswith("_")]
    g["passed"] = all(v["ok"] for v in scored) and not halluc
    g["score"] = round(sum(v.get("score", 1.0 if v["ok"] else 0.0) for v in scored) / max(1, len(scored)), 3)
    g["topk_rank"] = topk_rank(case, ans, g["passed"])
    return g


def topk_rank(case: dict, ans: dict, passed: bool, k: int = 3) -> int | None:
    """Where the right place was among the top candidates the analysis ranked (Phase 5, P5-0): 1 when the answer
    passes; 2 or 3 when that runner-up's code (function, check and its side) contains every must-mention name; None
    otherwise. Measures how often 'show the alternatives' could help, not what the answer said."""
    if passed:
        return 1
    must = case.get("must_mention") or []
    cands = ((ans.get("meta") or {}).get("learning") or {}).get("candidates") or []
    if not must:
        return None
    for c in cands[1:k]:
        text = f"{c.get('label') or ''}\n{c.get('text') or ''}".lower()
        if all(m.lower() in text for m in must):
            return c.get("rank")
    return None


def summarize(results: list[dict]) -> dict:
    def agg(key: str) -> dict:
        vals = [r["checks"][key] for r in results if key in r["checks"]]
        if not vals:
            return {"cases": 0}
        return {"cases": len(vals), "passed": sum(1 for v in vals if v["ok"]),
                "accuracy": round(sum(v.get("score", 1.0 if v["ok"] else 0.0) for v in vals) / len(vals), 3)}
    rej = sum(r["checks"]["_unsupported_claims"]["rejected"] for r in results)
    tot = sum(r["checks"]["_unsupported_claims"]["claims"] for r in results)
    hall = [(r["id"], r["checks"]["_hallucinated_names"]["names"]) for r in results if r["checks"]["_hallucinated_names"]["names"]]
    cp_v = sum(r["checks"]["_citation_precision"]["verified"] for r in results)
    cp_t = sum(r["checks"]["_citation_precision"]["total"] for r in results)
    miss = sum(r["checks"]["_missing_evidence"]["without_evidence"] for r in results)
    paras = sum(r["checks"]["_missing_evidence"]["paragraphs"] for r in results)
    return {
        "cases": len(results), "passed": sum(1 for r in results if r["passed"]),
        "within_top3": sum(1 for r in results if r.get("topk_rank")),     # the right place among the top 3 candidates
        "mean_score": round(sum(r["score"] for r in results) / max(1, len(results)), 3),
        "symbol_identification": agg("symbols"), "call_paths_and_flows": agg("dataflow"), "file_line_references": agg("citations"),
        "scenario_results": agg("scenario"), "scenario_outputs": agg("outputs"), "execution_trace": agg("trace"),
        "branch_outcomes": agg("branches"), "intent_routing": agg("intent"), "false_positives": agg("false_positives"),
        "no_invention_traps": agg("no_invention"),
        "unsupported_claims": {"rejected_by_verifier": rej, "claims": tot,
                               "rate": round(rej / tot, 4) if tot else 0.0},
        "hallucinated_names_in_answers": {"cases": len(hall), "details": hall},
        "citation_precision": {"verified_claims": cp_v, "claims": cp_t, "rate": round(cp_v / cp_t, 4) if cp_t else 1.0},
        "missing_evidence": {"paragraphs_without_evidence": miss, "paragraphs": paras},
        "mean_seconds": round(sum(r["seconds"] or 0 for r in results) / max(1, len(results)), 2),
    }


def to_markdown(summary: dict, results: list[dict], label: str) -> str:
    L = [f"# Verity benchmark — {label}", "", f"Cases: {summary['cases']} · passed: {summary['passed']} · mean score: "
         f"{summary['mean_score']} · mean time per question: {summary['mean_seconds']} s" +
         (f" · right place within the top 3 candidates: {summary['within_top3']}" if summary.get("within_top3") else ""),
         "", "| metric | result |", "|---|---|"]
    for k in ("symbol_identification", "call_paths_and_flows", "file_line_references", "scenario_results", "scenario_outputs",
              "execution_trace", "branch_outcomes", "intent_routing", "false_positives", "no_invention_traps"):
        v = summary[k]
        if v.get("cases"):
            L.append(f"| {k.replace('_', ' ')} | {v['passed']}/{v['cases']} passed · accuracy {v['accuracy']:.3f} |")
    u = summary["unsupported_claims"]
    L.append(f"| unsupported claims (rejected by verifier) | {u['rejected_by_verifier']} of {u['claims']} ({u['rate']:.2%}) — removed before display |")
    h = summary["hallucinated_names_in_answers"]
    L.append(f"| hallucinated code names in final answers | {h['cases']} case(s) |")
    cp = summary["citation_precision"]
    L.append(f"| citation precision (claims whose cited line re-verified) | {cp['verified_claims']}/{cp['claims']} ({cp['rate']:.2%}) |")
    me = summary["missing_evidence"]
    L.append(f"| answer paragraphs without any evidence | {me['paragraphs_without_evidence']} of {me['paragraphs']} |")
    L += ["", "## Cases", "", "| id | category | intent | passed | score | failing checks |", "|---|---|---|---|---|---|"]
    for r in results:
        fails = [f"{k}: {json.dumps({kk: vv for kk, vv in v.items() if kk in ('missing', 'found', 'expected', 'got', 'why')}, default=str)[:160]}"
                 for k, v in r["checks"].items() if not k.startswith("_") and not v["ok"]]
        if r["checks"]["_hallucinated_names"]["names"]:
            fails.append("hallucinated: " + ", ".join(r["checks"]["_hallucinated_names"]["names"]))
        L.append(f"| {r['id']} | {r['category']} | {r['intent']} | {'✓' if r['passed'] else '✗'} | {r['score']} | "
                 f"{'; '.join(fails).replace('|', '/')} |")
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(config.ROOT / "bench" / "results"))
    ap.add_argument("--suite", default=str(config.ROOT / "bench" / "suite.json"))
    ap.add_argument("--llm", action="store_true", help="phrase answers with the local model (default: Direct mode)")
    ap.add_argument("--embed", action="store_true", help="index and search with meaning search (embeddings) too")
    ap.add_argument("--only", default=None, help="comma-separated case ids")
    ap.add_argument("--no-reindex", action="store_true")
    a = ap.parse_args(argv)
    from ..engine.core import EngineCore
    suite = json.loads(Path(a.suite).read_text())
    core = EngineCore(llm_mode="auto" if a.llm else "direct", embeddings=bool(a.embed))
    roots = {k: str((config.ROOT / v).resolve()) for k, v in suite["projects"].items()}
    try:
        for k, r in roots.items():
            if not a.no_reindex:
                j = core.start_index(r, full=True)
                res = core.wait(j["id"], 1800)
                print(f"[bench] indexed {k}: {res['state']} {res.get('result', {}).get('seconds') if res.get('result') else ''}")
        vocabs = {k: vocabulary(core.project(r).store) for k, r in roots.items()}
        results = []
        cases = suite["cases"]
        if a.only:
            ids = set(a.only.split(","))
            cases = [c for c in cases if c["id"] in ids]
        for case in cases:
            t0 = time.time()
            try:
                r = core.ask(roots[case["project"]], case["q"], use_llm=a.llm, timeout=900)
                ans = r["result"]
            except Exception as e:  # noqa: BLE001
                ans = {"answer": [f"ERROR {e}"], "evidence": [], "flow": [], "sections": {"reasoning": [], "verification":
                       {"report": {}}}, "meta": {"seconds": time.time() - t0}, "route": {}}
            g = grade(case, ans, vocabs[case["project"]])
            g["answer"] = ans.get("answer")
            g["prose_mode"] = ans.get("meta", {}).get("prose", {}).get("mode")
            results.append(g)
            print(f"[bench] {'PASS' if g['passed'] else 'FAIL'} {case['id']:<26} score={g['score']:.2f} "
                  f"{g['seconds'] or 0:.1f}s")
        summary = summarize(results)
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        label = ("llm" if a.llm else "direct") + ("-embed" if a.embed else "")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (out / f"bench-{label}-{stamp}.json").write_text(json.dumps({"summary": summary, "results": results}, indent=1,
                                                                    default=str))
        md = to_markdown(summary, results, f"{label} mode ({stamp})")
        (out / f"bench-{label}-{stamp}.md").write_text(md)
        (out / f"latest-{label}.md").write_text(md)
        (out / f"latest-{label}.json").write_text(json.dumps({"summary": summary, "results": results}, indent=1, default=str))
        print(md)
        return 0 if summary["passed"] == summary["cases"] else 1
    finally:
        core.shutdown()


if __name__ == "__main__":
    sys.exit(main())
