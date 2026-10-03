"""In-memory graphs built from the fact store (design §14): call graph (with resolved indirect calls), value-flow graph
(context-sensitive slicing by call-site matching), reference index, entry-point reachability, change impact and a
small evaluator for path conditions. The fact store stays the source of truth; graphs are rebuilt when it changes."""
from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..extract.recon import load_api_pack


@dataclass
class Edge:
    src: str
    dst: str
    kind: str
    file: str | None
    line: int | None
    fn: str | None = None
    expr: str = ""
    site: int | None = None
    cfg: str = "default"


@dataclass
class CallSite:
    caller: str
    callee: str
    file: str
    line: int
    kind: str
    res: str
    via: str | None
    args: list[str]
    guards: list[dict]
    cfg: str = "default"


class SourceCache:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._lines: dict[str, tuple[tuple, list[str]]] = {}

    def lines(self, rel: str) -> list[str]:
        """File lines, re-read whenever the file changes on disk (the user may edit while the engine runs)."""
        p = self.root / rel
        try:
            stt = p.stat()
            sig = (stt.st_mtime_ns, stt.st_size)
        except OSError:
            return []
        hit = self._lines.get(rel)
        if hit and hit[0] == sig:
            return hit[1]
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            txt = ""
        ls = txt.split("\n")
        self._lines[rel] = (sig, ls)
        return ls

    def line(self, rel: str | None, n: int | None) -> str:
        if not rel or not n:
            return ""
        ls = self.lines(rel)
        return ls[n - 1].rstrip() if 0 < n <= len(ls) else ""

    def snippet(self, rel: str, start: int, end: int, pad: int = 0) -> list[tuple[int, str]]:
        ls = self.lines(rel)
        a = max(1, start - pad)
        b = min(len(ls), end + pad)
        return [(i, ls[i - 1].rstrip()) for i in range(a, b + 1)]


class CodeGraph:
    def __init__(self, store, root: str | Path, config: str = "default"):
        self.st = store
        self.root = Path(root)
        self.src = SourceCache(root)
        self.config = config           # "default" or "+MACRO" (that macro defined)
        self.api = load_api_pack()
        self._load()

    # ------------------------------------------------------------------------------------------ loading
    def _cfg_ok(self, cfg: str | None) -> bool:
        cfg = cfg or "default"
        if self.config == "default":
            return not cfg.startswith("+")
        macro = self.config[1:]
        parts = cfg.split(";")
        if cfg.startswith("+"):
            return cfg == self.config
        return f"-{macro}" not in parts

    def _load(self) -> None:
        st = self.st
        self.sym: dict[str, dict] = {r["key"]: dict(r) for r in st.q("SELECT * FROM symbol")}
        self.by_name: dict[str, list[str]] = defaultdict(list)
        for k, s in self.sym.items():
            self.by_name[s["name"]].append(k)
        self.defined = {k for k, s in self.sym.items()
                        if s["kind"] in ("function", "method", "constructor") and s["is_def"] and s["file"]
                        and s["file"] != "<stub>" and self._cfg_ok(s.get("cfg"))}
        # a function defined twice (the program's own + a test / example / analysis-model copy): the copy's body facts
        # are dropped so they do not mix into the real function
        from ..pathkinds import is_secondary_path
        primary = {k: s["file"] for k, s in self.sym.items() if s["kind"] in ("function", "method", "constructor") and
                   s["is_def"] and s["file"] and not is_secondary_path(s["file"])}
        sec_cache: dict[str, bool] = {}

        def shadow(fn: str | None, file: str | None) -> bool:
            pf = primary.get(fn or "")
            if not pf or not file or file == pf:
                return False
            if file not in sec_cache:
                sec_cache[file] = is_secondary_path(file)
            return sec_cache[file]
        # calls: indirect rows are replaced by their resolved targets when those exist
        rows = [dict(r) for r in st.q("SELECT * FROM call") if not shadow(r["caller"], r["file"])]
        resolved_sites = {(r["file"], r["line"], r["col"], r["via"]) for r in rows if r["derived"]}
        resolved_name_sites = {(r["file"], r["line"], r["col"]) for r in rows if r["derived"] and r["res"] == "name"}
        self.calls: list[CallSite] = []
        for r in rows:
            if not self._cfg_ok(r["cfg"]):
                continue
            if r["callee"] is None:
                if (r["file"], r["line"], r["col"], r["via"]) in resolved_sites:
                    continue
                callee = f"?{r['via'] or 'unknown'}"          # unresolved indirect call: kept, marked
            elif r["callee"].startswith("name:"):
                if (r["file"], r["line"], r["col"]) in resolved_name_sites:
                    continue
                callee = r["callee"]
            else:
                callee = r["callee"]
            self.calls.append(CallSite(r["caller"], callee, r["file"], r["line"], r["kind"], r["res"], r["via"],
                                       json.loads(r["args"] or "[]"), json.loads(r["guards"] or "[]"), r["cfg"]))
        self.out_calls: dict[str, list[CallSite]] = defaultdict(list)
        self.in_calls: dict[str, list[CallSite]] = defaultdict(list)
        for c in self.calls:
            self.out_calls[c.caller].append(c)
            self.in_calls[c.callee].append(c)
        for v in self.out_calls.values():
            v.sort(key=lambda c: (c.file, c.line))
        # value flow
        self.fwd: dict[str, list[Edge]] = defaultdict(list)
        self.bwd: dict[str, list[Edge]] = defaultdict(list)
        for r in st.q("SELECT * FROM flow"):
            if not self._cfg_ok(r["cfg"]) or shadow(r["fn"], r["file"]):
                continue
            e = Edge(r["src"], r["dst"], r["kind"], r["file"], r["line"], r["fn"], r["expr"] or "", r["site"], r["cfg"])
            self.fwd[e.src].append(e)
            self.bwd[e.dst].append(e)
        # references
        self.occ: dict[str, list[dict]] = defaultdict(list)
        for r in st.q("SELECT * FROM occ"):
            if not self._cfg_ok(r["cfg"]) or shadow(r["fn"], r["file"]):
                continue
            d = dict(r)
            d["guards"] = json.loads(d["guards"] or "[]")
            self.occ[d["sym"]].append(d)
        self.branches: dict[str, list[dict]] = defaultdict(list)
        for r in st.q("SELECT * FROM branch"):
            if self._cfg_ok(r["cfg"]) and not shadow(r["fn"], r["file"]):
                d = dict(r)
                d["reads"] = json.loads(d["reads"] or "[]")
                if d["kind"] == "early_exit":
                    # C#: an `if` whose body always leaves (`if (x == null) throw …;`) is an if like any other
                    d["kind"], d["early_exit"] = "if", True
                self.branches[d["fn"]].append(d)
        # entry points of code this build compiles (not ones that exist only with optional features switched on)
        self.entries = {r["sym"]: dict(r) for r in st.q("SELECT * FROM entry")
                        if r["sym"] not in self.sym or self._cfg_ok(self.sym[r["sym"]].get("cfg"))}
        self.io = [dict(r) for r in st.q("SELECT * FROM io")]
        self.io_by_sym: dict[str, list[dict]] = defaultdict(list)
        for r in self.io:
            self.io_by_sym[r["sym"]].append(r)
        self.consts = {r["sym"]: r["value"] for r in st.q("SELECT sym, value FROM constv")}
        self.stubs = {r["name"] for r in st.q("SELECT name FROM stub_symbol")}
        self.fnaddr = [dict(r) for r in st.q("SELECT * FROM fnaddr")]

    def record_fields(self, *type_names: str) -> list[str]:
        """Field keys of the struct named by any of these type spellings (typedef name, tag, canonical)."""
        for tn in type_names:
            t = re.sub(r"\b(const|volatile|struct|union|restrict)\b|\*|&", "", tn or "").strip()
            if not t:
                continue
            for k in self.by_name.get(t, []):
                if self.sym[k]["kind"] in ("struct", "union"):
                    return sorted([f for f in self.sym if self.sym[f].get("container") == k and self.sym[f]["kind"] == "field"],
                                  key=lambda f: self.sym[f]["line"] or 0)
        return []

    # ------------------------------------------------------------------------------------------ naming
    def name(self, key: str) -> str:
        if not key:
            return "(unknown)"                 # never crash an answer over a missing key
        if key in self.sym:
            s = self.sym[key]
            cs = key[:2] in ("M:", "F:", "P:", "E:", "T:")
            if s["kind"] in ("field", "param", "local", "property", "event") or (cs and s["kind"] in ("method", "constructor")):
                q = s["qual"] or s["name"]
                if cs:           # C#: Class.member (namespaces dropped)
                    parts = q.split(".")
                    if s["kind"] in ("param", "local") and len(parts) >= 3:
                        return ".".join(parts[-2:])
                    return ".".join(parts[-2:]) if len(parts) >= 2 else q
                return q
            return s["name"]
        if "#lambda@" in key:
            return f"lambda in {self.name(key.split('#', 1)[0])}"
        m = re.match(r"^(.*)#param:(\w+)$", key)
        if m:
            fn = self.name(m.group(1))
            p = self.sym.get(key)
            if m.group(2).isdigit() and not p:
                # a parameter of a library function with no source here: "argument 1 of fgets()" reads as English and
                # is not mistaken for a code name
                return f"argument {int(m.group(2)) + 1} of {fn}()"
            return p["qual"] if p else fn
        if key.endswith("#ret"):
            return f"{self.name(key[:-4])}() result"
        if key.startswith("?"):
            return f"(unresolved call through {self.name(key[1:])})"
        if key.startswith("name:"):
            return key[5:]
        if re.match(r"^[MFPETN]:", key):
            body = key[2:].split("(")[0]
            parts = body.split(".")
            return ".".join(parts[-2:]) if len(parts) >= 2 else body
        tail = re.split(r"@", key)[-1]
        return tail.split("#")[0] or key

    def display(self, key: str) -> str:
        s = self.sym.get(key)
        nm = self.name(key)
        if ".#ctor" in nm or ".ctor" in nm.split(".")[-1:]:
            # C# constructors: `Thread.#ctor` is written `new Thread()`
            return f"new {nm.split('.#ctor')[0].split('..ctor')[0].split('.')[-1]}()"
        if s and s["kind"] in ("function", "method", "constructor"):
            return f"{self.name(key)}()" if key[:2] == "M:" else f"{s['name']}()"
        if "#lambda@" in key:
            return self.name(key)
        if key.endswith("#ret"):
            return f"{self.name(key[:-4])}() result"
        return self.name(key)

    def is_function(self, key: str) -> bool:
        s = self.sym.get(key)
        return bool(s and s["kind"] in ("function", "method", "constructor"))

    def fn_of(self, key: str) -> str | None:
        """Enclosing function of a param/local/ret node."""
        if "#" in key:
            base = key.split("#", 1)[0]
            return base if base in self.sym and self.is_function(base) else None
        return None

    def loc(self, key: str) -> tuple[str | None, int | None]:
        s = self.sym.get(key)
        if s:
            return s["file"], s["line"]
        f = self.fn_of(key)
        if f:
            return self.loc(f)
        return None, None

    # ------------------------------------------------------------------------------------------ call graph
    def callers(self, fn: str) -> list[CallSite]:
        """Call sites of fn; the program's own code first, tests / examples / tool models after it."""
        return sorted(self.in_calls.get(fn, []), key=lambda c: self.is_test(c.caller))

    def callees(self, fn: str) -> list[CallSite]:
        return list(self.out_calls.get(fn, []))

    def transitive_callers(self, fn: str, max_depth: int = 12) -> dict[str, int]:
        dist = {fn: 0}
        q = deque([fn])
        while q:
            x = q.popleft()
            if dist[x] >= max_depth:
                continue
            for c in self.in_calls.get(x, []):
                if c.caller not in dist:
                    dist[c.caller] = dist[x] + 1
                    q.append(c.caller)
        dist.pop(fn, None)
        return dist

    def transitive_callees(self, fn: str, max_depth: int = 12) -> dict[str, int]:
        dist = {fn: 0}
        q = deque([fn])
        while q:
            x = q.popleft()
            if dist[x] >= max_depth:
                continue
            for c in self.out_calls.get(x, []):
                if c.callee not in dist and not c.callee.startswith("?"):
                    dist[c.callee] = dist[x] + 1
                    q.append(c.callee)
        dist.pop(fn, None)
        return dist

    def call_path(self, src: str, dst: str, max_len: int = 16) -> list[CallSite] | None:
        prev: dict[str, CallSite | None] = {src: None}
        q = deque([src])
        while q:
            x = q.popleft()
            if x == dst:
                break
            for c in self.out_calls.get(x, []):
                if c.callee not in prev:
                    prev[c.callee] = c
                    q.append(c.callee)
        if dst not in prev:
            return None
        path = []
        cur = dst
        while prev[cur] is not None:
            path.append(prev[cur])
            cur = prev[cur].caller
        path.reverse()
        return path if len(path) <= max_len else None

    def entry_points(self) -> list[str]:
        return list(self.entries)

    def paths_from_entries(self, fn: str, limit: int = 4) -> list[list[CallSite]]:
        """Shortest call chains from each entry point that reaches fn."""
        out = []
        for e in self.entries:
            if e == fn:
                out.append([])
                continue
            p = self.call_path(e, fn)
            if p:
                out.append(p)
        out.sort(key=lambda p: (any(self.is_test(c.caller) for c in p), len(p)))
        return out[:limit]

    def roots(self) -> list[str]:
        """Defined functions nothing calls (entry points, callbacks, dead code or externally invoked)."""
        return sorted(f for f in self.defined if not self.in_calls.get(f))

    # ------------------------------------------------------------------------------------------ references
    def refs(self, sym: str, roles: Iterable[str] | None = None) -> list[dict]:
        rs = self.occ.get(sym, [])
        if roles:
            roles = set(roles)
            rs = [r for r in rs if r["role"] in roles]
        return sorted(rs, key=lambda r: (r["file"], r["line"]))

    def writers(self, sym: str) -> list[dict]:
        return self.refs(sym, ("write", "rw"))

    def readers(self, sym: str) -> list[dict]:
        return self.refs(sym, ("read", "rw"))

    # ------------------------------------------------------------------------------------------ value flow
    def _external(self, fn: str) -> bool:
        return fn not in self.defined

    def _ext_summary_targets(self, fnkey: str, param_idx: int | None, site: tuple[str, int], forward: bool):
        """Summary edges for an external function at one call site (API pack: copies/pure/opaque)."""
        name = self.name(fnkey)
        file, line = site
        out: list[Edge] = []
        if forward:
            if name in self.api.get("copies", {}):
                s, d = self.api["copies"][name]
                if param_idx is not None and param_idx == s:
                    for e in self.fwd.get(f"{fnkey}#param:{d}", []):
                        if e.file == file and (e.site or e.line) == line:
                            out.append(e)
                    return out
            for e in self.fwd.get(f"{fnkey}#ret", []):
                if e.file == file and (e.site or e.line) == line:
                    out.append(e)
            if name not in self.api.get("pure", []) and name not in self.api.get("copies", {}):
                for k, edges in ((k, v) for k, v in self.fwd.items() if k.startswith(fnkey + "#param:")):
                    for e in edges:
                        if e.kind == "outparam" and e.file == file and (e.site or e.line) == line:
                            out.append(e)
        else:
            for k in [f"{fnkey}#param:{i}" for i in range(8)]:
                for e in self.bwd.get(k, []):
                    if e.file == file and (e.site or e.line) == line:
                        out.append(e)
        return out

    def fn_occ(self, fn: str) -> list[tuple[str, dict]]:
        """(symbol, occurrence) pairs inside one function — an index, so per-function scans don't walk every
        occurrence of the project."""
        idx = getattr(self, "_occ_by_fn", None)
        if idx is None:
            idx = defaultdict(list)
            for sym, occs in self.occ.items():
                for o in occs:
                    if o.get("fn"):
                        idx[o["fn"]].append((sym, o))
            self._occ_by_fn = idx
        return idx.get(fn, [])

    def calls_on_line(self, file: str, line: int) -> list[str]:
        """Names of the functions called on one source line."""
        idx = getattr(self, "_calls_by_line", None)
        if idx is None:
            idx = defaultdict(list)
            for c in self.calls:
                idx[(c.file, c.line)].append(self.name(c.callee))
            self._calls_by_line = idx
        return idx.get((file, line), [])

    def occ_keys_of(self, fn: str) -> list[str]:
        """Parameter / local symbol keys of fn that have occurrences (fn#param:…, fn#local:…)."""
        idx = getattr(self, "_fn_scoped", None)
        if idx is None:
            idx = defaultdict(list)
            for k in self.occ:
                if "#" in k:
                    idx[k.split("#", 1)[0]].append(k)
            self._fn_scoped = idx
        return idx.get(fn, [])

    def is_test(self, key: str) -> bool:
        """Defined in secondary code: tests, docs/examples, analysis-tool models (see pathkinds)."""
        from ..pathkinds import is_secondary_path
        s = self.sym.get(key)
        return bool(s) and is_secondary_path(s.get("file"))

    def is_input(self, node: str) -> bool:
        """A node whose value comes from outside the program: an input API's result or written argument, or a
        memory-mapped register read."""
        base = node.split("#", 1)[0]
        ios = self.io_by_sym.get(base) or self.io_by_sym.get(node)
        if not ios or not any(i["dir"] == "source" for i in ios):
            return False
        if node == base:
            return True                            # a register / MMIO field
        if node.endswith("#ret"):
            return True
        m = re.search(r"#param:(\d+)$", node)
        if m:
            written = self.api.get("writes", {}).get(self.name(base), [])
            return int(m.group(1)) in written or not written
        return False

    def _next(self, node: str, ctx: tuple, fwd: bool) -> list[tuple[Edge, tuple]]:
        """Context-sensitive value-flow transitions from (node, call-site stack)."""
        nxt: list[tuple[Edge, tuple]] = []
        for e in (self.fwd if fwd else self.bwd).get(node, []):
            site = (e.file, e.site or e.line)
            returning = (e.src.endswith("#ret") and e.kind != "fnptr_ret") or e.kind == "outparam"
            entering = e.kind == "arg"
            # forward: leave a callee (pop) through its result, then enter a callee (push) through an argument;
            # backward: the mirror image. fnptr_arg / fnptr_ret edges stay at the same call site.
            pop, push = (returning, entering) if fwd else (entering, returning)
            c2 = ctx
            if pop:
                if c2 and c2[-1] != site:
                    continue                   # only return to the call site we came from
                c2 = c2[:-1]
            if push:
                c2 = (c2 + (site,))[-12:]
            nxt.append((e, c2))
        base = node.split("#", 1)[0]
        if "#" in node and self._external(base) and base in self.sym:
            # the value passes through an external (library) function at this call site: one synthetic edge
            if fwd and "#param:" in node and ctx:
                idx = int(node.rsplit(":", 1)[1]) if node.rsplit(":", 1)[1].isdigit() else None
                for e in self._ext_summary_targets(base, idx, ctx[-1], True):
                    nxt.append((Edge(node, e.dst, "api", e.file, e.line, e.fn,
                                     f"through {self.name(base)}(): {e.expr}", e.site, e.cfg), ctx[:-1]))
            elif not fwd and node.endswith("#ret") and ctx:
                for e in self._ext_summary_targets(base, None, ctx[-1], False):
                    nxt.append((Edge(e.src, node, "api", e.file, e.line, e.fn,
                                     f"into {self.name(base)}(): {e.expr}", e.site, e.cfg), ctx[:-1]))
            elif not fwd and "#param:" in node and ctx and self.name(base) in self.api.get("copies", {}):
                # memcpy(dst, src, n) backwards: what is in dst came from src at this call site
                s_idx, d_idx = self.api["copies"][self.name(base)]
                if node.endswith(f"#param:{d_idx}"):
                    for e in self.bwd.get(f"{base}#param:{s_idx}", []):
                        if e.file == ctx[-1][0] and (e.site or e.line) == ctx[-1][1]:
                            nxt.append((Edge(e.src, node, "api", e.file, e.line, e.fn,
                                             f"copied by {self.name(base)}(): {e.expr}", e.site, e.cfg), ctx[:-1]))
        out = []
        for e, c2 in nxt:
            other = e.dst if fwd else e.src
            if "#" not in other:
                c2 = ()        # globals and struct fields are context-free: the call context ends here
            out.append((e, c2))
        return out

    def slice(self, start: str, direction: str = "backward", max_nodes: int = 600, max_depth: int = 80
              ) -> dict[str, Any]:
        """Value-flow slice. Returns {nodes: {key: depth}, edges: [Edge], terminals: [key]}.
        Context-sensitive: entering a callee through a call site only returns through the same site."""
        fwd = direction == "forward"
        seen: set[tuple[str, tuple]] = set()
        nodes: dict[str, int] = {start: 0}
        edges: list[Edge] = []
        edge_set: set[tuple] = set()
        terminals: list[str] = []
        q: deque[tuple[str, tuple, int]] = deque([(start, (), 0)])
        while q and len(nodes) < max_nodes:
            node, ctx, d = q.popleft()
            if (node, ctx) in seen or d > max_depth:
                continue
            seen.add((node, ctx))
            if not fwd and node != start and self.is_input(node):
                terminals.append(node)             # data produced by an input: its previous contents don't matter
                continue
            nxt = self._next(node, ctx, fwd)
            if not nxt:
                terminals.append(node)
            for e, c2 in nxt:
                other = e.dst if fwd else e.src
                ek = (e.src, e.dst, e.file, e.line, e.kind)
                if ek not in edge_set:
                    edge_set.add(ek)
                    edges.append(e)
                if other not in nodes:
                    nodes[other] = d + 1
                q.append((other, c2, d + 1))
        return {"nodes": nodes, "edges": edges, "terminals": sorted(set(terminals))}

    def flow_path(self, start: str, goal, direction: str = "forward", prefer=None, max_states: int = 60000
                  ) -> list[Edge]:
        """Cheapest context-valid value-flow path from start to a node satisfying goal(node) (or == goal).
        Edges are returned in data order (source first) for both directions."""
        import heapq
        fwd = direction == "forward"
        is_goal = goal if callable(goal) else (lambda n, g=goal: n == g)
        start_state = (start, ())
        dist = {start_state: 0.0}
        prev: dict[tuple, tuple[tuple, Edge]] = {}
        pq = [(0.0, 0, start, ())]
        tie = 0
        found = None
        n = 0
        while pq and n < max_states:
            d, _, node, ctx = heapq.heappop(pq)
            n += 1
            if d > dist.get((node, ctx), 1e18):
                continue
            if node != start and is_goal(node):
                found = (node, ctx)
                break
            for e, c2 in self._next(node, ctx, fwd):
                other = e.dst if fwd else e.src
                w = 1.0 if prefer is None else prefer(other)
                st = (other, c2)
                if d + w < dist.get(st, 1e18):
                    dist[st] = d + w
                    prev[st] = ((node, ctx), e)
                    tie += 1
                    heapq.heappush(pq, (d + w, tie, other, c2))
        if not found:
            return []
        path = []
        cur = found
        while cur in prev:
            p, e = prev[cur]
            path.append(e)
            cur = p
        path.reverse()
        return path if fwd else list(reversed(path))

    def origins(self, sym: str) -> list[dict[str, Any]]:
        """Where a value ultimately comes from: terminal nodes of the backward slice, classified."""
        sl = self.slice(sym, "backward")
        out = []
        for t in sl["terminals"]:
            out.append({"key": t, "what": self.classify_origin(t)})
        return out

    def classify_origin(self, key: str) -> str:
        base = key.split("#", 1)[0]
        if base in self.io_by_sym or key in self.io_by_sym:
            io = (self.io_by_sym.get(base) or self.io_by_sym.get(key))[0]
            return f"input: {io['api']}"
        if key.endswith("#ret") and self._external(base):
            return "result of an external/library function"
        if "#param:" in key:
            fn = key.split("#", 1)[0]
            if fn in self.entries:
                return "parameter of an entry point (supplied by the caller/runtime)"
            if not self.in_calls.get(fn):
                return "parameter of a function nothing in this code calls"
            return "parameter"
        s = self.sym.get(key)
        if s and s["kind"] == "enum_const":
            return "constant"
        if key in self.consts:
            return f"constant = {self.consts[key]}"
        if s and s["kind"] in ("global", "field") and not self.bwd.get(key):
            return "never assigned from other data in this code (literal/initial value or external)"
        return "literal or unmodelled source"

    # ------------------------------------------------------------------------------------------ impact
    def impact(self, fn: str) -> dict[str, Any]:
        """What could be affected by changing fn: callers (transitive, up to entry points), data that depends on its
        result or on what it writes, and what those values reach (sinks)."""
        callers = self.transitive_callers(fn)
        written = {o["sym"] for o in self.occ_in(fn) if o["role"] in ("write", "rw")}
        written = {w for w in written if not w.startswith(fn + "#")}
        roots = [f"{fn}#ret"] + sorted(written)
        affected_data: dict[str, int] = {}
        sinks = []
        edges: list[Edge] = []
        for r in roots:
            sl = self.slice(r, "forward", max_nodes=300)
            edges += sl["edges"]
            for k, d in sl["nodes"].items():
                if k not in affected_data or d < affected_data[k]:
                    affected_data[k] = d
            for t in sl["terminals"]:
                base = t.split("#", 1)[0]
                if base in self.io_by_sym:
                    sinks.append(t)
        readers_fns: set[str] = set()
        for k in affected_data:
            for o in self.occ.get(k, []):
                if o["role"] in ("read", "rw") and o["fn"] and o["fn"] != fn:
                    readers_fns.add(o["fn"])
            f = self.fn_of(k)
            if f and f != fn:
                readers_fns.add(f)
        readers_fns = {f for f in readers_fns if f != fn and f in self.defined}
        entry_paths = self.paths_from_entries(fn)
        return {"callers": callers, "writes": sorted(written), "data": affected_data, "data_edges": edges,
                "functions_using_result": sorted(readers_fns), "sinks": sorted(set(sinks)),
                "entry_paths": entry_paths, "reads": sorted({o["sym"] for o in self.occ_in(fn) if o["role"] == "read"})}

    def occ_in(self, fn: str) -> list[dict]:
        out = []
        for sym, lst in self.occ.items():
            for o in lst:
                if o["fn"] == fn:
                    out.append(o)
        return out

    # ------------------------------------------------------------------------------------------ conditions
    def eval_value(self, expr: str, env: dict[str, Any]) -> tuple[Any, list[str]]:
        return eval_c_expr(expr, env, self.macro_values())

    def eval_cond(self, cond: str, env: dict[str, Any]) -> tuple[bool | None, list[str]]:
        """Evaluate a C condition with known values (scenario inputs + constants). Returns (value|None, unknown names)."""
        return eval_c_condition(cond, env, self.macro_values())

    def macro_values(self) -> dict[str, Any]:
        out = {}
        for k, v in self.consts.items():
            name = self.name(k) if not k.startswith("c:macro@") else k[8:]
            val = _parse_num(v)
            if val is not None:
                out[name] = val
        return out


# ---------------------------------------------------------------------------------------------- evaluator
_TOKEN = re.compile(r"\s*(?:(\d+\.\d*(?:[eE][-+]?\d+)?[fFlL]?|\.\d+(?:[eE][-+]?\d+)?[fFlL]?|0[xX][0-9a-fA-F]+[uUlL]*|\d+[uUlL]*)"
                    r"|('(?:\\.|[^'])')|([A-Za-z_][A-Za-z0-9_]*(?:(?:\.|->)[A-Za-z_][A-Za-z0-9_]*)*)|(&&|\|\||==|!=|<=|>=|<<|>>|[-+*/%<>!()&|^~?:]))")


def _parse_num(v: str):
    v = str(v).strip().strip("()").strip()
    m = re.fullmatch(r"[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?[fFlLuU]*", v)
    if m:
        s = re.sub(r"[fFlLuU]+$", "", v)
        return float(s) if any(c in s for c in ".eE") else int(s)
    m = re.fullmatch(r"0[xX]([0-9a-fA-F]+)[uUlL]*", v)
    if m:
        return int(m.group(1), 16)
    return None


def eval_c_expr(expr: str, env: dict[str, Any], macros: dict[str, Any]) -> tuple[Any, list[str]]:
    """Value of a C expression over known values (numbers, macros, enum constants, env); (None, unknowns) if not."""
    v, unknown = _eval(expr, env, macros)
    return v, unknown


def eval_c_condition(cond: str, env: dict[str, Any], macros: dict[str, Any]) -> tuple[bool | None, list[str]]:
    v, unknown = _eval(cond, env, macros)
    return (None if v is None else bool(v)), unknown


def _eval(cond: str, env: dict[str, Any], macros: dict[str, Any]) -> tuple[Any, list[str]]:
    out: list[str] = []
    unknown: list[str] = []
    pos = 0
    cond = cond.strip()
    for m in _TOKEN.finditer(cond):
        if m.start() != pos and cond[pos:m.start()].strip():
            return None, ["<unparsed>"]
        pos = m.end()
        num, ch, ident, op = m.groups()
        if num:
            out.append(repr(_parse_num(num)))
        elif ch:
            out.append(repr(ord(ch[1:-1].encode().decode("unicode_escape"))))
        elif ident:
            key = ident.replace("->", ".")
            val = None
            for cand in (key, key.split(".")[-1]):
                if cand in env:
                    val = env[cand]
                    break
            if val is None and key in macros:
                val = macros[key]
            if val is None:
                if ident in ("NULL",):
                    val = 0
                elif ident in ("true", "TRUE"):
                    val = 1
                elif ident in ("false", "FALSE"):
                    val = 0
            if val is None:
                unknown.append(ident)
                out.append("__U__")
            else:
                out.append(repr(int(val) if isinstance(val, bool) else val))
        elif op:
            out.append({"&&": " and ", "||": " or ", "!": " not ", "/": "/"}.get(op, op))
    if pos != len(cond) and cond[pos:].strip():
        return None, ["<unparsed>"]
    if unknown:
        return None, unknown
    expr = " ".join(out)
    try:
        val = eval(expr, {"__builtins__": {}}, {})  # noqa: S307 — tokens are whitelisted numbers/operators only
    except Exception:
        return None, ["<unevaluable>"]
    return val, []
