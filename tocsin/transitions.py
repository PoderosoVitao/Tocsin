from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from tocsin.alerts import create_deliveries, should_alert
from tocsin.models import Check, Event, State
from tocsin.schedule import next_due


# Moves a check to a new state and records the change as an event, in the
# caller's transaction, along with a pending delivery per channel when the
# change is one that alerts. Every state change goes through here, so the
# event log is a complete history and no caller can forget the alert.
#
# Inside the check's cooldown an alerting change is recorded as suppressed
# instead of sent; the sweep sends one alert for where the check settled once
# the cooldown is over (see sweep_settled).
async def transition(
    session: AsyncSession,
    check: Check,
    to_state: State,
    reason: str,
    now: datetime,
    run_id: int | None = None,
) -> Event:
    event = Event(
        check_id=check.id,
        run_id=run_id,
        from_state=check.state,
        to_state=to_state,
        reason=reason,
        created_at=now,
    )
    check.state = to_state
    session.add(event)
    await session.flush()
    if should_alert(event.from_state, event.to_state):
        if in_cooldown(check, now):
            event.suppressed = True
        else:
            await alert(session, check, event, now)
    return event


def in_cooldown(check: Check, now: datetime) -> bool:
    if check.last_alert_at is None:
        return False
    return now - check.last_alert_at < timedelta(seconds=check.cooldown_seconds)


# Sends an event to the check's channels and remembers what the check was
# last reported as, which the cooldown and reminders are measured from.
async def alert(session: AsyncSession, check: Check, event: Event, now: datetime) -> None:
    await create_deliveries(session, event, now)
    check.last_alert_at = now
    check.last_alerted_state = event.to_state


# Sets when the next ping is due, counted from `after`, and the deadline the
# sweep uses: the due time plus grace, or, while a run is open on a check with
# a maximum duration, the moment that run becomes too long if that is sooner.
def schedule_next(check: Check, after: datetime) -> None:
    check.due_at = next_due(check.schedule, check.timezone, after)
    check.next_expected_at = check.due_at + timedelta(seconds=check.grace_seconds)
    if check.last_start_at is not None and check.max_duration_seconds is not None:
        too_long_at = check.last_start_at + timedelta(seconds=check.max_duration_seconds)
        check.next_expected_at = min(check.next_expected_at, too_long_at)


def clear_schedule(check: Check) -> None:
    check.due_at = None
    check.next_expected_at = None


# The state to show for a check: stored states as they are, except that an up
# check past its due time (but still inside its grace period) shows as late.
def displayed_state(check: Check, now: datetime) -> State:
    if check.state == State.UP and check.due_at is not None and now > check.due_at:
        return State.LATE
    return State(check.state)
