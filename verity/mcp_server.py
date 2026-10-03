"""MCP server (design §10): exposes Verity to AI assistants over the Model Context Protocol, stdio transport
(newline-delimited JSON-RPC 2.0). Every tool calls the same EngineCore as the CLI and the desktop app — there is no
second implementation of the analysis. Implemented directly on the protocol (no SDK) so it stays dependency-free and
offline; logs go to stderr only (stdout carries protocol messages)."""
from __future__ import annotations

import json
import sys
import threading
import traceback
from typing import Any

from . import __version__

PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]

TOOLS = [
    {"name": "verity_index", "title": "Index a C/C# project",
     "description": "Build or update Verity's local index of a C/C# codebase (incremental). Run before asking questions.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string", "description": "Absolute folder path"},
                                                       "full": {"type": "boolean", "default": False}},
                     "required": ["path"], "additionalProperties": False}},
    {"name": "verity_status", "title": "Index status",
     "description": "Whether the project is indexed and up to date, with counts and fidelity.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
                     "additionalProperties": False}},
    {"name": "verity_ask", "title": "Ask about the code",
     "description": "Answer a natural-language question about the codebase (data flow, callers, purpose, what happens "
                    "when/if, change impact). Every statement is verified against the source and cited as file:line; "
                    "scenario questions are answered by running the code in a sandbox. Returns the answer, the data "
                    "flow, the evidence and each statement's certainty.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "question": {"type": "string"},
                                                       "details": {"type": "boolean", "default": False,
                                                                   "description": "include reasoning/verification"}},
                     "required": ["path", "question"], "additionalProperties": False}},
    {"name": "verity_search", "title": "Find symbols",
     "description": "Find functions, variables, fields, types and macros by name or description.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "query": {"type": "string"},
                                                       "k": {"type": "integer", "default": 15}},
                     "required": ["path", "query"], "additionalProperties": False}},
    {"name": "verity_symbol", "title": "Symbol facts",
     "description": "Definition, signature, doc comment, callers, callees, writers and readers of one symbol.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "name": {"type": "string"}},
                     "required": ["path", "name"], "additionalProperties": False}},
    {"name": "verity_correct", "title": "Correct or confirm an answer",
     "description": "When the user says an answer from verity_ask described the wrong code, record the right place: "
                    "pick one of the answer's alternatives (pick: 2 or 3) or give a file and line of the project. "
                    "Verity then uses it for that question and similar ones on this project (always disclosed in the "
                    "answer, undoable). confirm=true keeps a right answer as a test case; undo=<correction id> stops "
                    "using a correction. Only record what the user said; never guess a correction.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string"}, "answer_id": {"type": "string", "description": "the id verity_ask returned"},
         "pick": {"type": "integer", "description": "an alternative's number (2, 3)"},
         "file": {"type": "string"}, "line": {"type": "integer"},
         "note": {"type": "string", "description": "why this place is right, in the user's words"},
         "confirm": {"type": "boolean", "description": "the answer was right"},
         "undo": {"type": "string", "description": "a correction id, e.g. c-0007"}},
         "required": ["path"], "additionalProperties": False}},
    {"name": "verity_source", "title": "Read source",
     "description": "Source lines of a project file around a line (read-only, only files inside the project).",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "file": {"type": "string"},
                                                       "line": {"type": "integer"}},
                     "required": ["path", "file"], "additionalProperties": False}},
]


def log(*a: Any) -> None:
    print("[verity-mcp]", *a, file=sys.stderr, flush=True)


class MCPServer:
    def __init__(self) -> None:
        from .engine.core import EngineCore
        self.core = EngineCore()
        self.out_lock = threading.Lock()
        self.inflight: list[threading.Thread] = []

    def send(self, msg: dict) -> None:
        with self.out_lock:
            sys.stdout.write(json.dumps(msg, default=str) + "\n")
            sys.stdout.flush()

    def result(self, mid, res) -> None:
        self.send({"jsonrpc": "2.0", "id": mid, "result": res})

    def error(self, mid, code: int, message: str) -> None:
        self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}})

    # ------------------------------------------------------------------------------------------ tools
    def call_tool(self, name: str, args: dict) -> dict:
        core = self.core
        if name == "verity_index":
            j = core.start_index(args["path"], bool(args.get("full")))
            r = core.wait(j["id"], 3600)
            if r["state"] != "done":
                return _text(f"Indexing {r['state']}: {r.get('error') or ''}", True)
            s = r["result"]
            lines = [f"Indexed in {s.get('seconds', 0):.1f} s."]
            for ph, info in s.get("phases", {}).items():
                lines.append(f"- {ph}: {info.get('detail', '')}")
            lines += [f"! {w}" for w in s.get("warnings", [])]
            return _text("\n".join(lines), structured=s)
        if name == "verity_status":
            st = core.project_status(args["path"])
            return _text(f"{st['name']}: {st['label']} — counts {st.get('counts')}", structured=st)
        if name == "verity_ask":
            st = core.project_status(args["path"])
            if not st["ready"] or st["stale"]:
                j = core.start_index(args["path"])
                r = core.wait(j["id"], 3600)
                if r["state"] != "done":
                    return _text(f"The project could not be indexed: {r.get('error') or r['state']}", True)
            r = core.ask(args["path"], args["question"])
            ans = r["result"]
            from .render.render import to_text
            txt = to_text(ans, details=bool(args.get("details")))
            lm = (ans.get("meta") or {}).get("learning") or {}
            if ans.get("id"):
                txt += f"\n\nAnswer id {ans['id']}" + (" (if the user says another place is right, record it with "
                                                       "verity_correct)" if lm.get("correctable") else "")
            st = {k: ans[k] for k in ("question", "answer", "flow", "evidence", "status", "status_counts")}
            st.update(id=ans.get("id"), alternatives=lm.get("alternatives") or [], corrections_used=lm.get("used") or [])
            return _text(txt, structured=st)
        if name == "verity_correct":
            path = args["path"]
            if args.get("undo"):
                r = core.project(path).learner().remove(args["undo"])
                return _text(r["message"], structured=r)
            if not args.get("answer_id"):
                return _text("answer_id is required (the id verity_ask returned).", True)
            if args.get("confirm"):
                r = core.answer_confirm(path, args["answer_id"], note=args.get("note") or "")
                return _text(r["message"], structured=r)
            target = {"file": args["file"], "line": args.get("line")} if args.get("file") else None
            r = core.answer_correct(path, args["answer_id"], pick=args.get("pick"), target=target,
                                    note=args.get("note") or "", author="mcp")
            c = r["correction"]
            txt = f"{r['message']}\nRight place: {c['target_label']}\nUndo: verity_correct undo={c['id']}"
            for t in r.get("glossary_suggestions") or []:
                txt += f"\nGlossary suggestion (the user can accept it in Verity): {t['term']} -> {', '.join(t['identifiers'])}"
            return _text(txt, structured=r)
        if name == "verity_search":
            hits = core.search(args["path"], args["query"], int(args.get("k", 15)))
            txt = "\n".join(f"{h['name']} ({h['kind']}) {h['file']}:{h['line']}" for h in hits) or "No matches."
            return _text(txt, structured={"hits": hits})
        if name == "verity_symbol":
            s = core.symbol(args["path"], name=args["name"])
            return _text(s.get("card") or f"{s['name']} {s['file']}:{s['line']}", structured=s)
        if name == "verity_source":
            r = core.source(args["path"], args["file"], around=args.get("line"))
            txt = "\n".join(f"{i:5d}  {t}" for i, t in enumerate(r["lines"], r["start"]))
            return _text(txt, structured={k: r[k] for k in ("file", "start", "end", "total", "stale")})
        raise KeyError(name)

    # ------------------------------------------------------------------------------------------ loop
    def handle(self, msg: dict) -> None:
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if method is None:
            return                                   # a response to something we never send
        if mid is None:                              # notification
            return
        try:
            if method == "initialize":
                want = params.get("protocolVersion")
                ver = want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                self.result(mid, {"protocolVersion": ver,
                                  "capabilities": {"tools": {"listChanged": False}},
                                  "serverInfo": {"name": "verity", "title": "Verity code intelligence",
                                                 "version": __version__},
                                  "instructions": "Local, offline code intelligence for C/C# projects. Call "
                                                  "verity_index once per project, then verity_ask with questions."})
            elif method == "ping":
                self.result(mid, {})
            elif method == "tools/list":
                self.result(mid, {"tools": TOOLS})
            elif method == "tools/call":
                name = params.get("name")
                if name not in {t["name"] for t in TOOLS}:
                    self.error(mid, -32602, f"Unknown tool: {name}")
                    return
                try:
                    self.result(mid, self.call_tool(name, params.get("arguments") or {}))
                except Exception as e:  # tool errors are results with isError, per the spec
                    log(traceback.format_exc())
                    self.result(mid, _text(f"{type(e).__name__}: {e}", True))
            elif method in ("resources/list", "prompts/list"):
                self.result(mid, {"resources": []} if method == "resources/list" else {"prompts": []})
            else:
                self.error(mid, -32601, f"Method not found: {method}")
        except Exception as e:  # noqa: BLE001
            log(traceback.format_exc())
            self.error(mid, -32603, f"Internal error: {e}")

    def serve(self) -> int:
        log(f"Verity MCP server {__version__} ready (stdio)")
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            for m in (msg if isinstance(msg, list) else [msg]):
                t = threading.Thread(target=self.handle, args=(m,), daemon=True)
                t.start()
                self.inflight.append(t)
            self.inflight = [t for t in self.inflight if t.is_alive()]
        # client closed stdin: finish what was asked before exiting
        for t in self.inflight:
            t.join(timeout=600)
        self.core.shutdown()
        return 0


def _text(t: str, is_error: bool = False, structured: dict | None = None) -> dict:
    r: dict[str, Any] = {"content": [{"type": "text", "text": t}], "isError": is_error}
    if structured is not None:
        r["structuredContent"] = json.loads(json.dumps(structured, default=str))
    return r


def main() -> int:
    return MCPServer().serve()


if __name__ == "__main__":
    sys.exit(main())
