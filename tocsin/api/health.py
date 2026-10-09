from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from tocsin.api.deps import Session
from tocsin.metrics import refresh_state_gauges

router = APIRouter(tags=["health"])


# Healthy means it can take pings: the database answers. Redis being down is
# reported but doesn't fail the check, since pings are still accepted without
# it (alerts wait in the database until it is back).
@router.get("/healthz")
async def healthz(request: Request, session: Session) -> JSONResponse:
    try:
        await session.execute(text("SELECT 1"))
        database = "ok"
    except SQLAlchemyError:
        database = "unavailable"
    try:
        await request.app.state.redis.ping()
        redis = "ok"
    except (RedisError, OSError):
        redis = "unavailable"

    healthy = database == "ok"
    return JSONResponse(
        {"status": "ok" if healthy else "unavailable", "database": database, "redis": redis},
        status_code=200 if healthy else 503,
    )


# Unauthenticated, like most Prometheus endpoints; it exposes counts, never
# names or ping URLs. Put it behind the reverse proxy if that matters.
@router.get("/metrics")
async def metrics(request: Request, session: Session) -> Response:
    await refresh_state_gauges(session, request.app.state.redis, datetime.now(UTC))
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
