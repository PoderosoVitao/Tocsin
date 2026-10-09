from __future__ import annotations

from datetime import UTC, datetime, timedelta

from prometheus_client import Counter, Gauge, Histogram
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tocsin.models import Check, Delivery, DeliveryStatus, State

LAST_SWEEP_KEY = "tocsin:last_sweep"

# A check this far past its deadline and still up means the sweep is not
# running: it runs every few seconds.
SWEEP_LAG_TOLERANCE = timedelta(minutes=1)

PINGS = Counter("tocsin_pings_total", "Pings accepted", ["kind"])
PINGS_REJECTED = Counter("tocsin_pings_rejected_total", "Pings refused", ["reason"])
PING_SECONDS = Histogram(
    "tocsin_ping_duration_seconds",
    "Time to handle a ping",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
)

CHECKS = Gauge("tocsin_checks", "Checks by state", ["state"])
DELIVERIES = Gauge("tocsin_deliveries", "Alert deliveries by status", ["status"])
UNSWEPT = Gauge(
    "tocsin_checks_overdue_unswept",
    "Checks past their deadline for over a minute that are still up; above zero, the sweep "
    "is not running",
)
LAST_SWEEP = Gauge("tocsin_last_sweep_timestamp_seconds", "When a sweep last finished (Unix time)")


# Refreshes the gauges that describe stored state. They are read from the
# database at scrape time rather than counted as things happen, because the
# worker and scheduler run in other processes and the database is the one
# place that knows the whole picture.
async def refresh_state_gauges(session: AsyncSession, redis: Redis, now: datetime) -> None:
    CHECKS.clear()
    for known_state in (State.NEW, State.UP, State.DOWN, State.PAUSED):
        CHECKS.labels(state=known_state).set(0)
    for state, count in await session.execute(
        select(Check.state, func.count()).group_by(Check.state)
    ):
        CHECKS.labels(state=state).set(count)

    DELIVERIES.clear()
    for known_status in DeliveryStatus:
        DELIVERIES.labels(status=known_status).set(0)
    for status, count in await session.execute(
        select(Delivery.status, func.count()).group_by(Delivery.status)
    ):
        DELIVERIES.labels(status=status).set(count)

    unswept = await session.scalar(
        select(func.count())
        .select_from(Check)
        .where(Check.state == State.UP, Check.next_expected_at < now - SWEEP_LAG_TOLERANCE)
    )
    UNSWEPT.set(unswept or 0)

    try:
        last_sweep = await redis.get(LAST_SWEEP_KEY)
    except RedisError:
        last_sweep = None
    if last_sweep is not None:
        LAST_SWEEP.set(float(last_sweep))


async def record_sweep(redis: Redis | None) -> None:
    if redis is None:
        return
    try:
        await redis.set(LAST_SWEEP_KEY, datetime.now(UTC).timestamp())
    except RedisError:
        pass
