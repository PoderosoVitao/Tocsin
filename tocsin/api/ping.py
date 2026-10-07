from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from tocsin.api.deps import AppSettings, Session
from tocsin.pings import Signal, record_ping

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
    try:
        ping_key = uuid.UUID(key)
    except ValueError:
        return PlainTextResponse("not found", status_code=404)
    try:
        parsed = Signal.parse(signal)
    except ValueError as exc:
        return PlainTextResponse(str(exc), status_code=400)

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
