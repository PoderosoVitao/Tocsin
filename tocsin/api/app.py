from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI
from redis.asyncio import Redis

from tocsin.api import checks, ping
from tocsin.api.deps import require_api_key
from tocsin.config import Settings, get_settings
from tocsin.db import Sessionmaker, create_engine, create_sessionmaker
from tocsin.redis_client import fast_failing


# Builds the API app. Tests pass in their own connections; in production the
# lifespan opens them when the server starts.
def create_app(
    settings: Settings | None = None,
    sessionmaker: Sessionmaker | None = None,
    redis: Redis | None = None,
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
        yield
        if redis is None:
            await app.state.redis.aclose()
        if engine is not None:
            await engine.dispose()

    app = FastAPI(title="tocsin", lifespan=lifespan)
    app.state.settings = settings
    app.state.sessionmaker = sessionmaker
    app.state.redis = redis

    api = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])
    api.include_router(checks.router)
    app.include_router(api)
    app.include_router(ping.router)
    return app
