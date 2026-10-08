from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy import func, insert, select

from tests.conftest import RecordingDispatcher
from tests.helpers import FakeSender, add_channel
from tocsin.alerts import deliver
from tocsin.config import Settings
from tocsin.db import Sessionmaker
from tocsin.models import Check, Delivery, Event, State, check_channels
from tocsin.pings import Signal, record_ping
from tocsin.redis_client import fast_failing
from tocsin.sweep import run_sweep, sweep

NOW = datetime(2026, 5, 1, 12, tzinfo=UTC)


async def add_checks(
    sessionmaker: Sessionmaker,
    count: int,
    *,
    state: State = State.UP,
    deadline: datetime = NOW - timedelta(minutes=1),
    channel_id: str | None = None,
) -> list[uuid.UUID]:
    async with sessionmaker() as session, session.begin():
        checks = [
            Check(
                name=f"job {i}",
                schedule="@hourly",
                state=state,
                last_ping_at=deadline - timedelta(hours=1),
                due_at=deadline - timedelta(minutes=5),
                next_expected_at=deadline,
            )
            for i in range(count)
        ]
        session.add_all(checks)
        await session.flush()
        if channel_id is not None:
            await session.execute(
                insert(check_channels),
                [{"check_id": c.id, "channel_id": uuid.UUID(channel_id)} for c in checks],
            )
        return [check.id for check in checks]


async def events_by_check(sessionmaker: Sessionmaker) -> Counter[uuid.UUID]:
    async with sessionmaker() as session:
        return Counter(await session.scalars(select(Event.check_id)))


async def test_sweep_moves_only_overdue_up_checks_down(sessionmaker: Sessionmaker) -> None:
    [overdue] = await add_checks(sessionmaker, 1)
    await add_checks(sessionmaker, 1, deadline=NOW + timedelta(minutes=1))
    for state in (State.NEW, State.PAUSED, State.DOWN):
        await add_checks(sessionmaker, 1, state=state)

    async with sessionmaker() as session, session.begin():
        [event] = await sweep(session, NOW)

    assert event.check_id == overdue
    assert (event.from_state, event.to_state) == ("up", "down")
    assert event.reason == "no ping since 2026-05-01 10:59 UTC; it was due at 2026-05-01 11:54 UTC"


# Two sweeps at once, step by step: the second one skips the row the first
# one has locked instead of waiting for it, and once the first commits the row
# no longer matches.
async def test_a_second_sweep_skips_rows_the_first_one_holds(sessionmaker: Sessionmaker) -> None:
    await add_checks(sessionmaker, 1)

    async with sessionmaker() as first:
        async with first.begin():
            assert len(await sweep(first, NOW)) == 1

            async with sessionmaker() as second, second.begin():
                assert await sweep(second, NOW) == []

    async with sessionmaker() as third, third.begin():
        assert await sweep(third, NOW) == []
    assert sum((await events_by_check(sessionmaker)).values()) == 1


# Section 16: "Two concurrent sweeps produce one event and one alert." Four
# sweeps run at once over forty overdue checks, in small batches so they
# interleave, and every delivery job they queue is then run twice.
async def test_concurrent_sweeps_produce_one_event_and_one_alert_per_check(
    sessionmaker: Sessionmaker, settings: Settings
) -> None:
    channel_id = await add_channel(sessionmaker)
    check_ids = await add_checks(sessionmaker, 40, channel_id=channel_id)
    dispatchers = [RecordingDispatcher() for _ in range(4)]

    swept = await asyncio.gather(*(run_sweep(sessionmaker, d, batch=5) for d in dispatchers))

    assert sum(swept) == 40
    assert await events_by_check(sessionmaker) == Counter({check_id: 1 for check_id in check_ids})
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(Delivery)) == 40

    sender = FakeSender()
    keys = [key for d in dispatchers for key in d.keys]
    await asyncio.gather(
        *(
            deliver(sessionmaker, {"telegram": sender}, settings, key, datetime.now(UTC))
            for key in keys + keys
        )
    )
    assert len(sender.sent) == 40
    assert len({alert.check_id for alert in sender.sent}) == 40


# A success ping that arrives while the sweep holds the row waits for it, then
# sees the check down and records a recovery, rather than overwriting a state
# it read before the sweep changed it.
async def test_a_ping_during_a_sweep_waits_and_reads_as_a_recovery(
    sessionmaker: Sessionmaker,
) -> None:
    await add_checks(sessionmaker, 1)
    async with sessionmaker() as session:
        check = await session.scalar(select(Check))
    assert check is not None

    async def ping() -> None:
        async with sessionmaker() as session, session.begin():
            await record_ping(session, check.ping_key, Signal.parse(None), b"", None, NOW, 100)

    async with sessionmaker() as sweeper:
        async with sweeper.begin():
            await sweep(sweeper, NOW)
            pinging = asyncio.ensure_future(ping())
            await asyncio.sleep(0.3)
            assert not pinging.done()
        await pinging

    async with sessionmaker() as session:
        events = list(await session.scalars(select(Event).order_by(Event.id)))
    assert [(e.from_state, e.to_state) for e in events] == [("up", "down"), ("down", "up")]


# Section 16: "A check scheduled across a daylight saving change is not
# falsely marked late." New York falls back on 1 November 2026, so a daily
# 03:00 job runs at 08:00 UTC that day instead of 07:00 UTC.
async def test_a_check_is_not_marked_down_early_across_a_dst_change(
    sessionmaker: Sessionmaker,
) -> None:
    async with sessionmaker() as session, session.begin():
        check = Check(
            name="backup", schedule="0 3 * * *", timezone="America/New_York", grace_seconds=600
        )
        session.add(check)
    async with sessionmaker() as session, session.begin():
        ping_time = datetime(2026, 10, 31, 7, 0, 30, tzinfo=UTC)
        await record_ping(session, check.ping_key, Signal.parse(None), b"", None, ping_time, 100)

    async def sweep_at(*time: int) -> list[Event]:
        async with sessionmaker() as session, session.begin():
            return await sweep(session, datetime(2026, 11, 1, *time, tzinfo=UTC))

    # Adding 24 hours would have expected it at 07:00 UTC and given up at 07:10.
    assert await sweep_at(7, 15) == []
    assert await sweep_at(8, 5) == []
    [event] = await sweep_at(8, 11)
    assert "it was due at 2026-11-01 03:00 EST" in event.reason


async def test_run_sweep_returns_early_while_another_sweep_holds_the_lock(
    sessionmaker: Sessionmaker, redis: Redis
) -> None:
    await add_checks(sessionmaker, 1)
    dispatcher = RecordingDispatcher()

    lock = redis.lock("tocsin:sweep", timeout=60)
    assert await lock.acquire()
    assert await run_sweep(sessionmaker, dispatcher, redis) == 0
    await lock.release()
    assert await run_sweep(sessionmaker, dispatcher, redis) == 1


async def test_run_sweep_still_sweeps_when_redis_is_down(sessionmaker: Sessionmaker) -> None:
    await add_checks(sessionmaker, 1)
    unreachable = fast_failing("redis://127.0.0.1:1/0")
    try:
        assert await run_sweep(sessionmaker, RecordingDispatcher(), unreachable) == 1
    finally:
        await unreachable.aclose()


async def test_run_sweep_works_through_every_batch(sessionmaker: Sessionmaker) -> None:
    channel_id = await add_channel(sessionmaker)
    await add_checks(sessionmaker, 7, channel_id=channel_id)
    dispatcher = RecordingDispatcher()

    assert await run_sweep(sessionmaker, dispatcher, batch=3) == 7
    assert len(dispatcher.keys) == 7
