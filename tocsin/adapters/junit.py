from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from typing import Any, Literal

from tocsin.adapters import MAX_NAME_LENGTH, MAX_SCENARIOS, SCHEMA, AdapterError

GroupBy = Literal["test", "class"]


def _nearest_rank(sorted_values: list[float], percentile: float) -> float:
    rank = max(1, math.ceil(percentile / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def _clip(name: str) -> str:
    return name if len(name) <= MAX_NAME_LENGTH else name[: MAX_NAME_LENGTH - 1] + "…"


def _parse(xml: bytes) -> ET.Element:
    # Entities are how an XML file can be made to expand into gigabytes. A test
    # report has no use for them, so a document that declares any is refused.
    if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
        raise AdapterError("the XML declares a DOCTYPE or entities, which are not accepted")
    try:
        return ET.fromstring(xml)
    except ET.ParseError as exc:
        raise AdapterError(f"the file is not valid XML ({exc})") from exc


# What one test case did: "pass", "fail" (a failure or an error) or "skip".
# Reruns that a build tool recorded as flaky (flakyFailure, rerunFailure) leave
# the test passing, as the tool itself reports it.
def _outcome(case: ET.Element) -> str:
    tags = {child.tag for child in case}
    if tags & {"failure", "error"}:
        return "fail"
    if "skipped" in tags:
        return "skip"
    return "pass"


def _seconds(case: ET.Element) -> float | None:
    try:
        value = float(case.get("time", ""))
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


class _Tally:
    def __init__(self) -> None:
        self.runs = 0
        self.passes = 0
        self.times_ms: list[float] = []


# Turns JUnit XML documents (the format most test tools can write: pytest --junitxml,
# Maven Surefire, Jest and Vitest reporters, Go's gotestsum, and more) into a
# tocsin.eval/v1 report. Several documents (Surefire writes one file per
# class) make one report.
#
# Every test case is one run of a scenario, and a scenario is a distinct test
# name, so a test that appears several times (a repeated or parametrised run)
# gets a pass rate. A suite with thousands of different tests would be too many
# scenarios, so `group_by="class"` makes each class (or suite) one scenario,
# with a run per test in it. Skipped tests are left out; a failure or an error
# counts as a failed run. Latency comes from the `time` attributes, in the
# same two percentiles (p50, p95) the schema's example uses.
def junit_report(
    documents: Sequence[bytes],
    *,
    suite: str | None = None,
    labels: dict[str, str] | None = None,
    report_url: str | None = None,
    group_by: GroupBy = "test",
) -> dict[str, Any]:
    roots = [_parse(xml) for xml in documents]
    tallies: dict[str, _Tally] = {}
    suite_name = suite or next((root.get("name") for root in roots if root.get("name")), None)

    def walk(node: ET.Element, enclosing: str | None) -> None:
        nonlocal suite_name
        for child in node:
            if child.tag in ("testsuites", "testsuite"):
                if not suite_name and child.get("name"):
                    suite_name = child.get("name")
                walk(child, child.get("name") or enclosing)
            elif child.tag == "testcase":
                outcome = _outcome(child)
                if outcome == "skip":
                    continue
                name = child.get("name") or "unnamed"
                classname = child.get("classname")
                if group_by == "class":
                    key = classname or enclosing or "unnamed"
                else:
                    key = f"{classname}.{name}" if classname else name
                tally = tallies.setdefault(_clip(key), _Tally())
                tally.runs += 1
                tally.passes += outcome == "pass"
                seconds = _seconds(child)
                if seconds is not None:
                    tally.times_ms.append(seconds * 1000)

    for root in roots:
        if root.tag not in ("testsuites", "testsuite"):
            raise AdapterError(f"this is not JUnit XML: its top element is <{root.tag}>")
    holder = ET.Element("holder")
    holder.extend(roots)
    walk(holder, None)

    if not tallies:
        raise AdapterError("no test cases were found, or all of them were skipped")
    if len(tallies) > MAX_SCENARIOS:
        hint = (
            "" if group_by == "class" else "; use --group-by class to make each class one scenario"
        )
        raise AdapterError(
            f"{len(tallies)} different tests, but a report holds at most {MAX_SCENARIOS}{hint}"
        )

    scenarios = []
    for name, tally in tallies.items():
        scenario: dict[str, Any] = {"name": name, "runs": tally.runs, "passes": tally.passes}
        if tally.times_ms:
            times = sorted(tally.times_ms)
            scenario["latency_ms"] = {
                "p50": round(_nearest_rank(times, 50), 3),
                "p95": round(_nearest_rank(times, 95), 3),
            }
        scenarios.append(scenario)

    report: dict[str, Any] = {
        "schema": SCHEMA,
        "suite": _clip(suite_name or "junit"),
        "labels": labels or {},
        "scenarios": scenarios,
    }
    if report_url:
        report["report_url"] = report_url
    return report
