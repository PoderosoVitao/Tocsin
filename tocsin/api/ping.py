from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from tocsin.alerts import dispatch_pending
from tocsin.api.deps import AppSettings, Session
from tocsin.metrics import PING_SECONDS, PINGS, PINGS_REJECTED
from tocsin.pings import Signal, record_ping
from tocsin.ratelimit import allow

router = APIRouter(tags=["ping"])

METHODS = ["GET", "POST", "HEAD"]


# Reads the request body, keeping only its last `limit` bytes in memory, so a
# job that posts a huge log can't exhaust the server's memory.
async def _body_tail(request: Request, limit: int) -> bytes:
    tail = b""
    async for chunk in request.stream():
        tail = (tail + chunk)[-limit:]
    return tail


async def _handle(
    request: Request, session: Session, settings: AppSettings, key: str, signal: str | None
) -> PlainTextResponse:
    with PING_SECONDS.time():
        response = await _record(request, session, settings, key, signal)
    if response.status_code != 200:
        PINGS_REJECTED.labels(reason=_REJECTIONS.get(response.status_code, "other")).inc()
    return response


_REJECTIONS = {400: "bad_signal", 404: "not_found", 429: "rate_limited"}


async def _record(
    request: Request, session: Session, settings: AppSettings, key: str, signal: str | None
) -> PlainTextResponse:
    try:
        ping_key = uuid.UUID(key)
    except ValueError:
        return PlainTextResponse("not found", status_code=404)
    try:
        parsed = Signal.parse(signal)
    except ValueError as exc:
        return PlainTextResponse(str(exc), status_code=400)
    # Checked before touching the database, so a job stuck in a loop costs one
    # Redis round trip per ping instead of a row lock.
    if not await allow(request.app.state.redis, f"ping:{ping_key}", settings.ping_rate_limit):
        return PlainTextResponse("rate limited", status_code=429, headers={"Retry-After": "60"})

    body = await _body_tail(request, settings.output_tail_bytes)
    source_ip = request.client.host if request.client else None
    outcome = await record_ping(
        session,
        ping_key,
        parsed,
        body,
        source_ip,
        datetime.now(UTC),
        settings.output_tail_bytes,
    )
    if outcome is None:
        return PlainTextResponse("not found", status_code=404)
    await session.commit()
    PINGS.labels(kind=parsed.kind).inc()
    await dispatch_pending(session, request.app.state.dispatcher)
    return PlainTextResponse("OK")


@router.api_route("/ping/{key}", methods=METHODS)
async def ping(
    request: Request, session: Session, settings: AppSettings, key: str
) -> PlainTextResponse:
    return await _handle(request, session, settings, key, None)


@router.api_route("/ping/{key}/{signal}", methods=METHODS)
async def ping_signal(
    request: Request, session: Session, settings: AppSettings, key: str, signal: str
) -> PlainTextResponse:
    return await _handle(request, session, settings, key, signal)
