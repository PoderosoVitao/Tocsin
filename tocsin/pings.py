from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tocsin.humanize import duration
from tocsin.models import Check, Event, Ping, PingKind, Run, RunStatus, State
from tocsin.transitions import schedule_next, transition


@dataclass(frozen=True)
class Signal:
    kind: PingKind
    exit_code: int | None = None

    @classmethod
    def parse(cls, value: str | None) -> Signal:
        if value is None:
            return cls(PingKind.SUCCESS)
        if value == "start":
            return cls(PingKind.START)
        if value == "fail":
            return cls(PingKind.FAIL)
        if value.isdigit() and int(value) <= 255:
            code = int(value)
            return cls(PingKind.SUCCESS if code == 0 else PingKind.FAIL, code)
        raise ValueError("a ping signal is start, fail, or an exit code from 0 to 255")


@dataclass
class PingOutcome:
    check: Check
    events: list[Event] = field(default_factory=list)


# Keeps the last `limit` bytes of a job's output as text. Postgres text can't
# hold NUL characters, which binary output sometimes contains.
def output_tail(body: bytes, limit: int) -> str | None:
    if not body:
        return None
    return body[-limit:].decode("utf-8", errors="replace").replace("\x00", "�")


def _failure_reason(signal: Signal) -> str:
    if signal.exit_code is None:
        return "the job reported a failure"
    return f"the job exited with code {signal.exit_code}"


# The run a start ping opened and no finish ping has closed yet. Only looked
# up when the check says a run is open, so jobs that never send start pings
# pay nothing for it.
async def _open_run(session: AsyncSession, check: Check) -> Run | None:
    if check.last_start_at is None:
        return None
    return await session.scalar(
        select(Run)
        .where(Run.check_id == check.id, Run.status == RunStatus.RUNNING)
        .order_by(Run.id.desc())
        .limit(1)
    )


def _too_long(check: Check, run: Run) -> bool:
    limit = check.max_duration_seconds
    return limit is not None and run.duration_ms is not None and run.duration_ms > limit * 1000


# Records one ping and applies it to its check, in the caller's transaction.
# Returns None for an unknown ping key.
#
# This is the hot path, so it stays small: lock the check row, insert the
# ping, open or close a run, update the check. Anything slow, such as sending
# alerts, happens after the commit, outside the request. The row lock
# serialises pings with the sweep, so a success ping and an overdue sweep for
# the same check can't both act on a stale state.
#
# A start ping opens a run and a finish ping closes it, giving it a duration.
# One run is tracked at a time: a second start before a finish marks the first
# run unfinished. A finish with no start makes a run without a duration.
async def record_ping(
    session: AsyncSession,
    ping_key: uuid.UUID,
    signal: Signal,
    body: bytes,
    source_ip: str | None,
    now: datetime,
    tail_limit: int,
) -> PingOutcome | None:
    check = await session.scalar(select(Check).where(Check.ping_key == ping_key).with_for_update())
    if check is None:
        return None

    session.add(
        Ping(
            check_id=check.id,
            kind=signal.kind,
            received_at=now,
            exit_code=signal.exit_code,
            source_ip=source_ip,
        )
    )
    check.last_ping_at = now
    outcome = PingOutcome(check)
    open_run = await _open_run(session, check)

    if signal.kind == PingKind.START:
        if open_run is not None:
            open_run.status = RunStatus.UNFINISHED
        session.add(Run(check_id=check.id, status=RunStatus.RUNNING, started_at=now))
        check.last_start_at = now
        await session.flush()
        if check.state == State.PAUSED:
            return outcome
        # A start shows the job ran on schedule; from here the deadline is the
        # next scheduled run, or the maximum duration if that comes first.
        schedule_next(check, now)
        if check.state == State.NEW:
            outcome.events.append(await transition(session, check, State.UP, "first ping", now))
        return outcome

    run = open_run or Run(check_id=check.id)
    run.status = RunStatus.SUCCESS if signal.kind == PingKind.SUCCESS else RunStatus.FAIL
    run.finished_at = now
    run.exit_code = signal.exit_code
    run.output_tail = output_tail(body, tail_limit)
    if run.started_at is not None:
        run.duration_ms = int((now - run.started_at).total_seconds() * 1000)
    check.last_start_at = None
    session.add(run)
    await session.flush()

    # A paused check keeps its history but never changes state, so a job that
    # keeps running while paused can't raise an alert.
    if check.state == State.PAUSED:
        return outcome

    schedule_next(check, now)
    # A run that succeeded but took longer than the maximum counts as a failure
    # too: it's the same problem the sweep reports while the run is going.
    failed = signal.kind == PingKind.FAIL or _too_long(check, run)
    if failed and check.state != State.DOWN:
        if signal.kind == PingKind.FAIL:
            reason = _failure_reason(signal)
        else:
            reason = (
                f"the run took {duration(timedelta(milliseconds=run.duration_ms or 0))}, "
                f"longer than the {duration(check.max_duration_seconds or 0)} maximum"
            )
        outcome.events.append(await transition(session, check, State.DOWN, reason, now, run.id))
    elif not failed and check.state != State.UP:
        reason = "first ping" if check.state == State.NEW else "the job succeeded"
        outcome.events.append(await transition(session, check, State.UP, reason, now, run.id))
    return outcome
