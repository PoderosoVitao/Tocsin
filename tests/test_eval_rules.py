from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from tests.helpers import add_channel, create_check
from tocsin.db import Sessionmaker
from tocsin.evaluation import (
    Counts,
    EvalRules,
    Evaluation,
    Rule,
    ScenarioResult,
    Verdict,
    evaluate,
)
from tocsin.models import Delivery, EvalReport, Event


def run(
    passes: int,
    runs: int,
    name: str = "a",
    latency: dict[str, Any] | None = None,
    cost: float | None = None,
) -> ScenarioResult:
    return ScenarioResult(name, runs, passes, latency, cost)


def check_rules(
    scenarios: list[ScenarioResult], baselines: dict[str, Counts] | None = None, **rules: Any
) -> Evaluation:
    return evaluate(EvalRules(**rules), scenarios, baselines or {})


def test_with_nothing_to_compare_against_a_report_is_fine() -> None:
    result = check_rules([run(1, 50)])
    assert (result.verdict, result.findings) == (Verdict.OK, [])


def test_a_floor_fails_when_even_the_generous_end_of_the_interval_is_below_it() -> None:
    result = check_rules([run(47, 50)], floor=0.99)
    [finding] = result.findings
    assert (result.verdict, finding.rule, finding.scenario) == (Verdict.FAILING, Rule.FLOOR, "a")
    assert "passed 47/50" in finding.message and "below the 99% floor" in finding.message


def test_a_floor_is_not_broken_when_the_interval_still_reaches_it() -> None:
    # 8 of 10 is 80%, under an 85% floor, but its interval runs up to about 90%:
    # with so few runs it isn't clear the agent is below the bar.
    assert check_rules([run(8, 10)], floor=0.85).verdict == Verdict.OK
    assert check_rules([run(8, 10)], floor=0.95).verdict == Verdict.FAILING
    assert check_rules([run(500, 700)], floor=0.85).verdict == Verdict.FAILING


def test_a_clear_drop_against_the_baseline_is_a_regression() -> None:
    result = check_rules([run(32, 40)], {"a": Counts(94, 100)})
    [finding] = result.findings
    assert (result.verdict, finding.rule) == (Verdict.FAILING, Rule.REGRESSION)
    assert "fell from 94% (94/100) to 80% (32/40)" in finding.message
    assert "p = " in finding.message
    assert result.reason().startswith("evaluation failed: a: pass rate fell")


def test_a_drop_that_may_be_noise_needs_more_data_and_says_how_much() -> None:
    result = check_rules([run(4, 5)], {"a": Counts(94, 100)})
    [finding] = result.findings
    assert (result.verdict, finding.rule, finding.runs_needed) == (
        Verdict.NEEDS_DATA,
        Rule.NEEDS_DATA,
        18,
    )
    assert "not enough data to tell" in finding.message
    assert "about 18 more runs would settle it" in finding.message
    assert finding.as_json()["runs_needed"] == 18
    assert not result.reason().startswith("evaluation failed")


def test_a_small_or_clearly_absent_drop_is_fine() -> None:
    assert check_rules([run(93, 100)], {"a": Counts(94, 100)}).verdict == Verdict.OK
    assert check_rules([run(46, 50)], {"a": Counts(90, 100)}).verdict == Verdict.OK
    assert check_rules([run(5, 10)], {"a": Counts(94, 100)}, regression=False).verdict == Verdict.OK


def test_the_thresholds_for_a_drop_are_configurable() -> None:
    baseline = {"a": Counts(940, 1000)}
    assert check_rules([run(900, 1000)], baseline).verdict == Verdict.OK
    assert check_rules([run(900, 1000)], baseline, min_drop=0.02).verdict == Verdict.FAILING
    # Over the minimum but not statistically clear: needs data, not an alert.
    assert check_rules([run(930, 1000)], baseline, min_drop=0.005).verdict == Verdict.NEEDS_DATA


def test_a_scenario_with_no_baseline_is_not_compared() -> None:
    result = check_rules([run(1, 50, "new_scenario")], {"a": Counts(94, 100)})
    assert result.verdict == Verdict.OK


def test_budgets_fail_on_latency_percentiles_and_total_cost() -> None:
    scenarios = [
        run(10, 10, "a", {"p50": 400, "p95": 2400}, 0.7),
        run(10, 10, "b", {"p50": 300}, 0.6),
    ]
    result = check_rules(scenarios, max_latency_ms={"p95": 2000, "p50": 500}, max_cost_usd=1.0)
    assert result.verdict == Verdict.FAILING
    assert [f.message for f in result.findings] == [
        "a: p95 latency 2400 ms is over the 2000 ms limit",
        "the report's cost, $1.30, is over the $1.00 limit",
    ]
    within = check_rules(scenarios, max_latency_ms={"p95": 2500}, max_cost_usd=1.5)
    assert within.verdict == Verdict.OK


def test_a_failure_wins_over_needs_data_and_the_reason_lists_the_first_findings() -> None:
    scenarios = [
        run(4, 5, "a"),
        run(30, 40, "b"),
        run(1, 10, "c"),
        run(1, 10, "d"),
        run(1, 10, "e"),
    ]
    baseline = {name: Counts(94, 100) for name in "abcde"}
    result = check_rules(scenarios, baseline)
    assert result.verdict == Verdict.FAILING
    assert len(result.findings) == 5
    assert result.reason().endswith("and 2 more")
    assert result.reason().count("pass rate fell") == 3


def test_the_rules_have_defaults_and_refuse_nonsense() -> None:
    rules = EvalRules()
    assert (rules.floor, rules.regression, rules.baseline_reports) == (None, True, 5)
    for bad in ({"floor": 0}, {"floor": 1.5}, {"alpha": 1}, {"baseline_reports": 0}):
        with pytest.raises(ValueError):
            EvalRules(**bad)


def report(
    passes: int, runs: int = 50, *, latency: dict[str, int] | None = None, **fields: Any
) -> bytes:
    scenario: dict[str, Any] = {"name": "refunds", "runs": runs, "passes": passes}
    if latency:
        scenario["latency_ms"] = latency
    return json.dumps(
        {
            "schema": "tocsin.eval/v1",
            "suite": "agent",
            "labels": {"prompt_version": "v1"},
            "scenarios": [scenario],
            **fields,
        }
    ).encode()


async def send(client: httpx.AsyncClient, check: dict[str, Any], payload: bytes) -> None:
    response = await client.post(check["ping_path"], content=payload, headers={"Authorization": ""})
    assert response.status_code == 200, response.text


async def state(client: httpx.AsyncClient, check: dict[str, Any]) -> str:
    return str((await client.get(f"/api/checks/{check['id']}")).json()["state"])


async def history(sessionmaker: Sessionmaker) -> list[tuple[str, str]]:
    async with sessionmaker() as session:
        events = await session.scalars(select(Event).order_by(Event.id))
        return [(e.from_state, e.to_state) for e in events]


async def eval_check(client: httpx.AsyncClient, **fields: Any) -> dict[str, Any]:
    return await create_check(client, name="nightly", kind="eval", **fields)


async def verdicts(client: httpx.AsyncClient, check: dict[str, Any]) -> list[str]:
    reports = (await client.get(f"/api/checks/{check['id']}/eval")).json()
    return [str(item["verdict"]) for item in reversed(reports)]


async def test_a_regression_takes_the_check_down_and_alerts_then_recovery_brings_it_up(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker, dispatcher: Any
) -> None:
    await add_channel(sessionmaker)
    check = await eval_check(client)
    for passes in (47, 46, 47):
        await send(client, check, report(passes))
    assert await state(client, check) == "up"

    await send(client, check, report(33))
    assert await state(client, check) == "down"
    async with sessionmaker() as session:
        down = (await session.scalars(select(Event).where(Event.to_state == "down"))).one()
        assert (await session.scalars(select(Delivery))).all()
    assert down.reason.startswith("evaluation failed: refunds: pass rate fell from 93.3%")
    assert len(dispatcher.keys) == 1  # the way down; coming up for the first time is silent

    await send(client, check, report(47))
    assert await state(client, check) == "up"
    assert await verdicts(client, check) == ["ok", "ok", "ok", "failing", "ok"]
    assert await history(sessionmaker) == [("new", "up"), ("up", "down"), ("down", "up")]


async def test_not_enough_data_is_shown_but_does_not_alert(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker, dispatcher: Any
) -> None:
    await add_channel(sessionmaker)
    check = await eval_check(client)
    for _ in range(3):
        await send(client, check, report(47))
    await send(client, check, report(4, runs=5))

    assert await state(client, check) == "up"
    assert await history(sessionmaker) == [("new", "up")]
    assert dispatcher.keys == []
    [newest, *_] = (await client.get(f"/api/checks/{check['id']}/eval")).json()
    assert newest["verdict"] == "needs_data"
    [finding] = newest["findings"]
    assert (finding["rule"], finding["scenario"]) == ("needs_data", "refunds")
    assert finding["runs_needed"] > 0
    assert "not enough data" in finding["message"]


async def test_a_check_that_is_down_is_not_recovered_by_an_unclear_report(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check = await eval_check(client)
    for _ in range(3):
        await send(client, check, report(47))
    await send(client, check, report(30))
    assert await state(client, check) == "down"

    await send(client, check, report(4, runs=5))
    assert await state(client, check) == "down"
    assert await history(sessionmaker) == [("new", "up"), ("up", "down")]
    assert await verdicts(client, check) == ["ok", "ok", "ok", "failing", "needs_data"]


# Otherwise a regression that lasts would, after a few nights, become what
# the baseline says is normal, and the alert would quietly end.
async def test_failing_reports_are_left_out_of_the_baseline(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check = await eval_check(client, eval_rules={"baseline_reports": 3})
    for _ in range(3):
        await send(client, check, report(47))
    for _ in range(4):
        await send(client, check, report(30))
    assert await verdicts(client, check) == ["ok"] * 3 + ["failing"] * 4
    assert await state(client, check) == "down"
    assert await history(sessionmaker) == [("new", "up"), ("up", "down")]


async def test_the_floor_and_budgets_fail_a_report(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check = await eval_check(client, eval_rules={"floor": 0.95, "max_latency_ms": {"p95": 2000}})
    await send(client, check, report(40, latency={"p95": 1500}))
    assert await state(client, check) == "down"
    await send(client, check, report(50, latency={"p95": 2500}))
    assert await state(client, check) == "down"
    await send(client, check, report(50, latency={"p95": 1500}))
    assert await state(client, check) == "up"

    async with sessionmaker() as session:
        findings = [
            [f["rule"] for f in r.findings]
            for r in await session.scalars(select(EvalReport).order_by(EvalReport.id))
        ]
    assert findings == [["floor"], ["budget"], []]


async def test_a_pinned_report_is_the_baseline(
    client: httpx.AsyncClient,
) -> None:
    check = await eval_check(client)
    await send(client, check, report(49))
    await send(client, check, report(35))  # a regression against 49/50
    assert await state(client, check) == "down"
    [_, first] = (await client.get(f"/api/checks/{check['id']}/eval")).json()

    pinned = await client.patch(
        f"/api/checks/{check['id']}",
        json={"eval_rules": {"baseline_report_id": first["id"], "min_drop": 0.5}},
    )
    assert pinned.status_code == 200
    assert pinned.json()["eval_rules"]["baseline_report_id"] == first["id"]
    await send(client, check, report(35))
    assert await state(client, check) == "up"  # a 28-point drop is under a 50-point minimum


async def test_rules_are_validated_and_only_for_eval_checks(client: httpx.AsyncClient) -> None:
    job = await create_check(client)
    assert job["eval_rules"] is None
    too_early = await client.post(
        "/api/checks",
        json={
            "name": "x",
            "schedule": "@daily",
            "kind": "eval",
            "eval_rules": {"baseline_report_id": 1},
        },
    )
    assert too_early.status_code == 422
    for_a_job = await client.post(
        "/api/checks",
        json={"name": "x", "schedule": "@daily", "eval_rules": {"floor": 0.9}},
    )
    assert for_a_job.status_code == 422
    assert (
        await client.patch(f"/api/checks/{job['id']}", json={"eval_rules": {"floor": 0.9}})
    ).status_code == 422

    check = await eval_check(client, eval_rules={"floor": 0.9})
    assert check["eval_rules"]["floor"] == 0.9
    assert check["eval_rules"]["baseline_reports"] == 5
    unknown = await client.patch(
        f"/api/checks/{check['id']}", json={"eval_rules": {"baseline_report_id": 99}}
    )
    assert unknown.status_code == 422
    assert "no report 99" in unknown.json()["detail"]
    bad = await client.patch(f"/api/checks/{check['id']}", json={"eval_rules": {"floor": 3}})
    assert bad.status_code == 422

    replaced = await client.patch(
        f"/api/checks/{check['id']}", json={"eval_rules": {"max_cost_usd": 5}}
    )
    rules = replaced.json()["eval_rules"]
    assert (rules["floor"], rules["max_cost_usd"]) == (None, 5)
