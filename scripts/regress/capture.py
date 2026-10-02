#!/usr/bin/env python3
"""Records everything the engine produces for the benchmark suites, for before/after comparisons: index statistics per
project and the complete answer object per question. Each run indexes from scratch in its own data folder, so runs are
independent of each other and of data/.

  python scripts/regress/capture.py --out <dir> [--mode direct|direct-embed|llm-embed] [--suites bench/suite.json ...]

Then compare with scripts/regress/compare.py. Direct mode over all suites takes about 10 minutes on an M3 (lwIP alone
is about 4); llm-embed needs the local model and takes about 25 minutes for the six suites it is usually run on."""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALL_SUITES = ["suite", "heldout", "dialects", "realworld", "realworld_heldout"] + \
             [f"realworld_heldout{n}" for n in range(2, 10)] + ["realworld_lwip"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", choices=["direct", "direct-embed", "llm-embed"], default="direct")
    ap.add_argument("--suites", nargs="+", default=[f"bench/{s}.json" for s in ALL_SUITES])
    ap.add_argument("--home", default="", help="data folder for this run (default: a fresh temporary one)")
    ap.add_argument("--keep-home", action="store_true", help="keep the temporary data folder afterwards")
    a = ap.parse_args()
    home = a.home or tempfile.mkdtemp(prefix="verity-capture-")
    os.environ["VERITY_HOME"] = home                     # before the engine reads its configuration
    sys.path.insert(0, str(ROOT))
    from verity import config                            # noqa: E402
    from verity.bench import run                         # noqa: E402
    from verity.engine.core import EngineCore            # noqa: E402
    llm = a.mode == "llm-embed"
    core = EngineCore(llm_mode="auto" if llm else "direct", embeddings=a.mode != "direct")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    t_all = time.time()
    try:
        for suite_path in a.suites:
            suite = json.loads((ROOT / suite_path).read_text())
            name = Path(suite_path).stem
            roots = {k: str((config.ROOT / v).resolve()) for k, v in suite["projects"].items()}
            stats = {}
            for k, r in roots.items():
                t0 = time.time()
                res = core.wait(core.start_index(r, full=True)["id"], 3600)
                st = core.project(r).store
                counts = {}
                for row in st.q("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                    t = row["name"]
                    counts[t] = st.q(f'SELECT count(*) AS n FROM "{t}"')[0]["n"]
                stats[k] = {"state": res.get("state"), "counts": counts, "seconds": round(time.time() - t0, 1)}
                print(f"[capture] {name}/{k} indexed: {res.get('state')} in {time.time() - t0:.0f} s", flush=True)
            vocabs = {k: run.vocabulary(core.project(r).store) for k, r in roots.items()}
            cases = []
            for case in suite["cases"]:
                t0 = time.time()
                try:
                    ans = core.ask(roots[case["project"]], case["q"], use_llm=llm, timeout=900)["result"]
                except Exception as e:  # noqa: BLE001
                    ans = {"answer": [f"ERROR {e}"], "evidence": [], "flow": [], "meta": {}, "route": {},
                           "sections": {"reasoning": [], "verification": {"report": {}}}}
                g = run.grade(case, ans, vocabs[case["project"]])
                cases.append({"suite": name, "id": case["id"], "passed": g["passed"], "score": g["score"],
                              "grade": g, "result": ans, "seconds": round(time.time() - t0, 2)})
                print(f"[capture] {name} {'PASS' if g['passed'] else 'FAIL'} {case['id']} {time.time() - t0:.1f} s",
                      flush=True)
            (out / f"{name}.json").write_text(json.dumps({"stats": stats, "cases": cases}, indent=1, default=str,
                                                         sort_keys=True))
            print(f"[capture] {name}: {sum(c['passed'] for c in cases)}/{len(cases)}", flush=True)
    finally:
        core.shutdown()
        if not a.home and not a.keep_home:
            shutil.rmtree(home, ignore_errors=True)
    print(f"[capture] done in {time.time() - t_all:.0f} s -> {out}", flush=True)
    return 0


if __name__ == "__main__":          # required: the engine's worker processes re-import this module
    sys.exit(main())
