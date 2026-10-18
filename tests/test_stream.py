from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
from redis.asyncio import Redis

from tests.conftest import RecordingDispatcher
from tests.helpers import create_check, ping_at
from tocsin.db import Sessionmaker
from tocsin.events import CHANNEL, publish, stream_events
from tocsin.redis_client import fast_failing
from tocsin.sweep import run_sweep


class Client:
    def __init__(self) -> None:
        self.gone = False

    async def disconnected(self) -> bool:
        return self.gone


async def next_data(stream: AsyncIterator[str], event: str) -> dict[str, object]:
    async with asyncio.timeout(5):
        async for chunk in stream:
            if chunk.startswith(f"event: {event}\n"):
                data: dict[str, object] = json.loads(chunk.split("data: ", 1)[1])
                return data
    raise AssertionError(f"no {event} event")


async def test_the_stream_says_hello_then_relays_published_events(redis: Redis) -> None:
    client = Client()
    stream = stream_events(redis, client.disconnected)

    assert await anext(stream) == "retry: 3000\n\n"
    assert await anext(stream) == "event: hello\ndata: {}\n\n"
    await publish(redis, {"type": "check.updated", "check_id": "c1", "state": "down"})

    event = await next_data(stream, "check.updated")
    assert event == {"type": "check.updated", "check_id": "c1", "state": "down"}
    client.gone = True
    await stream.aclose()


async def test_the_stream_ends_when_the_client_disconnects(redis: Redis) -> None:
    client = Client()
    stream = stream_events(redis, client.disconnected)
    await anext(stream)
    client.gone = True
    async with asyncio.timeout(5):
        assert [chunk async for chunk in stream] == ["event: hello\ndata: {}\n\n"]
    assert (await redis.pubsub_numsub(CHANNEL))[0][1] == 0


async def test_idle_streams_send_keepalives(redis: Redis) -> None:
    client = Client()
    stream = stream_events(redis, client.disconnected, keepalive=0.1)
    chunks = [await anext(stream) for _ in range(3)]
    assert chunks[2] == ": keepalive\n\n"
    client.gone = True
    await stream.aclose()


async def test_the_stream_reports_when_redis_is_unavailable() -> None:
    unreachable = fast_failing("redis://127.0.0.1:1/0")
    try:
        chunks = [chunk async for chunk in stream_events(unreachable, Client().disconnected)]
    finally:
        await unreachable.aclose()
    assert chunks == ['event: error\ndata: {"message": "live updates are unavailable"}\n\n']


async def test_publishing_never_raises_when_redis_is_down() -> None:
    unreachable = fast_failing("redis://127.0.0.1:1/0")
    try:
        await publish(unreachable, {"type": "check.updated"})
        await publish(None, {"type": "check.updated"})
    finally:
        await unreachable.aclose()


async def test_the_stream_endpoint_needs_an_api_key(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/stream", headers={"Authorization": ""})
    assert response.status_code == 401


# Subscribes to the channel, makes changes through the real handlers and the
# sweep, and checks that each one is announced after it has committed.
async def test_changes_are_announced(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker, redis: Redis
) -> None:
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    await pubsub.subscribe(CHANNEL)

    async def announced() -> dict[str, str]:
        async with asyncio.timeout(5):
            while True:
                message = await pubsub.get_message(timeout=1.0)
                if message is not None:
                    data: dict[str, str] = json.loads(message["data"])
                    return data

    check = await create_check(client, schedule="@hourly", grace_seconds=0)
    assert await announced() == {
        "type": "check.updated",
        "check_id": check["id"],
        "state": "new",
        "last_ping_at": None,
    }

    await client.get(check["ping_path"])
    assert (await announced())["state"] == "up"

    await client.patch(f"/api/checks/{check['id']}", json={"paused": True})
    assert (await announced())["state"] == "paused"
    await client.patch(f"/api/checks/{check['id']}", json={"paused": False})
    assert (await announced())["state"] == "up"

    await ping_at(sessionmaker, check["id"], None, datetime.now(UTC) - timedelta(hours=3))
    await run_sweep(sessionmaker, RecordingDispatcher(), redis)
    assert await announced() == {"type": "check.updated", "check_id": check["id"], "state": "down"}

    await client.delete(f"/api/checks/{check['id']}")
    assert await announced() == {"type": "check.deleted", "check_id": check["id"]}
    await pubsub.aclose()
