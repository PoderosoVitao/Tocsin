from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import select

from tocsin.api.deps import Session
from tocsin.api.schemas import AlertOut
from tocsin.models import Channel, Check, Delivery, DeliveryStatus, Event

router = APIRouter(prefix="/alerts", tags=["alerts"])


# The alert log: every delivery across all checks, newest first. Filter with
# `status` (for example failed, to see what never got through); page with
# `before`, the smallest id on the previous page.
@router.get("")
async def list_alerts(
    session: Session,
    status: DeliveryStatus | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    before: int | None = None,
) -> list[AlertOut]:
    query = (
        select(Delivery, Channel.name, Event, Check.name)
        .join(Channel, Channel.id == Delivery.channel_id)
        .join(Event, Event.id == Delivery.event_id)
        .join(Check, Check.id == Event.check_id)
    )
    if status is not None:
        query = query.where(Delivery.status == status)
    if before is not None:
        query = query.where(Delivery.id < before)
    rows = await session.execute(query.order_by(Delivery.id.desc()).limit(limit))
    return [
        AlertOut.from_rows(delivery, channel_name, event, check_name)
        for delivery, channel_name, event, check_name in rows
    ]
