"""Supervised worker processes for extraction (design §08 "workers are processes; a crash costs one file").

Each worker runs `task_fn(payload)` for one file at a time. The supervisor enforces a per-task timeout, notices a
worker that died (e.g. a native crash inside libclang), kills/replaces it, and reports that task as failed instead of
failing the whole index. Cancellation kills all workers immediately."""
from __future__ import annotations

import multiprocessing as mp
import os
import time
import traceback
from typing import Any, Callable, Iterable, Iterator


def _worker_main(conn, init_fn: Callable, init_args: tuple, task_fn: Callable) -> None:
    try:
        # lower our priority so the UI and the engine stay responsive (design §08 priorities)
        os.nice(5)
    except Exception:
        pass
    try:
        init_fn(*init_args)
    except Exception:
        conn.send(("init_error", traceback.format_exc()))
        return
    conn.send(("ready", None))
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return
        if msg is None:
            return
        tid, payload = msg
        try:
            res = task_fn(payload)
            conn.send(("ok", (tid, res)))
        except MemoryError:
            conn.send(("err", (tid, "out of memory while processing this file")))
        except Exception as e:  # noqa: BLE001 — we report every failure per file
            conn.send(("err", (tid, f"{type(e).__name__}: {e}")))


class _Slot:
    def __init__(self, ctx, init_fn, init_args, task_fn):
        parent, child = ctx.Pipe()
        self.conn = parent
        self.proc = ctx.Process(target=_worker_main, args=(child, init_fn, init_args, task_fn), daemon=True)
        self.proc.start()
        child.close()
        self.task: tuple[int, Any] | None = None
        self.started = 0.0
        self.ready = False

    def kill(self) -> None:
        try:
            self.proc.kill()
        except Exception:
            pass
        try:
            self.proc.join(timeout=2)
        except Exception:
            pass
        try:
            self.conn.close()
        except Exception:
            pass


class WorkerPool:
    def __init__(self, init_fn: Callable, init_args: tuple, task_fn: Callable, workers: int | None = None,
                 timeout: float = 180.0):
        self.ctx = mp.get_context("spawn")
        self.init_fn, self.init_args, self.task_fn = init_fn, init_args, task_fn
        self.n = max(1, workers or max(1, (os.cpu_count() or 2) - 2))
        self.timeout = timeout
        self.slots: list[_Slot] = []
        self._startup_failures = 0

    def _spawn(self) -> _Slot:
        return _Slot(self.ctx, self.init_fn, self.init_args, self.task_fn)

    def run(self, payloads: Iterable[Any], cancelled: Callable[[], bool] = lambda: False
            ) -> Iterator[tuple[Any, bool, Any]]:
        """Yields (payload, ok, result-or-error-message) as tasks finish (unordered)."""
        queue = list(enumerate(payloads))
        queue.reverse()
        total = len(queue)
        done = 0
        payload_of = {i: p for i, p in queue}
        self.slots = [self._spawn() for _ in range(min(self.n, max(1, total)))]
        try:
            while done < total:
                if cancelled():
                    raise KeyboardInterrupt("cancelled")
                # hand out work
                for s in self.slots:
                    if s.ready and s.task is None and queue:
                        s.task = queue.pop()
                        s.started = time.monotonic()
                        try:
                            s.conn.send(s.task)
                        except (BrokenPipeError, OSError):
                            pass
                conns = [s.conn for s in self.slots]
                ready = mp.connection.wait(conns, timeout=0.2)
                for s in list(self.slots):
                    if s.conn in ready:
                        try:
                            kind, data = s.conn.recv()
                        except (EOFError, OSError):
                            kind, data = "died", None
                        if kind == "ready":
                            s.ready = True
                            continue
                        if kind == "init_error":
                            raise RuntimeError(f"worker failed to start: {data}")
                        if kind in ("ok", "err"):
                            tid, res = data
                            s.task = None
                            done += 1
                            self._startup_failures = 0
                            yield payload_of[tid], kind == "ok", res
                            continue
                        # died while holding a task (or before it became ready)
                        if not s.ready:
                            self._startup_failures += 1
                            if self._startup_failures >= 3:
                                raise RuntimeError("extraction worker processes fail to start")
                        self._replace(s)
                        if s.task is not None:
                            done += 1
                            yield payload_of[s.task[0]], False, "the parser process crashed on this file"
                        continue
                    if s.task is not None and time.monotonic() - s.started > self.timeout:
                        tid = s.task[0]
                        self._replace(s)
                        done += 1
                        yield payload_of[tid], False, f"timed out after {int(self.timeout)}s"
                    elif not s.proc.is_alive() and s.conn not in ready:
                        tid = s.task[0] if s.task else None
                        if not s.ready:
                            self._startup_failures += 1
                            if self._startup_failures >= 3:
                                raise RuntimeError("extraction worker processes fail to start")
                        self._replace(s)
                        if tid is not None:
                            done += 1
                            yield payload_of[tid], False, "the parser process exited unexpectedly"
        finally:
            self.close()

    def _replace(self, s: _Slot) -> None:
        s.kill()
        i = self.slots.index(s)
        self.slots[i] = self._spawn()

    def close(self) -> None:
        for s in self.slots:
            try:
                s.conn.send(None)
            except Exception:
                pass
        deadline = time.monotonic() + 1.0
        for s in self.slots:
            s.proc.join(timeout=max(0.0, deadline - time.monotonic()))
            if s.proc.is_alive():
                s.kill()
        self.slots = []
