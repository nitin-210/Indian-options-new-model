"""FastAPI app: the web page, a JSON API and a server-sent-event stream.

Bound to 127.0.0.1 only. Requests whose Host header is not this machine are
refused (protects against DNS-rebinding), and changes (POST) must come from
the app's own page.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import WEB_DIR, Settings, save_settings, settings_from_dict
from .engine import Engine, Hub

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, settings: Settings, engine: Engine, source, hub: Hub, journal, port: int):
        self.settings = settings
        self.engine = engine
        self.source = source
        self.hub = hub
        self.journal = journal
        self.port = port
        self.tasks: list[asyncio.Task] = []
        self.stopping = False  # set on Ctrl+C so live streams end at once


def predictor(rt: Runtime):
    """The dashboard's prediction builder (created on first use)."""
    if getattr(rt, "_predictor", None) is None:
        from .predict import Predictor
        rt._predictor = Predictor(rt.engine)
    return rt._predictor


async def broadcaster(rt: Runtime):
    tick = 0
    last_phases = None
    while True:
        await asyncio.sleep(0.25)
        tick += 1
        e = rt.engine
        try:
            if not rt.hub.queues:
                continue
            rt.hub.publish("bars", e.latest_bars())
            # A new signal (or exit) goes to the page at once, not at the next once-a-second update.
            phases = tuple((k, st.phase, bool(st.plan), bool(st.trade and st.trade.get("status") == "closed"))
                           for k, st in e.idx.items())
            changed = phases != last_phases
            last_phases = phases
            if tick % 2 == 0 or changed:
                st = e.state_json()
                st["source"] = rt.source.status()
                rt.hub.publish("state", st)
            if changed and tick % 4 != 0:
                rt.hub.publish("predictions", predictor(rt).snapshot())
            if tick % 4 == 0:
                rt.hub.publish("predictions", predictor(rt).snapshot())
                rt.hub.publish("chain", {k: e.chain_json(k) for k in e.idx})
                rt.hub.publish("opt_bars", e.latest_opt_bars())
                rt.hub.publish("movers", e.movers_json())
        except Exception:  # pragma: no cover - keep streaming no matter what
            log.exception("broadcast failed")


def create_app(rt: Runtime, start_background: bool = True) -> FastAPI:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_background:
            rt.tasks.append(asyncio.create_task(_guard(rt.source.start(), "source")))
            rt.tasks.append(asyncio.create_task(broadcaster(rt)))
        yield
        for t in rt.tasks:
            t.cancel()

    app = FastAPI(title="India options signals", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    allowed_hosts = {f"127.0.0.1:{rt.port}", f"localhost:{rt.port}"}
    allowed_origins = {f"http://127.0.0.1:{rt.port}", f"http://localhost:{rt.port}"}
    if not start_background:  # unit tests use FastAPI's TestClient
        allowed_hosts.add("testserver")
        allowed_origins.add("http://testserver")

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.headers.get("host", "")
        if host not in allowed_hosts:
            return JSONResponse({"error": "This app only answers on 127.0.0.1."}, status_code=403)
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and origin not in allowed_origins:
                return JSONResponse({"error": "Cross-site request refused."}, status_code=403)
            if "application/json" not in request.headers.get("content-type", ""):
                return JSONResponse({"error": "JSON only."}, status_code=415)
        resp = await call_next(request)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    async def index():
        # Stamp the build into the page and its asset links, so a browser never runs an older dashboard.
        from . import BUILD
        html = (WEB_DIR / "dashboard.html").read_text(encoding="utf-8")
        html = (html.replace("__BUILD__", BUILD)
                    .replace("/static/dashboard.js", f"/static/dashboard.js?b={BUILD}")
                    .replace("/static/dashboard.css", f"/static/dashboard.css?b={BUILD}"))
        return HTMLResponse(html)

    @app.get("/classic")
    async def classic():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/api/predictions")
    async def predictions():
        return predictor(rt).snapshot()

    # ---- paper desk: your own one-click paper trades (nothing is sent to a broker)
    from .paper import PaperError

    def _paper(fn):
        try:
            return fn()
        except PaperError as ex:
            raise HTTPException(400, str(ex))

    @app.get("/api/paper/ticket")
    async def paper_ticket(kind: str = "index", name: str = "NIFTY", lots: int = 1):
        return _paper(lambda: rt.engine.paper.ticket(kind, name.upper() if kind == "index" else name, rt.engine.clock(),
                                                     lots))

    @app.post("/api/paper/open")
    async def paper_open(body: dict):
        kind = str(body.get("kind", "index"))
        name = str(body.get("name", ""))
        name = name.upper() if kind == "index" else name
        return _paper(lambda: rt.engine.paper.open(kind, name, str(body.get("side", "")).upper(), rt.engine.clock(),
                                                   body.get("lots", 1)))

    @app.post("/api/paper/straddle")
    async def paper_straddle(body: dict):
        kind = str(body.get("kind", "index"))
        name = str(body.get("name", ""))
        name = name.upper() if kind == "index" else name
        return _paper(lambda: rt.engine.paper.open_straddle(kind, name, rt.engine.clock(), lots=body.get("lots", 1)))

    @app.post("/api/paper/prefs")
    async def paper_prefs(body: dict):
        if "auto_straddle" in body:
            rt.engine.paper.set_auto_straddle(bool(body["auto_straddle"]))
        return rt.engine.paper.trades(rt.engine.clock())

    @app.post("/api/paper/exit")
    async def paper_exit(body: dict):
        if body.get("group"):
            return _paper(lambda: rt.engine.paper.exit_group(str(body["group"]), rt.engine.clock()))
        try:
            tid = int(body.get("id"))
        except (TypeError, ValueError):
            raise HTTPException(400, "Which trade?")
        return _paper(lambda: rt.engine.paper.exit(tid, rt.engine.clock()))

    @app.get("/api/paper/trades")
    async def paper_trades():
        return rt.engine.paper.trades(rt.engine.clock())

    @app.post("/api/paper/reset")
    async def paper_reset(body: dict):
        from .paper import CAPITAL
        try:
            cap = float(body.get("capital") or CAPITAL)
        except (TypeError, ValueError):
            raise HTTPException(400, "Capital must be a number.")
        if not 50_000 <= cap <= 10_000_000:
            raise HTTPException(400, "Choose a paper capital between ₹50,000 and ₹1 crore.")
        rt.engine.journal.paper_reset(rt.engine.mode, cap)
        rt.engine.paper.tokens.clear()
        return rt.engine.paper.trades(rt.engine.clock())

    def idx_or_404(index: str) -> str:
        index = index.upper()
        if index not in rt.engine.idx:
            raise HTTPException(404, "Unknown index")
        return index

    @app.get("/api/state")
    async def state():
        s = rt.engine.state_json()
        s["source"] = rt.source.status()
        return s

    @app.get("/api/candles")
    async def candles(index: str = "NIFTY", tf: int = 5):
        return rt.engine.bars_json(idx_or_404(index), 1 if tf == 1 else 5)

    @app.get("/api/context")
    async def context(index: str = "NIFTY", tf: int = 5, days: int = 2):
        return rt.engine.context_json(idx_or_404(index), 1 if tf == 1 else 5, days)

    @app.get("/api/older")
    async def older(index: str = "NIFTY", tf: int = 5, before: str = "", n: int = 5, around: str = ""):
        from datetime import date as _d
        try:
            b = _d.fromisoformat(before) if before else rt.engine.clock().date()
            a = _d.fromisoformat(around) if around else None
        except ValueError:
            raise HTTPException(400, "dates are YYYY-MM-DD")
        return await asyncio.to_thread(rt.engine.older_json, idx_or_404(index), tf, b, n, a)

    @app.get("/api/chart-days")
    async def chart_days(index: str = "NIFTY"):
        days = await asyncio.to_thread(rt.engine.chart_days, idx_or_404(index))
        return {"first": days[0].isoformat() if days else None, "last": days[-1].isoformat() if days else None,
                "days": [d.isoformat() for d in days]}

    @app.get("/api/chain")
    async def chain(index: str = "NIFTY"):
        return rt.engine.chain_json(idx_or_404(index))

    @app.get("/api/option")
    async def option(token: str):
        if not rt.engine.select_token(token):
            raise HTTPException(404, "Unknown option")
        return rt.engine.option_json(token)

    @app.get("/api/stock")
    async def stock(name: str, tf: int = 5):
        d = rt.engine.stock_json(name, 1 if tf == 1 else 5)
        if d is None:
            raise HTTPException(404, "Unknown stock")
        return d

    @app.get("/api/movers")
    async def movers():
        return rt.engine.movers_json()

    @app.post("/api/mover")
    async def mover(body: dict):
        if not rt.engine.select_mover(str(body.get("name", ""))):
            raise HTTPException(404, "Unknown stock")
        return rt.engine.movers_json()

    @app.get("/api/odds")
    async def get_odds(index: str = "NIFTY"):
        return rt.engine.odds_json(idx_or_404(index))

    @app.post("/api/odds/rerun")
    async def rerun(body: dict):
        rt.engine.rerun_mc(idx_or_404(str(body.get("index", "NIFTY"))))
        return {"ok": True}

    @app.get("/api/journal")
    async def journal():
        mode = rt.engine.mode
        return {"mode": mode, "trades": rt.journal.list(mode, 300), "stats": rt.journal.stats(mode)}

    @app.get("/api/settings")
    async def get_settings():
        return asdict(rt.engine.s)

    @app.post("/api/settings")
    async def post_settings(body: dict):
        try:
            new = settings_from_dict(body, base=rt.engine.s)
        except (TypeError, ValueError) as e:
            return JSONResponse({"ok": False, "errors": [f"Invalid value: {e}"]}, status_code=400)
        if "max_loss_per_trade" in body:
            new.max_loss_confirmed = True
        errs = new.validate()
        if errs:
            return JSONResponse({"ok": False, "errors": errs}, status_code=400)
        save_settings(new)
        rt.engine.s = new
        rt.settings = new
        if hasattr(rt.source, "s"):
            rt.source.s = new
        rt.engine.log_event("settings", "Settings saved. New signals use them; an active plan keeps its stop.")
        return {"ok": True, "settings": asdict(new)}

    @app.post("/api/replay")
    async def replay(body: dict):
        rt.source.control(str(body.get("action", "")), body.get("speed"))
        return rt.source.status()

    # Past days: rebuild a day and replay it on the next port for practice (not offered inside a replay itself)
    practice = None
    if rt.engine.mode != "replay":
        from .practice import Practice
        practice = Practice(rt.port + 1)

    @app.get("/api/practice")
    async def practice_status():
        # inside a practice replay: point back to the main screen, where days are picked
        return practice.status() if practice else {"available": False, "home": f"http://127.0.0.1:{rt.port - 1}/#days"}

    @app.post("/api/practice")
    async def practice_control(body: dict):
        if practice is None:
            raise HTTPException(400, "Past days are not available inside a replay")
        if body.get("action") == "stop":
            await practice.stop()
            return {"ok": True}
        return await practice.start(str(body.get("day") or ""), body.get("folder") or None, bool(body.get("stocks")))

    @app.get("/api/events")
    async def events():
        return list(rt.engine.events)

    @app.get("/api/stream")
    async def stream(request: Request):
        q = rt.hub.subscribe()

        async def gen():
            try:
                s = rt.engine.state_json()
                s["source"] = rt.source.status()
                yield "retry: 2000\n\n"
                import json as _json
                yield f"event: state\ndata: {_json.dumps(s, default=str)}\n\n"
                idle = 0
                while not rt.stopping:
                    if await request.is_disconnected():
                        break
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=1)
                        idle = 0
                        yield msg
                    except asyncio.TimeoutError:
                        idle += 1
                        if idle >= 10:
                            idle = 0
                            yield ": keep-alive\n\n"
            finally:
                rt.hub.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no"})

    return app


async def _guard(coro, name: str):
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("%s stopped with an error", name)
