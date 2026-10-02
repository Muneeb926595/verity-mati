#!/usr/bin/env python3
"""Compares a capture made after a change with one or two captures made before it (scripts/regress/capture.py).

  python scripts/regress/compare.py <after> <before-1> [<before-2>] [--text-only] [--map OLD=NEW ...]

Even unchanged code gives slightly different answers from run to run (the order of tied candidates, memory addresses
in scenario output, internal ids), so record TWO baselines. With two, an answer counts as unchanged when it equals
either baseline, or when the two baselines already differ for that question. Timings, run folders and the answer-
history table are ignored. --text-only compares only what the user reads (pass/fail and the answer paragraphs): use it
for model-worded runs. --map rewrites a string in the baselines first (for example an old checkout path).
Exit code 0: no pass/fail change and nothing unexplained."""
import argparse
import json
import re
import sys
from pathlib import Path

VOLATILE = re.compile(r"^(seconds|ms|elapsed.*|took.*|started|finished|created|opened|stamp|timestamp|duration.*|"
                      r"run_dir|stdout_tail)$")
VOLATILE_TABLES = {"answer", "card_fts_data", "card_fts_idx", "card_fts_config"}     # history; FTS internals


def norm(o, maps, top=False):
    if isinstance(o, dict):
        return {k: norm(v, maps) for k, v in o.items() if not VOLATILE.match(k) and not (top and k == "id")}
    if isinstance(o, list):
        return [norm(v, maps) for v in o]
    if isinstance(o, str):
        for old, new in maps:
            o = o.replace(old, new)
        return re.sub(r"\b\d{8}-\d{6}(-\d+)?\b", "<stamp>", o)
    if isinstance(o, float):
        return round(o, 6)
    return o


def first_diff(a, b, path=""):
    if type(a) is not type(b):
        return path, a, b
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                return f"{path}/{k}", a.get(k, "<absent>"), b.get(k, "<absent>")
            d = first_diff(a[k], b[k], f"{path}/{k}")
            if d:
                return d
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path}[len]", len(a), len(b)
        for i, (x, y) in enumerate(zip(a, b)):
            d = first_diff(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    return None if a == b else (path, a, b)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("after")
    ap.add_argument("before", nargs="+")
    ap.add_argument("--text-only", action="store_true")
    ap.add_argument("--map", action="append", default=[], help="OLD=NEW string rewrite applied to the baselines")
    a = ap.parse_args()
    maps = [tuple(m.split("=", 1)) for m in a.map]
    after, befores = Path(a.after), [Path(b) for b in a.before[:2]]

    def key(c, m):
        if a.text_only:
            return norm({"passed": c["passed"], "answer": c["result"].get("answer")}, m)
        return {"passed": c["passed"], "score": c["score"], "grade": norm(c["grade"], m),
                "result": norm(c["result"], m, top=True)}

    same = varies = unexplained = 0
    pass_changes, index_changes = [], []
    for fa in sorted(after.glob("*.json")):
        runs = [json.loads((d / fa.name).read_text()) for d in befores if (d / fa.name).exists()]
        if not runs:
            print(f"no baseline for {fa.name}")
            continue
        x = json.loads(fa.read_text())
        for proj, sx in x["stats"].items():
            cx = {k: v for k, v in sx["counts"].items() if k not in VOLATILE_TABLES}
            cbs = [{k: v for k, v in r["stats"].get(proj, {}).get("counts", {}).items() if k not in VOLATILE_TABLES}
                   for r in runs]
            if all(cx != cb for cb in cbs):
                known = {k for cb in cbs[1:] for k in set(cb) | set(cbs[0]) if cb.get(k) != cbs[0].get(k)}
                new = {k: ([cb.get(k) for cb in cbs], cx.get(k)) for k in set(cx) | set(cbs[0])
                       if all(cx.get(k) != cb.get(k) for cb in cbs) and k not in known}
                if new:
                    index_changes.append(f"{fa.stem}/{proj}: {new}")
        base_cases = [{c["id"]: c for c in r["cases"]} for r in runs]
        for c in x["cases"]:
            bs = [bc[c["id"]] for bc in base_cases if c["id"] in bc]
            if not bs:
                print(f"no baseline for {fa.stem}/{c['id']}")
                continue
            if any(b["passed"] != c["passed"] for b in bs):
                pass_changes.append(f"{fa.stem}/{c['id']}: {'/'.join(str(b['passed']) for b in bs)} -> {c['passed']}")
            kx = key(c, [])
            kbs = [key(b, maps) for b in bs]
            if any(kx == kb for kb in kbs):
                same += 1
            elif len(kbs) > 1 and kbs[0] != kbs[1]:
                varies += 1
            else:
                unexplained += 1
                p, u, v = first_diff(kbs[0], kx)
                print(f"DIFFERENT {fa.stem}/{c['id']} at {p}\n   before: {str(u)[:300]}\n   after : {str(v)[:300]}")
    print(f"\nunchanged: {same}; varies between the baselines too: {varies}; different: {unexplained}")
    print("pass/fail changes:", pass_changes or "none")
    print("index differences beyond the baselines:", index_changes or "none")
    return 1 if pass_changes or unexplained else 0


if __name__ == "__main__":
    sys.exit(main())
