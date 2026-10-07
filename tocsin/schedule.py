from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cronsim import CronSim, CronSimError

MIN_INTERVAL = timedelta(minutes=1)

_ALIASES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

_INTERVAL = re.compile(r"^@every\s+(\d+)\s*([smhd])$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}

# A cron expression that never fires (such as 0 0 31 2 *) would make the
# search below loop forever, so it gives up after this many candidates.
_MAX_CANDIDATES = 1000


class ScheduleError(ValueError):
    pass


def _interval(schedule: str) -> timedelta | None:
    match = _INTERVAL.match(schedule.strip())
    if match is None:
        return None
    amount, unit = match.groups()
    return timedelta(**{_UNITS[unit]: int(amount)})


def _cron(schedule: str) -> str:
    expression = _ALIASES.get(schedule.strip().lower(), schedule.strip())
    # cronsim also takes a six-field form with seconds; tocsin sweeps every few
    # seconds, so only the standard five fields are accepted.
    if len(expression.split()) != 5:
        raise ScheduleError("a cron schedule needs five fields: minute hour day month weekday")
    return expression


def _zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError(f"unknown timezone: {timezone}") from exc


# Raises ScheduleError when the schedule or timezone can't be used. Called
# when a check is created or edited, so a bad schedule is rejected up front
# instead of failing later inside the sweep.
def validate(schedule: str, timezone: str) -> None:
    _zone(timezone)
    interval = _interval(schedule)
    if interval is not None:
        if interval < MIN_INTERVAL:
            raise ScheduleError("an @every interval must be at least one minute")
        return
    if schedule.strip().startswith("@every"):
        raise ScheduleError("write intervals as @every <number><s|m|h|d>, e.g. @every 90m")
    next_due(schedule, timezone, datetime.now(UTC))


# Returns the first time the job is due strictly after `after` (a UTC time).
#
# "@every 90m" schedules count from the last ping. Cron schedules are read in
# the check's own timezone, through cronsim, which follows Debian cron across
# daylight saving changes: a job in the skipped hour is due right after the
# jump, and a fixed-time job in the repeated hour is due once, on the first
# pass. One extra guard is needed: started from inside the repeated hour,
# cronsim can return the first pass, which is already in the past, so
# candidates are compared as absolute times and anything not after `after`
# is skipped.
def next_due(schedule: str, timezone: str, after: datetime) -> datetime:
    interval = _interval(schedule)
    if interval is not None:
        return after + interval

    zone = _zone(timezone)
    try:
        candidates = CronSim(_cron(schedule), after.astimezone(zone))
        for _, candidate in zip(range(_MAX_CANDIDATES), candidates, strict=False):
            if candidate > after:
                return candidate.astimezone(UTC)
    except CronSimError as exc:
        raise ScheduleError(str(exc)) from exc
    raise ScheduleError("this schedule never fires")
