from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select

from tests.helpers import create_check
from tocsin.db import Sessionmaker
from tocsin.models import Check, Event, Run
from tocsin.pings import Signal, record_ping
from tocsin.sweep import sweep

T0 = datetime(2026, 5, 1, 3, 0, tzinfo=UTC)


async def ping(
    sessionmaker: Sessionmaker, check_id: str, signal: str | None, at: datetime, body: bytes = b""
) -> None:
    async with sessionmaker() as session, session.begin():
        check = await session.get_one(Check, check_id)
        await record_ping(session, check.ping_key, Signal.parse(signal), body, None, at, 10_000)


async def sweep_at(sessionmaker: Sessionmaker, at: datetime) -> list[Event]:
    async with sessionmaker() as session, session.begin():
        return await sweep(session, at)


async def runs(sessionmaker: Sessionmaker) -> list[Run]:
    async with sessionmaker() as session:
        return list(await session.scalars(select(Run).order_by(Run.id)))


async def test_start_and_finish_make_one_run_with_a_duration(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check = await create_check(client)
    await ping(sessionmaker, check["id"], "start", T0)
    await ping(sessionmaker, check["id"], "0", T0 + timedelta(minutes=12, seconds=3), b"done")

    [run] = await runs(sessionmaker)
    assert (run.status, run.started_at, run.duration_ms) == ("success", T0, 723_000)
    assert (run.exit_code, run.output_tail) == (0, "done")
    read = (await client.get(f"/api/checks/{check['id']}")).json()
    assert read["running_since"] is None


async def test_a_failed_run_keeps_its_exit_code_and_output(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check = await create_check(client)
    await ping(sessionmaker, check["id"], "start", T0)
    await ping(sessionmaker, check["id"], "3", T0 + timedelta(seconds=5), b"permission denied")

    [run] = await runs(sessionmaker)
    assert (run.status, run.exit_code, run.duration_ms) == ("fail", 3, 5000)
    assert run.output_tail == "permission denied"


async def test_a_second_start_marks_the_first_run_unfinished(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check = await create_check(client)
    await ping(sessionmaker, check["id"], "start", T0)
    await ping(sessionmaker, check["id"], "start", T0 + timedelta(hours=1))
    await ping(sessionmaker, check["id"], None, T0 + timedelta(hours=1, minutes=1))

    first, second = await runs(sessionmaker)
    assert (first.status, first.finished_at) == ("unfinished", None)
    assert (second.status, second.duration_ms) == ("success", 60_000)


async def test_a_finish_without_a_start_has_no_duration(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check = await create_check(client)
    await ping(sessionmaker, check["id"], None, T0)
    [run] = await runs(sessionmaker)
    assert (run.status, run.started_at, run.duration_ms) == ("success", None, None)


# Section 16: "A job that starts and never finishes is detected." With no
# maximum duration, it is caught when its next run was due.
async def test_a_job_that_starts_and_never_finishes_is_detected(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check = await create_check(client, schedule="0 3 * * *", grace_seconds=600)
    await ping(sessionmaker, check["id"], "start", T0)

    assert await sweep_at(sessionmaker, T0 + timedelta(hours=23)) == []
    [event] = await sweep_at(sessionmaker, T0 + timedelta(days=1, minutes=11))
    assert event.reason == "started 2026-05-01 03:00 UTC and never finished"


async def test_a_run_longer_than_the_maximum_is_detected_while_running(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check = await create_check(client, schedule="0 3 * * *", max_duration_seconds=1800)
    await ping(sessionmaker, check["id"], "start", T0)

    assert await sweep_at(sessionmaker, T0 + timedelta(minutes=29)) == []
    [event] = await sweep_at(sessionmaker, T0 + timedelta(minutes=31))
    assert event.reason == (
        "running for 31m, longer than the 30m maximum (started 2026-05-01 03:00 UTC)"
    )

    # Finishing late doesn't count as a recovery; the next run on time does.
    await ping(sessionmaker, check["id"], None, T0 + timedelta(minutes=45))
    await ping(sessionmaker, check["id"], "start", T0 + timedelta(days=1))
    await ping(sessionmaker, check["id"], None, T0 + timedelta(days=1, minutes=10))
    async with sessionmaker() as session:
        events = list(await session.scalars(select(Event).order_by(Event.id)))
    assert [(e.from_state, e.to_state) for e in events] == [
        ("new", "up"),
        ("up", "down"),
        ("down", "up"),
    ]


# If the run finishes too late before a sweep notices, the finish ping
# reports it instead, so the alert doesn't depend on timing.
async def test_a_run_that_finishes_after_the_maximum_alerts_on_finish(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check = await create_check(client, max_duration_seconds=600)
    await ping(sessionmaker, check["id"], "start", T0)
    await ping(sessionmaker, check["id"], None, T0 + timedelta(minutes=10, seconds=30))

    async with sessionmaker() as session:
        event = await session.scalar(select(Event).order_by(Event.id.desc()))
    assert event is not None
    assert (event.to_state, event.reason) == (
        "down",
        "the run took 10m 30s, longer than the 10m maximum",
    )


async def test_runs_api_lists_newest_first_with_paging(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check = await create_check(client)
    for minute in range(3):
        await ping(sessionmaker, check["id"], "start", T0 + timedelta(minutes=minute))
        await ping(sessionmaker, check["id"], None, T0 + timedelta(minutes=minute, seconds=20))

    page = (await client.get(f"/api/checks/{check['id']}/runs", params={"limit": 2})).json()
    assert [run["duration_ms"] for run in page] == [20_000, 20_000]
    assert page[0]["id"] > page[1]["id"]
    rest = (
        await client.get(f"/api/checks/{check['id']}/runs", params={"before": page[-1]["id"]})
    ).json()
    assert len(rest) == 1
    missing = await client.get("/api/checks/00000000-0000-0000-0000-000000000000/runs")
    assert missing.status_code == 404


async def test_max_duration_can_be_set_and_changed(client: httpx.AsyncClient) -> None:
    check = await create_check(client, max_duration_seconds=900)
    assert check["max_duration_seconds"] == 900
    updated = await client.patch(f"/api/checks/{check['id']}", json={"max_duration_seconds": 60})
    assert updated.json()["max_duration_seconds"] == 60
    bad = await client.patch(f"/api/checks/{check['id']}", json={"max_duration_seconds": 0})
    assert bad.status_code == 422
