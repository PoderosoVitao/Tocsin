from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from tests.helpers import add_channel, create_check
from tocsin.config import Settings
from tocsin.db import Sessionmaker
from tocsin.evalreport import ReportError, parse_report
from tocsin.evalstats import wilson_interval
from tocsin.models import Delivery, EvalReport, EvalScenario, Event, Run

LIMIT = 1_000_000

REPORT: dict[str, Any] = {
    "schema": "tocsin.eval/v1",
    "suite": "refund-agent",
    "labels": {"model": "claude-sonnet-5-5", "prompt_version": "v12", "git_sha": "4f2c9a1"},
    "scenarios": [
        {
            "name": "refund_tool_never_called",
            "runs": 50,
            "passes": 47,
            "latency_ms": {"p50": 820, "p95": 2400},
            "cost_usd": {"total": 1.84},
        },
        {"name": "asks_for_order_number", "runs": 40, "passes": 40},
    ],
    "report_url": "https://ci.example.com/runs/812",
}


def body(**changes: Any) -> bytes:
    report = {**REPORT, **changes}
    return json.dumps({key: value for key, value in report.items() if value is not None}).encode()


def rejection(payload: bytes, limit: int = LIMIT) -> str:
    with pytest.raises(ReportError) as caught:
        parse_report(payload, limit)
    return str(caught.value)


def test_the_example_report_from_the_spec_is_accepted() -> None:
    report, raw = parse_report(body(), LIMIT)
    assert raw == REPORT
    assert (report.suite, report.labels["prompt_version"]) == ("refund-agent", "v12")
    first, second = report.scenarios
    assert (first.runs, first.passes, first.latency_ms, first.cost_usd.total) == (  # type: ignore[union-attr]
        50,
        47,
        {"p50": 820, "p95": 2400},
        1.84,
    )
    assert (second.latency_ms, second.cost_usd) == (None, None)
    assert report.summary() == "refund-agent: 87/90 passed in 2 scenarios"


def test_only_the_suite_and_scenario_counts_are_required() -> None:
    minimal = json.dumps(
        {
            "schema": "tocsin.eval/v1",
            "suite": "s",
            "scenarios": [{"name": "a", "runs": 1, "passes": 0}],
        }
    ).encode()
    report, _ = parse_report(minimal, LIMIT)
    assert (report.labels, report.report_url) == ({}, None)
    assert report.summary() == "s: 0/1 passed in 1 scenario"


def test_fields_a_newer_tool_adds_are_ignored_but_kept_in_the_raw_report() -> None:
    extra = json.loads(body())
    extra["tool"] = "stochast 0.4"
    extra["scenarios"][0]["tags"] = ["tools"]
    report, raw = parse_report(json.dumps(extra).encode(), LIMIT)
    assert raw["tool"] == "stochast 0.4"
    assert report.scenarios[0].name == "refund_tool_never_called"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"", "no evaluation report was sent"),
        (b"  \n", "no evaluation report was sent"),
        (b"OK", "not valid JSON"),
        (b"[1, 2]", "must be a JSON object"),
        (body(schema="tocsin.eval/v2"), "schema is 'tocsin.eval/v2', expected 'tocsin.eval/v1'"),
        (json.dumps({"suite": "s"}).encode(), "schema is None"),
        (body(suite=""), "suite"),
        (body(scenarios=[]), "scenarios"),
        (body(scenarios=[{"name": "a", "runs": 0, "passes": 0}]), "scenarios.0.runs"),
        (
            body(scenarios=[{"name": "a", "runs": 5, "passes": 6}]),
            "passes (6) can't be more than runs (5)",
        ),
        (body(scenarios=[{"name": "a", "runs": 5, "passes": -1}]), "scenarios.0.passes"),
        (body(scenarios=[{"name": "a", "runs": 5.5, "passes": 1}]), "scenarios.0.runs"),
        (body(scenarios=[{"name": "a", "runs": "5", "passes": 1}]), "scenarios.0.runs"),
        (body(scenarios=[{"name": "a", "runs": True, "passes": 1}]), "scenarios.0.runs"),
        (
            body(scenarios=[{"name": "a", "runs": 5, "passes": 1}] * 2),
            "two scenarios are named 'a'",
        ),
        (
            body(scenarios=[{"name": "a", "runs": 5, "passes": 1, "latency_ms": {"p50": -3}}]),
            "p50 must be a number that is not negative",
        ),
        (
            body(scenarios=[{"name": "a", "runs": 5, "passes": 1, "cost_usd": {"total": -1}}]),
            "scenarios.0.cost_usd.total",
        ),
        (body(labels={"model": 3}), "labels.model"),
        (body(labels={"": "x"}), "a label name must be 1 to 64 characters"),
        (body(labels={"model": "x" * 201}), "longer than 200 characters"),
        (body(report_url="javascript:alert(1)"), "must start with http:// or https://"),
    ],
)
def test_a_report_that_is_not_usable_is_refused_with_the_reason(
    payload: bytes, message: str
) -> None:
    assert message in rejection(payload)


def test_non_finite_numbers_are_refused() -> None:
    # json.loads accepts the NaN and Infinity that Python's own json.dumps writes.
    payload = body().replace(b'"p95": 2400', b'"p95": Infinity')
    assert "p95" in rejection(payload)
    payload = body().replace(b'"total": 1.84', b'"total": NaN')
    assert "cost_usd.total" in rejection(payload)


def test_an_oversized_report_is_refused_without_parsing_it() -> None:
    assert "larger than 100 bytes" in rejection(body(), limit=100)


def test_many_problems_are_summarised_not_listed_in_full() -> None:
    scenarios = [{"name": f"s{i}", "runs": 1, "passes": 2} for i in range(20)]
    message = rejection(body(scenarios=scenarios))
    assert message.count("can't be more than") == 5
    assert message.endswith("and 15 more")


async def eval_check(client: httpx.AsyncClient, **fields: Any) -> dict[str, Any]:
    return await create_check(client, name="nightly", kind="eval", **fields)


async def send(client: httpx.AsyncClient, check: dict[str, Any], payload: bytes) -> httpx.Response:
    return await client.post(check["ping_path"], content=payload, headers={"Authorization": ""})


async def test_a_report_with_the_success_ping_is_stored_with_its_scenarios(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check = await eval_check(client)
    response = await send(client, check, body())
    assert (response.status_code, response.text) == (200, "OK")

    async with sessionmaker() as session:
        [report] = await session.scalars(select(EvalReport))
        [run] = await session.scalars(select(Run))
        scenarios = list(await session.scalars(select(EvalScenario).order_by(EvalScenario.id)))
    assert (str(report.check_id), report.run_id, report.suite) == (
        check["id"],
        run.id,
        "refund-agent",
    )
    assert report.labels == REPORT["labels"]
    assert report.report_url == "https://ci.example.com/runs/812"
    assert report.raw == REPORT
    assert [(s.name, s.runs, s.passes) for s in scenarios] == [
        ("refund_tool_never_called", 50, 47),
        ("asks_for_order_number", 40, 40),
    ]
    assert (scenarios[0].latency_ms, scenarios[0].cost_usd) == ({"p50": 820, "p95": 2400}, 1.84)
    assert (scenarios[1].latency_ms, scenarios[1].cost_usd) == (None, None)
    assert (run.status, run.output_tail) == ("success", "refund-agent: 87/90 passed in 2 scenarios")
    assert (await client.get(f"/api/checks/{check['id']}")).json()["state"] == "up"


async def test_the_eval_endpoint_returns_reports_with_counts_and_intervals(
    client: httpx.AsyncClient,
) -> None:
    check = await eval_check(client)
    await send(client, check, body())
    await send(
        client,
        check,
        body(
            labels={"model": "claude-sonnet-5-5", "prompt_version": "v13"},
            scenarios=[{"name": "asks_for_order_number", "runs": 40, "passes": 31}],
            report_url=None,
        ),
    )

    reports = (await client.get(f"/api/checks/{check['id']}/eval")).json()
    newest, oldest = reports
    assert newest["labels"]["prompt_version"] == "v13"
    assert newest["report_url"] is None
    [scenario] = newest["scenarios"]
    low, high = wilson_interval(31, 40)
    assert (scenario["runs"], scenario["passes"], scenario["pass_rate"]) == (40, 31, 31 / 40)
    assert (scenario["low"], scenario["high"]) == (pytest.approx(low), pytest.approx(high))
    assert [s["name"] for s in oldest["scenarios"]] == [
        "refund_tool_never_called",
        "asks_for_order_number",
    ]
    assert oldest["scenarios"][0]["latency_ms"] == {"p50": 820, "p95": 2400}
    assert oldest["scenarios"][0]["cost_usd"] == 1.84
    assert oldest["run_id"] < newest["run_id"]

    paged = (await client.get(f"/api/checks/{check['id']}/eval?before={newest['id']}")).json()
    assert [report["id"] for report in paged] == [oldest["id"]]


async def test_the_eval_endpoint_needs_a_key_and_a_known_check(client: httpx.AsyncClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    assert (await client.get(f"/api/checks/{missing}/eval")).status_code == 404
    unauthorised = await client.get(f"/api/checks/{missing}/eval", headers={"Authorization": ""})
    assert unauthorised.status_code == 401


async def test_a_success_ping_without_a_report_fails_the_run_and_alerts(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker, dispatcher: Any
) -> None:
    await add_channel(sessionmaker)
    check = await eval_check(client)

    response = await client.get(check["ping_path"], headers={"Authorization": ""})
    assert response.status_code == 422
    assert "no evaluation report was sent" in response.text

    async with sessionmaker() as session:
        [run] = await session.scalars(select(Run))
        [event] = await session.scalars(select(Event))
        reports = list(await session.scalars(select(EvalReport)))
        deliveries = list(await session.scalars(select(Delivery)))
    assert run.status == "fail"
    assert run.output_tail is not None and "no evaluation report" in run.output_tail
    assert (event.to_state, event.run_id) == ("down", run.id)
    assert "no evaluation report was sent" in event.reason
    assert reports == []
    assert len(deliveries) == len(dispatcher.keys) == 1
    assert (await client.get(f"/api/checks/{check['id']}")).json()["state"] == "down"


async def test_an_unreadable_report_fails_the_run_and_says_why(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check = await eval_check(client)
    bad = body(scenarios=[{"name": "a", "runs": 5, "passes": 9}])
    response = await send(client, check, bad)
    assert response.status_code == 422
    assert "passes (9) can't be more than runs (5)" in response.text

    async with sessionmaker() as session:
        [event] = await session.scalars(select(Event))
        assert list(await session.scalars(select(EvalScenario))) == []
    assert event.reason == response.text

    # The next good report brings it back up.
    assert (await send(client, check, body())).status_code == 200
    assert (await client.get(f"/api/checks/{check['id']}")).json()["state"] == "up"


async def test_a_report_over_the_size_limit_is_refused(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    settings.eval_report_max_bytes = 300
    check = await eval_check(client)
    response = await send(client, check, body())
    assert response.status_code == 422
    assert "larger than 300 bytes" in response.text


async def test_a_report_longer_than_the_output_limit_still_parses(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    scenarios = [{"name": f"scenario-{i:03}", "runs": 10, "passes": 9} for i in range(400)]
    payload = body(scenarios=scenarios)
    assert len(payload) > 10_000
    check = await eval_check(client)
    assert (await send(client, check, payload)).status_code == 200
    async with sessionmaker() as session:
        assert len(list(await session.scalars(select(EvalScenario)))) == 400


async def test_start_and_fail_pings_on_an_eval_check_need_no_report(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check = await eval_check(client)
    assert (await send(client, check, b"")).status_code == 422
    start = await client.get(check["ping_path"] + "/start", headers={"Authorization": ""})
    assert start.status_code == 200
    failed = await client.post(
        check["ping_path"] + "/2",
        content=b"model API returned 529",
        headers={"Authorization": ""},
    )
    assert failed.status_code == 200
    async with sessionmaker() as session:
        runs = list(await session.scalars(select(Run).order_by(Run.id)))
    assert [(run.status, run.output_tail) for run in runs][-1] == ("fail", "model API returned 529")


async def test_an_ordinary_job_ignores_a_report_in_its_ping(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check = await create_check(client)
    assert (await send(client, check, body())).status_code == 200
    async with sessionmaker() as session:
        assert list(await session.scalars(select(EvalReport))) == []
        [run] = await session.scalars(select(Run))
    assert run.output_tail is not None and run.output_tail.startswith("{")


async def test_deleting_a_check_deletes_its_reports(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check = await eval_check(client)
    await send(client, check, body())
    assert (await client.delete(f"/api/checks/{check['id']}")).status_code == 204
    async with sessionmaker() as session:
        assert list(await session.scalars(select(EvalReport))) == []
        assert list(await session.scalars(select(EvalScenario))) == []
