"""Second parser cross-check (design §13): cppcheck parses the same C code independently. We compare, per file, the
function definitions and the calls each function makes. Agreement raises confidence; disagreement is recorded as a
diagnostic so answers touching those functions say so. cppcheck runs on a mirror copy of the sources because it
writes `.dump` files next to its inputs (the user's folder is never modified)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

from .. import config, osutil


# standard macros cppcheck reports as calls (the compiler expands them): not disagreements
MACRO_LIKE = {"va_start", "va_arg", "va_end", "va_copy", "assert", "offsetof", "sizeof", "static_assert", "_Static_assert",
              "isnan", "isinf", "isfinite", "signbit", "fpclassify", "setjmp", "errno", "alignof", "_Alignof", "typeof"}


def cppcheck_path() -> Path | None:
    # tools/cppcheck/bin/cppcheck (built from source), tools/cppcheck/cppcheck.exe (Windows installer contents), or
    # a normal installation
    for p in (config.TOOLS / "cppcheck" / "bin" / "cppcheck", config.TOOLS / "cppcheck" / "cppcheck"):
        p = osutil.exe(p)
        if p.exists():
            return p
    w = shutil.which("cppcheck")
    if w:
        return Path(w)
    if osutil.IS_WINDOWS:
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")):
            q = Path(base) / "Cppcheck" / "cppcheck.exe" if base else None
            if q and q.exists():
                return q
    return None


def run(store, root: Path, profile, cancel: threading.Event | None = None,
        progress: Callable[[int, int], None] | None = None, max_files: int = 400) -> dict[str, Any]:
    cancel = cancel or threading.Event()
    progress = progress or (lambda d, t: None)
    exe = cppcheck_path()
    if exe is None:
        return {"state": "skipped", "detail": "cppcheck is not installed; skipped the second-parser check"}
    if profile is None:
        return {"state": "skipped", "detail": "No C code to cross-check"}
    files = [r["path"] for r in store.q("SELECT path FROM file WHERE lang='c' AND path LIKE '%.c' AND fidelity!='F4' "
                                        "ORDER BY path")]
    if not files:
        return {"state": "skipped", "detail": "No C files to cross-check"}
    files = files[:max_files]
    shas = {r["path"]: r["sha"] for r in store.q("SELECT path, sha FROM file WHERE lang='c'")}
    indexed_at = {r["path"]: r["indexed_at"] or 0 for r in store.q("SELECT path, indexed_at FROM file WHERE lang='c'")}
    prev = (store.meta_get("crosscheck") or {}).get("files", {})
    mirror = config.repo_dir(root) / "work" / "xcheck"
    mirror.mkdir(parents=True, exist_ok=True)
    # mirror only what changed (cppcheck writes .dump files next to its inputs; the user's folder is never touched)
    for rel, sha in shas.items():
        dst = mirror / rel
        stamp = mirror / (rel + ".sha")
        try:
            if stamp.exists() and stamp.read_text() == sha:
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / rel, dst)
            stamp.write_text(sha or "")
        except OSError:
            pass
    inc_args = []
    for d in profile.include_dirs:
        try:
            rel = Path(d).resolve().relative_to(root.resolve())
            inc_args.append("-I" + str(mirror / rel))
        except ValueError:
            continue
    inc_args.append("-I" + profile.stub_dir)
    defs = [f"-D{k}={v}" if v else f"-D{k}" for k, v in profile.defines.items() if not k.startswith("_M")]
    per_file: dict[str, dict] = {}
    todo = []
    for rel in files:
        p = prev.get(rel)
        if p and p.get("sha") == shas.get(rel) and "functions_agree" in p and p.get("checked_at", 0) > indexed_at.get(rel, 0):
            per_file[rel] = p                     # unchanged since the last check: reuse
        else:
            todo.append(rel)

    def check(rel: str) -> tuple[str, dict | None, list[str]]:
        src = mirror / rel
        try:
            subprocess.run([str(exe), "--dump", "--quiet", "--language=c", "--std=c11", "--max-configs=1",
                            *inc_args, *defs, str(src)], capture_output=True, timeout=120, cwd=str(mirror))
        except (subprocess.TimeoutExpired, OSError):
            return rel, None, []
        dump = Path(str(src) + ".dump")
        if not dump.exists():
            return rel, None, []
        try:
            cp_fns, cp_calls = _parse_dump(dump, src)
        except ET.ParseError:
            return rel, None, []
        cl_fns = {r["name"]: r["line"] for r in store.q(
            "SELECT name, line FROM symbol WHERE file=? AND kind='function' AND is_def=1", (rel,))}
        both = set(cp_fns) & set(cl_fns)
        oc = set(cl_fns) - set(cp_fns)
        op = set(cp_fns) - set(cl_fns)
        cl_calls: dict[str, set[str]] = {}
        for r in store.q("SELECT s.name caller, t.name callee, c.callee ckey FROM call c JOIN symbol s ON s.key=c.caller "
                         "LEFT JOIN symbol t ON t.key=c.callee WHERE c.file=? AND c.callee IS NOT NULL", (rel,)):
            name = r["callee"] or (r["ckey"] or "").split("@")[-1]
            cl_calls.setdefault(r["caller"], set()).add(name)
        dis = []
        agree = total = 0
        for fn in both:
            a_, b_ = cl_calls.get(fn, set()), cp_calls.get(fn, set()) - MACRO_LIKE
            total += len(a_ | b_)
            agree += len(a_ & b_)
            for miss in sorted(b_ - a_):
                dis.append(f"cppcheck sees {fn}() call {miss}() but the main parser does not")
        for fn in sorted(op):
            dis.append(f"cppcheck finds a definition of {fn}() the main parser did not see")
        return rel, {"functions_agree": len(both), "only_main": sorted(oc), "only_cppcheck": sorted(op),
                     "disagreements": len(dis), "calls_agree": agree, "calls_total": total, "sha": shas.get(rel),
                     "checked_at": __import__("time").time()}, dis

    import concurrent.futures as cf
    import os as _os
    done = 0
    with cf.ThreadPoolExecutor(max_workers=max(1, (_os.cpu_count() or 2) - 2)) as pool:
        futs = [pool.submit(check, rel) for rel in todo]
        for fut in cf.as_completed(futs):
            if cancel.is_set():
                for f in futs:
                    f.cancel()
                return {"state": "cancelled", "detail": "cancelled"}
            rel, res, dis = fut.result()
            done += 1
            progress(done, len(todo))
            if res is None:
                continue
            store.exec("DELETE FROM diag WHERE file=? AND severity='crosscheck'", (rel,))
            for d in dis[:20]:
                store.exec("INSERT INTO diag VALUES(?,?,?,?)", (rel, 0, "crosscheck", d))
            per_file[rel] = res
    agree_fn = sum(v.get("functions_agree", 0) for v in per_file.values())
    only_clang = sum(len(v.get("only_main", [])) for v in per_file.values())
    only_cpp = sum(len(v.get("only_cppcheck", [])) for v in per_file.values())
    agree_calls = sum(v.get("calls_agree", 0) for v in per_file.values())
    call_total = sum(v.get("calls_total", 0) for v in per_file.values())
    progress(len(files), len(files))
    rate = agree_calls / call_total if call_total else 1.0
    fn_rate = agree_fn / max(1, agree_fn + only_clang + only_cpp)
    store.meta_set("crosscheck", {"function_agreement": round(fn_rate, 4), "call_agreement": round(rate, 4),
                                  "files": per_file})
    return {"state": "done",
            "detail": f"cppcheck agrees on {fn_rate:.0%} of function definitions and {rate:.0%} of direct calls"
                      + (f" ({len(todo)} file(s) checked, {len(files) - len(todo)} unchanged)" if len(todo) < len(files) else ""),
            "function_agreement": round(fn_rate, 4), "call_agreement": round(rate, 4)}


def _parse_dump(dump: Path, src: Path) -> tuple[dict[str, int], dict[str, set[str]]]:
    tree = ET.parse(dump)
    fns: dict[str, int] = {}
    calls: dict[str, set[str]] = {}
    for cfg in tree.getroot().iter("dump"):
        tokens = {}
        toklist = []
        for t in cfg.iter("token"):
            tokens[t.get("id")] = t
            toklist.append(t)
        scopes = {}
        for s in cfg.iter("scope"):
            scopes[s.get("id")] = s
            if s.get("type") == "Function":
                start = tokens.get(s.get("bodyStart"))
                if start is not None and os.path.basename(start.get("file") or "") == src.name:
                    fns[s.get("className")] = int(start.get("linenr"))
        fn_names = {sid: s.get("className") for sid, s in scopes.items() if s.get("type") == "Function"}

        def owner(scope_id):
            seen = 0
            while scope_id and seen < 50:
                if scope_id in fn_names:
                    return fn_names[scope_id]
                scope_id = scopes.get(scope_id).get("nestedIn") if scope_id in scopes else None
                seen += 1
            return None

        for i, t in enumerate(toklist):
            if t.get("isName") != "true" and not (t.get("str", "")[:1].isalpha() or t.get("str", "")[:1] == "_"):
                continue
            nxt = toklist[i + 1] if i + 1 < len(toklist) else None
            if nxt is None or nxt.get("str") != "(":
                continue
            if t.get("variable") or t.get("str") in ("if", "while", "for", "switch", "return", "sizeof"):
                continue
            prev = toklist[i - 1] if i > 0 else None
            if prev is not None and prev.get("str") in (".", "->"):
                continue
            fo = owner(t.get("scope"))
            if fo:
                calls.setdefault(fo, set()).add(t.get("str"))
        break  # first configuration only
    return fns, calls
