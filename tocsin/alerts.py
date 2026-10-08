from __future__ import annotations

import logging
import random
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import NamedTuple, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tocsin.channels import Alert, PermanentError, Sender, TransientError
from tocsin.config import Settings
from tocsin.db import Sessionmaker
from tocsin.models import (
    Channel,
    Check,
    Delivery,
    DeliveryStatus,
    Event,
    Run,
    State,
    check_channels,
)

log = logging.getLogger(__name__)

# A new delivery is queued straight after its transaction commits. The relay
# only picks it up if it is still pending this long afterwards, which means
# the direct enqueue was lost (Redis down, or a crash between commit and
# enqueue).
RELAY_DELAY = timedelta(seconds=30)
# How long a delivery the relay has queued is left alone before the relay
# queues it again, in case that job was lost too.
RELAY_LEASE = timedelta(seconds=60)

OUTPUT_EXCERPT_LINES = 15
OUTPUT_EXCERPT_CHARS = 1500

# Where pending deliveries wait to be queued once the transaction that
# created them commits. Kept on the session so every code path that changes
# state hands its deliveries to the same place.
PENDING_KEY = "tocsin.pending_deliveries"


class DeliveryKey(NamedTuple):
    event_id: int
    channel_id: uuid.UUID


class Dispatcher(Protocol):
    async def deliver(self, keys: Sequence[DeliveryKey]) -> None: ...


def should_alert(from_state: str, to_state: str) -> bool:
    return to_state == State.DOWN or (from_state == State.DOWN and to_state == State.UP)


# Creates one pending delivery per channel the check alerts, in the caller's
# transaction, and remembers them for dispatch_pending.
async def create_deliveries(session: AsyncSession, event: Event, now: datetime) -> None:
    channel_ids = list(
        await session.scalars(
            select(check_channels.c.channel_id).where(check_channels.c.check_id == event.check_id)
        )
    )
    pending: list[DeliveryKey] = session.info.setdefault(PENDING_KEY, [])
    for channel_id in channel_ids:
        session.add(
            Delivery(
                event_id=event.id,
                channel_id=channel_id,
                status=DeliveryStatus.PENDING,
                next_attempt_at=now + RELAY_DELAY,
            )
        )
        pending.append(DeliveryKey(event.id, channel_id))
    await session.flush()


# Queues the deliveries created in a transaction that has now committed. A
# failure here loses nothing: the rows exist, and the relay will find them.
async def dispatch_pending(session: AsyncSession, dispatcher: Dispatcher) -> None:
    keys: list[DeliveryKey] = session.info.pop(PENDING_KEY, [])
    if not keys:
        return
    try:
        await dispatcher.deliver(keys)
    except Exception:
        log.exception("could not queue %d deliveries; the relay will retry", len(keys))


# Exponential backoff with "equal jitter": half of the delay is fixed, half is
# random. The random half spreads out retries from many deliveries that failed
# together (one Telegram outage fails them all at once); the fixed half keeps
# a retry from firing immediately after the failure.
def backoff(attempt: int, base: float, cap: float, rng: random.Random) -> timedelta:
    ceiling = min(cap, base * 2 ** (attempt - 1))
    return timedelta(seconds=ceiling / 2 + rng.uniform(0, ceiling / 2))


def _excerpt(output: str | None) -> str | None:
    if not output:
        return None
    lines = output.rstrip().splitlines()[-OUTPUT_EXCERPT_LINES:]
    return "\n".join(lines)[-OUTPUT_EXCERPT_CHARS:]


def build_alert(event: Event, check: Check, run: Run | None, settings: Settings) -> Alert:
    link = None
    if settings.dashboard_url:
        link = f"{settings.dashboard_url.rstrip('/')}/#/checks/{check.id}"
    excerpt = _excerpt(run.output_tail) if run and event.to_state == State.DOWN else None
    return Alert(
        event_id=event.id,
        check_id=check.id,
        check_name=check.name,
        from_state=event.from_state,
        to_state=event.to_state,
        reason=event.reason,
        occurred_at=event.created_at,
        link=link,
        output_excerpt=excerpt,
    )


# Makes one attempt at delivering an event to a channel, and returns the
# delivery's status afterwards (or "busy" if another worker holds it).
#
# Safe to call any number of times for the same pair. The row is locked with
# SKIP LOCKED for the whole attempt, so two workers given the same job can't
# both send, and a delivery already sent or given up on is left alone. The
# lock is held across the network call; at one user's alert volume that is
# a fine price for not needing a separate "sending" state and lease.
#
# A crash after the channel accepted the message but before the commit means
# the attempt is repeated, so a channel can, rarely, get a message twice.
# That is the deliberate side to err on: a duplicate alert beats a lost one.
async def deliver(
    sessionmaker: Sessionmaker,
    senders: Mapping[str, Sender],
    settings: Settings,
    key: DeliveryKey,
    now: datetime,
    rng: random.Random | None = None,
) -> str:
    rng = rng or random.Random()
    async with sessionmaker() as session, session.begin():
        delivery = await session.scalar(
            select(Delivery)
            .where(Delivery.event_id == key.event_id, Delivery.channel_id == key.channel_id)
            .with_for_update(skip_locked=True)
        )
        if delivery is None:
            return "busy"
        if delivery.status in (DeliveryStatus.SENT, DeliveryStatus.FAILED):
            return delivery.status

        event = await session.get_one(Event, key.event_id)
        check = await session.get_one(Check, event.check_id)
        channel = await session.get_one(Channel, key.channel_id)
        run = await session.get(Run, event.run_id) if event.run_id is not None else None
        alert = build_alert(event, check, run, settings)

        delivery.attempts += 1
        try:
            sender = senders.get(channel.kind)
            if sender is None:
                raise PermanentError(f"no sender for channel kind {channel.kind}")
            delivery.external_id = await sender.send(channel.config, alert)
        except PermanentError as exc:
            delivery.status = DeliveryStatus.FAILED
            delivery.last_error = str(exc)
            delivery.next_attempt_at = None
        except Exception as exc:
            delivery.last_error = str(exc) or type(exc).__name__
            if delivery.attempts >= settings.delivery_max_attempts:
                delivery.status = DeliveryStatus.FAILED
                delivery.next_attempt_at = None
            else:
                delay = backoff(
                    delivery.attempts,
                    settings.delivery_backoff_base_seconds,
                    settings.delivery_backoff_cap_seconds,
                    rng,
                )
                if isinstance(exc, TransientError) and exc.retry_after:
                    delay = max(delay, timedelta(seconds=exc.retry_after))
                delivery.status = DeliveryStatus.RETRYING
                delivery.next_attempt_at = now + delay
            if not isinstance(exc, TransientError):
                log.exception("unexpected error delivering event %s", key.event_id)
        else:
            delivery.status = DeliveryStatus.SENT
            delivery.sent_at = now
            delivery.last_error = None
            delivery.next_attempt_at = None
        return delivery.status


# Claims deliveries whose next attempt is due (retries, and new deliveries
# whose direct enqueue was lost) and pushes their next attempt time out by a
# lease, so the next relay run doesn't queue them again. Returns their keys
# for the caller to queue after committing.
async def claim_due_deliveries(
    session: AsyncSession, now: datetime, limit: int = 100
) -> list[DeliveryKey]:
    due = list(
        await session.scalars(
            select(Delivery)
            .where(
                Delivery.status.in_([DeliveryStatus.PENDING, DeliveryStatus.RETRYING]),
                Delivery.next_attempt_at <= now,
            )
            .order_by(Delivery.next_attempt_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    for delivery in due:
        delivery.next_attempt_at = now + RELAY_LEASE
    return [DeliveryKey(d.event_id, d.channel_id) for d in due]
