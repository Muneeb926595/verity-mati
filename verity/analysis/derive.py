"""Link step (design §13/§14): facts that need the whole repository.

* function pointers, dispatch tables and callbacks → call targets, by propagating function addresses over value flows
  (points-to over the flow graph; field-based, context-insensitive) — res "table" / "flow"; signature matching only
  as a last resort (res "signature", never above INFERENCE);
* C# virtual/interface calls → overrides (CHA), narrowed by instantiated types (RTA); events → subscribed handlers;
* syntax-only (F4) calls matched by name (res "name");
* I/O sites (API knowledge pack, naming heuristics, memory-mapped registers) and entry points.

Every derived row is flagged `derived=1` and rebuilt from scratch on every index run."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..extract.recon import load_api_pack


def run(store, root: Path) -> dict[str, Any]:
    st = store
    st.exec("DELETE FROM call WHERE derived=1")
    st.exec("DELETE FROM flow WHERE derived=1")
    st.exec("DELETE FROM entry WHERE derived=1")
    st.exec("DELETE FROM io WHERE derived=1")
    stats: dict[str, Any] = {"resolved": defaultdict(int), "unresolved": 0}
    syms = {r["key"]: dict(r) for r in st.q("SELECT key, kind, name, qual, sig, type, file, line, is_def, stub, lang "
                                            "FROM symbol")}
    functions = {k: s for k, s in syms.items() if s["kind"] in ("function", "method", "constructor")}
    defined = {k for k, s in functions.items() if s["is_def"] and s["file"] and s["file"] != "<stub>"}
    by_name: dict[str, list[str]] = defaultdict(list)
    for k in defined:
        by_name[functions[k]["name"]].append(k)

    new_calls: list[dict] = []
    new_flows: list[dict] = []

    # 1. name-matched calls from syntax-only files --------------------------------------------------------------
    for r in st.q("SELECT * FROM call WHERE callee LIKE 'name:%'"):
        name = r["callee"][5:]
        cands = by_name.get(name, [])
        if len(cands) == 1:
            new_calls.append(_call_row(r, cands[0], "name"))
            stats["resolved"]["name"] += 1

    # 2. function pointers / tables / callbacks -----------------------------------------------------------------
    flows_by_src: dict[str, list[str]] = defaultdict(list)
    for r in st.q("SELECT src, dst FROM flow"):
        flows_by_src[r["src"]].append(r["dst"])
    pts: dict[str, set[str]] = defaultdict(set)
    direct_kind: dict[tuple[str, str], str] = {}
    for r in st.q("SELECT fn, dst, kind FROM fnaddr"):
        pts[r["dst"]].add(r["fn"])
        direct_kind[(r["dst"], r["fn"])] = r["kind"]
    fnptr_calls = [dict(r) for r in st.q("SELECT * FROM call WHERE kind IN ('fnptr','delegate') AND callee IS NULL "
                                         "AND derived=0")]
    bridges_done: set[tuple[str, str]] = set()
    for _round in range(8):
        _propagate(pts, flows_by_src)
        changed = False
        for c in fnptr_calls:
            via = c["via"]
            if not via:
                continue
            nargs = len(json.loads(c["args"] or "[]"))
            for tgt in list(pts.get(via, ())):
                if (via, tgt) in bridges_done or not _arity_ok(syms, tgt, nargs):
                    continue
                bridges_done.add((via, tgt))
                nparams = _param_count(syms, tgt)
                for i in range(max(nparams, len(json.loads(c["args"] or "[]")))):
                    a, b = f"{via}#param:{i}", f"{tgt}#param:{i}"
                    flows_by_src[a].append(b)
                    new_flows.append({"src": a, "dst": b, "kind": "fnptr_arg", "file": c["file"], "line": c["line"],
                                      "fn": c["caller"], "expr": f"call through {via.split('@')[-1]}", "site": c["line"]})
                a, b = f"{tgt}#ret", f"{via}#ret"
                flows_by_src[a].append(b)
                new_flows.append({"src": a, "dst": b, "kind": "fnptr_ret", "file": c["file"], "line": c["line"],
                                  "fn": c["caller"], "expr": f"return through {via.split('@')[-1]}", "site": c["line"]})
                changed = True
        if not changed:
            break
    addr_taken = {r["fn"] for r in st.q("SELECT DISTINCT fn FROM fnaddr")} | \
                 {r["sym"] for r in st.q("SELECT DISTINCT sym FROM occ WHERE role='addr'")}
    seen_sites: set[tuple] = set()
    for c in fnptr_calls:
        via = c["via"]
        n_args = len(json.loads(c["args"] or "[]"))
        # a call with N arguments cannot land in a function taking M != N parameters (value flow through void*
        # and shared callback plumbing over-approximates; the call's own shape rules those targets out)
        targets = sorted(t for t in pts.get(via, ()) if _arity_ok(syms, t, n_args)) if via else []
        res = None
        if targets:
            res = "table" if any(direct_kind.get((via, t)) == "table" for t in targets) else "flow"
        else:
            n = len(json.loads(c["args"] or "[]"))
            cands = [f for f in addr_taken if f in defined and _param_count(syms, f) == n]
            if 0 < len(cands) <= 8:
                targets, res = sorted(cands), "signature"
        if not targets:
            stats["unresolved"] += 1
            continue
        for t in targets:
            key = (c["file"], c["line"], c["col"], t)
            if key in seen_sites:
                continue
            seen_sites.add(key)
            new_calls.append(_call_row(c, t, res))
            stats["resolved"][res] += 1

    # 3. C#: virtual / interface dispatch (CHA narrowed by RTA) and events --------------------------------------
    overrides: dict[str, set[str]] = defaultdict(set)
    for r in st.q("SELECT sub, base FROM inherit WHERE kind IN ('overrides','implements') AND sub LIKE 'M:%'"):
        overrides[r["base"]].add(r["sub"])
    instantiated = {r["type"] for r in st.q("SELECT DISTINCT type FROM instantiate")}

    def all_overrides(m: str) -> set[str]:
        out, stack = set(), [m]
        while stack:
            x = stack.pop()
            for y in overrides.get(x, ()):
                if y not in out:
                    out.add(y)
                    stack.append(y)
        return out

    for c in st.q("SELECT * FROM call WHERE kind IN ('virtual','interface') AND derived=0 AND callee IS NOT NULL"):
        subs = all_overrides(c["callee"])
        if not subs:
            continue
        rta = {s for s in subs if _declaring_type(s) in instantiated} if instantiated else subs
        for s in (rta or subs):
            new_calls.append(_call_row(c, s, "rta" if rta else "cha"))
            stats["resolved"]["rta" if rta else "cha"] += 1
            new_flows += _bridge(st, c["callee"], s, c)
    handlers: dict[str, set[str]] = defaultdict(set)
    for r in st.q("SELECT event, handler FROM subscribe WHERE op='+='"):
        if r["handler"]:
            handlers[r["event"]].add(r["handler"])
    for c in st.q("SELECT * FROM call WHERE kind='event' AND derived=0"):
        ev = c["via"] or c["callee"]
        for h in sorted(handlers.get(ev, ())):
            new_calls.append(_call_row(c, h, "flow"))
            stats["resolved"]["event"] += 1
            new_flows += _bridge(st, ev, h, c)

    # write calls / flows
    _insert_calls(st, new_calls)
    _insert_flows(st, new_flows)

    # 4. I/O sites --------------------------------------------------------------------------------------------
    api = load_api_pack()
    src_pat = [re.compile(p, re.I) for p in api.get("source_name_patterns", [])]
    sink_pat = [re.compile(p, re.I) for p in api.get("sink_name_patterns", [])]
    io_rows = []
    for r in st.q("SELECT c.caller, c.callee, c.file, c.line, s.name FROM call c JOIN symbol s ON s.key=c.callee "
                  "WHERE s.kind='function'"):
        if r["callee"] in defined:
            continue
        name = r["name"]
        if name in api["sources"]:
            io_rows.append((r["callee"], "source", api["sources"][name], r["file"], r["line"], r["caller"], "apipack"))
        elif name in api["sinks"]:
            io_rows.append((r["callee"], "sink", api["sinks"][name], r["file"], r["line"], r["caller"], "apipack"))
        elif name in api.get("pure", []) or name in api.get("copies", {}):
            continue
        elif any(p.search(name) for p in src_pat):
            io_rows.append((r["callee"], "source", "hardware/OS input (by name)", r["file"], r["line"], r["caller"], "heuristic"))
        elif any(p.search(name) for p in sink_pat):
            io_rows.append((r["callee"], "sink", "hardware/OS output (by name)", r["file"], r["line"], r["caller"], "heuristic"))
    # memory-mapped registers: fields of stubbed register blocks, or volatile struct fields reached as CAPS->CAPS
    for r in st.q("SELECT o.sym, o.file, o.line, o.fn, o.role FROM occ o WHERE o.sym LIKE 'c:@S@__vr_regs_%'"):
        io_rows.append((r["sym"], "source" if r["role"] == "read" else "sink", "memory-mapped register",
                        r["file"], r["line"], r["fn"], "mmio"))
    # C#: the worker may emit io records itself; keep them (they are inserted by the extractor as t=io)
    st.executemany("INSERT OR IGNORE INTO io(sym, dir, api, file, line, fn, res, derived) VALUES(?,?,?,?,?,?,?,1)", io_rows)
    stats["io"] = st.one("SELECT count(*) n FROM io")["n"]

    # 5. entry points ------------------------------------------------------------------------------------------
    entries = []
    spawn = dict(api.get("spawn", {}))
    register = api.get("register", {})
    # thread-start wrappers of an OS abstraction layer (sys_thread_new(name, fn, …) -> pthread_create/CreateThread):
    # a function of this code that starts threads itself and takes a function-pointer parameter starts that function
    wrapped_by: dict[str, str] = {}
    wrapped_all: dict[str, set[str]] = {}
    fnptr_params: dict[str, int] = {}
    for r in st.q("SELECT container, idx, sig FROM symbol WHERE kind='param' AND sig LIKE '%(*)%'"):
        if r["container"] in defined and r["container"] not in fnptr_params and r["idx"] is not None:
            fnptr_params[r["container"]] = r["idx"]
    caller_names: dict[str, set[str]] = defaultdict(set)
    for r in st.q("SELECT c.caller, s.name FROM call c JOIN symbol s ON s.key = c.callee WHERE c.callee IS NOT NULL"):
        caller_names[r["caller"]].add(r["name"])
    direct_callees: dict[str, set[str]] = defaultdict(set)
    has_fnptr_call: set[str] = set()
    for r in st.q("SELECT caller, callee, kind FROM call WHERE derived=0"):
        if r["kind"] in ("fnptr", "delegate"):
            has_fnptr_call.add(r["caller"])
        elif r["callee"] in defined:
            direct_callees[r["caller"]].add(r["callee"])
    param_to: dict[str, set[str]] = defaultdict(set)
    for r in st.q("SELECT src, dst FROM flow WHERE src LIKE '%#param:%' AND dst LIKE '%#param:%'"):
        param_to[r["src"]].add(r["dst"])

    def trampoline(fn: str) -> bool:
        # a tiny function whose job is to call through a pointer it was handed (thread_wrapper, sys_thread_function)
        return fn in has_fnptr_call and len(direct_callees.get(fn, ())) <= 3

    def starts_its_param(w: str, idx: int, api_names: set[str]) -> bool:
        """w's function-pointer parameter really becomes the thread: passed straight to the start call, or w hands
        the OS a trampoline that calls it. (tcpip_init(initfunc, …) starts tcpip_thread, not initfunc.)"""
        for api in api_names:
            k = spawn.get(api)
            for key in by_name.get(api, []) + [a for a, s2 in syms.items() if s2["name"] == api and not s2.get("file")]:
                if f"{key}#param:{k}" in param_to.get(f"{w}#param:{idx}", ()):
                    return True
        for r2 in st.q("SELECT fn, dst FROM fnaddr WHERE kind='arg'"):
            m2 = re.match(r"^(.*)#param:(\d+)$", r2["dst"])
            if m2 and syms.get(m2.group(1), {}).get("name") in api_names and spawn.get(syms[m2.group(1)]["name"]) == \
                    int(m2.group(2)) and trampoline(r2["fn"]) and \
                    st.one("SELECT 1 FROM call WHERE caller=? AND callee=?", (w, m2.group(1))):
                return True
        return False
    for _ in range(3):
        grew = False
        for w, idx in fnptr_params.items():
            nm = functions[w]["name"]
            if nm in spawn:
                continue
            via = sorted(caller_names.get(w, set()) & set(spawn))
            if via and starts_its_param(w, idx, set(via)):
                spawn[nm] = idx
                wrapped_by[nm] = via[0]
                wrapped_all[nm] = set(via)          # one wrapper per OS port may call different OS APIs
                grew = True
        if not grew:
            break
    wrapper_keys = {k: functions[k]["name"] for k in fnptr_params if functions[k]["name"] in wrapped_by}
    for r in st.q("SELECT fn, dst, file, line FROM fnaddr WHERE kind='arg'"):
        m = re.match(r"^(.*)#param:(\d+)$", r["dst"])
        if not m:
            continue
        callee, idx = m.group(1), int(m.group(2))
        cname = syms.get(callee, {}).get("name")
        if cname in spawn and spawn[cname] == idx:
            how = (f" ({cname}() starts threads with " + " / ".join(f"{a}()" for a in sorted(wrapped_all[cname])) + ")") \
                if cname in wrapped_by else ""
            # the function a wrapper hands to the OS is its trampoline: it runs whatever the wrapper was given
            host = next((w for w, n in wrapper_keys.items() if cname in wrapped_all.get(n, ()) and trampoline(r["fn"]) and
                         st.one("SELECT 1 FROM call WHERE caller=? AND callee=? AND file=? AND line BETWEEN ? AND ?",
                                (w, callee, r["file"], r["line"] - 6, r["line"]))), None)   # the call may span lines
            if host:
                entries.append((r["fn"], "thread", f"the thread trampoline of {wrapper_keys[host]}(): started by {cname}() "
                                                   f"at {r['file']}:{r['line']}, it runs the function passed to "
                                                   f"{wrapper_keys[host]}()", "trampoline"))
            else:
                entries.append((r["fn"], "thread", f"started as a thread by {cname}() at {r['file']}:{r['line']}{how}",
                                "wrapper" if how else "apipack"))
        elif cname in register and register[cname][0] == idx:
            entries.append((r["fn"], "callback", f"{register[cname][1]} registered with {cname}() at {r['file']}:{r['line']}",
                            "apipack"))
    # interrupt handlers marked in the source (the keyword itself is removed by the preprocessor shims)
    from ..extract.recon import entry_patterns, read_text
    pats = entry_patterns()
    if pats:
        for r in st.q("SELECT path FROM file WHERE lang='c'"):
            txt = read_text(Path(root) / r["path"])
            for rx, why in pats:
                for m in rx.finditer(txt):
                    for k in by_name.get(m.group(1), []):
                        # upgrade a naming-convention guess to a keyword-confirmed interrupt handler
                        st.exec("UPDATE entry SET res='keyword', detail=? WHERE sym=? AND kind='isr'", (why, k))
                        if not st.one("SELECT 1 FROM entry WHERE sym=? AND kind='isr'", (k,)):
                            entries.append((k, "isr", why, "keyword"))
    called = {r["callee"] for r in st.q("SELECT DISTINCT callee FROM call WHERE callee IS NOT NULL")}
    isr_like = re.compile(r"(IRQHandler|_Handler|Handler|_ISR|ISR|Isr|_isr|_irq|_IRQ)$")
    # "…FromISR" / "…_from_isr": an API that may be called FROM an interrupt, not an interrupt handler
    from_isr = re.compile(r"(From|from_?)(ISR|Isr|isr)$")
    have_isr = {r["sym"] for r in st.q("SELECT sym FROM entry WHERE kind='isr'")} | {e[0] for e in entries if e[1] == "isr"}
    for k in defined:
        nm = functions[k]["name"]
        if isr_like.search(nm) and not from_isr.search(nm) and k not in called and k not in have_isr and \
                functions[k].get("lang") == "c":
            entries.append((k, "isr", "interrupt/exception handler (named like one and never called by this code, so "
                                      "hardware or a vector table calls it)", "pattern"))
    already = {e[0] for e in entries} | {r["sym"] for r in st.q("SELECT sym FROM entry WHERE derived=0")}
    for f in addr_taken:
        if f in defined and f not in called and f not in already:
            entries.append((f, "callback", "its address is stored but no call through it was found in this code "
                                           "(likely called by a library, OS or hardware)", "heuristic"))
    st.executemany("INSERT OR IGNORE INTO entry(sym, kind, detail, res, derived) VALUES(?,?,?,?,1)", entries)
    stats["entries"] = st.one("SELECT count(*) n FROM entry")["n"]
    stats["resolved"] = dict(stats["resolved"])
    stats["calls"] = st.one("SELECT count(*) n FROM call")["n"]
    return stats


def describe(stats: dict[str, Any]) -> str:
    r = stats.get("resolved", {})
    parts = []
    n_ptr = r.get("table", 0) + r.get("flow", 0)
    if n_ptr:
        parts.append(f"traced {n_ptr} call(s) through function pointers or tables")
    if r.get("signature"):
        parts.append(f"{r['signature']} matched by signature only")
    if r.get("cha") or r.get("rta"):
        parts.append(f"resolved {r.get('cha', 0) + r.get('rta', 0)} virtual call target(s)")
    if r.get("event"):
        parts.append(f"connected {r['event']} event(s) to their handlers")
    if stats.get("unresolved"):
        parts.append(f"{stats['unresolved']} indirect call(s) could not be resolved")
    parts.append(f"{stats.get('entries', 0)} entry point(s), {stats.get('io', 0)} input/output site(s)")
    s = "; ".join(parts)
    return s[:1].upper() + s[1:]


def _propagate(pts: dict[str, set[str]], flows_by_src: dict[str, list[str]]) -> None:
    work = [k for k, v in pts.items() if v]
    while work:
        n = work.pop()
        vals = pts[n]
        for d in flows_by_src.get(n, ()):
            before = len(pts[d])
            pts[d] |= vals
            if len(pts[d]) != before:
                work.append(d)


def _arity_ok(syms: dict[str, dict], fn: str, nargs: int) -> bool:
    s = syms.get(fn)
    sig = (s or {}).get("sig") or ""
    if not s or "(" not in sig or "..." in sig or "__arglist" in sig:
        return True                        # unknown or variadic: cannot rule it out
    return _param_count(syms, fn) == nargs


def _param_count(syms: dict[str, dict], fn: str) -> int:
    s = syms.get(fn)
    if not s:
        return 0
    sig = s.get("sig") or ""
    m = re.search(r"\((.*)\)", sig)
    if not m:
        return 0
    inner = m.group(1).strip()
    if not inner or inner == "void":
        return 0
    depth, n = 0, 1
    for ch in inner:
        if ch in "(<[":
            depth += 1
        elif ch in ")>]":
            depth -= 1
        elif ch == "," and depth == 0:
            n += 1
    return n


def _declaring_type(method_key: str) -> str:
    # "M:Ns.Type.Method(args)" -> "T:Ns.Type"
    body = method_key[2:].split("(")[0]
    return "T:" + body.rsplit(".", 1)[0]


_PARAMS_CACHE: dict[int, dict[str, list[tuple[int, str, str]]]] = {}


def _params(st, fn: str) -> list[tuple[int, str, str]]:
    """(position, name, key) of a function's parameters."""
    rows = st.q("SELECT key, name, idx FROM symbol WHERE kind='param' AND container=? ORDER BY idx", (fn,))
    out = [(r["idx"] if r["idx"] is not None else i, r["name"], r["key"]) for i, r in enumerate(rows)]
    if not out:
        rows = st.q("SELECT DISTINCT dst FROM flow WHERE dst LIKE ? ESCAPE '\\'", (fn.replace("_", "\\_") + "#param:%",))
        for i, r in enumerate(rows):
            suffix = r["dst"].rsplit("#param:", 1)[1]
            out.append((int(suffix) if suffix.isdigit() else i, suffix, r["dst"]))
    return out


def _bridge(st, src_callable: str, tgt: str, c) -> list[dict]:
    """Value bridges for a resolved dynamic call: arguments bound to the call's static target (or delegate/event)
    flow into the real target's parameters (by name, else position); the real target's result flows back."""
    sp = _params(st, src_callable)
    tp = _params(st, tgt)
    by_name = {n: k for _, n, k in tp}
    by_pos = {p: k for p, _, k in tp}
    out = []
    for pos, name, key in sp:
        dst = by_name.get(name) or by_pos.get(pos)
        if dst:
            out.append({"src": key, "dst": dst, "kind": "fnptr_arg", "file": c["file"], "line": c["line"], "fn": c["caller"],
                        "expr": "dynamic dispatch", "site": c["line"]})
    out.append({"src": f"{tgt}#ret", "dst": f"{src_callable}#ret", "kind": "fnptr_ret", "file": c["file"], "line": c["line"],
                "fn": c["caller"], "expr": "dynamic dispatch", "site": c["line"]})
    return out


def _call_row(c, callee: str, res: str) -> dict:
    c = dict(c)
    return {"caller": c["caller"], "callee": callee, "file": c["file"], "line": c["line"], "col": c["col"],
            "kind": c["kind"], "res": res, "via": c["via"], "args": c["args"], "guards": c["guards"],
            "cfg": c.get("cfg") or "default"}


def _insert_calls(st, rows: list[dict]) -> None:
    if not rows:
        return
    st.executemany(
        "INSERT OR IGNORE INTO call(caller, callee, file, line, col, kind, res, via, args, guards, cfg, derived) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
        [(r["caller"], r["callee"], r["file"], r["line"], r["col"], r["kind"], r["res"], r["via"], r["args"],
          r["guards"], r["cfg"]) for r in rows])


def _insert_flows(st, rows: list[dict]) -> None:
    if not rows:
        return
    st.executemany(
        "INSERT OR IGNORE INTO flow(src, dst, kind, file, line, fn, expr, cfg, derived, site) VALUES(?,?,?,?,?,?,?,?,1,?)",
        [(r["src"], r["dst"], r["kind"], r["file"], r["line"], r["fn"], r["expr"], "default", r["site"]) for r in rows])
