from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tocsin.evalstats import DEFAULT_ALPHA, DEFAULT_MIN_DROP, compare, wilson_interval
from tocsin.models import EvalReport, EvalScenario


class Verdict(StrEnum):
    OK = "ok"
    # A rule was broken: the check goes down and alerts.
    FAILING = "failing"
    # A drop that looks real but is too small a sample to call. Not an alert,
    # and not a recovery either.
    NEEDS_DATA = "needs_data"


class Rule(StrEnum):
    FLOOR = "floor"
    REGRESSION = "regression"
    BUDGET = "budget"
    NEEDS_DATA = "needs_data"


# What an eval check alerts on. Every field is optional: with none set, a
# check watches for regressions against its last five reports and nothing else.
class EvalRules(BaseModel):
    # Floor: alert when the upper end of a scenario's 95% interval is below
    # this pass rate, that is, when it is under the bar even on the generous
    # reading. A fraction, 0.9 for 90%.
    floor: float | None = Field(default=None, gt=0, le=1)
    # Regression against a baseline, on by default.
    regression: bool = True
    # The baseline is the runs of the previous `baseline_reports` reports
    # pooled together, or the one report pinned with `baseline_report_id`.
    baseline_reports: int = Field(default=5, ge=1, le=100)
    baseline_report_id: int | None = None
    # A drop smaller than this (a fraction) is never an alert, and one that
    # isn't clear at this significance level is "not enough data".
    min_drop: float = Field(default=DEFAULT_MIN_DROP, gt=0, lt=1)
    alpha: float = Field(default=DEFAULT_ALPHA, gt=0, lt=1)
    # Budget: alert when a latency percentile in a scenario ({"p95": 3000},
    # milliseconds) or the whole report's cost (US dollars) is over its limit.
    max_latency_ms: dict[str, float] = Field(default_factory=dict, max_length=20)
    max_cost_usd: float | None = Field(default=None, gt=0)


@dataclass(frozen=True)
class Counts:
    passes: int
    runs: int


@dataclass(frozen=True)
class Finding:
    rule: Rule
    scenario: str | None
    message: str
    # How many more runs would settle a "needs data" finding.
    runs_needed: int | None = None

    def as_json(self) -> dict[str, Any]:
        found: dict[str, Any] = {
            "rule": self.rule.value,
            "scenario": self.scenario,
            "message": self.message,
        }
        if self.runs_needed is not None:
            found["runs_needed"] = self.runs_needed
        return found


@dataclass(frozen=True)
class Evaluation:
    verdict: Verdict
    findings: list[Finding]

    # What goes in the alert, and in the event's reason.
    def reason(self) -> str:
        shown = [finding.message for finding in self.findings[:3]]
        more = len(self.findings) - len(shown)
        text = "; ".join(shown) + (f"; and {more} more" if more > 0 else "")
        return f"evaluation failed: {text}" if self.verdict == Verdict.FAILING else text


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    runs: int
    passes: int
    latency_ms: dict[str, Any] | None = None
    cost_usd: float | None = None


def _percent(rate: float) -> str:
    return f"{rate * 100:.1f}".removesuffix(".0") + "%"


def _floor(rules: EvalRules, scenario: ScenarioResult) -> Finding | None:
    if rules.floor is None:
        return None
    _, high = wilson_interval(scenario.passes, scenario.runs)
    if high >= rules.floor:
        return None
    return Finding(
        Rule.FLOOR,
        scenario.name,
        f"{scenario.name} passed {scenario.passes}/{scenario.runs}; even the generous end of "
        f"its 95% interval, {_percent(high)}, is below the {_percent(rules.floor)} floor",
    )


def _regression(
    rules: EvalRules, scenario: ScenarioResult, baseline: Counts | None
) -> Finding | None:
    if not rules.regression or baseline is None or baseline.runs == 0:
        return None
    result = compare(
        baseline.passes,
        baseline.runs,
        scenario.passes,
        scenario.runs,
        rules.alpha,
        rules.min_drop,
    )
    if result.verdict == "ok":
        return None
    change = (
        f"{scenario.name}: pass rate fell from {_percent(result.baseline_rate)} "
        f"({baseline.passes}/{baseline.runs}) to {_percent(result.current_rate)} "
        f"({scenario.passes}/{scenario.runs})"
    )
    if result.verdict == "regression":
        return Finding(Rule.REGRESSION, scenario.name, f"{change}, p = {result.p_value:.3g}")
    settle = (
        f"about {result.runs_needed} more runs would settle it"
        if result.runs_needed is not None
        else "too many more runs to say how many"
    )
    return Finding(
        Rule.NEEDS_DATA,
        scenario.name,
        f"{change}; not enough data to tell whether that is real, {settle}",
        result.runs_needed,
    )


def _budgets(rules: EvalRules, scenarios: Sequence[ScenarioResult]) -> list[Finding]:
    found = []
    for scenario in scenarios:
        for percentile, limit in rules.max_latency_ms.items():
            value = (scenario.latency_ms or {}).get(percentile)
            if value is not None and value > limit:
                found.append(
                    Finding(
                        Rule.BUDGET,
                        scenario.name,
                        f"{scenario.name}: {percentile} latency {value:g} ms is over the "
                        f"{limit:g} ms limit",
                    )
                )
    costs = [scenario.cost_usd for scenario in scenarios if scenario.cost_usd is not None]
    if rules.max_cost_usd is not None and costs and sum(costs) > rules.max_cost_usd:
        found.append(
            Finding(
                Rule.BUDGET,
                None,
                f"the report's cost, ${sum(costs):.2f}, is over the "
                f"${rules.max_cost_usd:.2f} limit",
            )
        )
    return found


# Applies an eval check's rules to one report, given each scenario's baseline
# counts (None when the scenario has none, as on its first report). Pure, so
# the rules can be tested without a database.
#
# Any floor, regression or budget finding fails the report. A drop that might
# be real but can't be told from noise yet is reported as "needs data": not a
# failure, but it is shown, and it doesn't count as a recovery.
def evaluate(
    rules: EvalRules,
    scenarios: Sequence[ScenarioResult],
    baselines: dict[str, Counts],
) -> Evaluation:
    findings: list[Finding] = []
    for scenario in scenarios:
        for found in (
            _floor(rules, scenario),
            _regression(rules, scenario, baselines.get(scenario.name)),
        ):
            if found is not None:
                findings.append(found)
    findings.extend(_budgets(rules, scenarios))

    if any(finding.rule != Rule.NEEDS_DATA for finding in findings):
        return Evaluation(Verdict.FAILING, findings)
    if findings:
        return Evaluation(Verdict.NEEDS_DATA, findings)
    return Evaluation(Verdict.OK, findings)


# The counts each scenario is compared against: those of the pinned report, or
# the previous `baseline_reports` reports pooled. A report that itself failed
# is left out of the pool, so a regression doesn't become the new normal after
# a few nights and quietly end the alert.
async def load_baselines(
    session: AsyncSession, check_id: Any, before_id: int, rules: EvalRules
) -> dict[str, Counts]:
    if rules.baseline_report_id is not None:
        report_ids: Sequence[int] = [rules.baseline_report_id]
    else:
        report_ids = list(
            await session.scalars(
                select(EvalReport.id)
                .where(
                    EvalReport.check_id == check_id,
                    EvalReport.id < before_id,
                    EvalReport.verdict != Verdict.FAILING,
                )
                .order_by(EvalReport.id.desc())
                .limit(rules.baseline_reports)
            )
        )
    totals: dict[str, tuple[int, int]] = {}
    rows = await session.execute(
        select(EvalScenario.name, EvalScenario.passes, EvalScenario.runs).where(
            EvalScenario.report_id.in_(report_ids)
        )
    )
    for name, passes, runs in rows:
        old_passes, old_runs = totals.get(name, (0, 0))
        totals[name] = (old_passes + passes, old_runs + runs)
    return {name: Counts(passes, runs) for name, (passes, runs) in totals.items()}
