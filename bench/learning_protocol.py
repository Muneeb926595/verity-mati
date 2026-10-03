#!/usr/bin/env python3
"""Phase 5 proof (docs/development/VERITY_LEARNING_FROM_CORRECTIONS_ONE_PAGER.html, section 07), on the tenth held-out
project (cJSON), whose questions were written from the source before Verity ever ran on it.

  python bench/learning_protocol.py [--embed | --llm] [--out bench/results/realworld_heldout10_learning]

1. First contact: every main question (bench/realworld_heldout10.json), no corrections.
2. One correction per miss, as a person would make it: the line that decides, picked in the source (CORRECTIONS).
3. The corrected questions asked again; every paraphrase and neighbour (bench/realworld_heldout10_followup.json).
4. A corrected line is edited and the project re-indexed: that correction must turn stale and not be used.
Two numbers are reported and never blended: before any correction, and after one correction per miss.
Runs on a private copy of the project with its own data folder, so nothing in samples/ or data/ changes."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "bench" / "realworld_heldout10.json"
FOLLOW = ROOT / "bench" / "realworld_heldout10_followup.json"

# The line a person who knows cJSON would pick for each question, read from the source (file, line, note).
CORRECTIONS = {
    "cj-deep-nesting": ("cJSON.c", 1497, "the nesting limit is checked when an array starts"),
    "cj-minus-no-digits": ("cJSON.c", 379, "strtod consumed nothing: not a number"),
    "cj-unterminated-string": ("cJSON.c", 851, "the closing quote is looked for here"),
    "cj-bad-surrogate": ("cJSON.c", 745, "the second half must be a low surrogate"),
    "cj-member-alloc-fail": ("cJSON.c", 1689, "a failed allocation of the next member"),
    "cj-prealloc-too-small": ("cJSON.c", 513, "a preallocated buffer never grows"),
    "cj-trailing-garbage": ("cJSON.c", 1174, "with require_null_terminated only whitespace may follow"),
    "cj-key-case": ("cJSON.c", 1955, "cJSON_GetObjectItem compares case-insensitively"),
    "cj-pointer-leading-zero": ("cJSON_Utils.c", 279, "leading zeros are refused"),
    "cj-patch-unknown-op": ("cJSON_Utils.c", 828, "an unknown op gives status 3"),
    "cj-custom-allocator": ("cJSON.c", 223, "the hooks are stored in global_hooks"),
    "cj-object-indent": ("cJSON.c", 1807, "one tab per nesting level"),
    "cj-generate-patches": ("cJSON_Utils.c", 1150, "create_patches composes each patch entry"),
    "cj-string-parser-callers": None,
}
EDIT = ("cJSON.c", 513, "if (p->noalloc) {", "if (p->noalloc != 0) {")     # step 4: change a corrected line


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true", help="answer with the local model and meaning search too")
    ap.add_argument("--embed", action="store_true", help="facts-only answers, with meaning search (embedding model)")
    ap.add_argument("--out", default=str(ROOT / "bench" / "results" / "realworld_heldout10_learning"))
    a = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="verity-learning-proof-"))
    os.environ["VERITY_HOME"] = str(work / "home")
    sys.path.insert(0, str(ROOT))
    from verity.bench.run import grade, vocabulary
    from verity.engine.core import EngineCore, EngineError

    main_suite = json.loads(MAIN.read_text())
    follow = json.loads(FOLLOW.read_text())
    proj = work / "cJSON"
    shutil.copytree(ROOT / main_suite["projects"]["cjson"], proj)
    core = EngineCore(llm_mode="auto" if a.llm else "direct", embeddings=bool(a.llm or a.embed))
    mode = "llm-embed" if a.llm else "direct-embed" if a.embed else "direct"
    out: dict = {"mode": mode, "started": time.strftime("%Y-%m-%d %H:%M"), "steps": {}}
    log = []

    def say(s: str) -> None:
        print(s, flush=True)
        log.append(s)

    def run(case: dict) -> dict:
        ans = core.ask(str(proj), case["q"], use_llm=a.llm, timeout=900)["result"]
        g = grade(case, ans, vocabulary(core.project(str(proj)).store))
        lm = ans["meta"].get("learning") or {}
        return {"id": case["id"], "passed": g["passed"], "missing": (g["checks"].get("symbols") or {}).get("missing"),
                "used": [(u["id"], u["how"]) for u in lm.get("used") or []], "answer_id": ans.get("id"),
                "correctable": lm.get("correctable"), "intent": ans["meta"].get("intent"),
                "answer": " ".join(ans["answer"])[:600]}

    try:
        r = core.wait(core.start_index(str(proj), full=True)["id"], 1800)
        assert r["state"] == "done", r
        # 1. first contact
        first = [run(c) for c in main_suite["cases"]]
        say(f"1. first contact: {sum(x['passed'] for x in first)}/{len(first)}")
        follow_before = [run(c) for c in follow["cases"]]
        par = [x for x, c in zip(follow_before, follow["cases"]) if c.get("paraphrase_of")]
        nb = [x for x, c in zip(follow_before, follow["cases"]) if c.get("neighbour_of")]
        say(f"   paraphrases before corrections: {sum(x['passed'] for x in par)}/{len(par)}; "
            f"neighbours: {sum(x['passed'] for x in nb)}/{len(nb)}")
        # 2. one correction per miss
        made, refused = {}, {}
        for x in first:
            if x["passed"]:
                continue
            spec = CORRECTIONS.get(x["id"])
            if not spec:
                refused[x["id"]] = "no single deciding line (a structural question)"
                continue
            f, ln, note = spec
            try:
                c = core.call("answer.correct", {"path": str(proj), "answer_id": x["answer_id"],
                                                 "target": {"file": f, "line": ln}, "note": note})
                made[x["id"]] = c["correction"]["id"]
            except EngineError as e:
                refused[x["id"]] = str(e)
        say(f"2. corrections: {len(made)} made, {len(refused)} not possible: {refused}")
        # 3. ask again: the corrected questions, then every paraphrase and neighbour
        by_id = {c["id"]: c for c in main_suite["cases"]}
        again = [run(by_id[i]) for i in made]
        disclosed = sum(1 for x in again if x["used"])
        say(f"3. corrected questions asked again: {sum(x['passed'] for x in again)}/{len(again)} right, "
            f"{disclosed}/{len(again)} disclosed the correction")
        after_main = []
        for c in main_suite["cases"]:
            after_main.append(next((x for x in again if x["id"] == c["id"]), None) or
                              next(x for x in first if x["id"] == c["id"]))
        follow_after = [run(c) for c in follow["cases"]]
        par_c = [(x, c) for x, c in zip(follow_after, follow["cases"]) if c.get("paraphrase_of") in made]
        par_u = [(x, c) for x, c in zip(follow_after, follow["cases"]) if c.get("paraphrase_of") and c["paraphrase_of"] not in made]
        nb_a = [(x, c) for x, c in zip(follow_after, follow["cases"]) if c.get("neighbour_of")]
        say(f"   paraphrases of corrected questions: {sum(x['passed'] for x, _ in par_c)}/{len(par_c)} right "
            f"({sum(1 for x, _ in par_c if x['used'])} used a correction)")
        say(f"   paraphrases of questions that needed no correction: {sum(x['passed'] for x, _ in par_u)}/{len(par_u)}")
        say(f"   neighbours (no target): {sum(x['passed'] for x, _ in nb_a)}/{len(nb_a)} right; "
            f"{sum(1 for x, _ in nb_a if x['used'])} used a correction")
        # 4. an edited line: the correction turns stale and is never applied
        f, ln, old, new = EDIT
        p = proj / f
        txt = p.read_text()
        assert old in txt.split("\n")[ln - 1], (f, ln)
        p.write_text(txt.replace(old, new, 1))
        core.wait(core.start_index(str(proj))["id"], 1800)
        lst = core.call("corrections.list", {"path": str(proj)})
        target_id = made.get("cj-prealloc-too-small")
        st = next((c for c in lst["corrections"] if c["id"] == target_id), None)
        q = run(by_id["cj-prealloc-too-small"]) if target_id else None
        stale_ok = bool(st and st["status"] == "stale" and q and not any(i == target_id for i, _ in q["used"]))
        say(f"4. edited {f}:{ln} and re-indexed: correction {target_id} is {st and st['status']} "
            f"({st and st['status_reason']}); used afterwards: {bool(q and q['used'])} -> {'OK' if stale_ok else 'FAILED'}")
        out["steps"] = {"first_contact": first, "follow_before": follow_before, "corrections": made,
                        "not_corrected": refused, "asked_again": again, "follow_after": follow_after,
                        "stale_check": {"correction": st, "ok": stale_ok, "answer": q}}
        out["numbers"] = {
            "before": {"main": f"{sum(x['passed'] for x in first)}/{len(first)}",
                       "paraphrases": f"{sum(x['passed'] for x in par)}/{len(par)}",
                       "neighbours": f"{sum(x['passed'] for x in nb)}/{len(nb)}"},
            "after_one_correction_per_miss": {
                "main": f"{sum(x['passed'] for x in after_main)}/{len(after_main)}",
                "corrected_questions": f"{sum(x['passed'] for x in again)}/{len(again)}",
                "paraphrases_of_corrected": f"{sum(x['passed'] for x, _ in par_c)}/{len(par_c)}",
                "paraphrases_needing_none": f"{sum(x['passed'] for x, _ in par_u)}/{len(par_u)}",
                "neighbours": f"{sum(x['passed'] for x, _ in nb_a)}/{len(nb_a)}"},
            "stale_after_edit": stale_ok}
        out["log"] = log
        dest = Path(a.out)
        dest.mkdir(parents=True, exist_ok=True)
        name = f"proof-{out['mode']}-{time.strftime('%Y%m%d-%H%M%S')}"
        (dest / f"{name}.json").write_text(json.dumps(out, indent=1, default=str))
        (dest / f"latest-{out['mode']}.json").write_text(json.dumps(out, indent=1, default=str))
        say(f"written: {dest / (name + '.json')}")
        print(json.dumps(out["numbers"], indent=1))
        return 0
    finally:
        core.shutdown()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
