from __future__ import annotations

import asyncio
import random
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from tests.conftest import RecordingDispatcher
from tests.helpers import FakeSender, add_channel, create_check
from tocsin.alerts import DeliveryKey, backoff, claim_due_deliveries, deliver
from tocsin.channels import PermanentError, TransientError
from tocsin.config import Settings
from tocsin.db import Sessionmaker
from tocsin.models import Delivery


async def deliveries(sessionmaker: Sessionmaker) -> list[Delivery]:
    async with sessionmaker() as session:
        return list(await session.scalars(select(Delivery).order_by(Delivery.id)))


async def a_failure(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker, dispatcher: RecordingDispatcher
) -> DeliveryKey:
    await add_channel(sessionmaker)
    check = await create_check(client)
    await client.get(check["ping_path"])
    await client.post(f"{check['ping_path']}/2", content=b"step 1 ok\nNo space left on device\n")
    [key] = dispatcher.keys
    return key


def run_delivery(
    sessionmaker: Sessionmaker,
    sender: FakeSender,
    settings: Settings,
    key: DeliveryKey,
    now: datetime | None = None,
) -> asyncio.Future[str]:
    return asyncio.ensure_future(
        deliver(
            sessionmaker,
            {"telegram": sender},
            settings,
            key,
            now or datetime.now(UTC),
            random.Random(0),
        )
    )


async def test_a_failure_creates_one_pending_delivery_per_channel(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker, dispatcher: RecordingDispatcher
) -> None:
    await add_channel(sessionmaker, "phone")
    await add_channel(sessionmaker, "team")
    check = await create_check(client)
    await client.get(check["ping_path"])
    await client.get(f"{check['ping_path']}/fail")

    rows = await deliveries(sessionmaker)
    assert len(rows) == 2
    assert {row.status for row in rows} == {"pending"}
    assert sorted(dispatcher.keys) == sorted(DeliveryKey(r.event_id, r.channel_id) for r in rows)


async def test_only_failures_and_recoveries_alert(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker, dispatcher: RecordingDispatcher
) -> None:
    await add_channel(sessionmaker)
    check = await create_check(client)
    await client.get(check["ping_path"])  # new -> up: no alert
    await client.patch(f"/api/checks/{check['id']}", json={"paused": True})
    await client.patch(f"/api/checks/{check['id']}", json={"paused": False})
    assert dispatcher.keys == []

    await client.get(f"{check['ping_path']}/fail")  # up -> down
    await client.get(check["ping_path"])  # down -> up
    assert len(dispatcher.keys) == 2


async def test_deliver_sends_the_alert_and_records_it(
    client: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    dispatcher: RecordingDispatcher,
    settings: Settings,
) -> None:
    settings.dashboard_url = "https://dash.test"
    key = await a_failure(client, sessionmaker, dispatcher)
    sender = FakeSender("msg-42")

    assert await run_delivery(sessionmaker, sender, settings, key) == "sent"

    [alert] = sender.sent
    assert alert.headline == "backup is down"
    assert alert.reason == "the job exited with code 2"
    assert alert.output_excerpt == "step 1 ok\nNo space left on device"
    assert alert.link == f"https://dash.test/#/checks/{alert.check_id}"
    [row] = await deliveries(sessionmaker)
    assert (row.status, row.attempts, row.external_id) == ("sent", 1, "msg-42")


# Section 16: "A retried alert delivery sends once." A duplicate job for a
# delivery that already went out finds the row sent and does nothing.
async def test_a_retried_delivery_sends_once(
    client: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    dispatcher: RecordingDispatcher,
    settings: Settings,
) -> None:
    key = await a_failure(client, sessionmaker, dispatcher)
    sender = FakeSender()

    results = [await run_delivery(sessionmaker, sender, settings, key) for _ in range(3)]

    assert results == ["sent", "sent", "sent"]
    assert len(sender.calls) == 1


# Two workers given the same job at the same moment: one holds the row lock
# while it sends, the other skips the locked row instead of sending too.
async def test_concurrent_deliveries_of_the_same_alert_send_once(
    client: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    dispatcher: RecordingDispatcher,
    settings: Settings,
) -> None:
    key = await a_failure(client, sessionmaker, dispatcher)
    sender = FakeSender(delay=0.2)

    results = await asyncio.gather(
        *(run_delivery(sessionmaker, sender, settings, key) for _ in range(4))
    )

    assert sorted(results) == ["busy", "busy", "busy", "sent"]
    assert len(sender.calls) == 1


async def test_a_transient_failure_schedules_a_retry(
    client: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    dispatcher: RecordingDispatcher,
    settings: Settings,
) -> None:
    key = await a_failure(client, sessionmaker, dispatcher)
    now = datetime.now(UTC)
    sender = FakeSender(TransientError("Telegram error 502: Bad Gateway"))

    assert await run_delivery(sessionmaker, sender, settings, key, now) == "retrying"

    [row] = await deliveries(sessionmaker)
    assert row.attempts == 1
    assert row.last_error == "Telegram error 502: Bad Gateway"
    assert row.next_attempt_at is not None
    assert now + timedelta(seconds=2.5) <= row.next_attempt_at <= now + timedelta(seconds=5)


async def test_the_channels_retry_after_is_respected(
    client: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    dispatcher: RecordingDispatcher,
    settings: Settings,
) -> None:
    key = await a_failure(client, sessionmaker, dispatcher)
    now = datetime.now(UTC)
    sender = FakeSender(TransientError("Telegram rate limit", retry_after=120))

    await run_delivery(sessionmaker, sender, settings, key, now)

    [row] = await deliveries(sessionmaker)
    assert row.next_attempt_at == now + timedelta(seconds=120)


async def test_a_delivery_is_marked_failed_after_the_last_attempt(
    client: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    dispatcher: RecordingDispatcher,
    settings: Settings,
) -> None:
    settings.delivery_max_attempts = 3
    key = await a_failure(client, sessionmaker, dispatcher)
    sender = FakeSender(*(TransientError("down") for _ in range(5)))

    results = [await run_delivery(sessionmaker, sender, settings, key) for _ in range(4)]

    assert results == ["retrying", "retrying", "failed", "failed"]
    assert len(sender.calls) == 3
    [row] = await deliveries(sessionmaker)
    assert (row.status, row.last_error, row.next_attempt_at) == ("failed", "down", None)


async def test_a_permanent_failure_is_not_retried(
    client: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    dispatcher: RecordingDispatcher,
    settings: Settings,
) -> None:
    key = await a_failure(client, sessionmaker, dispatcher)
    sender = FakeSender(PermanentError("Telegram refused the message (403): bot was blocked"))

    assert await run_delivery(sessionmaker, sender, settings, key) == "failed"
    assert await run_delivery(sessionmaker, sender, settings, key) == "failed"
    assert len(sender.calls) == 1


async def test_the_relay_picks_up_due_deliveries_once_per_lease(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker, dispatcher: RecordingDispatcher
) -> None:
    key = await a_failure(client, sessionmaker, dispatcher)
    now = datetime.now(UTC)

    async def claim(at: datetime) -> list[DeliveryKey]:
        async with sessionmaker() as session, session.begin():
            return await claim_due_deliveries(session, at)

    # Freshly created: the direct enqueue gets the first go.
    assert await claim(now) == []
    # Still pending after the relay delay, so the direct enqueue was lost.
    assert await claim(now + timedelta(seconds=31)) == [key]
    # Claimed: left alone until the lease runs out.
    assert await claim(now + timedelta(seconds=40)) == []
    assert await claim(now + timedelta(seconds=92)) == [key]


async def test_a_failed_attempt_is_retried_by_the_relay_and_sent_once(
    client: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    dispatcher: RecordingDispatcher,
    settings: Settings,
) -> None:
    key = await a_failure(client, sessionmaker, dispatcher)
    sender = FakeSender(TransientError("timeout"))
    now = datetime.now(UTC)

    assert await run_delivery(sessionmaker, sender, settings, key, now) == "retrying"
    later = now + timedelta(seconds=10)
    async with sessionmaker() as session, session.begin():
        assert await claim_due_deliveries(session, later) == [key]
    assert await run_delivery(sessionmaker, sender, settings, key, later) == "sent"
    assert await run_delivery(sessionmaker, sender, settings, key, later) == "sent"

    assert len(sender.calls) == 2
    assert len(sender.sent) == 1


async def test_an_unknown_delivery_is_reported_busy(
    sessionmaker: Sessionmaker, settings: Settings
) -> None:
    key = DeliveryKey(1, uuid.uuid4())
    assert await run_delivery(sessionmaker, FakeSender(), settings, key) == "busy"


@pytest.mark.parametrize(("attempt", "ceiling"), [(1, 5), (2, 10), (3, 20), (12, 1800)])
def test_backoff_grows_exponentially_with_jitter_up_to_the_cap(attempt: int, ceiling: int) -> None:
    rng = random.Random(1)
    delays = [backoff(attempt, 5, 1800, rng).total_seconds() for _ in range(200)]
    assert all(ceiling / 2 <= d <= ceiling for d in delays)
    assert len(set(delays)) > 1
