from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter
from sqlalchemy import func, select

from tocsin.api.checks import channel_ids_by_check
from tocsin.api.deps import AppSettings, Session
from tocsin.api.schemas import CheckOut, RunSummary
from tocsin.models import Check, Run

router = APIRouter(prefix="/overview", tags=["dashboard"])

RECENT_RUNS = 20


class OverviewItem(CheckOut):
    # Oldest first, so the newest run is last.
    recent_runs: list[RunSummary]


# Every check with its most recent runs, in one request, for the dashboard's
# overview: one query for the checks, one for all their runs.
@router.get("")
async def overview(session: Session, settings: AppSettings) -> list[OverviewItem]:
    checks = list(await session.scalars(select(Check).order_by(Check.name, Check.created_at)))
    channels = await channel_ids_by_check(session, [check.id for check in checks])

    newest_first = (
        func.row_number().over(partition_by=Run.check_id, order_by=Run.id.desc()).label("position")
    )
    ranked = select(Run.id, newest_first).subquery()
    runs = await session.scalars(
        select(Run)
        .join(ranked, Run.id == ranked.c.id)
        .where(ranked.c.position <= RECENT_RUNS)
        .order_by(Run.check_id, Run.id)
    )
    by_check: dict[uuid.UUID, list[RunSummary]] = {check.id: [] for check in checks}
    for run in runs:
        by_check[run.check_id].append(RunSummary.build(run))

    now = datetime.now(UTC)
    return [
        OverviewItem(
            **CheckOut.build(check, channels[check.id], settings.public_url, now).model_dump(),
            recent_runs=by_check[check.id],
        )
        for check in checks
    ]
