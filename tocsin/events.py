from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from tocsin.models import Check

log = logging.getLogger(__name__)

CHANNEL = "tocsin:events"
KEEPALIVE_SECONDS = 15


def check_updated(check: Check) -> dict[str, Any]:
    return {
        "type": "check.updated",
        "check_id": str(check.id),
        "state": check.state,
        "last_ping_at": check.last_ping_at.isoformat() if check.last_ping_at else None,
    }


# An alert delivery changed (sent, will be retried, or gave up). It happens in
# a worker, away from the pings and sweeps that publish the other events, so
# the alert log and check pages would otherwise not hear about retries.
def alert_updated(check_id: uuid.UUID) -> dict[str, Any]:
    return {"type": "alert.updated", "check_id": str(check_id)}


def check_deleted(check_id: uuid.UUID) -> dict[str, Any]:
    return {"type": "check.deleted", "check_id": str(check_id)}


# Tells every connected dashboard that something changed. Published after the
# change is committed, so a dashboard that reacts by re-reading always sees
# it. Messages are hints, not data: a dashboard that misses one (Redis down,
# a dropped connection) is only stale until its next refresh, so a failure
# here is logged and never allowed to fail a ping or a sweep.
async def publish(redis: Redis | None, message: dict[str, Any]) -> None:
    if redis is None:
        return
    try:
        await redis.publish(CHANNEL, json.dumps(message))
    except (RedisError, OSError) as exc:
        log.warning("could not publish %s: %s", message.get("type"), type(exc).__name__)


def format_sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# The body of GET /api/stream: a "hello" event, then one SSE event per
# published message, with a comment line every few seconds so proxies and
# browsers don't drop an idle connection. Ends when `disconnected` reports
# the client has gone, or Redis stops answering.
async def stream_events(
    redis: Redis,
    disconnected: Callable[[], Awaitable[bool]],
    keepalive: float = KEEPALIVE_SECONDS,
) -> AsyncIterator[str]:
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    try:
        await pubsub.subscribe(CHANNEL)
    except (RedisError, OSError):
        yield format_sse("error", {"message": "live updates are unavailable"})
        return

    try:
        yield "retry: 3000\n\n"
        yield format_sse("hello", {})
        last_write = time.monotonic()
        while not await disconnected():
            try:
                message = await pubsub.get_message(timeout=1.0)
            except (TimeoutError, RedisError, OSError):
                yield format_sse("error", {"message": "live updates were interrupted"})
                return
            if message is None:
                if time.monotonic() - last_write >= keepalive:
                    last_write = time.monotonic()
                    yield ": keepalive\n\n"
                continue
            last_write = time.monotonic()
            try:
                data = json.loads(message["data"])
            except (ValueError, TypeError):
                continue
            yield format_sse(str(data.get("type", "message")), data)
    finally:
        try:
            await pubsub.aclose()  # type: ignore[no-untyped-call]
        except (RedisError, OSError):
            pass
