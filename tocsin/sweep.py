from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from redis.asyncio.lock import Lock
from redis.exceptions import LockError, RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tocsin.alerts import Dispatcher, dispatch_pending
from tocsin.db import Sessionmaker
from tocsin.models import Check, Event, State
from tocsin.transitions import transition


def _local(moment: datetime | None, timezone: str) -> str:
    if moment is None:
        return "never"
    return moment.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M %Z")


def overdue_reason(check: Check) -> str:
    return (
        f"no ping since {_local(check.last_ping_at, check.timezone)}; "
        f"it was due at {_local(check.due_at, check.timezone)}"
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
async def sweep(session: AsyncSession, now: datetime, limit: int = 100) -> list[Event]:
    overdue = await session.scalars(
        select(Check)
        .where(Check.state == State.UP, Check.next_expected_at <= now)
        .order_by(Check.next_expected_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return [
        await transition(session, check, State.DOWN, overdue_reason(check), now)
        for check in overdue
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
        return total
    finally:
        if lock is not None:
            try:
                await lock.release()
            except (LockError, RedisError):
                pass
