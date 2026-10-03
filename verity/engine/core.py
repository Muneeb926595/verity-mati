"""EngineCore (design §08): the single local engine every client uses — desktop app and browser (over HTTP),
CLI and MCP (in-process). It owns projects, the job queue (index / ask / scenario), cancellation, progress events,
the model runtime, and the safety rules for reading files."""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import __version__, config
from ..agent.answer import Answerer
from ..index import Cancelled, IndexBusy, Indexer
from ..learn.service import Learner
from ..learn.store import LearnError
from ..store.db import FactStore


class EngineError(Exception):
    def __init__(self, message: str, code: int = -32000, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


@dataclass
class Job:
    id: str
    kind: str
    project: str
    params: dict
    state: str = "queued"        # queued running done failed cancelled
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    progress: dict | None = None
    result: Any = None
    error: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event)

    def public(self, with_result: bool = False) -> dict:
        d = {"id": self.id, "kind": self.kind, "project": self.project, "state": self.state, "created": self.created,
             "started": self.started, "finished": self.finished, "progress": self.progress, "error": self.error,
             "params": {k: v for k, v in self.params.items() if k != "path"}}
        if with_result:
            d["result"] = self.result
        return d


class Project:
    def __init__(self, root: Path):
        self.root = root
        self.id = config.repo_id(root)
        self.dir = config.repo_dir(root)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.store = FactStore(self.dir / "index.db")
        self._answerer: Answerer | None = None
        self._learner: Learner | None = None
        self.lock = threading.RLock()

    def learner(self) -> Learner:
        """This project's corrections and glossary (learned.db next to the index, never inside it)."""
        with self.lock:
            if self._learner is None:
                self._learner = Learner(self.store, self.root, self.dir)
            return self._learner

    def answerer(self, runtime, embedder) -> Answerer:
        with self.lock:
            if self._answerer is None:
                from ..runtime_exec.harness import Runner
                try:
                    learner = self.learner()
                except Exception:  # noqa: BLE001 — corrections that cannot be opened must never stop an answer
                    learner = None
                self._answerer = Answerer(self.store, self.root, runtime=runtime, runner=Runner(self.store, self.root),
                                          embedder=embedder, learner=learner)
            return self._answerer


class EngineCore:
    def __init__(self, llm_mode: str | None = None, embeddings: bool | None = None, workers: int | None = None):
        config.HOME.mkdir(parents=True, exist_ok=True)
        self.settings_path = config.HOME / "settings.json"
        self.settings = self._load_settings()
        # per-process overrides (CLI flags, benchmark) are NOT saved into the user's persistent settings
        self.overrides: dict[str, Any] = {}
        if llm_mode:
            self.overrides["llm_mode"] = llm_mode
        if embeddings is not None:
            self.overrides["embeddings"] = embeddings
        self.projects: dict[str, Project] = {}
        self.jobs: dict[str, Job] = {}
        self.subscribers: list[queue.Queue] = []
        self._sub_lock = threading.Lock()
        self._lock = threading.RLock()
        self.index_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vr-index")
        self.ask_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vr-ask")
        self.workers = workers
        self._runtime = None
        self.started = time.time()

    # ----------------------------------------------------------------------------------------- settings
    def _load_settings(self) -> dict:
        d = {"llm_mode": "auto", "embeddings": True, "recent": []}
        try:
            d.update(json.loads(self.settings_path.read_text()))
        except Exception:
            pass
        return d

    def opt(self, k: str, default: Any = None) -> Any:
        return self.overrides.get(k, self.settings.get(k, default))

    def _save_settings(self) -> None:
        try:
            tmp = self.settings_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.settings, indent=1))
            os.replace(tmp, self.settings_path)
        except OSError:
            pass

    # ----------------------------------------------------------------------------------------- runtime
    def runtime(self):
        if self._runtime is None:
            from ..llm.runtime import Runtime
            self._runtime = Runtime(mode=self.opt("llm_mode", "auto"))
        self._runtime.mode = self.opt("llm_mode", "auto")
        return self._runtime

    def embedder(self):
        if not self.opt("embeddings", True):
            return None
        from ..llm.runtime import Embedder
        return Embedder.maybe(self.runtime())

    # ----------------------------------------------------------------------------------------- events
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self._sub_lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._sub_lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def emit(self, ev: dict) -> None:
        ev.setdefault("ts", time.time())
        with self._sub_lock:
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass

    # ----------------------------------------------------------------------------------------- projects
    def _root(self, path: str) -> Path:
        if not path or not isinstance(path, str):
            raise EngineError("A folder path is required.", -32602)
        p = Path(os.path.expanduser(path)).resolve()
        if not p.exists():
            raise EngineError(f"Folder not found: {p}", -32602)
        if not p.is_dir():
            raise EngineError(f"Not a folder: {p}", -32602)
        return p

    def project(self, path: str) -> Project:
        root = self._root(path)
        key = str(root)
        with self._lock:
            if key not in self.projects:
                self.projects[key] = Project(root)
            return self.projects[key]

    def _remember(self, root: Path) -> None:
        rec = [r for r in self.settings.get("recent", []) if r.get("path") != str(root)]
        rec.insert(0, {"path": str(root), "name": root.name, "opened": time.time()})
        self.settings["recent"] = rec[:20]
        self._save_settings()

    def project_status(self, path: str) -> dict:
        pr = self.project(path)
        st = pr.store
        state = st.meta_get("index_state")
        summary = st.meta_get("last_summary") or {}
        counts = st.meta_get("counts") or {}
        running = [j.public() for j in self.jobs.values() if j.project == str(pr.root) and j.state in ("queued", "running")]
        stale = self._stale_files(pr) if state == "complete" else []
        langs = st.meta_get("languages") or []
        fidelity = st.meta_get("fidelity") or {}
        if running:
            label = "Indexing…"
        elif state == "complete":
            label = "Ready" if not stale else f"Ready — {len(stale)} file(s) changed since the last index"
        elif state in ("running",):
            label = "Indexing was interrupted — press Update to finish"
        elif state == "cancelled":
            label = "Indexing was cancelled — press Update to finish"
        elif state == "failed":
            label = "Indexing failed — see details"
        else:
            label = "Not indexed yet"
        return {"path": str(pr.root), "name": pr.root.name, "id": pr.id, "state": state or "new", "label": label,
                "ready": state == "complete" or (state in ("running", "cancelled") and counts.get("symbol")),
                "languages": langs, "counts": counts, "fidelity": fidelity, "stale": stale[:50],
                "indexed_at": st.meta_get("indexed_at"), "summary": summary, "jobs": running,
                "profile": st.meta_get("c_profile"), "crosscheck": {k: v for k, v in (st.meta_get("crosscheck") or {}).items()
                                                                  if k != "files"}}

    def _stale_files(self, pr: Project, limit: int = 2000) -> list[str]:
        rows = pr.store.q("SELECT path, size, mtime FROM file")
        out = []
        for r in rows[:limit]:
            p = pr.root / r["path"]
            try:
                stt = p.stat()
            except OSError:
                out.append(r["path"])
                continue
            if (r["size"] and stt.st_size != r["size"]) or (r["mtime"] and abs(stt.st_mtime - r["mtime"]) > 1e-6):
                out.append(r["path"])
        return out

    # ----------------------------------------------------------------------------------------- jobs
    def _new_job(self, kind: str, project: Project, params: dict) -> Job:
        j = Job(uuid.uuid4().hex[:12], kind, str(project.root), params)
        with self._lock:
            self.jobs[j.id] = j
            # keep the table small
            if len(self.jobs) > 200:
                for old in sorted(self.jobs.values(), key=lambda x: x.created)[:50]:
                    if old.state in ("done", "failed", "cancelled"):
                        self.jobs.pop(old.id, None)
        self.emit({"type": "job.queued", "job": j.public()})
        return j

    def _finish(self, j: Job, state: str, result: Any = None, error: str | None = None) -> None:
        j.state = state
        j.finished = time.time()
        j.result = result
        j.error = error
        self.emit({"type": f"job.{state}", "job": j.public(with_result=j.kind != "index")})

    def start_index(self, path: str, full: bool = False) -> dict:
        pr = self.project(path)
        for j in self.jobs.values():
            if j.kind == "index" and j.project == str(pr.root) and j.state in ("queued", "running"):
                return j.public()
        j = self._new_job("index", pr, {"full": full})
        self._remember(pr.root)
        self.index_pool.submit(self._run_index, j, pr, full)
        return j.public()

    def _run_index(self, j: Job, pr: Project, full: bool) -> None:
        if j.cancel.is_set():
            return self._finish(j, "cancelled")
        j.state = "running"
        j.started = time.time()

        def prog(ev: dict) -> None:
            j.progress = ev
            self.emit({"type": "index.progress", "job": j.id, "project": str(pr.root), **ev})
        try:
            store = FactStore(pr.dir / "index.db")      # the indexer writes through its own connection
            try:
                ix = Indexer(pr.root, store=store, on_progress=prog, cancel=j.cancel, embedder=self.embedder(),
                             workers=self.workers)
                summary = ix.run(full=full)
            finally:
                store.close()
            with pr.lock:
                pr._answerer = None
            try:
                moved = pr.learner().refresh()          # corrections follow their code, or turn stale
                if any(moved.values()):
                    summary = dict(summary, learning=moved)
            except Exception:  # noqa: BLE001 — learning must never fail an index
                pass
            self._finish(j, "done", summary)
        except Cancelled:
            self._finish(j, "cancelled")
        except IndexBusy as e:
            self._finish(j, "failed", error=str(e))
        except Exception as e:  # noqa: BLE001
            self._finish(j, "failed", error=f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}")

    def cancel(self, job_id: str) -> dict:
        j = self.jobs.get(job_id)
        if not j:
            raise EngineError("No such job.", -32602)
        j.cancel.set()
        self.emit({"type": "job.cancelling", "job": j.public()})
        return j.public()

    def job(self, job_id: str, with_result: bool = True) -> dict:
        j = self.jobs.get(job_id)
        if not j:
            raise EngineError("No such job.", -32602)
        return j.public(with_result=with_result)

    def wait(self, job_id: str, timeout: float | None = None) -> dict:
        t0 = time.time()
        while True:
            j = self.jobs.get(job_id)
            if j is None:
                raise EngineError("No such job.", -32602)
            if j.state in ("done", "failed", "cancelled"):
                return j.public(with_result=True)
            if timeout is not None and time.time() - t0 > timeout:
                return j.public()
            time.sleep(0.05)

    # ----------------------------------------------------------------------------------------- ask
    def start_ask(self, path: str, question: str, use_llm: bool | None = None) -> dict:
        pr = self.project(path)
        if not (question or "").strip():
            raise EngineError("Please type a question.", -32602)
        st = pr.store.meta_get("index_state")
        if not pr.store.one("SELECT 1 FROM symbol LIMIT 1"):
            raise EngineError("This project has not been indexed yet. Open it and wait until it says Ready.", -32001)
        j = self._new_job("ask", pr, {"question": question[:2000], "use_llm": use_llm})
        self.ask_pool.submit(self._run_ask, j, pr, question, use_llm)
        return j.public()

    def _run_ask(self, j: Job, pr: Project, question: str, use_llm: bool | None) -> None:
        if j.cancel.is_set():
            return self._finish(j, "cancelled")
        j.state = "running"
        j.started = time.time()

        def prog(ev: dict) -> None:
            j.progress = ev
            self.emit({"type": "ask.progress", "job": j.id, "project": str(pr.root), **ev})
        try:
            mode = self.opt("llm_mode", "auto")
            ul = (mode != "direct") if use_llm is None else bool(use_llm)
            ans = pr.answerer(self.runtime(), self.embedder_for_search()).ask(question, on_progress=prog, cancel=j.cancel,
                                                                              use_llm=ul)
            self._finish(j, "done", ans)
        except InterruptedError:
            self._finish(j, "cancelled")
        except Exception as e:  # noqa: BLE001
            self._finish(j, "failed", error=f"{type(e).__name__}: {e}")

    def embedder_for_search(self):
        # query embeddings only help if the index has vectors
        return self.embedder()

    def ask(self, path: str, question: str, use_llm: bool | None = None, timeout: float = 600) -> dict:
        j = self.start_ask(path, question, use_llm)
        r = self.wait(j["id"], timeout)
        if r["state"] == "failed":
            raise EngineError(r["error"] or "The question could not be answered.")
        return r

    # ----------------------------------------------------------------------------------------- queries
    def search(self, path: str, query: str, k: int = 20) -> list[dict]:
        pr = self.project(path)
        from ..retrieval.search import Searcher
        return [h.as_dict() for h in Searcher(pr.store, self.embedder()).search(query, k=int(k))]

    def symbol(self, path: str, key: str | None = None, name: str | None = None) -> dict:
        pr = self.project(path)
        g = pr.answerer(self.runtime(), None).graph()
        if not key and name:
            ks = [k for k in g.by_name.get(name, []) if g.sym[k].get("file")]
            if not ks:
                raise EngineError(f"No symbol named {name}.", -32602)
            ks.sort(key=lambda k: (0 if g.sym[k]["is_def"] else 1))
            key = ks[0]
        s = g.sym.get(key or "")
        if not s:
            raise EngineError("Unknown symbol.", -32602)
        card = pr.store.one("SELECT text FROM card WHERE sym=?", (key,))
        out = dict(s)
        out["card"] = card["text"] if card else None
        out["callers"] = [{"caller": c.caller, "name": g.display(c.caller), "file": c.file, "line": c.line, "res": c.res}
                          for c in g.callers(key)][:100]
        out["callees"] = [{"callee": c.callee, "name": g.display(c.callee), "file": c.file, "line": c.line, "res": c.res}
                          for c in g.callees(key)][:100]
        out["writers"] = [{"fn": g.display(o["fn"]) if o["fn"] else None, "file": o["file"], "line": o["line"]}
                          for o in g.writers(key)][:100]
        out["readers"] = [{"fn": g.display(o["fn"]) if o["fn"] else None, "file": o["file"], "line": o["line"]}
                          for o in g.readers(key)][:100]
        return out

    def source(self, path: str, file: str, start: int = 1, end: int | None = None, around: int | None = None) -> dict:
        pr = self.project(path)
        p = (pr.root / file).resolve()
        try:
            p.relative_to(pr.root)
        except ValueError:
            raise EngineError("That file is outside the project.", -32602)
        if not p.is_file():
            raise EngineError("File not found.", -32602)
        if p.stat().st_size > 8 * 1024 * 1024:
            raise EngineError("File is too large to display.", -32602)
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.split("\n")
        n = len(lines)
        if around:
            start, end = max(1, around - 40), min(n, around + 40)
        end = min(n, end or n)
        start = max(1, min(start, n))
        idx = pr.store.one("SELECT sha FROM file WHERE path=?", (file,))
        import hashlib
        stale = bool(idx and idx["sha"] and hashlib.sha256(p.read_bytes()).hexdigest() != idx["sha"])
        return {"file": file, "start": start, "end": end, "total": n, "lines": lines[start - 1:end], "stale": stale,
                "lang": "csharp" if file.endswith(".cs") else "c"}

    def callers(self, path: str, key: str) -> list[dict]:
        return self.symbol(path, key)["callers"]

    def callees(self, path: str, key: str) -> list[dict]:
        return self.symbol(path, key)["callees"]

    def history(self, path: str, limit: int = 30) -> list[dict]:
        pr = self.project(path)
        return [{"id": r["id"], "question": r["question"], "created": r["created"]}
                for r in pr.store.q("SELECT id, question, created FROM answer ORDER BY created DESC LIMIT ?", (limit,))]

    def answer_get(self, path: str, aid: str) -> dict:
        pr = self.project(path)
        r = pr.store.one("SELECT result FROM answer WHERE id=?", (aid,))
        if not r:
            raise EngineError("No such answer.", -32602)
        return json.loads(r["result"])

    def recent(self) -> list[dict]:
        out = []
        for r in self.settings.get("recent", []):
            p = Path(r["path"])
            d = {"path": r["path"], "name": r.get("name") or p.name, "opened": r.get("opened"), "exists": p.is_dir()}
            if p.is_dir():
                try:
                    st = self.project_status(r["path"])
                    d.update(label=st["label"], ready=st["ready"], languages=st["languages"])
                except Exception:
                    pass
            out.append(d)
        return out

    def forget(self, path: str) -> dict:
        self.settings["recent"] = [r for r in self.settings.get("recent", []) if r.get("path") != path]
        self._save_settings()
        return {"ok": True}

    def info(self) -> dict:
        from ..probe import describe, probe
        p = probe()
        rt = self.runtime()
        return {"version": __version__, "home": str(config.HOME), "uptime_s": round(time.time() - self.started, 1),
                "probe": p, "machine": describe(p), "llm": rt.status(),
                "settings": {**{k: v for k, v in self.settings.items() if k != "recent"}, **self.overrides},
                "offline": True}

    def set_settings(self, **kw) -> dict:
        for k in ("llm_mode", "embeddings"):
            if k in kw and kw[k] is not None:
                if k == "llm_mode" and kw[k] not in ("auto", "direct", "fast"):
                    raise EngineError("llm_mode must be auto, direct or fast", -32602)
                self.settings[k] = kw[k]
                self.overrides.pop(k, None)
        self._save_settings()
        return {k: v for k, v in self.settings.items() if k != "recent"}

    # ----------------------------------------------------------------------------------------- learning
    def _embed_fn(self):
        """The question-meaning function for new corrections: only when the embedding model is already running."""
        rt = self._runtime
        if rt is None or not self.opt("embeddings", True):
            return None
        try:
            if not (rt.status().get("embed") or {}).get("running"):
                return None
            emb = self.embedder()
        except Exception:  # noqa: BLE001
            return None
        if emb is None:
            return None
        return lambda q: [float(x) for x in emb.embed([q], query=True)[0]]

    def answer_correct(self, path: str, answer_id: str, pick: int | None = None, target: dict | None = None,
                       note: str = "", author: str | None = None) -> dict:
        lr = self.project(path).learner()
        r = lr.correct(answer_id, pick=pick, target=target, note=note,
                       author=author or os.environ.get("USER") or os.environ.get("USERNAME") or "", embed=self._embed_fn())
        self.emit({"type": "learning.changed", "project": str(self.project(path).root)})
        return r

    def answer_confirm(self, path: str, answer_id: str, note: str = "") -> dict:
        r = self.project(path).learner().confirm(answer_id, note=note)
        self.emit({"type": "learning.changed", "project": str(self.project(path).root)})
        return r

    def corrections_summary(self, path: str) -> dict:
        lst = self.project(path).learner().listing()
        return {"counts": lst["counts"], "recovered_from": lst["recovered_from"]}

    def corrections_export(self, path: str, format: str = "json", save: bool = False) -> dict:
        r = self.project(path).learner().export(format)
        if save:
            # the desktop app's web view cannot download files: write the export into the Downloads folder
            folder = Path.home() / "Downloads"
            if not folder.is_dir():
                folder = config.HOME / "exports"
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / r["filename"]
            n = 1
            while dest.exists():
                n += 1
                dest = folder / f"{Path(r['filename']).stem}-{n}{Path(r['filename']).suffix}"
            text = r["data"] if isinstance(r["data"], str) else json.dumps(r["data"], indent=1, default=str)
            dest.write_text(text, encoding="utf-8")
            r = {"format": r["format"], "filename": dest.name, "saved": str(dest)}
        return r

    def _learn_changed(self, path: str, result: Any) -> Any:
        self.emit({"type": "learning.changed", "project": str(self.project(path).root)})
        return result

    def shutdown(self) -> None:
        for j in list(self.jobs.values()):
            j.cancel.set()
        self.index_pool.shutdown(wait=False, cancel_futures=True)
        self.ask_pool.shutdown(wait=False, cancel_futures=True)
        if self._runtime is not None:
            self._runtime.stop()
        for pr in self.projects.values():
            if pr._learner is not None:
                pr._learner.close()
            pr.store.close()

    # ----------------------------------------------------------------------------------------- dispatch
    def call(self, method: str, params: dict | None) -> Any:
        params = params or {}
        m = METHODS.get(method)
        if m is None:
            raise EngineError(f"Unknown method {method}", -32601)
        try:
            return m(self, **params)
        except LearnError as e:
            raise EngineError(str(e), -32602)
        except TypeError as e:
            raise EngineError(f"Invalid parameters for {method}: {e}", -32602)


METHODS: dict[str, Callable[..., Any]] = {
    "engine.info": lambda e: e.info(),
    "settings.set": lambda e, **kw: e.set_settings(**kw),
    "project.recent": lambda e: e.recent(),
    "project.open": lambda e, path: (e._remember(e.project(path).root), e.project_status(path))[1],
    "project.status": lambda e, path: e.project_status(path),
    "project.forget": lambda e, path: e.forget(path),
    "index.start": lambda e, path, full=False: e.start_index(path, bool(full)),
    "job.cancel": lambda e, job: e.cancel(job),
    "job.get": lambda e, job, with_result=True: e.job(job, with_result),
    "job.list": lambda e: [j.public() for j in sorted(e.jobs.values(), key=lambda j: -j.created)][:50],
    "ask": lambda e, path, question, use_llm=None: e.start_ask(path, question, use_llm),
    "ask.wait": lambda e, path, question, use_llm=None, timeout=600: e.ask(path, question, use_llm, timeout),
    "search": lambda e, path, query, k=20: e.search(path, query, k),
    "symbol.get": lambda e, path, key=None, name=None: e.symbol(path, key, name),
    "source.get": lambda e, path, file, start=1, end=None, around=None: e.source(path, file, start, end, around),
    "callers": lambda e, path, key: e.callers(path, key),
    "callees": lambda e, path, key: e.callees(path, key),
    "history.list": lambda e, path, limit=30: e.history(path, limit),
    "answer.get": lambda e, path, id: e.answer_get(path, id),
    # learning from corrections (verity/learn): per project, stored in learned.db next to the index
    "answer.correct": lambda e, path, answer_id, pick=None, target=None, note="", author=None:
        e.answer_correct(path, answer_id, pick, target, note, author),
    "answer.confirm": lambda e, path, answer_id, note="": e.answer_confirm(path, answer_id, note),
    "answer.unconfirm": lambda e, path, id: e._learn_changed(path, e.project(path).learner().remove_confirmation(id)),
    "corrections.list": lambda e, path, status=None: e.project(path).learner().listing(status),
    "corrections.summary": lambda e, path: e.corrections_summary(path),
    "corrections.remove": lambda e, path, id: e._learn_changed(path, e.project(path).learner().remove(id)),
    "corrections.restore": lambda e, path, id: e._learn_changed(path, e.project(path).learner().restore(id)),
    "corrections.note": lambda e, path, id, note="": e._learn_changed(path, e.project(path).learner().set_note(id, note)),
    "corrections.export": lambda e, path, format="json", save=False: e.corrections_export(path, format, bool(save)),
    "corrections.import": lambda e, path, data: e._learn_changed(path, e.project(path).learner().import_(data)),
    "glossary.list": lambda e, path: e.project(path).learner().glossary_list(),
    "glossary.accept": lambda e, path, term, identifiers=None:
        e._learn_changed(path, e.project(path).learner().glossary_accept(term, identifiers)),
    "glossary.reject": lambda e, path, term: e._learn_changed(path, e.project(path).learner().glossary_reject(term)),
    "glossary.add": lambda e, path, term, identifiers: e._learn_changed(path, e.project(path).learner().glossary_add(term, identifiers)),
    "glossary.remove": lambda e, path, term: e._learn_changed(path, e.project(path).learner().glossary_remove(term)),
}
