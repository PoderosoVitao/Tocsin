from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from redis.asyncio.lock import Lock
from redis.exceptions import LockError, RedisError
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import QueryableAttribute

from tocsin.alerts import Dispatcher, dispatch_pending
from tocsin.db import Sessionmaker
from tocsin.humanize import duration, local_time
from tocsin.metrics import record_sweep
from tocsin.models import Check, Event, State
from tocsin.transitions import alert, transition


# Says why a check is overdue: a run that started and is taking too long, a
# run that started and never reported finishing, or no ping at all.
def overdue_reason(check: Check, now: datetime) -> str:
    if check.last_start_at is not None:
        started = local_time(check.last_start_at, check.timezone)
        limit = check.max_duration_seconds
        running = now - check.last_start_at
        if limit is not None and running >= timedelta(seconds=limit):
            return (
                f"running for {duration(running)}, longer than the {duration(limit)} maximum "
                f"(started {started})"
            )
        return f"started {started} and never finished"
    return (
        f"no ping since {local_time(check.last_ping_at, check.timezone)}; "
        f"it was due at {local_time(check.due_at, check.timezone)}"
    )


# Moves up to `limit` overdue checks to down, in the caller's transaction, and
# returns the events it wrote (each with its pending deliveries).
#
# This is what makes a missed run produce exactly one alert, however many
# sweeps run at once. Each overdue row is locked with FOR UPDATE SKIP LOCKED:
# a second sweep running at the same moment skips the rows the first one
# holds instead of waiting for them, and by the time those locks are released
# the rows are down and no longer match. The state change, its event and its
# deliveries are written in the one transaction, so they exist together or
# not at all. A ping for a locked check waits for the sweep to commit and then
# sees the new state, so a late success ping reads as a recovery, not as a
# check that was never down.
async def sweep_overdue(session: AsyncSession, now: datetime, limit: int) -> list[Event]:
    overdue = await session.scalars(
        select(Check)
        .where(Check.state == State.UP, Check.next_expected_at <= now)
        .order_by(Check.next_expected_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return [
        await transition(session, check, State.DOWN, overdue_reason(check, now), now)
        for check in overdue
    ]


# A timestamp column plus a number of seconds held in another column, in SQL.
def _plus_seconds(
    moment: QueryableAttribute[Any], seconds: QueryableAttribute[Any]
) -> ColumnElement[Any]:
    return moment + func.make_interval(0, 0, 0, 0, 0, 0, seconds)


# Sends one alert for each check whose cooldown has ended in a different state
# from the one its last alert reported, so a check that flapped and then
# settled is never left looking the way the last message said. Changes that
# land back where the last alert left the check send nothing.
async def sweep_settled(session: AsyncSession, now: datetime, limit: int) -> list[Event]:
    settled = await session.scalars(
        select(Check)
        .where(
            Check.state.in_([State.UP, State.DOWN]),
            Check.last_alerted_state.is_not(None),
            Check.state != Check.last_alerted_state,
            _plus_seconds(Check.last_alert_at, Check.cooldown_seconds) <= now,
        )
        .order_by(Check.last_alert_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    events = []
    for check in settled:
        held_back = select(Event).where(
            Event.check_id == check.id,
            Event.suppressed,
            Event.created_at > check.last_alert_at,
        )
        changes = await session.scalar(select(func.count()).select_from(held_back.subquery()))
        latest = await session.scalar(held_back.order_by(Event.id.desc()).limit(1))
        cause = latest.reason if latest is not None else "its state changed"
        times = "once" if changes == 1 else f"{changes} times"
        event = Event(
            check_id=check.id,
            run_id=latest.run_id if latest is not None else None,
            from_state=check.last_alerted_state,
            to_state=check.state,
            reason=(
                f"{cause} (it changed state {times} during the "
                f"{duration(check.cooldown_seconds)} cooldown after the last alert)"
            ),
            created_at=now,
        )
        session.add(event)
        await session.flush()
        await alert(session, check, event, now)
        events.append(event)
    return events


# Repeats the alert for checks that are still down, every reminder_seconds
# since the last alert, until they recover.
async def sweep_reminders(session: AsyncSession, now: datetime, limit: int) -> list[Event]:
    due = await session.scalars(
        select(Check)
        .where(
            Check.state == State.DOWN,
            Check.last_alerted_state == State.DOWN,
            Check.reminder_seconds.is_not(None),
            _plus_seconds(Check.last_alert_at, Check.reminder_seconds) <= now,
        )
        .order_by(Check.last_alert_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    events = []
    for check in due:
        went_down = await session.scalar(
            select(Event)
            .where(
                Event.check_id == check.id,
                Event.to_state == State.DOWN,
                Event.from_state != State.DOWN,
            )
            .order_by(Event.id.desc())
            .limit(1)
        )
        since = went_down.created_at if went_down is not None else check.last_alert_at
        cause = f": {went_down.reason}" if went_down is not None else ""
        event = Event(
            check_id=check.id,
            run_id=went_down.run_id if went_down is not None else None,
            from_state=State.DOWN,
            to_state=State.DOWN,
            reason=f"down since {local_time(since, check.timezone)}{cause}",
            created_at=now,
        )
        session.add(event)
        await session.flush()
        await alert(session, check, event, now)
        events.append(event)
    return events


# Everything the scheduled sweep does, in the caller's transaction: overdue
# checks go down, checks that settled after a cooldown get their alert, and
# checks that stayed down get their reminder. Each pass claims its rows with
# FOR UPDATE SKIP LOCKED and changes them so they no longer match, which is
# what keeps every one of these alerts to exactly one, however many sweeps run.
async def sweep(session: AsyncSession, now: datetime, limit: int = 100) -> list[Event]:
    return [
        *await sweep_overdue(session, now, limit),
        *await sweep_settled(session, now, limit),
        *await sweep_reminders(session, now, limit),
    ]


# One scheduled sweep: sweeps in batches until nothing overdue is left (or a
# bounded number of batches has run), queueing the alerts after each commit.
#
# The Redis lock only saves work. When sweeps are queued faster than they
# finish, the extras return at once instead of competing for rows. Nothing
# depends on it for correctness: a Redis lock can expire in the middle of a
# sweep (a slow database, a paused process), and then the row locks above are
# what keep two sweeps from alerting twice. If Redis can't be reached, the
# sweep runs without the lock.
async def run_sweep(
    sessionmaker: Sessionmaker,
    dispatcher: Dispatcher,
    redis: Redis | None = None,
    batch: int = 100,
    max_batches: int = 20,
) -> int:
    lock: Lock | None = None
    if redis is not None:
        lock = redis.lock("tocsin:sweep", timeout=60, blocking=False)
        try:
            if not await lock.acquire():
                return 0
        except RedisError:
            lock = None

    try:
        total = 0
        for _ in range(max_batches):
            async with sessionmaker() as session:
                async with session.begin():
                    events = await sweep(session, datetime.now(UTC), batch)
                await dispatch_pending(session, dispatcher)
            total += len(events)
            if len(events) < batch:
                break
        await record_sweep(redis)
        return total
    finally:
        if lock is not None:
            try:
                await lock.release()
            except (LockError, RedisError):
                pass
