from __future__ import annotations

import asyncio
import hmac
import json as _json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from guardian import __version__
from guardian.application.engine import TradingEngine
from guardian.application.trainer import ParameterTrainer
from guardian.infrastructure.settings import Settings


def create_app(engine: TradingEngine, settings: Settings, close_exchange) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await engine.stop()
        await close_exchange()

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            expected = (
                settings.dashboard_token.get_secret_value() if settings.dashboard_token else ""
            )
            supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
            if expected and not hmac.compare_digest(supplied, expected):
                return JSONResponse({"detail": "No autorizado"}, status_code=401)
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                origin = request.headers.get("origin")
                if origin and origin not in {
                    f"http://{settings.app_host}:{settings.app_port}",
                    f"http://127.0.0.1:{settings.app_port}",
                    f"http://localhost:{settings.app_port}",
                }:
                    return JSONResponse({"detail": "Origen rechazado"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    static_dir = Path(__file__).parent / "static"

    @app.get("/", include_in_schema=False)
    async def dashboard():
        return FileResponse(static_dir / "index.html")

    @app.get("/assets/{name}", include_in_schema=False)
    async def asset(name: str):
        if name not in {
            "app.js",
            "styles.css",
            "training.css",
            "shadow.css",
            "console.css",
            "operations.css",
            "pro.css",
            "chart.css",
            "favicon.svg",
        }:
            raise HTTPException(404)
        return FileResponse(static_dir / name)

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "mode": settings.trading_mode,
            "version": __version__,
            "simulation_version": ParameterTrainer.SIMULATION_VERSION,
        }

    @app.get("/api/status")
    async def status():
        payload = engine.status_dict()
        payload["system"] = {
            "version": __version__,
            "simulation_version": ParameterTrainer.SIMULATION_VERSION,
        }
        price = engine.status.last_price or 0
        snapshot = engine.repository.risk_snapshot(
            datetime.now(ZoneInfo(settings.local_timezone)).date(),
            price,
            settings.local_timezone,
        )
        payload["risk"] = {
            "realized_pnl_today": str(snapshot.realized_pnl_today),
            "trades_today": snapshot.trades_today,
            "entries_today": snapshot.entries_today,
            "position_quote": str(snapshot.position_quote),
            "max_daily_loss": str(engine.risk.limits.max_daily_loss_quote),
            "max_trades": engine.risk.limits.max_trades_per_day,
        }
        return payload

    @app.get("/api/orders")
    async def orders(limit: int = 50):
        return engine.repository.list_orders(limit)

    @app.get("/api/events")
    async def events(limit: int = 80):
        return engine.repository.list_events(limit)

    @app.get("/api/performance")
    async def performance():
        return engine.repository.performance_summary()

    @app.get("/api/market")
    async def market(interval: str | None = None, limit: int = 100):
        selected = interval or engine.interval
        if selected not in {"1m", "5m", "15m", "1h"}:
            raise HTTPException(400, "Temporalidad no permitida")
        try:
            return await engine.market_interval_dict(selected, limit)
        except Exception as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get("/api/training")
    async def training():
        return {
            "dataset_samples": engine.repository.observation_count(),
            "latest": engine.repository.latest_training_result(),
            "running": engine.training_running,
        }

    @app.get("/api/shadow")
    async def shadow():
        if not engine.shadow_lab:
            return {"enabled": False, "models": [], "trades": []}
        return engine.shadow_lab.report(engine.status.last_price)

    @app.post("/api/shadow/{model_id}/promote")
    async def promote_shadow(model_id: str):
        if len(model_id) != 24 or any(char not in "0123456789abcdef" for char in model_id):
            raise HTTPException(400, "Identificador de modelo inválido")
        try:
            return engine.promote_shadow_model(model_id)
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/train")
    async def train():
        try:
            return await engine.train_now()
        except Exception as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post("/api/training/promote")
    async def promote_training():
        try:
            return engine.promote_latest_strategy()
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/start")
    async def start(x_confirm_live: str | None = Header(default=None)):
        if settings.trading_mode == "live" and x_confirm_live != "I-ACCEPT-REAL-MONEY-RISK":
            raise HTTPException(409, "Falta confirmación explícita para dinero real")
        try:
            await engine.start()
        except Exception as exc:
            raise HTTPException(503, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/stop")
    async def stop():
        await engine.stop()
        return {"ok": True}

    @app.post("/api/emergency-stop")
    async def emergency_stop():
        await engine.emergency_stop()
        return {"ok": True, "emergency_stop": True}

    @app.post("/api/emergency-reset")
    async def emergency_reset():
        engine.reset_emergency_stop()
        return {"ok": True, "emergency_stop": False}

    @app.get("/api/equity")
    async def equity(limit: int = 200):
        limit = min(max(limit, 10), 1000)
        perf = engine.repository.performance_summary()
        curve = perf.get("equity_curve", [])
        return curve[-limit:]

    @app.get("/api/events/stream")
    async def events_stream():
        async def generator():
            recent = engine.repository.list_events(limit=1)
            last_seen_time = recent[0]["created_at"] if recent else "1970-01-01T00:00:00+00:00"
            keep_alive_counter = 0
            yield 'data: {"stream": "ready"}\n\n'
            while True:
                try:
                    all_events = engine.repository.list_events(limit=50)
                    new_events = [
                        ev for ev in reversed(all_events)
                        if ev["created_at"] > last_seen_time
                    ]
                    for event in new_events:
                        last_seen_time = event["created_at"]
                        yield f"data: {_json.dumps(event, ensure_ascii=False)}\n\n"
                    keep_alive_counter += 1
                    if keep_alive_counter >= 5:
                        yield 'data: {"keep_alive": true}\n\n'
                        keep_alive_counter = 0
                    await asyncio.sleep(3)
                except asyncio.CancelledError:
                    break
                except Exception:
                    break
        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
