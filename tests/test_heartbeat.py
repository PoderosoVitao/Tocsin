from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import insert

from tocsin.config import Settings
from tocsin.db import Sessionmaker
from tocsin.heartbeat import heartbeat
from tocsin.metrics import LAST_SWEEP_KEY
from tocsin.models import Channel, Check, Delivery, Event

NOW = datetime(2026, 5, 1, 12, tzinfo=UTC)
EXTERNAL = "https://hc-ping.test/5f0c1d2e"


class Recorder:
    def __init__(self, api_status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.api_status = api_status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/healthz":
            return httpx.Response(self.api_status, text='{"status":"ok"}')
        return httpx.Response(200, text="OK")

    @property
    def external(self) -> list[tuple[str, str, str]]:
        return [
            (r.method, str(r.url), r.content.decode())
            for r in self.requests
            if str(r.url).startswith(EXTERNAL)
        ]


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def http(recorder: Recorder) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(recorder))


@pytest.fixture
def monitored(settings: Settings) -> Settings:
    settings.self_ping_url = EXTERNAL
    return settings


async def beat(
    sessionmaker: Sessionmaker,
    redis: Redis,
    http: httpx.AsyncClient,
    settings: Settings,
    now: datetime = NOW,
) -> list[str] | None:
    return await heartbeat(sessionmaker, redis, http, settings, now)


async def swept_at(redis: Redis, moment: datetime) -> None:
    await redis.set(LAST_SWEEP_KEY, moment.timestamp())


async def test_a_healthy_tocsin_pings_the_external_check(
    sessionmaker: Sessionmaker,
    redis: Redis,
    http: httpx.AsyncClient,
    monitored: Settings,
    recorder: Recorder,
) -> None:
    await swept_at(redis, NOW - timedelta(seconds=5))
    assert await beat(sessionmaker, redis, http, monitored) == []
    assert recorder.external == [("GET", EXTERNAL, "")]


async def test_nothing_is_sent_without_a_self_ping_url(
    sessionmaker: Sessionmaker, redis: Redis, http: httpx.AsyncClient, settings: Settings
) -> None:
    assert await beat(sessionmaker, redis, http, settings) is None


async def test_a_stalled_sweep_is_reported_through_fail(
    sessionmaker: Sessionmaker,
    redis: Redis,
    http: httpx.AsyncClient,
    monitored: Settings,
    recorder: Recorder,
) -> None:
    await swept_at(redis, NOW - timedelta(minutes=7))
    async with sessionmaker() as session, session.begin():
        session.add(
            Check(
                name="backup",
                schedule="@hourly",
                state="up",
                next_expected_at=NOW - timedelta(hours=1),
            )
        )

    problems = await beat(sessionmaker, redis, http, monitored)

    assert problems == [
        "1 overdue checks have not been marked down",
        "the last sweep finished 7m ago",
    ]
    assert recorder.external == [("POST", f"{EXTERNAL}/fail", "\n".join(problems))]


# On a fresh install the first heartbeat may come before the first sweep;
# that only becomes a problem after the usual two-minute allowance.
async def test_a_missing_first_sweep_is_only_reported_after_a_while(
    sessionmaker: Sessionmaker, redis: Redis, http: httpx.AsyncClient, monitored: Settings
) -> None:
    assert await beat(sessionmaker, redis, http, monitored) == []
    later = await beat(sessionmaker, redis, http, monitored, NOW + timedelta(minutes=3))
    assert later == ["no sweep has finished in 3m"]


async def test_failed_and_stuck_alerts_are_reported(
    sessionmaker: Sessionmaker, redis: Redis, http: httpx.AsyncClient, monitored: Settings
) -> None:
    await swept_at(redis, NOW)
    async with sessionmaker() as session, session.begin():
        check = Check(name="backup", schedule="@hourly", state="down")
        channel = Channel(kind="telegram", name="phone", config={})
        session.add_all([check, channel])
        await session.flush()
        events = [
            Event(check_id=check.id, from_state="up", to_state="down", reason="x", created_at=NOW)
            for _ in range(2)
        ]
        session.add_all(events)
        await session.flush()
        await session.execute(
            insert(Delivery),
            [
                {
                    "event_id": events[0].id,
                    "channel_id": channel.id,
                    "status": "failed",
                    "last_error": "Telegram refused the message (403): bot was blocked",
                    "updated_at": NOW - timedelta(minutes=3),
                },
                {
                    "event_id": events[1].id,
                    "channel_id": channel.id,
                    "status": "retrying",
                    "next_attempt_at": NOW - timedelta(minutes=10),
                },
            ],
        )

    problems = await beat(sessionmaker, redis, http, monitored)

    assert problems == [
        "1 alerts could not be delivered in the last 15m "
        "(latest error: Telegram refused the message (403): bot was blocked)",
        "1 alerts are waiting with nothing retrying them",
    ]


async def test_an_unhealthy_api_is_reported(
    sessionmaker: Sessionmaker, redis: Redis, monitored: Settings
) -> None:
    monitored.self_check_api_url = "http://api:8000"
    await swept_at(redis, NOW)
    http = httpx.AsyncClient(transport=httpx.MockTransport(Recorder(api_status=503)))

    problems = await beat(sessionmaker, redis, http, monitored)

    assert problems == ['the API is unhealthy (503: {"status":"ok"})']


async def test_an_unreachable_external_monitor_does_not_raise(
    sessionmaker: Sessionmaker, redis: Redis, monitored: Settings
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    await swept_at(redis, NOW)
    http = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    assert await beat(sessionmaker, redis, http, monitored) == []


async def test_an_unreachable_redis_is_reported(
    sessionmaker: Sessionmaker, http: httpx.AsyncClient, monitored: Settings
) -> None:
    from tocsin.redis_client import fast_failing

    unreachable = fast_failing("redis://127.0.0.1:1/0")
    try:
        problems = await beat(sessionmaker, unreachable, http, monitored)
    finally:
        await unreachable.aclose()
    assert problems is not None and problems[0].startswith("Redis is unreachable")
