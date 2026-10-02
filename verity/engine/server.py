"""Local engine daemon (design §08, D-impl-3): JSON-RPC 2.0 over HTTP + Server-Sent Events, bound to 127.0.0.1 only,
protected by a per-session bearer token. The desktop app starts it and passes the token to its webview; browser mode
opens http://127.0.0.1:<port>/?token=<token>. Serves the UI from ui/dist.

  python -m verity.engine.server [--port 0] [--token T] [--print-ready]
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import queue
import secrets
import signal
import sys
import threading
from pathlib import Path

from aiohttp import web

from .. import config
from .core import EngineCore, EngineError

UI_DIR = config.ROOT / "ui" / "dist"
STOPPING = web.AppKey("stopping", dict)


def make_app(core: EngineCore, token: str) -> web.Application:
    app = web.Application(client_max_size=4 * 1024 * 1024)
    app[STOPPING] = {"v": False}          # mutable holder: app state must not be reassigned after start

    def authed(req: web.Request) -> bool:
        h = req.headers.get("Authorization", "")
        t = h[7:] if h.startswith("Bearer ") else req.query.get("token", "")
        return bool(t) and hmac.compare_digest(t, token)

    @web.middleware
    async def security(req: web.Request, handler):
        # only local clients; no cross-origin access from web pages the user visits
        peer = req.remote or ""
        if peer not in ("127.0.0.1", "::1", "localhost"):
            return web.Response(status=403, text="local access only")
        origin = req.headers.get("Origin")
        if origin and not (origin.startswith("http://127.0.0.1") or origin.startswith("http://localhost") or
                           origin.startswith("tauri://") or origin.startswith("http://tauri.localhost") or
                           origin.startswith("https://tauri.localhost")):
            return web.Response(status=403, text="cross-origin requests are not allowed")
        resp = await handler(req)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    app.middlewares.append(security)

    async def rpc(req: web.Request) -> web.Response:
        if not authed(req):
            return web.json_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32003, "message": "unauthorized"}},
                                     status=401)
        try:
            body = await req.json()
        except Exception:
            return web.json_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
        batch = body if isinstance(body, list) else [body]
        out = []
        for msg in batch:
            mid = msg.get("id") if isinstance(msg, dict) else None
            if not isinstance(msg, dict) or not isinstance(msg.get("method"), str):
                out.append({"jsonrpc": "2.0", "id": mid, "error": {"code": -32600, "message": "invalid request"}})
                continue
            params = msg.get("params") or {}
            if not isinstance(params, dict):
                out.append({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "params must be an object"}})
                continue
            try:
                res = await asyncio.get_running_loop().run_in_executor(None, core.call, msg["method"], params)
                out.append({"jsonrpc": "2.0", "id": mid, "result": res})
            except EngineError as e:
                out.append({"jsonrpc": "2.0", "id": mid, "error": {"code": e.code, "message": str(e), "data": e.data}})
            except Exception as e:  # noqa: BLE001
                out.append({"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": f"{type(e).__name__}: {e}"}})
        return web.json_response(out if isinstance(body, list) else out[0], dumps=lambda o: json.dumps(o, default=str))

    async def events(req: web.Request) -> web.StreamResponse:
        if not authed(req):
            return web.Response(status=401, text="unauthorized")
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                           "X-Accel-Buffering": "no"})
        await resp.prepare(req)
        q = core.subscribe()
        loop = asyncio.get_running_loop()
        try:
            await resp.write(b": connected\n\n")
            while True:
                try:
                    ev = await loop.run_in_executor(None, q.get, True, 2.0)
                    data = json.dumps(ev, default=str)
                    await resp.write(f"event: {ev.get('type', 'message')}\ndata: {data}\n\n".encode())
                except queue.Empty:
                    if req.app[STOPPING]["v"]:
                        break
                    await resp.write(b": keep-alive\n\n")
        except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
            pass
        finally:
            core.unsubscribe(q)
        return resp

    async def health(req: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def index_html(req: web.Request) -> web.Response:
        f = UI_DIR / "index.html"
        if not f.exists():
            return web.Response(text="The UI has not been built. Run: bash scripts/build_ui.sh", content_type="text/plain")
        return web.FileResponse(f)

    app.router.add_post("/rpc", rpc)
    app.router.add_get("/events", events)
    app.router.add_get("/health", health)
    app.router.add_get("/", index_html)
    if UI_DIR.exists() and (UI_DIR / "assets").exists():
        app.router.add_static("/assets", UI_DIR / "assets")
    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Verity local engine")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--token", default=os.environ.get("VERITY_TOKEN") or secrets.token_urlsafe(24))
    ap.add_argument("--print-ready", action="store_true", help="print a JSON line with port+token once listening")
    ap.add_argument("--open", action="store_true", help="open the browser UI")
    ap.add_argument("--llm", choices=["auto", "direct", "fast"], default=None)
    a = ap.parse_args(argv)
    core = EngineCore(llm_mode=a.llm)
    app = make_app(core, a.token)

    async def run() -> None:
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", a.port)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        url = f"http://127.0.0.1:{port}/?token={a.token}"
        ready = {"ready": True, "port": port, "token": a.token, "url": url, "pid": os.getpid()}
        (config.HOME / "engine.json").write_text(json.dumps(ready))
        if a.print_ready:
            print(json.dumps(ready), flush=True)
        else:
            print(f"Verity engine listening on 127.0.0.1:{port}\nOpen: {url}", flush=True)
        if a.open:
            import webbrowser
            webbrowser.open(url)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        # exit when the parent (desktop app) goes away
        ppid = os.getppid()

        async def watch_parent():
            while not stop.is_set():
                await asyncio.sleep(2)
                if os.getppid() != ppid and os.environ.get("VERITY_EXIT_WITH_PARENT") == "1":
                    stop.set()
        asyncio.create_task(watch_parent())
        await stop.wait()
        app[STOPPING]["v"] = True
        try:
            await asyncio.wait_for(runner.cleanup(), timeout=5)
        except asyncio.TimeoutError:
            pass

    try:
        asyncio.run(run())
    finally:
        core.shutdown()
        try:
            (config.HOME / "engine.json").unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
