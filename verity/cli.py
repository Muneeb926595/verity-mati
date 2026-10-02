"""Verity command line (design §10). Thin client over EngineCore — the same engine the desktop app and MCP use.

  verity index  PATH [--full]          build or update the index (incremental)
  verity ask    PATH "QUESTION"        answer a question (add --details for the expandable sections, --json for raw)
  verity search PATH "TEXT"            find symbols
  verity symbol PATH NAME              facts about one symbol
  verity source PATH FILE[:LINE]       print source around a line
  verity status PATH                   index state
  verity probe                         what this machine can do (models, compilers, sandbox)
  verity serve  [--port N] [--open]    run the local engine for the desktop/browser UI
  verity ui                            run the engine and open the browser UI
  verity mcp                           MCP server on stdio (for AI assistants)
  verity bench  [--out DIR]            run the known-answer benchmark
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time

from rich.console import Console

from . import __version__

con = Console(highlight=False)
err = Console(stderr=True, highlight=False)


def _core(args):
    from .engine.core import EngineCore
    mode = "direct" if getattr(args, "direct", False) else ("auto" if getattr(args, "llm", False) else None)
    return EngineCore(llm_mode=mode, embeddings=not getattr(args, "no_embed", False))


def _follow(core, job_id: str, kind: str) -> dict:
    """Print friendly progress until the job finishes. Ctrl-C cancels the job."""
    q = core.subscribe()
    last = ""
    try:
        with con.status("Starting…", spinner="dots") as status:
            while True:
                try:
                    ev = q.get(timeout=0.2)
                except Exception:
                    ev = None
                j = core.jobs.get(job_id)
                if ev and ev.get("job") == job_id and ev.get("type") in ("index.progress", "ask.progress"):
                    if ev["type"] == "index.progress":
                        done_parts = ev["phase_index"] - (0 if ev["state"] in ("done", "skipped") else 1)
                        head = f"Understanding your project… {done_parts} of {ev['phase_total']} parts complete"
                        line = f"{ev['label']}"
                        if ev.get("done") is not None and ev.get("total"):
                            line += f" ({ev['done']}/{ev['total']})"
                        status.update(f"{head} — {line}")
                        if ev["state"] in ("done", "skipped", "failed") and ev.get("detail") and ev["detail"] != last:
                            mark = {"done": "[green]✓[/]", "skipped": "[dim]–[/]", "failed": "[red]✗[/]"}[ev["state"]]
                            con.print(f"  {mark} {ev['label']}: [dim]{ev['detail']}[/]")
                            last = ev["detail"]
                    else:
                        status.update(f"{ev['label']}…" + (f" [dim]{ev['detail']}[/]" if ev.get("detail") else ""))
                if j and j.state in ("done", "failed", "cancelled"):
                    return j.public(with_result=True)
    except KeyboardInterrupt:
        core.cancel(job_id)
        con.print("[yellow]Cancelling…[/]")
        r = core.wait(job_id, 30)
        return r
    finally:
        core.unsubscribe(q)


def cmd_index(a) -> int:
    core = _core(a)
    try:
        j = core.start_index(a.path, full=a.full)
        r = _follow(core, j["id"], "index")
        if r["state"] == "done":
            s = r["result"]
            con.print(f"[bold green]Ready.[/] Indexed in {s.get('seconds', 0):.1f} s.")
            for w in s.get("warnings", []):
                con.print(f"  [yellow]![/] {w}")
            return 0
        if r["state"] == "cancelled":
            con.print("[yellow]Indexing cancelled.[/] Run the same command again to finish (work done so far is kept).")
            return 130
        err.print(f"[red]Indexing failed:[/] {r.get('error')}")
        return 1
    finally:
        core.shutdown()


def cmd_ask(a) -> int:
    core = _core(a)
    try:
        st = core.project_status(a.path)
        if not st["ready"] or a.reindex or st["stale"]:
            if st["stale"] and not a.reindex:
                con.print(f"[dim]{len(st['stale'])} file(s) changed since the last index; updating first…[/]")
            j = core.start_index(a.path)
            r = _follow(core, j["id"], "index")
            if r["state"] != "done":
                err.print(f"[red]Could not index the project:[/] {r.get('error') or r['state']}")
                return 1
        use_llm = False if a.direct else (True if a.llm else None)
        j = core.start_ask(a.path, a.question, use_llm)
        r = _follow(core, j["id"], "ask")
        if r["state"] != "done":
            err.print(f"[red]{r.get('error') or 'cancelled'}[/]")
            return 1
        ans = r["result"]
        if a.json:
            print(json.dumps(ans, indent=1, default=str))
            return 0
        from .render.render import to_text
        print(to_text(ans, details=a.details))
        pm = ans.get("meta", {}).get("prose", {})
        how = {"model": f"written by {pm.get('model')}",
               "model+facts": f"summary by {pm.get('model')}, followed by the complete checked facts"}.get(
            pm.get("mode"), "Direct mode (built from facts only)")
        picked = " · the local model chose which of the found places to describe (see Assumptions, -d)" \
            if ans["meta"].get("model_choices") else ""
        con.print(f"[dim]{ans['meta']['seconds']:.1f} s · {how}" + (f" · {pm['fallback']}" if pm.get("fallback") else "") + (f" · {pm['skipped']}" if pm.get("skipped") else "")
                  + picked + "[/]")
        return 0
    finally:
        core.shutdown()


def cmd_search(a) -> int:
    core = _core(a)
    try:
        hits = core.search(a.path, a.text, a.k)
        if a.json:
            print(json.dumps(hits, indent=1))
            return 0
        if not hits:
            con.print("No matches.")
        for h in hits:
            con.print(f"{h['name']:<32} [dim]{h['kind']:<10}[/] {h['file']}:{h['line']}  [dim]{', '.join(h['why'])}[/]")
        return 0
    finally:
        core.shutdown()


def cmd_symbol(a) -> int:
    core = _core(a)
    try:
        s = core.symbol(a.path, name=a.name)
        if a.json:
            print(json.dumps(s, indent=1, default=str))
            return 0
        con.print(f"[bold]{s['name']}[/] ({s['kind']}) {s['file']}:{s['line']}")
        if s.get("card"):
            con.print(s["card"], markup=False)
        return 0
    finally:
        core.shutdown()


def cmd_source(a) -> int:
    core = _core(a)
    try:
        f, _, ln = a.file.partition(":")
        r = core.source(a.path, f, around=int(ln) if ln else None)
        for i, t in enumerate(r["lines"], r["start"]):
            mark = ">" if ln and i == int(ln) else " "
            con.print(f"{mark}{i:5d}  {t}", markup=False)
        if r["stale"]:
            con.print("[yellow]This file changed since it was indexed.[/]")
        return 0
    finally:
        core.shutdown()


def cmd_status(a) -> int:
    core = _core(a)
    try:
        s = core.project_status(a.path)
        if a.json:
            print(json.dumps(s, indent=1, default=str))
            return 0
        con.print(f"[bold]{s['name']}[/] — {s['label']}")
        c = s.get("counts") or {}
        if c:
            con.print(f"  {c.get('file', 0)} files · {c.get('symbol', 0)} symbols · {c.get('call', 0)} calls · "
                      f"{c.get('flow', 0)} value flows")
        for k, v in (s.get("fidelity") or {}).items():
            con.print(f"  fidelity {k}: {v['files']} files, {v['loc']} lines")
        if s.get("stale"):
            con.print(f"  [yellow]{len(s['stale'])} file(s) changed since the last index[/]")
        return 0
    finally:
        core.shutdown()


def cmd_probe(a) -> int:
    from .probe import describe, probe
    p = probe(refresh=True)
    if a.json:
        print(json.dumps(p, indent=1, default=str))
        return 0
    for ln in describe(p):
        con.print(ln)
    return 0


def cmd_serve(a) -> int:
    from .engine.server import main as serve
    argv = ["--port", str(a.port)]
    if a.open:
        argv.append("--open")
    return serve(argv)


def cmd_ui(a) -> int:
    from .engine.server import main as serve
    return serve(["--port", str(a.port), "--open"])


def cmd_mcp(a) -> int:
    from .mcp_server import main as mcp_main
    return mcp_main()


def cmd_bench(a) -> int:
    from .bench.run import main as bench_main
    return bench_main(["--out", a.out] + (["--llm"] if a.llm else []))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="verity", description="Verity — understand your C/C# codebase, fully offline.")
    ap.add_argument("--version", action="version", version=f"verity {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, llm=False):
        p.add_argument("--json", action="store_true", help="machine-readable output")
        p.add_argument("--no-embed", action="store_true", help="do not use the local embedding model")
        if llm:
            g = p.add_mutually_exclusive_group()
            g.add_argument("--direct", action="store_true", help="never use the AI model (facts-only wording)")
            g.add_argument("--llm", action="store_true", help="use the local AI model to phrase the answer")
        return p

    p = common(sub.add_parser("index", help="index a project"))
    p.add_argument("path")
    p.add_argument("--full", action="store_true", help="re-read everything")
    p.set_defaults(fn=cmd_index)
    p = common(sub.add_parser("ask", help="ask a question"), llm=True)
    p.add_argument("path")
    p.add_argument("question")
    p.add_argument("--details", "-d", action="store_true", help="show reasoning, path, assumptions and verification")
    p.add_argument("--reindex", action="store_true")
    p.set_defaults(fn=cmd_ask)
    p = common(sub.add_parser("search", help="find symbols"))
    p.add_argument("path")
    p.add_argument("text")
    p.add_argument("-k", type=int, default=15)
    p.set_defaults(fn=cmd_search)
    p = common(sub.add_parser("symbol", help="facts about a symbol"))
    p.add_argument("path")
    p.add_argument("name")
    p.set_defaults(fn=cmd_symbol)
    p = common(sub.add_parser("source", help="print source"))
    p.add_argument("path")
    p.add_argument("file", help="FILE or FILE:LINE (relative to the project)")
    p.set_defaults(fn=cmd_source)
    p = common(sub.add_parser("status", help="index status"))
    p.add_argument("path")
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser("probe", help="machine capabilities")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_probe)
    p = sub.add_parser("serve", help="run the local engine")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--open", action="store_true")
    p.set_defaults(fn=cmd_serve)
    p = sub.add_parser("ui", help="open the browser UI")
    p.add_argument("--port", type=int, default=0)
    p.set_defaults(fn=cmd_ui)
    p = sub.add_parser("mcp", help="MCP server (stdio)")
    p.set_defaults(fn=cmd_mcp)
    p = sub.add_parser("bench", help="known-answer benchmark")
    p.add_argument("--out", default=str(__import__("verity.config", fromlist=["ROOT"]).ROOT / "bench" / "results"))
    p.add_argument("--llm", action="store_true", help="also measure answers phrased by the AI model")
    p.set_defaults(fn=cmd_bench)
    a = ap.parse_args(argv)
    try:
        return a.fn(a) or 0
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001
        from .engine.core import EngineError
        if isinstance(e, (EngineError, FileNotFoundError, ValueError)):
            err.print(f"[red]{e}[/]")
            return 2
        raise


if __name__ == "__main__":
    sys.exit(main())
