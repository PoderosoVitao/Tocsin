from __future__ import annotations

import io
import json
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from tests.helpers import FakeTocsin, create_check
from tocsin.adapters import AdapterError
from tocsin.adapters.junit import junit_report
from tocsin.cli import main
from tocsin.db import Sessionmaker
from tocsin.evalreport import parse_report
from tocsin.models import EvalScenario

CHECK = "0b9e5bd4-77a4-4a2c-9a51-6b1f6b2d3c11"

# What pytest --junitxml writes.
PYTEST_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests"><testsuite name="pytest" errors="0" failures="1" skipped="1"
tests="4" time="0.120">
<testcase classname="tests.test_refund" name="test_never_calls_tool" time="0.012"/>
<testcase classname="tests.test_refund" name="test_asks_for_order" time="0.034">
<failure message="assert 1 == 2">Traceback ... AssertionError</failure></testcase>
<testcase classname="tests.test_refund" name="test_slow" time="0.5">
<skipped type="pytest.skip" message="needs a GPU">skipped</skipped></testcase>
<testcase classname="tests.test_refund" name="test_polite" time="0.020"/>
</testsuite></testsuites>"""

# One file of several, as Maven Surefire writes (a file per test class).
SUREFIRE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.example.RefundTest" tests="3" errors="1" failures="0" skipped="0">
<testcase name="flaky" classname="com.example.RefundTest" time="0.5">
<flakyFailure message="timed out"/></testcase>
<testcase name="broken" classname="com.example.RefundTest" time="0.1">
<error message="NullPointerException" type="java.lang.NullPointerException"/></testcase>
<testcase name="fine" classname="com.example.RefundTest" time="0.2"/>
</testsuite>"""

OTHER_CLASS_XML = b"""<testsuite name="com.example.OrderTest">
<testcase name="fine" classname="com.example.OrderTest" time="0.3"/></testsuite>"""


def scenarios(report: dict[str, Any]) -> dict[str, tuple[int, int]]:
    return {s["name"]: (s["runs"], s["passes"]) for s in report["scenarios"]}


def test_a_pytest_report_becomes_one_scenario_per_test_and_skips_are_left_out() -> None:
    report = junit_report([PYTEST_XML])
    assert report["schema"] == "tocsin.eval/v1"
    assert report["suite"] == "pytest tests"
    assert scenarios(report) == {
        "tests.test_refund.test_never_calls_tool": (1, 1),
        "tests.test_refund.test_asks_for_order": (1, 0),
        "tests.test_refund.test_polite": (1, 1),
    }
    first = report["scenarios"][0]
    assert first["latency_ms"] == {"p50": 12.0, "p95": 12.0}


def test_the_report_the_adapter_writes_passes_the_servers_own_validation() -> None:
    report = junit_report(
        [PYTEST_XML, SUREFIRE_XML],
        labels={"model": "m", "git_sha": "abc"},
        report_url="https://ci.example.com/1",
    )
    parsed, raw = parse_report(json.dumps(report).encode(), 1_000_000)
    assert (parsed.suite, parsed.labels["git_sha"], parsed.report_url) == (
        "pytest tests",
        "abc",
        "https://ci.example.com/1",
    )
    assert raw == report


def test_a_test_that_appears_several_times_gets_a_pass_rate_and_latency_percentiles() -> None:
    cases = "".join(
        f'<testcase classname="e" name="refund" time="{n}">'
        + ("<failure/>" if n in (3, 7) else "")
        + "</testcase>"
        for n in range(1, 11)
    )
    report = junit_report([f"<testsuite name='s'>{cases}</testsuite>".encode()])
    [scenario] = report["scenarios"]
    assert (scenario["name"], scenario["runs"], scenario["passes"]) == ("e.refund", 10, 8)
    # Nearest-rank percentiles of 1s..10s.
    assert scenario["latency_ms"] == {"p50": 5000.0, "p95": 10000.0}


def test_errors_count_as_failures_and_flaky_reruns_leave_the_test_passing() -> None:
    report = junit_report([SUREFIRE_XML])
    assert report["suite"] == "com.example.RefundTest"
    assert scenarios(report) == {
        "com.example.RefundTest.flaky": (1, 1),
        "com.example.RefundTest.broken": (1, 0),
        "com.example.RefundTest.fine": (1, 1),
    }


def test_several_files_make_one_report() -> None:
    report = junit_report([SUREFIRE_XML, OTHER_CLASS_XML])
    assert report["suite"] == "com.example.RefundTest"
    assert len(report["scenarios"]) == 4
    assert scenarios(report)["com.example.OrderTest.fine"] == (1, 1)


def test_grouping_by_class_makes_each_class_one_scenario() -> None:
    report = junit_report([SUREFIRE_XML, OTHER_CLASS_XML], group_by="class")
    assert scenarios(report) == {"com.example.RefundTest": (3, 2), "com.example.OrderTest": (1, 1)}


def test_a_test_without_a_classname_is_named_by_itself() -> None:
    xml = b"<testsuite name='s'><testcase name='alone'/><testcase/></testsuite>"
    assert scenarios(junit_report([xml])) == {"alone": (1, 1), "unnamed": (1, 1)}
    assert "latency_ms" not in junit_report([xml])["scenarios"][0]


def test_a_bad_time_is_ignored_not_fatal() -> None:
    xml = (
        b"<testsuite name='s'><testcase name='a' time='soon'/>"
        b"<testcase name='b' time='-1'/></testsuite>"
    )
    assert all("latency_ms" not in s for s in junit_report([xml])["scenarios"])


def test_the_suite_name_can_be_chosen_and_has_a_fallback() -> None:
    xml = b"<testsuite><testcase name='a'/></testsuite>"
    assert junit_report([xml])["suite"] == "junit"
    assert junit_report([xml], suite="refund-agent")["suite"] == "refund-agent"
    assert junit_report([PYTEST_XML], suite="mine")["suite"] == "mine"


def test_a_very_long_test_name_is_shortened_to_what_the_schema_allows() -> None:
    xml = f"<testsuite name='s'><testcase name='{'x' * 500}'/></testsuite>".encode()
    [scenario] = junit_report([xml])["scenarios"]
    assert len(scenario["name"]) == 200 and scenario["name"].endswith("…")


def test_too_many_different_tests_for_one_report_say_how_to_group_them() -> None:
    cases = "".join(f"<testcase classname='c{n % 10}' name='t{n}'/>" for n in range(1001))
    xml = f"<testsuite name='big'>{cases}</testsuite>".encode()
    with pytest.raises(AdapterError, match=r"1001 different tests.*--group-by class"):
        junit_report([xml])
    assert len(junit_report([xml], group_by="class")["scenarios"]) == 10


@pytest.mark.parametrize(
    ("xml", "message"),
    [
        (b"", "not valid XML"),
        (b"<testsuite><testcase", "not valid XML"),
        (b"<html><body/></html>", "its top element is <html>"),
        (b"<testsuite name='s'/>", "no test cases"),
        (
            b"<testsuite><testcase name='a'><skipped/></testcase></testsuite>",
            "all of them were skipped",
        ),
        (
            b'<!DOCTYPE x [<!ENTITY a "aaaa">]><testsuite><testcase name="&a;"/></testsuite>',
            "DOCTYPE or entities",
        ),
        (b'<!doctype x><testsuite><testcase name="a"/></testsuite>', "DOCTYPE or entities"),
    ],
)
def test_input_that_is_not_usable_is_refused_with_the_reason(xml: bytes, message: str) -> None:
    with pytest.raises(AdapterError, match=message):
        junit_report([xml])


async def test_a_converted_report_is_accepted_by_an_eval_check(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check = await create_check(client, kind="eval", name="tests")
    body = json.dumps(junit_report([PYTEST_XML, SUREFIRE_XML])).encode()
    response = await client.post(check["ping_path"], content=body, headers={"Authorization": ""})
    assert response.status_code == 200, response.text
    async with sessionmaker() as session:
        found = {s.name: (s.runs, s.passes) for s in await session.scalars(select(EvalScenario))}
    assert found["tests.test_refund.test_asks_for_order"] == (1, 0)
    assert len(found) == 6


@pytest.fixture
def results(tmp_path: Any) -> list[str]:
    first = tmp_path / "a.xml"
    second = tmp_path / "b.xml"
    first.write_bytes(PYTEST_XML)
    second.write_bytes(OTHER_CLASS_XML)
    return [str(first), str(second)]


def report_cli(tocsin: FakeTocsin, *args: str) -> int:
    return main(
        ["report", "junit", "--check", CHECK, "--server", tocsin.url, "--timeout", "2", *args]
    )


def test_the_command_sends_the_report_as_the_success_ping(
    tocsin: FakeTocsin, results: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    status = report_cli(tocsin, "--label", "model=m", "--label", "git_sha=abc", *results)
    assert status == 0
    [(method, path, body)] = tocsin.requests
    assert (method, path) == ("POST", f"/ping/{CHECK}")
    sent = json.loads(body)
    assert sent["labels"] == {"model": "m", "git_sha": "abc"}
    assert len(sent["scenarios"]) == 4
    assert "reported 4 runs in 4 scenarios" in capsys.readouterr().out


def test_the_command_can_read_standard_input_and_print_instead_of_sending(
    tocsin: FakeTocsin, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Stdin:
        buffer = io.BytesIO(OTHER_CLASS_XML)

    monkeypatch.setattr("sys.stdin", Stdin)
    assert report_cli(tocsin, "--dry-run", "-") == 0
    assert tocsin.requests == []
    assert (
        json.loads(capsys.readouterr().out)["scenarios"][0]["name"] == "com.example.OrderTest.fine"
    )


def test_results_that_cannot_be_read_tell_tocsin_the_job_failed(
    tocsin: FakeTocsin, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "broken.xml"
    broken.write_bytes(b"<testsuite><testcase")
    assert report_cli(tocsin, str(broken)) == 1
    [(_, path, body)] = tocsin.requests
    assert path == f"/ping/{CHECK}/fail"
    assert b"could not read the test results: the file is not valid XML" in body
    assert "not valid XML" in capsys.readouterr().err


def test_a_report_tocsin_refuses_is_shown_with_its_reason(
    results: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeTocsin(status=422, reply=b"the evaluation report is invalid")
    try:
        assert report_cli(fake, *results) == 1
    finally:
        fake.server.shutdown()
    err = capsys.readouterr().err
    assert "refused the report (422: the evaluation report is invalid)" in err
    assert len(fake.requests) == 1  # a refusal is not retried


def test_an_unreachable_server_is_an_error_not_a_silent_success(
    results: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    status = main(
        [
            "report",
            "junit",
            "--check",
            CHECK,
            "--server",
            "http://127.0.0.1:9",
            "--timeout",
            "1",
            *results,
        ]
    )
    assert status == 1
    assert "could not reach tocsin" in capsys.readouterr().err


def test_bad_arguments_exit_with_a_usage_error(
    tocsin: FakeTocsin, results: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TOCSIN_CHECK", raising=False)
    assert report_cli(tocsin, "--label", "no-equals-sign", *results) == 2
    assert report_cli(tocsin, "/no/such/file.xml") == 2
    assert main(["report", "junit", "--server", tocsin.url, *results]) == 2
    assert tocsin.requests == []
