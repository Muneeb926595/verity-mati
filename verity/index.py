"""The indexing pipeline (design §08/§13/§00): scan → build profile → extract C (supervised libclang workers, stub loop,
free-macro variants) → extract C# (Roslyn worker) → cross-check → link (derive) → search index.

Properties: incremental (content hashes + header dependencies), interruption-safe (each file's facts and its file row
commit in one transaction; derived layers are rebuilt every run), cancellable, and reports friendly progress."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from . import config, osutil
from .extract import recon
from .extract.pool import WorkerPool
from .store.db import FactStore

PHASES = [
    ("scan", "Looking at your project files"),
    ("plan", "Working out how the code is built"),
    ("c", "Reading the C code"),
    ("cs", "Reading the C# code"),
    ("check", "Double-checking with a second parser"),
    ("link", "Connecting the pieces together"),
    ("search", "Preparing search"),
]
MAX_VARIANT_MACROS = 4
ALL_FEATURES = "ALL_FEATURES"          # the variant build with every optional feature switched on
MAX_ALL_FEATURES_FILES = 600
IDENTITY = {
    "occ": ("sym", "file", "line", "col", "role"),
    "call": ("from", "to", "file", "line", "col", "via"),
    "flow": ("src", "dst", "file", "line", "kind"),
    "branch": ("fn", "file", "line", "kind"),
    "fnaddr": ("fn", "dst", "file", "line"),
}


class Cancelled(Exception):
    pass


class IndexBusy(Exception):
    pass


def open_store(root: str | os.PathLike) -> FactStore:
    d = config.repo_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    return FactStore(d / "index.db")


class Indexer:
    def __init__(self, root: str | os.PathLike, store: FactStore | None = None,
                 on_progress: Callable[[dict], None] | None = None, cancel: threading.Event | None = None,
                 embedder: Any = None, workers: int | None = None, file_timeout: float = 180.0):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(f"Folder not found: {self.root}")
        self.dir = config.repo_dir(self.root)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.work = self.dir / "work"
        self.store = store or FactStore(self.dir / "index.db")
        self.on_progress = on_progress or (lambda e: None)
        self.cancel = cancel or threading.Event()
        self.embedder = embedder
        self.workers = workers
        self.file_timeout = file_timeout
        self.t0 = time.monotonic()
        self.summary: dict[str, Any] = {"phases": {}, "warnings": []}
        self._lock_fh = None

    # ------------------------------------------------------------------------------------------ progress
    def emit(self, phase: str, state: str, detail: str = "", done: int | None = None, total: int | None = None,
             **extra: Any) -> None:
        idx = [p for p, _ in PHASES].index(phase)
        ev = {"type": "index.progress", "repo": str(self.root), "phase": phase, "label": dict(PHASES)[phase],
              "phase_index": idx + 1, "phase_total": len(PHASES), "state": state, "detail": detail,
              "done": done, "total": total, "elapsed_s": round(time.monotonic() - self.t0, 1)}
        if done is not None and total and done > 0 and state == "running":
            el = time.monotonic() - self._phase_t0
            ev["eta_s"] = round(el / done * (total - done), 1)
        ev.update(extra)
        self.on_progress(ev)

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Cancelled("indexing was cancelled")

    def _begin(self, phase: str, detail: str = "") -> None:
        self._phase_t0 = time.monotonic()
        self.emit(phase, "running", detail)

    def _end(self, phase: str, state: str = "done", detail: str = "", **extra: Any) -> None:
        self.summary["phases"][phase] = {"state": state, "detail": detail,
                                         "seconds": round(time.monotonic() - self._phase_t0, 2), **extra}
        self.emit(phase, state, detail, **extra)

    # ------------------------------------------------------------------------------------------ locking
    def _acquire(self) -> None:
        self._lock_fh = open(self.dir / "index.lock", "w")
        try:
            osutil.lock_exclusive(self._lock_fh)
        except OSError:
            self._lock_fh.close()
            self._lock_fh = None
            raise IndexBusy("This project is already being indexed by another Verity process.")

    def _release(self) -> None:
        if self._lock_fh:
            try:
                osutil.unlock(self._lock_fh)
                self._lock_fh.close()
            except Exception:
                pass
            self._lock_fh = None

    # ------------------------------------------------------------------------------------------ main
    def run(self, full: bool = False) -> dict[str, Any]:
        self._acquire()
        st = self.store
        prev_state = st.meta_get("index_state")
        if prev_state == "running":
            self.summary["warnings"].append("The previous indexing run was interrupted; resuming from where it stopped.")
        st.meta_set("index_state", "running")
        st.meta_set("repo_root", str(self.root))
        try:
            self._run(full)
            st.meta_set("index_state", "complete")
            st.meta_set("indexed_at", time.time())
            self.summary["state"] = "complete"
        except Cancelled:
            st.meta_set("index_state", "cancelled")
            self.summary["state"] = "cancelled"
            raise
        except BaseException:
            st.meta_set("index_state", "failed")
            self.summary["state"] = "failed"
            raise
        finally:
            self.summary["seconds"] = round(time.monotonic() - self.t0, 2)
            st.meta_set("last_summary", self.summary)
            self._release()
        return self.summary

    def _run(self, full: bool) -> None:
        st = self.store
        # 1. scan ------------------------------------------------------------------------------------
        self._begin("scan")
        scan = recon.scan_repo(self.root)
        self.scan = scan
        langs = scan.languages
        nfiles = len(scan.c_files) + len(scan.h_files) + len(scan.cs_files)
        if nfiles == 0:
            self._end("scan", "done", "No C or C# source files were found in this folder.")
            st.meta_set("languages", [])
            for p in ("plan", "c", "cs", "check", "link", "search"):
                self._phase_t0 = time.monotonic()
                self._end(p, "skipped", "nothing to do")
            self._purge_missing(set())
            return
        st.meta_set("languages", langs)
        st.meta_set("build_files", scan.build)
        if scan.too_large:
            self.summary["warnings"].append(
                f"{len(scan.too_large)} very large file(s) are only indexed by name (over {recon.MAX_FILE_BYTES // 1048576} MB).")
        current = set(scan.c_files + scan.h_files + scan.cs_files + scan.too_large)
        removed = self._purge_missing(current)
        self._end("scan", "done", f"Found {len(scan.c_files)} C, {len(scan.h_files)} header and "
                                  f"{len(scan.cs_files)} C# files" + (f"; {removed} removed since last time" if removed else ""),
                  files=nfiles, languages=langs)
        self.check_cancel()

        # 2. plan -----------------------------------------------------------------------------------
        self._begin("plan")
        hints = config.load_hints(self.root)
        profile = None
        if scan.c_files or scan.h_files:
            profile = recon.plan_c_profile(self.root, scan, self.work, hints)
            self.profile = profile
            prof_desc = {"packs": profile.packs, "label": profile.label, "capture": profile.capture,
                         "data_model": profile.data_model, "missing_headers": sorted(profile.unresolved_includes),
                         "free_macros": profile.free_macros, "defines": profile.defines, "knobs": profile.knobs,
                         "args": [a for a in profile.args if a.startswith("-f") or a.startswith("-std")],
                         "shim_dirs": profile.shim_dirs}
            st.meta_set("c_profile", prof_desc)
            ph = hashlib.sha1(json.dumps([profile.args, profile.include_dirs, profile.defines, profile.packs,
                                          sorted(profile.unresolved_includes), profile.capture,
                                          _code_version()], sort_keys=True).encode()).hexdigest()[:16]
            if st.meta_get("c_profile_hash") != ph:
                full = True
            self.profile_hash = ph
            how = {"compile_commands": "from the recorded build (compile_commands.json)",
                   "cmake": "from the CMake project", "project-file": "from the project file",
                   "reconstructed": "reconstructed from the source (no build needed)"}.get(profile.capture, profile.capture)
            detail = f"C: {profile.label}; build settings {how}"
            if profile.shim_dirs:
                detail += "; platform SDK headers not on this machine — Verity's API knowledge pack stands in for them"
            if profile.unresolved_includes:
                detail += f"; {len(profile.unresolved_includes)} missing header(s) replaced by stand-ins"
        else:
            detail = "No C code"
        st.meta_set("hints", hints)
        self._end("plan", "done", detail)
        self.check_cancel()

        # 3. C -------------------------------------------------------------------------------------
        if profile is not None:
            self._phase_c(profile, full)
            st.meta_set("c_profile_hash", self.profile_hash)
        else:
            self._phase_t0 = time.monotonic()
            self._end("c", "skipped", "No C code in this project")
        self.check_cancel()

        # 4. C# ------------------------------------------------------------------------------------
        if scan.cs_files:
            self._phase_cs(full)
        else:
            self._phase_t0 = time.monotonic()
            self._end("cs", "skipped", "No C# code in this project")
        self.check_cancel()

        # 5. cross-check -----------------------------------------------------------------------------
        self._phase_check()
        self.check_cancel()

        # 6. link ------------------------------------------------------------------------------------
        self._begin("link", "Resolving callbacks, tables and entry points")
        from .analysis import derive
        stats = derive.run(st, self.root)
        self._end("link", "done", derive.describe(stats), **{"stats": stats})
        self.check_cancel()

        # 7. search ----------------------------------------------------------------------------------
        self._begin("search", "Building the search index")
        from .retrieval import cards
        cstats = cards.build(st, embedder=self.embedder, cancel=self.cancel,
                             progress=lambda d, t: self.emit("search", "running", "Building the search index", d, t))
        self._end("search", "done", cards.describe(cstats), **{"stats": cstats})
        self._write_status()

    # ------------------------------------------------------------------------------------------ files
    def _purge_missing(self, current: set[str]) -> int:
        known = {r["path"] for r in self.store.q("SELECT path FROM file")}
        gone = sorted(known - current)
        if gone:
            self.store.delete_file_facts(gone)
        return len(gone)

    def _file_states(self) -> dict[str, dict]:
        return {r["path"]: dict(r) for r in self.store.q("SELECT path, sha, size, mtime, fidelity, profile FROM file")}

    def _sha(self, rel: str, prev: dict | None) -> tuple[str, int, float]:
        p = self.root / rel
        stt = p.stat()
        if prev and prev.get("size") == stt.st_size and abs((prev.get("mtime") or 0) - stt.st_mtime) < 1e-6 and prev.get("sha"):
            return prev["sha"], stt.st_size, stt.st_mtime
        return recon.sha256_file(p), stt.st_size, stt.st_mtime

    # ------------------------------------------------------------------------------------------ C phase
    def _phase_c(self, profile: recon.CProfile, full: bool) -> None:
        st = self.store
        scan = self.scan
        self._begin("c", "Checking what changed")
        prev = self._file_states()
        shas: dict[str, tuple[str, int, float]] = {}
        for rel in scan.c_files + scan.h_files:
            shas[rel] = self._sha(rel, prev.get(rel))
        changed_h = {h for h in scan.h_files if full or h not in prev or prev[h]["sha"] != shas[h][0]}
        # which headers each TU includes (from the previous run)
        inc: dict[str, set[str]] = {}
        for r in st.q("SELECT file, target FROM include_edge WHERE resolved=1"):
            inc.setdefault(r["file"], set()).add(r["target"])

        def reaches_changed(tu: str) -> bool:
            seen, stack = set(), [tu]
            while stack:
                f = stack.pop()
                for t in inc.get(f, ()):
                    if t in changed_h:
                        return True
                    if t not in seen:
                        seen.add(t)
                        stack.append(t)
            return False

        included = set()
        for f, ts in inc.items():
            included |= ts
        todo = []
        for rel in scan.c_files:
            p = prev.get(rel)
            if full or p is None or p["sha"] != shas[rel][0] or p.get("fidelity") == "F4" or reaches_changed(rel):
                todo.append(rel)
        # headers no .c file includes are parsed on their own so their declarations are still known
        orphan_h = []
        if full or not prev:
            included_now = set()
            for rel in scan.c_files:
                txt = recon.read_text(self.root / rel)
                for m in recon.INCLUDE_RE.finditer(txt):
                    included_now.add(os.path.basename(m.group(2)))
            for h in scan.h_files:
                if os.path.basename(h) not in included_now:
                    orphan_h.append(h)
        else:
            for h in changed_h:
                if h not in included:
                    orphan_h.append(h)
        # header facts are replaced wholesale when a header changes
        if changed_h:
            st.delete_file_facts(sorted(changed_h))
        if full:
            # headers: their facts are re-emitted by the TUs; .c files are replaced one by one as they finish,
            # so an interrupted full re-index never leaves the project without facts
            st.delete_file_facts(sorted(set(scan.h_files)))
            st.exec("DELETE FROM symbol WHERE file IS NULL AND is_def=0")   # external API declarations are re-learned
        tasks = sorted(set(todo)) + sorted(set(orphan_h))
        # free-macro variants (design §00.4): the few configuration macros guarding the most code
        variants = self._variant_plan(profile, tasks)
        vt = [(rel, cfg, defs) for m, cfg, rels, defs in variants for rel in rels]
        payloads = [(rel, "default", {}) for rel in tasks] + vt
        total = len(payloads)
        if total == 0:
            self._end("c", "done", "No C changes since the last index", files=0)
            return
        self.emit("c", "running", f"Reading {len(tasks)} C file(s)", 0, total)
        default_ids: dict[str, dict[str, set]] = {}
        variant_rels = {rel for _, _, rels, _ in variants for rel in rels}
        n_ok = n_fail = 0
        fid_counts: dict[str, int] = {}
        stub_names: dict[str, set[str]] = {}
        pool = WorkerPool(_c_init, (str(self.root), profile, set()), _c_task, workers=self.workers,
                          timeout=self.file_timeout)
        done = 0
        pending_variant: list[tuple[str, str, list[dict]]] = []

        def results():
            """Supervised worker processes; if they cannot start (restricted environment), extract in-process."""
            finished: set[int] = set()
            try:
                for i, item in enumerate(pool.run(payloads, cancelled=self.cancel.is_set)):
                    finished.add(id(item[0]))
                    yield item
            except RuntimeError as e:
                if "fail to start" not in str(e):
                    raise
                self.summary["warnings"].append("Parser worker processes could not be started here; files were read "
                                                "one at a time in the engine process instead.")
                _c_init(str(self.root), profile, set())
                for pl in payloads:
                    if id(pl) in finished:
                        continue
                    self.check_cancel()
                    try:
                        yield pl, True, _c_task(pl)
                    except Exception as ex:  # noqa: BLE001
                        yield pl, False, f"{type(ex).__name__}: {ex}"
        try:
            for (rel, cfg, _defs), ok, res in results():
                done += 1
                if cfg != "default":
                    if ok:
                        pending_variant.append((rel, cfg, res[1]))
                    self.emit("c", "running", f"Checking configuration {cfg} in {rel}", done, total, file=rel)
                    continue
                sha, size, mtime = shas.get(rel, ("", 0, 0.0))
                loc = _count_loc(self.root / rel)
                if ok:
                    stats, recs = res
                    fid = stats.get("fidelity", "F3") if stats.get("ok") else "F4"
                    if not stats.get("ok"):
                        recs = _fallback_records(self.root, rel)
                    elif stats.get("errors"):
                        recs += _supplement(recs, self.root, rel)
                    for n in stats.get("stubbed", []):
                        stub_names.setdefault(n, set()).add(rel)
                    recs.append({"t": "file", "path": rel, "lang": "c", "sha": sha, "loc": loc, "fidelity": fid,
                                 "errors": stats.get("errors", 0), "stubs": stats.get("stubbed", []),
                                 "profile": self.profile_hash, "size": size, "mtime": mtime})
                    if rel in variant_rels:
                        default_ids[rel] = _identities(recs)
                    n_ok += 1
                else:
                    recs = _fallback_records(self.root, rel)
                    fid = "F4"
                    recs.append({"t": "file", "path": rel, "lang": "c", "sha": sha, "loc": loc, "fidelity": "F4",
                                 "errors": 1, "stubs": [], "profile": self.profile_hash, "size": size, "mtime": mtime})
                    recs.append({"t": "diag", "file": rel, "line": 0, "severity": "error",
                                 "msg": f"Full analysis failed ({res}); only names were indexed for this file."})
                    self.summary["warnings"].append(f"{rel}: {res} — indexed by name only")
                    n_fail += 1
                fid_counts[fid] = fid_counts.get(fid, 0) + 1
                st.delete_file_facts([rel])
                st.insert_records(recs)
                self.emit("c", "running", f"Read {rel}", done, total, file=rel)
        except KeyboardInterrupt:
            raise Cancelled("indexing was cancelled")
        # headers: file rows (their facts were emitted by the TUs that include them)
        hrecs = []
        for h in scan.h_files:
            if h in tasks:
                continue
            if full or h in changed_h or h not in prev:
                sha, size, mtime = shas[h]
                hrecs.append({"t": "file", "path": h, "lang": "c", "sha": sha, "loc": _count_loc(self.root / h),
                              "fidelity": "F2", "errors": 0, "stubs": [], "profile": self.profile_hash,
                              "size": size, "mtime": mtime})
        if hrecs:
            st.insert_records(hrecs)
        # variant facts: +M for facts only present when M is defined, -M for facts that disappear
        vstats = self._apply_variants(pending_variant, default_ids)
        for n, files in stub_names.items():
            st.exec("INSERT OR REPLACE INTO stub_symbol VALUES(?,?,?)", (n, "stub", json.dumps(sorted(files))))
        for m, info in profile.free_macros.items():
            st.exec("INSERT OR REPLACE INTO freemacro VALUES(?,?,?,?)",
                    (m, info.get("lines", 0), json.dumps(info.get("files", [])), int(any(v[0] == m for v in variants))))
        detail = f"Read {n_ok + n_fail} C file(s)"
        if n_fail:
            detail += f"; {n_fail} could only be read partially"
        switches = [v[0] for v in variants if v[0] != ALL_FEATURES]
        if switches:
            detail += f"; also checked {len(switches)} configuration switch(es): " + ", ".join(switches)
        if any(v[0] == ALL_FEATURES for v in variants):
            detail += f"; also read the code with all {len(profile.knobs)} optional feature(s) switched on"
        self._end("c", "done", detail, files=n_ok + n_fail, failed=n_fail, fidelity=fid_counts, variants=vstats)

    def _variant_plan(self, profile: recon.CProfile, tasks: list[str]) -> list[tuple[str, str, list[str]]]:
        fm = profile.free_macros or {}
        ranked = sorted(fm.items(), key=lambda kv: -kv[1].get("lines", 0))[:MAX_VARIANT_MACROS]
        out = []
        c_files = set(self.scan.c_files)
        for m, info in ranked:
            files = set(info.get("files", []))
            rels = set()
            for f in files:
                if f in c_files:
                    rels.add(f)
                else:  # a header: every TU that includes it by name
                    base = os.path.basename(f)
                    for c in c_files:
                        if base in recon.read_text(self.root / c):
                            rels.add(c)
            rels &= set(tasks)
            if rels:
                out.append((m, f"+{m}", sorted(rels), {m: "1"}))
        # budget: variant passes re-parse whole files; on big projects keep them to about half the work of one index
        budget = max(24, len(tasks) // 2)
        kept, used = [], 0
        for m, cfg, rels, defs in sorted(out, key=lambda x: len(x[2])):
            if used + len(rels) <= budget:
                kept.append((m, cfg, rels, defs))
                used += len(rels)
            else:
                self.summary["warnings"].append(f"Configuration switch {m} was not analysed as a separate build (it affects "
                                                f"{len(rels)} files; budget {budget}).")
        # the all-features build: every overridable option that is off by default switched on, so code that only
        # exists in some product configurations (optional protocol features, INCLUDE_x APIs) is also known
        knobs = getattr(profile, "knobs", None) or {}
        if knobs:
            names = list(knobs)
            rels = sorted(c for c in tasks if c in c_files and any(n in recon.read_text(self.root / c) for n in names) or
                          c in c_files and _includes_any(self.root / c, knobs))
            if 0 < len(rels) <= MAX_ALL_FEATURES_FILES:
                kept.append((ALL_FEATURES, f"+{ALL_FEATURES}", rels, {n: k["value"] for n, k in knobs.items()}))
            elif rels:
                self.summary["warnings"].append(f"The all-features build was skipped ({len(rels)} files > "
                                                f"{MAX_ALL_FEATURES_FILES}).")
        return kept

    def _apply_variants(self, pending: list[tuple[str, str, list[dict]]], default_ids: dict[str, dict[str, set]]) -> dict:
        st = self.store
        stats: dict[str, dict[str, int]] = {}
        for rel, cfg, recs in pending:
            macro = cfg[1:]
            base = default_ids.get(rel)
            if base is None:
                continue
            vids = _identities(recs)
            only_v = []
            for r in recs:
                t = r.get("t")
                if t in IDENTITY:
                    ident = tuple(r.get(k) for k in IDENTITY[t])
                    if ident not in base.get(t, set()):
                        r = dict(r)
                        r["cfg"] = cfg
                        only_v.append(r)
                elif t == "symbol" and r.get("file") == rel:
                    only_v.append(dict(r, cfg=cfg))
            st.insert_records(only_v)
            n_minus = 0
            for t, ids in base.items():
                gone = ids - vids.get(t, set())
                table = {"occ": "occ", "call": "call", "flow": "flow", "branch": "branch", "fnaddr": "fnaddr"}[t]
                cols = {"occ": ("sym", "file", "line", "col", "role"),
                        "call": ("caller", "callee", "file", "line", "col", "via"),
                        "flow": ("src", "dst", "file", "line", "kind"),
                        "branch": ("fn", "file", "line", "kind"), "fnaddr": ("fn", "dst", "file", "line")}[t]
                if t == "fnaddr":
                    continue
                for ident in gone:
                    where = " AND ".join(f"{c} IS ?" for c in cols)
                    st.exec(f"UPDATE {table} SET cfg = CASE WHEN cfg='default' THEN ? ELSE cfg || ';' || ? END "
                            f"WHERE {where}", (f"-{macro}", f"-{macro}", *ident))
                    n_minus += 1
            s = stats.setdefault(macro, {"added": 0, "removed": 0})
            s["added"] += len(only_v)
            s["removed"] += n_minus
        return stats

    # ------------------------------------------------------------------------------------------ C# phase
    def _phase_cs(self, full: bool) -> None:
        from .extract import csharp
        st = self.store
        self._begin("cs", "Starting the C# compiler")
        prev = self._file_states()
        shas = {rel: self._sha(rel, prev.get(rel)) for rel in self.scan.cs_files}
        changed = [r for r in self.scan.cs_files if full or r not in prev or prev[r]["sha"] != shas[r][0]
                   or prev[r].get("fidelity") == "F4"]
        if not changed:
            self._end("cs", "done", "No C# changes since the last index", files=0)
            return
        # C# semantic analysis needs the whole compilation: re-run over all files, replace facts for all C# files.
        res = csharp.extract(self.root, self.scan, self.work, cancel=self.cancel,
                             progress=lambda d, t, msg: self.emit("cs", "running", msg, d, t))
        if res["records"] is None:
            raise Cancelled("indexing was cancelled")
        st.delete_file_facts(self.scan.cs_files)
        recs = res["records"]
        seen_files = {r["path"] for r in recs if r.get("t") == "file"}
        for rel in self.scan.cs_files:
            if rel not in seen_files:
                sha, size, mtime = shas[rel]
                recs.append({"t": "file", "path": rel, "lang": "csharp", "sha": sha, "loc": _count_loc(self.root / rel),
                             "fidelity": res.get("fidelity", "F4"), "errors": 0, "stubs": [], "profile": res.get("mode", ""),
                             "size": size, "mtime": mtime})
        for r in recs:
            if r.get("t") == "file":
                sha, size, mtime = shas.get(r["path"], (r.get("sha", ""), 0, 0.0))
                r.update(sha=sha, size=size, mtime=mtime)
        st.insert_records(recs)
        if res.get("warning"):
            self.summary["warnings"].append(res["warning"])
        self._end("cs", "done", res.get("detail", f"Read {len(self.scan.cs_files)} C# file(s)"),
                  files=len(self.scan.cs_files), mode=res.get("mode"))

    # ------------------------------------------------------------------------------------------ cross-check
    def _phase_check(self) -> None:
        from .extract import crosscheck
        self._begin("check")
        res = crosscheck.run(self.store, self.root, getattr(self, "profile", None), cancel=self.cancel,
                             progress=lambda d, t: self.emit("check", "running", "Comparing with cppcheck", d, t))
        self._end("check", res["state"], res["detail"], **{k: v for k, v in res.items() if k not in ("state", "detail")})

    def _write_status(self) -> None:
        st = self.store
        rows = st.q("SELECT fidelity, count(*) n, sum(loc) loc FROM file GROUP BY fidelity")
        st.meta_set("fidelity", {r["fidelity"]: {"files": r["n"], "loc": r["loc"] or 0} for r in rows})
        st.meta_set("counts", {t: st.one(f"SELECT count(*) n FROM {t}")["n"] for t in
                               ("file", "symbol", "call", "flow", "occ", "branch", "entry", "io")})


# ---------------------------------------------------------------------------------------------- helpers
def _code_version() -> str:
    """Extractor code version: re-index when the extractor itself changes."""
    h = hashlib.sha1()
    for f in ("c_clang.py", "recon.py"):
        try:
            h.update((Path(__file__).parent / "extract" / f).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:10]


def _includes_any(path: Path, knobs: dict) -> bool:
    """The file includes a project header that declares one of the options (e.g. CO_config.h)."""
    hdrs = {os.path.basename(k["where"].rsplit(":", 1)[0]) for k in knobs.values()}
    text = recon.read_text(path)
    return any(re.search(rf'#\s*include\s*[<"][^>"]*{re.escape(h)}[>"]', text) for h in hdrs)


def _count_loc(p: Path) -> int:
    try:
        with open(p, "rb") as f:
            return sum(1 for ln in f if ln.strip())
    except OSError:
        return 0


def _identities(recs: list[dict]) -> dict[str, set]:
    out: dict[str, set] = {}
    for r in recs:
        t = r.get("t")
        if t in IDENTITY:
            out.setdefault(t, set()).add(tuple(r.get(k) for k in IDENTITY[t]))
    return out


def _fallback_records(root: Path, rel: str) -> list[dict]:
    from .extract import ts_fallback
    try:
        return ts_fallback.extract_c(root, rel)
    except Exception as e:  # noqa: BLE001
        return [{"t": "diag", "file": rel, "line": 0, "severity": "error", "msg": f"fallback parser failed: {e}"}]


def _supplement(recs: list[dict], root: Path, rel: str) -> list[dict]:
    """A file with parse errors: add the function definitions (and their calls) that the syntax-only parser sees but
    the compiler front end lost during error recovery."""
    have = {r["name"] for r in recs if r.get("t") == "symbol" and r.get("kind") == "function" and r.get("file") == rel
            and r.get("is_def")}
    extra = []
    ts = _fallback_records(root, rel)
    lost = {r["key"] for r in ts if r.get("t") == "symbol" and r.get("kind") == "function" and r["name"] not in have}
    for r in ts:
        if (r.get("t") == "symbol" and r.get("key") in lost) or (r.get("t") == "call" and r.get("from") in lost):
            extra.append(r)
    return extra


def _c_init(root: str, profile, known):
    from .extract import c_clang
    c_clang.worker_init(root, profile, known)


def _c_task(payload):
    from .extract import c_clang
    return c_clang.worker_task(payload)
