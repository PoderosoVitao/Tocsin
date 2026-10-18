from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from tocsin.config import Settings
from tocsin.db import Sessionmaker
from tocsin.humanize import duration
from tocsin.metrics import LAST_SWEEP_KEY, count_unswept
from tocsin.models import Delivery, DeliveryStatus

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 60
WAITING_FOR_SWEEP_KEY = "tocsin:waiting_for_first_sweep"
# The sweep runs every five seconds; this long without one means it stopped.
SWEEP_STALE_AFTER = timedelta(minutes=2)
# Failed deliveries are reported for this long, so the external monitor sees
# them even if a heartbeat or two is lost.
FAILED_DELIVERY_WINDOW = timedelta(minutes=15)
# A delivery this far past its next attempt means nothing is retrying it.
STUCK_DELIVERY_AFTER = timedelta(minutes=5)


async def _database_problems(sessionmaker: Sessionmaker, now: datetime) -> list[str]:
    problems = []
    async with sessionmaker() as session:
        unswept = await count_unswept(session, now)
        if unswept:
            problems.append(f"{unswept} overdue checks have not been marked down")

        failed = select(Delivery).where(
            Delivery.status == DeliveryStatus.FAILED,
            Delivery.updated_at >= now - FAILED_DELIVERY_WINDOW,
        )
        failed_count = await session.scalar(select(func.count()).select_from(failed.subquery()))
        if failed_count:
            latest = await session.scalar(failed.order_by(Delivery.updated_at.desc()).limit(1))
            error = latest.last_error if latest is not None else None
            problems.append(
                f"{failed_count} alerts could not be delivered in the last "
                f"{duration(FAILED_DELIVERY_WINDOW)} (latest error: {error})"
            )

        stuck = await session.scalar(
            select(func.count())
            .select_from(Delivery)
            .where(
                Delivery.status.in_([DeliveryStatus.PENDING, DeliveryStatus.RETRYING]),
                Delivery.next_attempt_at < now - STUCK_DELIVERY_AFTER,
            )
        )
        if stuck:
            problems.append(f"{stuck} alerts are waiting with nothing retrying them")
    return problems


# Whether sweeps are running. A fresh install has no sweep yet, so a missing
# one only counts once the first heartbeat is older than the usual allowance;
# otherwise the external monitor would raise a false alarm on first start.
async def _sweep_problem(redis: Redis, now: datetime) -> str | None:
    last_sweep = await redis.get(LAST_SWEEP_KEY)
    if last_sweep is not None:
        age = now - datetime.fromtimestamp(float(last_sweep), UTC)
        return f"the last sweep finished {duration(age)} ago" if age > SWEEP_STALE_AFTER else None

    await redis.set(WAITING_FOR_SWEEP_KEY, now.timestamp(), nx=True)
    waiting_since = float(await redis.get(WAITING_FOR_SWEEP_KEY) or now.timestamp())
    waited = now - datetime.fromtimestamp(waiting_since, UTC)
    return f"no sweep has finished in {duration(waited)}" if waited > SWEEP_STALE_AFTER else None


# Lists what is wrong with this tocsin, in words for a person; empty when all
# is well. The heartbeat task itself running proves the scheduler, Redis and
# a worker are alive; this adds what that can't show.
async def find_problems(
    sessionmaker: Sessionmaker,
    redis: Redis,
    http: httpx.AsyncClient,
    settings: Settings,
    now: datetime,
) -> list[str]:
    problems = []
    try:
        problems += await _database_problems(sessionmaker, now)
    except (SQLAlchemyError, OSError) as exc:
        problems.append(f"the database is unreachable ({type(exc).__name__})")

    try:
        problem = await _sweep_problem(redis, now)
    except RedisError as exc:
        problem = f"Redis is unreachable ({type(exc).__name__})"
    if problem:
        problems.append(problem)

    if settings.self_check_api_url:
        url = f"{settings.self_check_api_url.rstrip('/')}/healthz"
        try:
            response = await http.get(url, timeout=5)
            if response.status_code != 200:
                problems.append(f"the API is unhealthy ({response.status_code}: {response.text})")
        except httpx.HTTPError as exc:
            problems.append(f"the API is unreachable ({type(exc).__name__})")
    return problems


# Pings the external check: its plain URL when healthy, /fail with the list of
# problems when not, so the external monitor alerts at once with the reason
# rather than waiting for a heartbeat to go missing. Returns the problems, or
# None when no self_ping_url is configured.
async def heartbeat(
    sessionmaker: Sessionmaker,
    redis: Redis,
    http: httpx.AsyncClient,
    settings: Settings,
    now: datetime,
) -> list[str] | None:
    if not settings.self_ping_url:
        return None
    problems = await find_problems(sessionmaker, redis, http, settings, now)
    url = settings.self_ping_url.rstrip("/")
    try:
        if problems:
            await http.post(f"{url}/fail", content="\n".join(problems), timeout=10)
        else:
            await http.get(url, timeout=10)
    except httpx.HTTPError as exc:
        log.warning("could not reach the self-monitoring URL: %s", type(exc).__name__)
    return problems
