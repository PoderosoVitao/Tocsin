from __future__ import annotations

import json
import math
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator
from pydantic.types import StrictInt

SCHEMA = "tocsin.eval/v1"

MAX_SCENARIOS = 1000
MAX_LABELS = 20


class ReportError(ValueError):
    """The body of an evaluation check's success ping is not a usable report."""


# Percentiles are named as in the report: "p50", "p95", "p99", and so on.
def _finite_non_negative(values: dict[str, float]) -> dict[str, float]:
    for name, value in values.items():
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be a number that is not negative")
    return values


class Cost(BaseModel):
    total: float = Field(ge=0, allow_inf_nan=False)


class Scenario(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    # Counts rather than a percentage, so tocsin can do its own statistics.
    runs: Annotated[StrictInt, Field(ge=1)]
    passes: Annotated[StrictInt, Field(ge=0)]
    latency_ms: dict[str, float] | None = Field(default=None, max_length=20)
    cost_usd: Cost | None = None

    @field_validator("passes")
    @classmethod
    def _passes_within_runs(cls, passes: int, info: Any) -> int:
        runs = info.data.get("runs")
        if runs is not None and passes > runs:
            raise ValueError(f"passes ({passes}) can't be more than runs ({runs})")
        return passes

    @field_validator("latency_ms")
    @classmethod
    def _latency_is_sane(cls, latency: dict[str, float] | None) -> dict[str, float] | None:
        return None if latency is None else _finite_non_negative(latency)


class Report(BaseModel):
    schema_: Literal["tocsin.eval/v1"] = Field(alias="schema")
    suite: str = Field(min_length=1, max_length=200)
    # What was being evaluated: model, prompt_version, git_sha, or anything
    # else the tool wants to record. They let the dashboard say what changed
    # between the last good run and a bad one.
    labels: dict[str, str] = Field(default_factory=dict, max_length=MAX_LABELS)
    scenarios: list[Scenario] = Field(min_length=1, max_length=MAX_SCENARIOS)
    report_url: str | None = Field(default=None, max_length=2000)

    @field_validator("labels")
    @classmethod
    def _labels_are_short(cls, labels: dict[str, str]) -> dict[str, str]:
        for name, value in labels.items():
            if not 0 < len(name) <= 64:
                raise ValueError("a label name must be 1 to 64 characters")
            if len(value) > 200:
                raise ValueError(f"label {name!r} is longer than 200 characters")
        return labels

    # The link is shown on the dashboard, so only web links are accepted: a
    # javascript: URL would run in the page of whoever clicked it.
    @field_validator("report_url")
    @classmethod
    def _report_url_is_a_web_link(cls, url: str | None) -> str | None:
        if url is not None and not url.lower().startswith(("http://", "https://")):
            raise ValueError("must start with http:// or https://")
        return url

    @field_validator("scenarios")
    @classmethod
    def _names_are_unique(cls, scenarios: list[Scenario]) -> list[Scenario]:
        seen: set[str] = set()
        for scenario in scenarios:
            if scenario.name in seen:
                raise ValueError(f"two scenarios are named {scenario.name!r}")
            seen.add(scenario.name)
        return scenarios

    def summary(self) -> str:
        runs = sum(scenario.runs for scenario in self.scenarios)
        passes = sum(scenario.passes for scenario in self.scenarios)
        count = len(self.scenarios)
        return f"{self.suite}: {passes}/{runs} passed in {count} scenario{'s' * (count != 1)}"


def _describe(error: ValidationError) -> str:
    problems = []
    for item in error.errors()[:5]:
        where = ".".join(str(part) for part in item["loc"])
        text = str(item["msg"]).removeprefix("Value error, ")
        problems.append(f"{where}: {text}" if where else text)
    more = len(error.errors()) - len(problems)
    return "; ".join(problems) + (f"; and {more} more" if more > 0 else "")


# Reads an evaluation report from the body of a ping. Raises ReportError with a
# message fit to show the person who set the job up, since it is also what the
# failed run's alert says.
def parse_report(body: bytes, limit: int) -> tuple[Report, dict[str, Any]]:
    if not body.strip():
        raise ReportError(
            f"no evaluation report was sent; an eval check expects a {SCHEMA} JSON body "
            "with its success ping"
        )
    if len(body) > limit:
        raise ReportError(f"the evaluation report is larger than {limit} bytes")
    try:
        raw = json.loads(body)
    except ValueError as exc:
        raise ReportError(f"the evaluation report is not valid JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise ReportError("the evaluation report must be a JSON object")
    found = raw.get("schema")
    if found != SCHEMA:
        raise ReportError(f"the evaluation report's schema is {found!r}, expected {SCHEMA!r}")
    try:
        return Report.model_validate(raw), raw
    except ValidationError as exc:
        raise ReportError(f"the evaluation report is invalid: {_describe(exc)}") from exc
