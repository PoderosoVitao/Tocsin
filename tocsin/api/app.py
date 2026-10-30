from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis

from tocsin.alerts import Dispatcher
from tocsin.api import alerts, channels, checks, health, keys, overview, ping, stream
from tocsin.api.deps import require_api_key
from tocsin.channels import Sender
from tocsin.config import Settings, get_settings
from tocsin.db import Sessionmaker, create_engine, create_sessionmaker
from tocsin.redis_client import fast_failing

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent.parent / "static"


# Builds the API app. Tests pass in their own connections; in production the
# lifespan opens them when the server starts.
def create_app(
    settings: Settings | None = None,
    sessionmaker: Sessionmaker | None = None,
    redis: Redis | None = None,
    dispatcher: Dispatcher | None = None,
    senders: Mapping[str, Sender] | None = None,
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = None
        if sessionmaker is None:
            engine = create_engine(settings.database_url)
            app.state.sessionmaker = create_sessionmaker(engine)
        if redis is None:
            app.state.redis = fast_failing(settings.redis_url)
        broker = None
        if dispatcher is None:
            from tocsin.worker import TaskiqDispatcher, create_broker

            broker = create_broker(settings)
            # The API only enqueues. If Redis is down at startup it should still
            # take pings: deliveries are in the database and the relay will
            # queue them once Redis is back.
            try:
                await broker.startup()
            except Exception:
                log.exception("task broker unavailable at startup; continuing without it")
            app.state.dispatcher = TaskiqDispatcher(broker)
        http = None
        if senders is None:
            from tocsin.worker import build_senders

            http = httpx.AsyncClient()
            app.state.senders = build_senders(http, settings)
        yield
        if http is not None:
            await http.aclose()
        if broker is not None:
            await broker.shutdown()
        if redis is None:
            await app.state.redis.aclose()
        if engine is not None:
            await engine.dispose()

    app = FastAPI(title="tocsin", lifespan=lifespan)
    app.state.settings = settings
    app.state.sessionmaker = sessionmaker
    app.state.redis = redis
    app.state.dispatcher = dispatcher
    app.state.senders = senders

    api = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])
    api.include_router(alerts.router)
    api.include_router(checks.router)
    api.include_router(channels.router)
    api.include_router(keys.router)
    api.include_router(overview.router)
    api.include_router(stream.router)
    app.include_router(api)
    app.include_router(ping.router)
    app.include_router(health.router)

    # The dashboard is plain static files, with no build step. Browsers are
    # told to revalidate them so an update never pairs new HTML with a stale
    # script.
    @app.middleware("http")
    async def revalidate_frontend(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
