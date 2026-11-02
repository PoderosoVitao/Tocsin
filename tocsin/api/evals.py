from __future__ import annotations

import uuid

from fastapi import APIRouter, Query
from sqlalchemy import select

from tocsin.api.checks import get_check
from tocsin.api.deps import Session
from tocsin.api.schemas import EvalReportOut
from tocsin.models import EvalReport, EvalScenario

router = APIRouter(prefix="/checks", tags=["eval"])


# The evaluation reports an eval check has received, newest first, each with
# its scenarios' counts, pass rates and 95% intervals. Page with `before`, the
# smallest id on the previous page.
@router.get("/{check_id}/eval")
async def list_reports(
    check_id: uuid.UUID,
    session: Session,
    limit: int = Query(default=50, ge=1, le=500),
    before: int | None = None,
) -> list[EvalReportOut]:
    await get_check(session, check_id)
    query = select(EvalReport).where(EvalReport.check_id == check_id)
    if before is not None:
        query = query.where(EvalReport.id < before)
    reports = list(await session.scalars(query.order_by(EvalReport.id.desc()).limit(limit)))

    scenarios: dict[int, list[EvalScenario]] = {report.id: [] for report in reports}
    rows = await session.scalars(
        select(EvalScenario)
        .where(EvalScenario.report_id.in_(list(scenarios)))
        .order_by(EvalScenario.id)
    )
    for scenario in rows:
        scenarios[scenario.report_id].append(scenario)
    return [EvalReportOut.build(report, scenarios[report.id]) for report in reports]
