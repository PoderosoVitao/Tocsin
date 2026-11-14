from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import Any

from tocsin.adapters import MAX_NAME_LENGTH, MAX_SCENARIOS, SCHEMA, AdapterError

PERCENTILES = (50, 95, 99)


# The p-th percentile by linear interpolation between the two closest ranks,
# the same convention (numpy's default) Stochast uses in its own reports, so
# the numbers tocsin shows are the numbers Stochast's report shows.
def _percentile(ordered: list[float], p: float) -> float:
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (p / 100)
    lower, upper = math.floor(rank), math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (1 - (rank - lower)) + ordered[upper] * (rank - lower)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


# A run passed if it raised no error and every assertion in it held: Stochast's
# own definition (RunRecord.passed), including that a run lost to a transport
# error counts as not passed.
def _passed(record: dict[str, Any]) -> bool:
    assertions = record.get("assertions")
    if not isinstance(assertions, list):
        raise AdapterError("a run has no list of assertions")
    return record.get("error") is None and all(
        isinstance(a, dict) and a.get("passed") is True for a in assertions
    )


def _records(document: bytes, source: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(document)
    except ValueError as exc:
        raise AdapterError(f"{source} is not valid JSON ({exc})") from exc
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise AdapterError(f"{source} is not a Stochast results file (a list of run records)")
    for item in payload:
        if not isinstance(item.get("scenario_name"), str) or "assertions" not in item:
            raise AdapterError(f"{source} is not a Stochast results file (a run has no scenario)")
    return payload


# Turns Stochast's saved results (stochast-results/<scenario>.json, a list of
# run records) into a tocsin.eval/v1 report: one scenario per file, with its
# number of runs and how many passed, latency percentiles from the runs'
# latency_ms, and the cost summed over the runs when cost tracking was on.
#
# Only counts and numbers are taken. The runs' outputs, tool calls and
# messages stay on the machine, since they can hold anything the agent saw.
# Stochast doesn't record what model or prompt it ran against, so say so with
# labels.
def stochast_report(
    documents: Sequence[tuple[str, bytes]],
    *,
    suite: str | None = None,
    labels: dict[str, str] | None = None,
    report_url: str | None = None,
) -> dict[str, Any]:
    scenarios: dict[str, dict[str, Any]] = {}
    for source, document in documents:
        records = _records(document, source)
        if not records:
            raise AdapterError(f"{source} has no runs")
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            grouped.setdefault(record["scenario_name"], []).append(record)
        for name, runs in grouped.items():
            if name in scenarios:
                raise AdapterError(
                    f"scenario {name!r} is in more than one file; send one batch of it at a time"
                )
            scenarios[name[:MAX_NAME_LENGTH]] = _scenario(name, runs)

    if not scenarios:
        raise AdapterError("no results were given")
    if len(scenarios) > MAX_SCENARIOS:
        raise AdapterError(
            f"{len(scenarios)} scenarios, but a report holds at most {MAX_SCENARIOS}"
        )
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "suite": suite or "stochast",
        "labels": labels or {},
        "scenarios": list(scenarios.values()),
    }
    if report_url:
        report["report_url"] = report_url
    return report


def _scenario(name: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    scenario: dict[str, Any] = {
        "name": name[:MAX_NAME_LENGTH],
        "runs": len(runs),
        "passes": sum(_passed(run) for run in runs),
    }
    latencies = sorted(v for run in runs if (v := _number(run.get("latency_ms"))) is not None)
    if latencies:
        scenario["latency_ms"] = {f"p{p}": round(_percentile(latencies, p), 3) for p in PERCENTILES}
    costs = [v for run in runs if (v := _number(run.get("cost_usd"))) is not None]
    # Stochast only tracks cost when given per-token prices, and records 0
    # otherwise, so a total of 0 means "not tracked" and is left out.
    if sum(costs) > 0:
        scenario["cost_usd"] = {"total": round(sum(costs), 6)}
    return scenario
