from __future__ import annotations

import time

import httpx
from redis.asyncio import Redis

from tocsin.config import Settings
from tocsin.ratelimit import allow
from tocsin.redis_client import fast_failing


async def test_allow_counts_hits_per_key(redis: Redis) -> None:
    assert [await allow(redis, "a", limit=2) for _ in range(3)] == [True, True, False]
    assert await allow(redis, "b", limit=2)


async def test_allow_fails_open_quickly_when_redis_is_down() -> None:
    unreachable = fast_failing("redis://127.0.0.1:1/0")
    started = time.monotonic()
    try:
        assert await allow(unreachable, "a", limit=0)
    finally:
        await unreachable.aclose()
    assert time.monotonic() - started < 1


async def test_pings_over_the_limit_get_429(client: httpx.AsyncClient, settings: Settings) -> None:
    settings.ping_rate_limit = 2
    created = (await client.post("/api/checks", json={"name": "x", "schedule": "@hourly"})).json()
    path = created["ping_url"].removeprefix("https://tocsin.test")

    statuses = [(await client.get(path)).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]
