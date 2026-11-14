from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from tests.helpers import FakeTocsin, create_check
from tocsin.adapters import AdapterError
from tocsin.adapters.stochast import stochast_report
from tocsin.cli import main
from tocsin.db import Sessionmaker
from tocsin.evalreport import parse_report
from tocsin.models import EvalScenario

CHECK = "0b9e5bd4-77a4-4a2c-9a51-6b1f6b2d3c11"
FIXTURES = Path(__file__).parent / "fixtures"

# Written by Stochast's own save_run_records: nine runs of one scenario (two
# failed an assertion, one was lost to a transport error, with the costs
# priced) and four runs of another that all passed with no cost tracking.
# The numbers below are what Stochast's own analyze_scenario reports for them.
LOOKUP = ("refund_status_lookup.json", FIXTURES / "refund_status_lookup.json")
FRAUD = ("escalates_fraud.json", FIXTURES / "escalates_fraud.json")


def load(*paths: tuple[str, Path]) -> list[tuple[str, bytes]]:
    return [(name, path.read_bytes()) for name, path in paths]


def run(
    scenario: str = "s", passed: bool = True, error: str | None = None, **fields: Any
) -> dict[str, Any]:
    return {
        "run_index": 0,
        "scenario_name": scenario,
        "assertions": [{"label": "a", "passed": passed, "detail": ""}],
        "error": error,
        **fields,
    }


def document(*runs: dict[str, Any]) -> bytes:
    return json.dumps(list(runs)).encode()


def test_the_counts_and_percentiles_match_what_stochast_reports_for_the_same_files() -> None:
    report = stochast_report(load(LOOKUP, FRAUD))
    lookup, fraud = report["scenarios"]
    assert lookup == {
        "name": "refund_status_lookup",
        "runs": 9,
        "passes": 6,
        "latency_ms": {"p50": 910.2, "p95": 1980.54, "p99": 2236.828},
        "cost_usd": {"total": 0.0756},
    }
    # No cost tracking means every cost is 0, which is left out, not reported as free.
    assert fraud == {
        "name": "escalates_fraud",
        "runs": 4,
        "passes": 4,
        "latency_ms": {"p50": 101.5, "p95": 102.85, "p99": 102.97},
    }
    assert (report["schema"], report["suite"], report["labels"]) == (
        "tocsin.eval/v1",
        "stochast",
        {},
    )


def test_the_report_passes_the_servers_own_validation() -> None:
    report = stochast_report(
        load(LOOKUP), suite="refund-agent", labels={"model": "m"}, report_url="https://ci/1"
    )
    parsed, raw = parse_report(json.dumps(report).encode(), 1_000_000)
    assert (parsed.suite, parsed.labels, parsed.report_url) == (
        "refund-agent",
        {"model": "m"},
        "https://ci/1",
    )
    assert raw == report


# The runs hold what the agent was asked and answered, and what its tools
# returned. Only counts and numbers leave the machine.
def test_nothing_from_the_runs_content_is_in_the_report() -> None:
    text = json.dumps(stochast_report(load(LOOKUP, FRAUD)))
    for content in ("Order 4471", "lookup_order", "no idea", "shipped", "transport error"):
        assert content not in text


def test_a_run_counts_as_passed_the_way_stochast_counts_it() -> None:
    doc = document(
        run(passed=True),
        run(passed=False),
        run(passed=True, error="RuntimeError: boom"),
        run(error="transport error after 3 retries"),
        {"scenario_name": "s", "assertions": [], "error": None},
    )
    [scenario] = stochast_report([("s.json", doc)])["scenarios"]
    assert (scenario["runs"], scenario["passes"]) == (5, 2)


def test_one_scenario_per_name_even_when_a_file_holds_several() -> None:
    doc = document(run("a"), run("b", passed=False), run("a"))
    report = stochast_report([("all.json", doc)])
    assert [(s["name"], s["runs"], s["passes"]) for s in report["scenarios"]] == [
        ("a", 2, 2),
        ("b", 1, 0),
    ]


def test_a_scenario_in_two_files_is_refused_not_added_up() -> None:
    first, second = document(run("a")), document(run("a", passed=False))
    with pytest.raises(AdapterError, match="'a' is in more than one file"):
        stochast_report([("baseline.json", first), ("variant.json", second)])


def test_values_that_are_not_numbers_are_left_out_not_fatal() -> None:
    doc = document(
        run(latency_ms="slow", cost_usd=None),
        run(latency_ms=-5, cost_usd=float("nan")),
        run(latency_ms=True),
    )
    [scenario] = stochast_report([("s.json", doc)])["scenarios"]
    assert "latency_ms" not in scenario and "cost_usd" not in scenario


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"", "x.json is not valid JSON"),
        (b"{}", "not a Stochast results file"),
        (b"[1, 2]", "not a Stochast results file"),
        (b'[{"name": "a"}]', "a run has no scenario"),
        (b'[{"scenario_name": "a", "assertions": "yes"}]', "no list of assertions"),
        (b"[]", "x.json has no runs"),
    ],
)
def test_input_that_is_not_stochast_output_is_refused_with_the_reason(
    content: bytes, message: str
) -> None:
    with pytest.raises(AdapterError, match=message):
        stochast_report([("x.json", content)])


def test_giving_no_results_is_refused() -> None:
    with pytest.raises(AdapterError, match="no results"):
        stochast_report([])


async def test_the_converted_report_is_accepted_by_an_eval_check(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check = await create_check(client, kind="eval", name="agent")
    body = json.dumps(stochast_report(load(LOOKUP, FRAUD))).encode()
    response = await client.post(check["ping_path"], content=body, headers={"Authorization": ""})
    assert response.status_code == 200, response.text
    async with sessionmaker() as session:
        found = {s.name: (s.runs, s.passes) for s in await session.scalars(select(EvalScenario))}
    assert found == {"refund_status_lookup": (9, 6), "escalates_fraud": (4, 4)}


def report_cli(tocsin: FakeTocsin, *args: str) -> int:
    return main(
        ["report", "stochast", "--check", CHECK, "--server", tocsin.url, "--timeout", "2", *args]
    )


def test_the_command_sends_a_whole_results_directory(tocsin: FakeTocsin) -> None:
    status = report_cli(tocsin, "--label", "model=m", "--suite", "agent", str(FIXTURES))
    assert status == 0
    [(method, path, body)] = tocsin.requests
    assert (method, path) == ("POST", f"/ping/{CHECK}")
    sent = json.loads(body)
    assert (sent["suite"], sent["labels"]) == ("agent", {"model": "m"})
    assert {s["name"] for s in sent["scenarios"]} == {"refund_status_lookup", "escalates_fraud"}


def test_results_that_cannot_be_read_tell_tocsin_the_job_failed(
    tocsin: FakeTocsin, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    junk = tmp_path / "scenario.json"
    junk.write_text('{"not": "stochast"}')
    assert report_cli(tocsin, str(junk)) == 1
    [(_, path, body)] = tocsin.requests
    assert path == f"/ping/{CHECK}/fail"
    assert b"could not read the Stochast results" in body
    assert "not a Stochast results file" in capsys.readouterr().err


def test_a_directory_with_no_results_is_a_usage_error(tocsin: FakeTocsin, tmp_path: Path) -> None:
    assert report_cli(tocsin, str(tmp_path)) == 2
    assert tocsin.requests == []
