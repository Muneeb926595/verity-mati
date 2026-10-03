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

Teaching Verity (per project, stored next to the index, always disclosed and undoable):
  verity correct     PATH ANSWER_ID --pick N | --line FILE:LINE [--note TEXT]
  verity confirm     PATH ANSWER_ID                  the answer was right (kept as a test case)
  verity corrections PATH [list|undo ID|restore ID|note ID TEXT|export|import FILE]
  verity glossary    PATH [list|accept TERM|reject TERM|add TERM IDENT…|remove TERM]
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time

from rich.console import Console
from rich.markup import escape

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
        _print_correct_hint(a.path, ans)
        return 0
    finally:
        core.shutdown()


def _q(p: str) -> str:
    return f'"{p}"' if any(c in p for c in " '\t") else p


def _print_correct_hint(path: str, ans: dict) -> None:
    """Under an answer: its id, and how to correct or confirm it."""
    aid = ans.get("id")
    if not aid:
        return
    lm = (ans.get("meta") or {}).get("learning") or {}
    lines = [f"Answer {aid}"]
    if lm.get("alternatives"):
        lines.append(f"  correct it:  verity correct {_q(path)} {aid} --pick {lm['alternatives'][0]['rank']}")
    elif lm.get("correctable"):
        lines.append(f"  correct it:  verity correct {_q(path)} {aid} --line FILE:LINE")
    lines.append(f"  right?       verity confirm {_q(path)} {aid}")
    for ln in lines:
        # one command per line and never wrapped by rich: a wrapped command breaks when it is copied
        con.print(ln, style="dim", markup=False, highlight=False, soft_wrap=True)


def _learner_core(a):
    from .engine.core import EngineCore
    return EngineCore(llm_mode="direct", embeddings=False)


def cmd_correct(a) -> int:
    core = _learner_core(a)
    try:
        target = None
        if a.line:
            f, _, ln = a.line.rpartition(":")
            if not f or not ln.isdigit():
                err.print("[red]--line must be FILE:LINE, for example src/ec_main.c:93[/]")
                return 2
            target = {"file": f, "line": int(ln)}
        r = core.answer_correct(a.path, a.answer_id, pick=a.pick, target=target, note=a.note or "")
        if a.json:
            print(json.dumps(r, indent=1, default=str))
            return 0
        c = r["correction"]
        con.print(f"[bold green]{r['message']}[/]")
        con.print(f"  Right place: {c['target_label']}", markup=False)
        for old in r.get("replaced") or []:
            con.print(f"  [dim]It replaces {old}.[/]")
        for t in r.get("glossary_suggestions") or []:
            con.print(f"  Glossary suggestion: “{escape(t['term'])}” → {escape(', '.join(t['identifiers']))}  "
                      f"[dim](accept: verity glossary {escape(_q(a.path))} accept \"{escape(t['term'])}\")[/]")
        con.print(f"  [dim]Undo: verity corrections {escape(_q(a.path))} undo {c['id']}[/]")
        return 0
    finally:
        core.shutdown()


def cmd_confirm(a) -> int:
    core = _learner_core(a)
    try:
        r = core.answer_confirm(a.path, a.answer_id, note=a.note or "")
        if a.json:
            print(json.dumps(r, indent=1, default=str))
            return 0
        con.print(f"[bold green]{r['message']}[/]")
        return 0
    finally:
        core.shutdown()


def cmd_corrections(a) -> int:
    core = _learner_core(a)
    try:
        lr = core.project(a.path).learner()
        act = a.action or "list"
        if act == "list":
            lst = lr.listing()
            if a.json:
                print(json.dumps(lst, indent=1, default=str))
                return 0
            from .learn.service import summarize
            con.print(f"[bold]{escape(core.project(a.path).root.name)}[/] — {summarize(lst)}")
            if lst.get("recovered_from"):
                con.print(f"  [yellow]The learning file was damaged and started again; the old copy is at "
                          f"{escape(lst['recovered_from'])}[/]")
            show = [c for c in lst["corrections"] if a.all or c["status"] != "removed"]
            for c in show:
                colour = {"active": "green", "stale": "yellow", "removed": "dim"}.get(c["status"], "")
                con.print(f"  [{colour}]{c['id']}  {c['status']:<7}[/] {c['created_label']:<12} {escape(c['question'])}",
                          highlight=False)
                con.print(f"      → {c['target_file']}:{c['target_line']}  {c['target_label']}", markup=False)
                if c.get("note"):
                    con.print(f"      “{c['note']}”", markup=False)
                if c["status"] != "active" and c.get("status_reason"):
                    con.print(f"      [dim]{escape(c['status_reason'])}[/]")
                if c.get("conflict") and c["status"] == "active":
                    con.print(f"      [yellow]disagrees with {', '.join(c['conflict'])}[/]")
            hidden = len(lst["corrections"]) - len(show)
            if hidden:
                con.print(f"  [dim]{hidden} undone (show them with --all)[/]")
            for k in lst["confirmations"]:
                con.print(f"  [dim]✓ confirmed[/] {escape(k['question'] or '')}  "
                          f"[dim](must mention {escape(', '.join(k['must_mention']))})[/]")
            for t in lst["glossary"]:
                if t["status"] in ("suggested", "accepted"):
                    con.print(f"  glossary {t['status']:<9} “{t['term']}” → {', '.join(t['identifiers'])}", highlight=False,
                              markup=False)
            return 0
        if act in ("undo", "restore", "note"):
            if not a.ref:
                err.print(f"[red]Say which correction: verity corrections PATH {act} c-0001[/]")
                return 2
            r = lr.remove(a.ref) if act == "undo" else lr.restore(a.ref) if act == "restore" else \
                lr.set_note(a.ref, " ".join(a.rest or []))
            con.print(r.get("message") or f"Saved the note for {a.ref}.", markup=False)
            return 0
        if act == "export":
            r = lr.export(a.format)
            text = r["data"] if isinstance(r["data"], str) else json.dumps(r["data"], indent=1, default=str)
            if a.output:
                from pathlib import Path
                Path(a.output).write_text(text, encoding="utf-8")
                con.print(f"Wrote {a.output}")
            else:
                print(text)
            return 0
        if act == "import":
            if not a.ref:
                err.print("[red]Say which file: verity corrections PATH import FILE[/]")
                return 2
            from pathlib import Path
            r = lr.import_(Path(a.ref).read_text(encoding="utf-8"))
            con.print(r["message"], markup=False)
            return 0
        err.print(f"[red]Unknown action {act} (list, undo, restore, note, export, import)[/]")
        return 2
    finally:
        core.shutdown()


def cmd_glossary(a) -> int:
    core = _learner_core(a)
    try:
        lr = core.project(a.path).learner()
        act = a.action or "list"
        if act == "list":
            terms = lr.glossary_list()
            if a.json:
                print(json.dumps(terms, indent=1, default=str))
                return 0
            if not terms:
                con.print("No glossary terms yet. Corrections suggest them; add one with: verity glossary PATH add TERM IDENT")
            for t in terms:
                src = "verity.toml" if t.get("read_only") else t.get("source") or ""
                con.print(f"  {t['status']:<9} “{t['term']}” → {', '.join(t['identifiers'])}  [dim]{src}[/]", highlight=False)
            return 0
        if not a.term:
            err.print(f"[red]Say which term: verity glossary PATH {act} TERM[/]")
            return 2
        if act == "accept":
            r = lr.glossary_accept(a.term, a.identifiers or None)
        elif act == "reject":
            r = lr.glossary_reject(a.term)
        elif act == "add":
            r = lr.glossary_add(a.term, a.identifiers or [])
        elif act == "remove":
            r = lr.glossary_remove(a.term)
        else:
            err.print(f"[red]Unknown action {act} (list, accept, reject, add, remove)[/]")
            return 2
        con.print(r.get("message") or "Done.", markup=False)
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
    p = sub.add_parser("correct", help="record the right place for an answer")
    p.add_argument("path")
    p.add_argument("answer_id")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--pick", type=int, help="the number of one of the answer's alternatives (2, 3)")
    g.add_argument("--line", help="FILE:LINE of the line that answers the question")
    p.add_argument("--note", help="why (shown with later answers)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_correct)
    p = sub.add_parser("confirm", help="the answer was right (kept as a test case)")
    p.add_argument("path")
    p.add_argument("answer_id")
    p.add_argument("--note")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_confirm)
    p = sub.add_parser("corrections", help="list, undo, restore, export or import corrections")
    p.add_argument("path")
    p.add_argument("action", nargs="?", choices=["list", "undo", "restore", "note", "export", "import"])
    p.add_argument("ref", nargs="?", help="correction id (undo / restore / note) or file (import)")
    p.add_argument("rest", nargs="*", help="the note text (note)")
    p.add_argument("--all", action="store_true", help="also list undone corrections")
    p.add_argument("--format", default="json", choices=["json", "bench", "toml"], help="export format")
    p.add_argument("-o", "--output", help="export to this file")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_corrections)
    p = sub.add_parser("glossary", help="this project's glossary words")
    p.add_argument("path")
    p.add_argument("action", nargs="?", choices=["list", "accept", "reject", "add", "remove"])
    p.add_argument("term", nargs="?")
    p.add_argument("identifiers", nargs="*")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_glossary)
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
        from .learn.store import LearnError
        if isinstance(e, (EngineError, LearnError, FileNotFoundError, ValueError)):
            err.print(f"[red]{e}[/]")
            return 2
        raise


if __name__ == "__main__":
    sys.exit(main())
