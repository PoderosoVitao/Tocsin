from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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


# Records one ping and applies it to its check, in the caller's transaction.
# Returns None for an unknown ping key.
#
# This is the hot path, so it stays small: lock the check row, insert the
# ping (and the run a finish ping closes), update the check. Anything slow,
# such as sending alerts, happens after the commit, outside the request.
# The row lock serialises pings with the sweep, so a success ping and an
# overdue sweep for the same check can't both act on a stale state.
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
    if signal.kind == PingKind.START:
        return outcome

    run = Run(
        check_id=check.id,
        status=RunStatus.SUCCESS if signal.kind == PingKind.SUCCESS else RunStatus.FAIL,
        finished_at=now,
        exit_code=signal.exit_code,
        output_tail=output_tail(body, tail_limit),
    )
    session.add(run)
    await session.flush()

    # A paused check keeps its history but never changes state, so a job that
    # keeps running while paused can't raise an alert.
    if check.state == State.PAUSED:
        return outcome

    schedule_next(check, now)
    if signal.kind == PingKind.SUCCESS and check.state != State.UP:
        reason = "first ping" if check.state == State.NEW else "the job succeeded"
        outcome.events.append(await transition(session, check, State.UP, reason, now, run.id))
    elif signal.kind == PingKind.FAIL and check.state != State.DOWN:
        outcome.events.append(
            await transition(session, check, State.DOWN, _failure_reason(signal), now, run.id)
        )
    return outcome
