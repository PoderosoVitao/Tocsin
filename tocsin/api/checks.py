from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from tocsin.api.deps import AppSettings, Session
from tocsin.api.schemas import CheckIn, CheckOut, CheckPatch
from tocsin.models import Channel, Check, State, check_channels
from tocsin.schedule import ScheduleError, validate
from tocsin.transitions import clear_schedule, schedule_next, transition

router = APIRouter(prefix="/checks", tags=["checks"])


async def get_check(session: AsyncSession, check_id: uuid.UUID, *, lock: bool = False) -> Check:
    query = select(Check).where(Check.id == check_id)
    if lock:
        query = query.with_for_update()
    check = await session.scalar(query)
    if check is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such check")
    return check


async def channel_ids_by_check(
    session: AsyncSession, check_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[uuid.UUID]]:
    rows = await session.execute(
        select(check_channels.c.check_id, check_channels.c.channel_id).where(
            check_channels.c.check_id.in_(check_ids)
        )
    )
    result: dict[uuid.UUID, list[uuid.UUID]] = {check_id: [] for check_id in check_ids}
    for check_id, channel_id in rows:
        result[check_id].append(channel_id)
    return result


async def _assign_channels(
    session: AsyncSession, check_id: uuid.UUID, channel_ids: list[uuid.UUID] | None
) -> list[uuid.UUID]:
    existing = set(await session.scalars(select(Channel.id)))
    wanted = list(existing) if channel_ids is None else list(dict.fromkeys(channel_ids))
    unknown = [str(channel_id) for channel_id in wanted if channel_id not in existing]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"no such channel: {', '.join(unknown)}",
        )

    await session.execute(delete(check_channels).where(check_channels.c.check_id == check_id))
    if wanted:
        await session.execute(
            insert(check_channels),
            [{"check_id": check_id, "channel_id": channel_id} for channel_id in wanted],
        )
    return wanted


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_check(body: CheckIn, session: Session, settings: AppSettings) -> CheckOut:
    check = Check(
        name=body.name,
        kind=body.kind,
        schedule=body.schedule,
        timezone=body.timezone,
        grace_seconds=body.grace_seconds,
    )
    session.add(check)
    await session.flush()
    channel_ids = await _assign_channels(session, check.id, body.channel_ids)
    await session.commit()
    return CheckOut.build(check, channel_ids, settings.public_url, datetime.now(UTC))


@router.get("")
async def list_checks(session: Session, settings: AppSettings) -> list[CheckOut]:
    checks = list(await session.scalars(select(Check).order_by(Check.name, Check.created_at)))
    channels = await channel_ids_by_check(session, [check.id for check in checks])
    now = datetime.now(UTC)
    return [CheckOut.build(c, channels[c.id], settings.public_url, now) for c in checks]


@router.get("/{check_id}")
async def read_check(check_id: uuid.UUID, session: Session, settings: AppSettings) -> CheckOut:
    check = await get_check(session, check_id)
    channels = await channel_ids_by_check(session, [check.id])
    return CheckOut.build(check, channels[check.id], settings.public_url, datetime.now(UTC))


# Edits a check, and pauses or resumes it. The row is locked for the whole
# edit so a ping or a sweep can't change the state between the read and the
# write.
@router.patch("/{check_id}")
async def update_check(
    check_id: uuid.UUID, body: CheckPatch, session: Session, settings: AppSettings
) -> CheckOut:
    now = datetime.now(UTC)
    check = await get_check(session, check_id, lock=True)
    changes = body.model_dump(exclude_unset=True, exclude={"paused", "channel_ids"})

    schedule = changes.get("schedule", check.schedule)
    timezone = changes.get("timezone", check.timezone)
    try:
        validate(schedule, timezone)
    except ScheduleError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    for field, value in changes.items():
        setattr(check, field, value)
    if changes.keys() & {"schedule", "timezone", "grace_seconds"} and check.state in (
        State.UP,
        State.DOWN,
    ):
        schedule_next(check, check.last_ping_at or now)

    if body.paused is True and check.state != State.PAUSED:
        await transition(session, check, State.PAUSED, "paused", now)
        clear_schedule(check)
    elif body.paused is False and check.state == State.PAUSED:
        # A check that has reported before resumes as up and is expected again
        # from now; one that never reported waits for its first ping.
        if check.last_ping_at is None:
            await transition(session, check, State.NEW, "resumed", now)
        else:
            await transition(session, check, State.UP, "resumed", now)
            schedule_next(check, now)

    if body.channel_ids is not None:
        channel_ids = await _assign_channels(session, check.id, body.channel_ids)
    else:
        channel_ids = (await channel_ids_by_check(session, [check.id]))[check.id]
    await session.commit()
    return CheckOut.build(check, channel_ids, settings.public_url, now)


@router.delete("/{check_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_check(check_id: uuid.UUID, session: Session) -> Response:
    check = await get_check(session, check_id)
    await session.delete(check)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
