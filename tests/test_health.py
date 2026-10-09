from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from redis.asyncio import Redis
from sqlalchemy import update

from tests.conftest import RecordingDispatcher
from tests.helpers import create_check
from tocsin.db import Sessionmaker
from tocsin.models import Check
from tocsin.sweep import run_sweep


def metric(text: str, name: str) -> float:
    for line in text.splitlines():
        if line.startswith(name + " "):
            return float(line.split()[-1])
    raise AssertionError(f"{name} not in metrics")


async def test_healthz(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz", headers={"Authorization": ""})
    assert response.json() == {"status": "ok", "database": "ok", "redis": "ok"}


async def test_metrics_count_pings_and_states(client: httpx.AsyncClient) -> None:
    check = await create_check(client)
    before = metric((await client.get("/metrics")).text, 'tocsin_pings_total{kind="fail"}')
    await client.get(f"{check['ping_path']}/fail")
    await client.get("/ping/not-a-uuid")

    text = (await client.get("/metrics", headers={"Authorization": ""})).text
    assert metric(text, 'tocsin_pings_total{kind="fail"}') == before + 1
    assert metric(text, 'tocsin_pings_rejected_total{reason="not_found"}') >= 1
    assert metric(text, 'tocsin_checks{state="down"}') == 1
    assert metric(text, 'tocsin_checks{state="up"}') == 0


async def test_metrics_reveal_a_stalled_sweep(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker, redis: Redis
) -> None:
    check = await create_check(client)
    await client.get(check["ping_path"])
    long_ago = datetime.now(UTC) - timedelta(hours=1)
    async with sessionmaker() as session, session.begin():
        await session.execute(update(Check).values(next_expected_at=long_ago))

    text = (await client.get("/metrics")).text
    assert metric(text, "tocsin_checks_overdue_unswept") == 1

    await run_sweep(sessionmaker, RecordingDispatcher(), redis)
    text = (await client.get("/metrics")).text
    assert metric(text, "tocsin_checks_overdue_unswept") == 0
    assert metric(text, "tocsin_last_sweep_timestamp_seconds") > long_ago.timestamp()
