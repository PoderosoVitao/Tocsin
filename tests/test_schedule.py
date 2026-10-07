from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tocsin.schedule import ScheduleError, next_due, validate

NEW_YORK = "America/New_York"


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def test_cron_schedule_in_utc() -> None:
    assert next_due("0 3 * * *", "UTC", utc(2026, 5, 1, 3, 0, 5)) == utc(2026, 5, 2, 3)


def test_cron_schedule_is_read_in_the_checks_timezone() -> None:
    # 03:00 in Sao Paulo (UTC-3, no daylight saving) is 06:00 UTC.
    assert next_due("0 3 * * *", "America/Sao_Paulo", utc(2026, 5, 1, 7)) == utc(2026, 5, 2, 6)


def test_alias_schedule() -> None:
    assert next_due("@hourly", "UTC", utc(2026, 5, 1, 3, 10)) == utc(2026, 5, 1, 4)


def test_interval_schedule_counts_from_the_ping() -> None:
    assert next_due("@every 90m", "UTC", utc(2026, 5, 1, 3, 10)) == utc(2026, 5, 1, 4, 40)


# On 1 November 2026 New York falls back from EDT (UTC-4) to EST (UTC-5). A
# daily 03:00 job runs at 07:00 UTC the day before and 08:00 UTC that day;
# adding 24 hours would expect it an hour early and mark it late.
def test_fall_back_does_not_expect_a_daily_job_an_hour_early() -> None:
    ping = utc(2026, 10, 31, 7, 0, 30)
    assert next_due("0 3 * * *", NEW_YORK, ping) == utc(2026, 11, 1, 8)


# 01:30 happens twice that night. Cron runs a fixed-time job once, on the
# first pass, so the job must not be expected again an hour later.
def test_fall_back_does_not_expect_a_fixed_time_job_twice() -> None:
    first_pass = utc(2026, 11, 1, 5, 30, 10)
    assert next_due("30 1 * * *", NEW_YORK, first_pass) == utc(2026, 11, 2, 6, 30)


# Starting inside the repeated hour, cronsim returns the first pass of 01:30,
# which is already in the past; the next due time must still be in the future.
def test_next_due_from_inside_the_repeated_hour_is_in_the_future() -> None:
    inside_second_pass = utc(2026, 11, 1, 6, 15)
    assert next_due("30 1 * * *", NEW_YORK, inside_second_pass) == utc(2026, 11, 2, 6, 30)


# On 8 March 2026 New York springs forward from 02:00 straight to 03:00. Cron
# runs a 02:30 job right after the jump, at 03:00 EDT (07:00 UTC).
def test_spring_forward_expects_a_skipped_job_right_after_the_jump() -> None:
    ping = utc(2026, 3, 7, 7, 30, 10)
    assert next_due("30 2 * * *", NEW_YORK, ping) == utc(2026, 3, 8, 7)


def test_spring_forward_day_is_23_hours_long() -> None:
    ping = utc(2026, 3, 7, 8, 0, 30)
    assert next_due("0 3 * * *", NEW_YORK, ping) == utc(2026, 3, 8, 7)


@pytest.mark.parametrize("schedule", ["*/15 * * * *", "30 1 * * *", "0 2 * * *", "@hourly"])
@pytest.mark.parametrize("start", [utc(2026, 3, 8, 5), utc(2026, 11, 1, 4)])
def test_next_due_is_always_in_the_future_across_a_transition(
    schedule: str, start: datetime
) -> None:
    for minute in range(0, 5 * 60, 7):
        after = start + timedelta(minutes=minute)
        due = next_due(schedule, NEW_YORK, after)
        assert after < due <= after + timedelta(days=1, hours=1)


@pytest.mark.parametrize(
    ("schedule", "timezone", "message"),
    [
        ("0 3 * *", "UTC", "five fields"),
        ("0 3 * * * *", "UTC", "five fields"),
        ("61 * * * *", "UTC", "minute"),
        ("0 0 30 2 *", "UTC", "day-of-month"),
        ("0 3 * * *", "Mars/Olympus_Mons", "unknown timezone"),
        ("@every 30s", "UTC", "at least one minute"),
        ("@every soon", "UTC", "@every <number>"),
    ],
)
def test_invalid_schedules_are_rejected(schedule: str, timezone: str, message: str) -> None:
    with pytest.raises(ScheduleError, match=message):
        validate(schedule, timezone)


def test_valid_schedules_pass_validation() -> None:
    validate("0 3 * * 1-5", NEW_YORK)
    validate("@every 2h", "UTC")
    validate("@daily", "Europe/Lisbon")
