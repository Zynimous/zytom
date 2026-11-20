"""FastAPI service (Plan.md「技術選型」: Python、FastAPI、SQLite).

* REST endpoints mirroring the MCP tools
* ``/calendar`` — 秘書日曆 page (month / week / day / list, layer toggles)
* ``/ws/alerts`` — clients subscribe to alerts over WebSocket (service push)
* ``/mcp`` — the same MCP server over streamable HTTP
* a background loop: startup check, tick every 30 min, daily stats, calendar sync

Bind to localhost by default. If ``ZYTOM_TOKEN`` is set, every request must
carry ``Authorization: Bearer <token>`` (WebSocket: ``?token=``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from . import alerts as alerts_mod
from . import calendar_view
from .ics import sync_sources
from .observe import daily_stats
from .service import Secretary
from .store import Store
from .timeutil import parse_dt, tz


class WebSocketHub:
    """Notifier that pushes alerts to every connected WebSocket client."""

    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    async def broadcast(self, alerts: list[dict]) -> None:
        msg = json.dumps({"type": "alerts", "alerts": alerts}, ensure_ascii=False, default=str)
        for ws in list(self.clients):
            try:
                await ws.send_text(msg)
            except Exception:
                self.clients.discard(ws)

    def send(self, alerts: list[dict]) -> None:  # Notifier protocol; may be called from a worker thread
        if self.loop and self.clients:
            asyncio.run_coroutine_threadsafe(self.broadcast(alerts), self.loop)


def create_app(store: Store | None = None, clock=None, scheduler: bool = True,
               interval_min: float = 30, mount_mcp: bool = True) -> FastAPI:
    store = store or Store()
    sec = Secretary(store, clock)
    hub = WebSocketHub()
    token = os.environ.get("ZYTOM_TOKEN")
    state: dict[str, Any] = {"last_stats": store.pref("last_stats_date"), "last_sync": None}

    def notifiers():
        return [hub, *alerts_mod.notifiers_from_prefs(sec.prefs)]

    def run_tick(summarize: bool = False) -> list[dict]:
        now = sec.now
        prefs = sec.prefs
        out = {}
        sync_h = float(prefs.get("calendar_sync_h", 6))
        if store.pref("calendar_sources") and (
                state["last_sync"] is None or (now - state["last_sync"]).total_seconds() > sync_h * 3600):
            out["calendars"] = sync_sources(store, prefs["tz"], now)
            state["last_sync"] = now
        sent = alerts_mod.tick(store, now, notifiers(), summarize=summarize)
        today = now.astimezone(tz(prefs["tz"])).date().isoformat()
        if today != state["last_stats"]:
            daily_stats(store, now)
            store.set_pref("last_stats_date", today)
            state["last_stats"] = today
        return sent

    async def loop_task():
        await asyncio.to_thread(run_tick, True)  # 啟動補檢查：停機期間的提醒合併成一則摘要
        while True:
            await asyncio.sleep(interval_min * 60)
            try:
                await asyncio.to_thread(run_tick)
            except Exception as e:  # keep the loop alive
                print(f"tick failed: {e!r}")

    mcp_app = None
    if mount_mcp:
        from .server import build_server

        mcp_server = build_server(store, sec.clock)
        mcp_app = mcp_server.streamable_http_app()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        hub.loop = asyncio.get_running_loop()
        task = asyncio.create_task(loop_task()) if scheduler else None
        async with contextlib.AsyncExitStack() as stack:
            if mcp_app is not None and hasattr(mcp_app.router, "lifespan_context"):
                await stack.enter_async_context(mcp_app.router.lifespan_context(mcp_app))
            try:
                yield
            finally:
                if task:
                    task.cancel()

    app = FastAPI(title="zytom 個人秘書", version="0.1.0", lifespan=lifespan)
    app.state.hub = hub
    app.state.secretary = sec
    app.state.run_tick = run_tick

    def auth(request: Request):
        if token and request.headers.get("authorization") != f"Bearer {token}":
            raise HTTPException(401, "missing or bad bearer token")

    @app.exception_handler(ValueError)
    async def value_error(_: Request, exc: ValueError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    guard = [Depends(auth)]

    # ------------------------------------------------------------ items
    @app.get("/items", dependencies=guard)
    def items(text: str | None = None, status: list[str] = Query(["open", "active"]),
              category: str | None = None, parent: str | None = None, time_type: str | None = None,
              due_before: str | None = None, due_after: str | None = None, source: str | None = None,
              limit: int = 100):
        return sec.query(text, status, category, parent, time_type, due_before, due_after, None, source, limit)

    @app.post("/items", dependencies=guard)
    def upsert(data: dict = Body(...)):
        if data.get("status") == "dropped":
            raise ValueError("dropping changes a commitment; use /apply with confirmed=true")
        return sec.upsert(data)

    @app.post("/log", dependencies=guard)
    def log(kind: str = Body(...), item_id: str | None = Body(None), data: dict | None = Body(None)):
        return sec.log_event(kind, item_id, data)

    # ----------------------------------------------------------- engine
    @app.get("/signals", dependencies=guard)
    def signals(windows: list[int] | None = Query(None), top: int | None = None):
        return sec.signals(windows, top)

    @app.post("/simulate", dependencies=guard)
    def simulate(changes: list[dict] = Body(..., embed=True)):
        return sec.simulate(changes)

    @app.get("/recommend", dependencies=guard)
    def recommend(start: str | None = None, end: str | None = None, minutes: int | None = None, n: int = 3):
        return sec.recommend(start, end, minutes, n)

    @app.get("/relieve", dependencies=guard)
    def relieve(target: float | None = None):
        return sec.relieve(target)

    @app.post("/apply", dependencies=guard)
    def apply(changes: list[dict] = Body(...), confirmed: bool = Body(False), note: str | None = Body(None)):
        return sec.apply(changes, confirmed, note)

    @app.get("/observations", dependencies=guard)
    def observations(scope: str | None = None, min_confidence: float = 0.0):
        return sec.observations(scope, True, min_confidence)

    @app.get("/context", dependencies=guard)
    def context():
        return sec.context()

    # ----------------------------------------------------------- alerts
    @app.get("/alerts", dependencies=guard)
    def list_alerts(undelivered_only: bool = False, limit: int = 20):
        return {"alerts": [a["body"] | {"delivered": bool(a["delivered"])} for a in store.alerts(undelivered_only, limit)]}

    @app.post("/alerts/{alert_id}/feedback", dependencies=guard)
    def feedback(alert_id: str, reaction: str = Body(..., embed=True)):
        if reaction not in ("accepted", "ignored", "not_needed"):
            raise ValueError("reaction must be accepted / ignored / not_needed")
        a = next((x for x in store.alerts(limit=500) if x["id"] == alert_id), None)
        if a is None:
            raise HTTPException(404, "alert not found")
        return {"logged": store.append("alert_feedback", None, {"alert_id": alert_id, "reaction": reaction,
                                                                 "rule": a["body"]["rule"],
                                                                 "items": a["body"]["items"]}).model_dump(mode="json")}

    @app.post("/tick", dependencies=guard)
    async def tick_now():
        return {"alerts": await asyncio.to_thread(run_tick)}

    @app.post("/subscriptions/webhook", dependencies=guard)
    def add_webhook(url: str = Body(..., embed=True)):
        ns = [n for n in (store.pref("notifiers", []) or []) if n.get("url") != url]
        ns.append({"type": "webhook", "url": url})
        store.set_pref("notifiers", ns)
        return {"notifiers": len(ns)}

    @app.websocket("/ws/alerts")
    async def ws_alerts(ws: WebSocket):
        if token and ws.query_params.get("token") != token:
            await ws.close(code=4401)
            return
        await ws.accept()
        hub.clients.add(ws)
        pending = [a["body"] for a in store.alerts(undelivered_only=True)]
        await ws.send_text(json.dumps({"type": "hello", "undelivered": pending}, ensure_ascii=False, default=str))
        try:
            while True:
                await ws.receive_text()  # clients may send pings; nothing else expected
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.discard(ws)

    # --------------------------------------------------------- calendar
    def _cal(view: str, date: str | None, layers: str | None, days: int) -> dict:
        if view not in calendar_view.VIEWS:
            raise ValueError(f"view must be one of {calendar_view.VIEWS}")
        lay = [x for x in (layers or "").split(",") if x] or None
        return calendar_view.build(sec.world(), view, date, lay, days)

    @app.get("/api/calendar", dependencies=guard)
    def calendar_json(view: str = "week", date: str | None = None, layers: str | None = None, days: int = 14):
        return _cal(view, date, layers, days)

    @app.get("/calendar", response_class=HTMLResponse, dependencies=guard)
    def calendar_page(view: str = "month", date: str | None = None, layers: str | None = None, days: int = 14):
        return calendar_view.render_html(_cal(view, date, layers, days), "/calendar", sec.now)

    @app.post("/calendar/sync", dependencies=guard)
    def calendar_sync():
        return {"results": sync_sources(store, sec.tz, sec.now)}

    @app.get("/health")
    def health():
        return {"ok": True, "now": sec.now.isoformat(), "ws_clients": len(hub.clients)}

    if mcp_app is not None:
        # 直接併入 MCP app 的 /mcp 路由（mount 會變成 /mcp/mcp）
        app.router.routes.extend(mcp_app.routes)
    return app


def main(argv: list[str] | None = None) -> None:
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser(prog="zytom-api")
    ap.add_argument("--db")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--interval", type=float, default=30, help="tick 間隔（分鐘）")
    ap.add_argument("--no-scheduler", action="store_true")
    args = ap.parse_args(argv)
    fixed = os.environ.get("ZYTOM_NOW")
    clock = (lambda: parse_dt(fixed)) if fixed else None
    app = create_app(Store(args.db) if args.db else None, clock, not args.no_scheduler, args.interval)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
