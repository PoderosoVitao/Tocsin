from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from tocsin.api.deps import Session
from tocsin.api.schemas import ChannelIn, ChannelOut
from tocsin.channels import Alert, DeliveryError, Sender
from tocsin.models import Channel, Check, State, check_channels

router = APIRouter(prefix="/channels", tags=["channels"])


async def _get_channel(session: AsyncSession, channel_id: uuid.UUID) -> Channel:
    channel = await session.get(Channel, channel_id)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such channel")
    return channel


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_channel(body: ChannelIn, session: Session) -> ChannelOut:
    channel = Channel(kind=body.kind, name=body.name, config=body.config)
    session.add(channel)
    await session.flush()
    if body.attach_to_existing_checks:
        check_ids = list(await session.scalars(select(Check.id)))
        if check_ids:
            await session.execute(
                insert(check_channels),
                [{"check_id": check_id, "channel_id": channel.id} for check_id in check_ids],
            )
    await session.commit()
    await session.refresh(channel)
    return ChannelOut.build(channel)


@router.get("")
async def list_channels(session: Session) -> list[ChannelOut]:
    channels = await session.scalars(select(Channel).order_by(Channel.name, Channel.created_at))
    return [ChannelOut.build(channel) for channel in channels]


@router.delete("/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(channel_id: uuid.UUID, session: Session) -> Response:
    await session.delete(await _get_channel(session, channel_id))
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# Sends a test message straight away, outside the queue, so a person setting
# up a channel sees at once whether the token and chat id work.
@router.post("/{channel_id}/test")
async def test_channel(channel_id: uuid.UUID, request: Request, session: Session) -> dict[str, str]:
    channel = await _get_channel(session, channel_id)
    sender: Sender | None = request.app.state.senders.get(channel.kind)
    if sender is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unsupported channel")
    alert = Alert(
        event_id=0,
        check_id=uuid.UUID(int=0),
        check_name="tocsin",
        from_state=State.UP,
        to_state=State.UP,
        reason="This is a test message. Alerts for your checks will arrive here.",
        occurred_at=datetime.now(UTC),
    )
    try:
        await sender.send(channel.config, alert)
    except DeliveryError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return {"status": "sent"}
