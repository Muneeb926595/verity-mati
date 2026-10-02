"""Local model runtime (design §09): llama.cpp `llama-server` subprocesses bound to 127.0.0.1 with a random API key.
One chat server (the chosen ladder rung) and one embedding server. Everything is optional: when a model or the server
binary is missing, callers get `LLMUnavailable` and the system answers in Direct mode (C0)."""
from __future__ import annotations

import atexit
import json
import os
import secrets
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np

from .. import config, osutil


class LLMUnavailable(Exception):
    pass


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _post(url: str, body: dict, key: str, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:     # 127.0.0.1 only
        return json.loads(r.read().decode())


class LlamaServer:
    def __init__(self, model_file: str, embedding: bool = False, ctx: int = 16384, parallel: int = 1,
                 extra: list[str] | None = None, label: str = ""):
        self.model = config.MODELS / model_file
        self.embedding = embedding
        self.ctx = ctx
        self.parallel = parallel
        self.extra = extra or []
        self.label = label or model_file
        self.model_id = model_file
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.key = secrets.token_hex(16)
        self._lock = threading.Lock()
        self.log_path = config.HOME / "logs" / (("embed" if embedding else "chat") + "-server.log")
        self.last_used = 0.0
        self.tok_per_s: float | None = None

    @property
    def binary(self) -> Path:
        return osutil.exe(config.TOOLS / "llama.cpp" / "llama-server")

    def available(self) -> tuple[bool, str]:
        if not self.binary.exists():
            return False, "llama-server is not installed (run scripts/setup_toolchains.sh, or scripts\\windows\\setup.cmd on Windows)"
        if not self.model.exists() or (self.model.with_suffix(self.model.suffix + ".part")).exists():
            return False, f"model file {self.model.name} is missing (run scripts/download_models.sh, or scripts\\windows\\setup.cmd on Windows)"
        return True, "ok"

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, timeout: float = 240.0) -> None:
        with self._lock:
            if self.running():
                return
            ok, why = self.available()
            if not ok:
                raise LLMUnavailable(why)
            reap_orphans()
            self.port = _free_port()
            cmd = [str(self.binary), "-m", str(self.model), "--host", "127.0.0.1", "--port", str(self.port),
                   "--api-key", self.key, "-c", str(self.ctx), "-np", str(self.parallel), "-ngl", "999",
                   "--no-webui"]
            if self.embedding:
                cmd += ["--embedding", "--pooling", "last", "-ub", "2048", "-b", "2048"]
            else:
                cmd += ["--jinja", "-fa", "auto"]
            cmd += self.extra
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            env = dict(os.environ, LLAMA_ARG_NO_WEBUI="1")
            logf = open(self.log_path, "w")
            self.proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, env=env,
                                         cwd=str(self.binary.parent))
            _register(self)
            t0 = time.monotonic()
            while time.monotonic() - t0 < timeout:
                if self.proc.poll() is not None:
                    tail = ""
                    try:
                        tail = self.log_path.read_text()[-800:]
                    except OSError:
                        pass
                    self.proc = None
                    raise LLMUnavailable(f"the model server exited while loading {self.model.name}: {tail.strip()[-300:]}")
                try:
                    req = urllib.request.Request(f"http://127.0.0.1:{self.port}/health")
                    with urllib.request.urlopen(req, timeout=2) as r:
                        if r.status == 200 and json.loads(r.read().decode()).get("status") == "ok":
                            self.last_used = time.monotonic()
                            return
                except (urllib.error.URLError, ConnectionError, TimeoutError, json.JSONDecodeError, OSError):
                    pass
                time.sleep(0.5)
            self.stop()
            raise LLMUnavailable(f"the model server did not become ready within {int(timeout)}s")

    def stop(self) -> None:
        with self._lock:
            if self.proc is not None:
                _unregister(self.proc.pid)
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=10)
                except Exception:
                    try:
                        self.proc.kill()
                        self.proc.wait(timeout=5)
                    except Exception:
                        pass
                self.proc = None

    # -------------------------------------------------------------------------------------------- chat
    def chat(self, messages: list[dict], schema: dict | None = None, max_tokens: int = 1024, temperature: float = 0.1,
             timeout: float = 240.0, reasoning: str | None = "low") -> dict[str, Any]:
        """Returns {"text", "json" (if schema), "reasoning", "usage", "seconds"}."""
        self.start()
        body: dict[str, Any] = {"messages": messages, "max_tokens": max_tokens, "temperature": temperature,
                                "top_p": 0.9, "seed": 7, "cache_prompt": True}
        if schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema,
                                                                            "strict": True}}
        if reasoning:
            body["reasoning_effort"] = reasoning
            body["chat_template_kwargs"] = {"reasoning_effort": reasoning}
        t0 = time.monotonic()
        try:
            r = _post(f"http://127.0.0.1:{self.port}/v1/chat/completions", body, self.key, timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise LLMUnavailable(f"model request failed: HTTP {e.code} {detail}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LLMUnavailable(f"model request failed: {e}")
        dt = time.monotonic() - t0
        self.last_used = time.monotonic()
        msg = r["choices"][0]["message"]
        text = msg.get("content") or ""
        out: dict[str, Any] = {"text": text, "reasoning": msg.get("reasoning_content") or "", "usage": r.get("usage", {}),
                               "seconds": round(dt, 2), "finish": r["choices"][0].get("finish_reason")}
        comp = (r.get("usage") or {}).get("completion_tokens") or 0
        timings = r.get("timings") or {}
        if timings.get("predicted_per_second"):
            self.tok_per_s = round(timings["predicted_per_second"], 1)
        elif comp and dt:
            self.tok_per_s = round(comp / dt, 1)
        if schema is not None:
            out["json"] = _parse_json(text)
        return out

    # -------------------------------------------------------------------------------------------- embeddings
    def embed(self, texts: list[str], query: bool = False) -> list[np.ndarray]:
        self.start()
        if query:
            texts = [f"Instruct: Given a question about a codebase, retrieve the code symbols that answer it\nQuery: {t}"
                     for t in texts]
        texts = [t[:6000] for t in texts]
        try:
            r = _post(f"http://127.0.0.1:{self.port}/v1/embeddings", {"input": texts, "model": "embed"}, self.key, 120)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LLMUnavailable(f"embedding request failed: {e}")
        self.last_used = time.monotonic()
        data = sorted(r["data"], key=lambda d: d["index"])
        out = []
        for d in data:
            v = np.asarray(d["embedding"], dtype=np.float32)
            out.append(v / (np.linalg.norm(v) + 1e-9))
        return out


def _parse_json(text: str) -> Any:
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[t.find("{"):]
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        a, b = t.find("{"), t.rfind("}")
        if a >= 0 and b > a:
            try:
                return json.loads(t[a:b + 1])
            except json.JSONDecodeError:
                return None
    return None


# ------------------------------------------------------------------------------------------------ child lifetime
# A model server must never outlive the Verity process that started it (12 GB held by an orphan starves the next
# start: Metal "Insufficient Memory"). Each server is recorded in HOME/run/<pid>.json with its owner; servers whose
# owner is gone are stopped at the next start, and every server of this process is stopped at exit.
_RUN_DIR = config.HOME / "run"
_live: dict[int, "LlamaServer"] = {}


def _register(srv: "LlamaServer") -> None:
    if srv.proc is None:
        return
    _live[srv.proc.pid] = srv
    try:
        _RUN_DIR.mkdir(parents=True, exist_ok=True)
        (_RUN_DIR / f"{srv.proc.pid}.json").write_text(json.dumps({"pid": srv.proc.pid, "owner": os.getpid(),
                                                                   "model": srv.model.name}))
    except OSError:
        pass


def _unregister(pid: int) -> None:
    _live.pop(pid, None)
    try:
        (_RUN_DIR / f"{pid}.json").unlink(missing_ok=True)
    except OSError:
        pass


def _alive(pid: int) -> bool:
    return osutil.pid_alive(pid)            # never os.kill(pid, 0): on Windows that terminates the process


def reap_orphans() -> list[int]:
    """Stop model servers left behind by a Verity process that no longer exists."""
    reaped = []
    if not _RUN_DIR.exists():
        return reaped
    for f in _RUN_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text())
            pid, owner = int(d["pid"]), int(d["owner"])
        except (OSError, ValueError, KeyError):
            f.unlink(missing_ok=True)
            continue
        if _alive(owner) and owner != os.getpid():
            continue                                  # another running Verity owns it
        if owner == os.getpid() and pid in _live:
            continue
        if _alive(pid):
            cmd = osutil.process_command(pid)
            if "llama-server" in cmd:
                try:
                    osutil.terminate(pid)
                    for _ in range(20):
                        if not _alive(pid):
                            break
                        time.sleep(0.25)
                    if _alive(pid):
                        osutil.terminate(pid, force=True)
                    reaped.append(pid)
                except (OSError, subprocess.SubprocessError):
                    pass
        f.unlink(missing_ok=True)
    return reaped


@atexit.register
def _stop_all() -> None:
    for srv in list(_live.values()):
        try:
            srv.stop()
        except Exception:
            pass


class Runtime:
    """Holds the chat + embedding servers chosen by the probe. Lazy: nothing starts until first use."""

    def __init__(self, choice: dict[str, Any] | None = None, mode: str = "auto"):
        from ..probe import probe
        self.choice = choice or probe()["choice"]
        self.mode = mode                   # "auto" | "direct" (never use an LLM) | "fast" (small rung)
        self._chat: LlamaServer | None = None
        self._fast: LlamaServer | None = None
        self._embed: LlamaServer | None = None
        self.last_error: str | None = None

    def chat_server(self) -> LlamaServer | None:
        if self.mode == "direct":
            return None
        m = self.choice.get("fast") if self.mode == "fast" else (self.choice.get("main") or self.choice.get("fast"))
        if not m:
            return None
        if self._chat is None or self._chat.model_id != m["file"]:
            extra = [str(a) for a in (m.get("args") or []) if a is not None]      # e.g. ["--n-cpu-moe", "24"] chosen by the stress test
            self._chat = LlamaServer(m["file"], ctx=m.get("ctx", 16384), label=m["label"], extra=extra)
            self._chat.reasoning = m.get("reasoning", False)
        return self._chat

    def embed_server(self) -> LlamaServer | None:
        m = self.choice.get("embed")
        if not m:
            return None
        if self._embed is None:
            self._embed = LlamaServer(m["file"], embedding=True, ctx=8192, parallel=2, label=m["label"],
                                      extra=[str(a) for a in (m.get("args") or []) if a is not None])     # e.g. ["-ngl", "0"]: GPU is full
        return self._embed

    def status(self) -> dict[str, Any]:
        cs = self.chat_server()
        es = self.embed_server()
        return {"mode": self.mode, "chat": {"label": cs.label if cs else None, "running": bool(cs and cs.running()),
                                            "tok_per_s": cs.tok_per_s if cs else None},
                "embed": {"label": es.label if es else None, "running": bool(es and es.running())},
                "last_error": self.last_error}

    def stop(self) -> None:
        for s in (self._chat, self._fast, self._embed):
            if s is not None:
                s.stop()


class Embedder:
    """Adapter used by the indexer/searcher; `None`-safe construction via `Embedder.maybe(runtime)`."""

    def __init__(self, server: LlamaServer):
        self.server = server
        self.model_id = server.model_id

    @staticmethod
    def maybe(rt: Runtime | None) -> "Embedder | None":
        if rt is None:
            return None
        s = rt.embed_server()
        if s is None or not s.available()[0]:
            return None
        return Embedder(s)

    def embed(self, texts: list[str], query: bool = False) -> list[np.ndarray]:
        return self.server.embed(texts, query=query)
