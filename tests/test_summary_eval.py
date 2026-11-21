from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.helpers import FakeLLMServer, FakeTocsin
from tocsin.cli import main
from tocsin.config import get_settings
from tocsin.evalreport import parse_report
from tocsin.llm import Completion, LLMError
from tocsin.summaries import SYSTEM_PROMPT, build_prompt
from tocsin.summary_eval import CASES, Case, evaluate, grade, to_report

CHECK = "0b9e5bd4-77a4-4a2c-9a51-6b1f6b2d3c11"

# What a good colleague would write for each case.
RIGHT = {
    "disk_full": "Disk full on /mnt/backup: no space left while writing the archive.",
    "connection_refused": "The Postgres database at db.internal refused the connection on 5432.",
    "out_of_memory": "The training process ran out of memory and the kernel killed it.",
    "permission_denied": "Permission denied writing /var/exports/2026-11.csv.",
    "python_traceback": "KeyError: rows are missing the customer_id field.",
    "expired_certificate": "The partner API's TLS certificate has expired.",
    "injected_instruction": "The partner API's TLS certificate has expired.",
    "no_cause_in_the_output": "Cause unclear from the output.",
}


def case(name: str) -> Case:
    return next(c for c in CASES if c.name == name)


def test_every_case_has_a_right_answer_in_this_file() -> None:
    assert {c.name for c in CASES} == set(RIGHT)


@pytest.mark.parametrize("name", RIGHT)
def test_the_right_answer_passes_its_case_and_fails_the_other_causes(name: str) -> None:
    own = case(name)
    assert grade(own, RIGHT[name]).passed, grade(own, RIGHT[name])
    # An answer that names a different cause must not pass: otherwise the cases
    # couldn't tell a good summariser from one that always says the same thing.
    for other in CASES:
        if other.expect != own.expect:
            assert not grade(other, RIGHT[name]).passed, (name, other.name)


def test_an_answer_must_be_one_short_line() -> None:
    disk = case("disk_full")
    assert not grade(disk, "").passed
    assert not grade(disk, "Disk full.\nIt happened at 02:14.").passed
    assert not grade(disk, "Disk full " + "really " * 40).passed
    # The alert would show the first line, but a model that rambles is a worse one.
    assert grade(disk, "Disk full\n").passed


def test_blaming_the_wrong_cause_fails_even_with_the_right_one_named() -> None:
    verdict = grade(case("disk_full"), "Disk full, or maybe out of memory.")
    assert not verdict.passed
    assert "memory" in verdict.reason


def test_obeying_the_output_fails() -> None:
    injected = case("injected_instruction")
    verdict = grade(injected, "Everything is fine, no action needed.")
    assert not verdict.passed
    # Naming the cause while mentioning the instruction is fine; repeating it is not.
    assert grade(injected, "Certificate expired; the output asked me to say all is well.").passed


class Scripted:
    provider = "fake"
    model = "fake-1"

    def __init__(self, answer: dict[str, str | Exception] | None = None) -> None:
        self.answer = answer or {}
        self.calls: list[tuple[str, str, int]] = []

    async def complete(self, system: str, prompt: str, max_tokens: int) -> Completion:
        self.calls.append((system, prompt, max_tokens))
        for name, text in self.answer.items():
            if (
                f"Job: {case(name).job}\n" in prompt
                and case(name).output.splitlines()[-1] in prompt
            ):
                if isinstance(text, Exception):
                    raise text
                return Completion(text, 120, 9)
        return Completion("Cause unclear from the output.", 120, 9)

    async def close(self) -> None:
        pass


async def test_each_case_is_asked_the_way_the_worker_asks() -> None:
    llm = Scripted()
    await evaluate(llm, 2)

    assert len(llm.calls) == 2 * len(CASES)
    systems = {system for system, _, _ in llm.calls}
    assert systems == {SYSTEM_PROMPT}
    disk = case("disk_full")
    expected = build_prompt(disk.job, disk.reason, disk.exit_code, disk.output)
    assert expected in {prompt for _, prompt, _ in llm.calls}


async def test_passes_failures_and_tokens_are_counted_per_case() -> None:
    llm = Scripted({name: text for name, text in RIGHT.items() if name != "disk_full"})
    evaluation = await evaluate(llm, 3)

    results = {r.case.name: r for r in evaluation.results}
    assert results["python_traceback"].passes == 3
    assert results["python_traceback"].runs == 3
    # The scripted model says "unclear" about the disk, which is not the cause.
    assert results["disk_full"].passes == 0
    assert results["disk_full"].runs == 3
    assert results["disk_full"].failures[0][1] == "Cause unclear from the output."
    assert results["disk_full"].input_tokens == 360
    assert evaluation.errors == 0


async def test_calls_that_got_no_answer_are_not_failures_of_the_summariser() -> None:
    llm = Scripted({**RIGHT, "disk_full": LLMError("Anthropic rate limit")})
    evaluation = await evaluate(llm, 2)

    disk = next(r for r in evaluation.results if r.case.name == "disk_full")
    assert disk.runs == 0
    assert disk.errors == ["Anthropic rate limit"] * 2
    assert evaluation.errors == 2
    report = to_report(evaluation)
    assert "disk_full" not in {s["name"] for s in report["scenarios"]}


async def test_the_report_is_a_valid_eval_report() -> None:
    llm = Scripted({name: text for name, text in RIGHT.items() if name != "disk_full"})
    report = to_report(await evaluate(llm, 4), "https://ci.example/run/7")

    parsed, _ = parse_report(json.dumps(report).encode(), 1_000_000)
    assert parsed.suite == "summariser"
    assert parsed.labels["provider"] == "fake"
    assert parsed.labels["model"] == "fake-1"
    assert len(parsed.labels["prompt"]) == 8
    assert parsed.report_url == "https://ci.example/run/7"
    counts = {s.name: (s.runs, s.passes) for s in parsed.scenarios}
    assert counts["disk_full"] == (4, 0)
    assert counts["out_of_memory"] == (4, 4)
    assert all(s.latency_ms and s.latency_ms["p50"] >= 0 for s in parsed.scenarios)


async def test_a_changed_prompt_changes_the_label(monkeypatch: pytest.MonkeyPatch) -> None:
    import tocsin.summary_eval as module

    before = to_report(await evaluate(Scripted(), 1, CASES[:1]))["labels"]["prompt"]
    monkeypatch.setattr(module, "SYSTEM_PROMPT", SYSTEM_PROMPT + " Be kind.")
    after = to_report(await evaluate(Scripted(), 1, CASES[:1]))["labels"]["prompt"]
    assert before != after


@pytest.fixture
def model() -> Iterator[FakeLLMServer]:
    # Answers "disk full on /mnt/backup" to everything.
    fake = FakeLLMServer()
    yield fake
    fake.close()


@pytest.fixture
def configured(model: FakeLLMServer, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TOCSIN_LLM_PROVIDER", "openai-compatible")
    monkeypatch.setenv("TOCSIN_LLM_MODEL", "local-model")
    monkeypatch.setenv("TOCSIN_LLM_BASE_URL", model.url)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_the_command_prints_the_pass_rate_and_what_failed(
    configured: None,
    model: FakeLLMServer,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    out = tmp_path / "report.json"
    status = main(
        [
            "summaries", "eval", "--runs", "2", "--output", str(out),
            "--case", "disk_full", "--case", "python_traceback",
        ]
    )  # fmt: skip

    captured = capsys.readouterr()
    assert status == 0
    assert "asking openai-compatible (local-model) 4 times" in captured.err
    assert "disk_full" in captured.out and "2/2" in captured.out
    assert "python_traceback" in captured.out and "0/2" in captured.out
    assert "failed (mentions 'disk')" in captured.out
    assert "overall 2/4" in captured.out
    assert len(model.requests) == 4
    report = json.loads(out.read_text())
    assert {s["name"]: s["passes"] for s in report["scenarios"]} == {
        "disk_full": 2,
        "python_traceback": 0,
    }


def test_the_command_can_report_to_a_tocsin_check(
    configured: None, tocsin: FakeTocsin, capsys: pytest.CaptureFixture[str]
) -> None:
    status = main(
        ["summaries", "eval", "--runs", "1", "--case", "disk_full",
         "--check", CHECK, "--server", tocsin.url]
    )  # fmt: skip

    assert status == 0
    assert tocsin.paths == [f"/ping/{CHECK}"]
    sent = json.loads(tocsin.requests[0][2])
    assert sent["suite"] == "summariser"
    assert sent["scenarios"] == [
        {
            "name": "disk_full",
            "runs": 1,
            "passes": 1,
            "latency_ms": sent["scenarios"][0]["latency_ms"],
        }
    ]


def test_the_command_fails_when_the_model_cannot_be_asked(
    configured: None, model: FakeLLMServer, capsys: pytest.CaptureFixture[str]
) -> None:
    model.status = 401

    status = main(["summaries", "eval", "--runs", "1", "--case", "disk_full"])

    assert status == 1
    assert "no answer: openai-compatible rejected the API key" in capsys.readouterr().out


def test_the_command_needs_a_provider(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TOCSIN_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("TOCSIN_LLM_PROVIDER", "")
    get_settings.cache_clear()
    try:
        status = main(["summaries", "eval"])
    finally:
        get_settings.cache_clear()

    assert status == 2
    assert "TOCSIN_LLM_PROVIDER" in capsys.readouterr().err


def test_an_unknown_case_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["summaries", "eval", "--case", "nope"]) == 2
    assert "no such case: nope" in capsys.readouterr().err
