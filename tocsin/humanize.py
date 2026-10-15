from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


# Formats a time for an alert, in the check's own timezone, since that is how
# its schedule is written.
def local_time(moment: datetime | None, timezone: str) -> str:
    if moment is None:
        return "never"
    return moment.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M %Z")


# Formats a duration to the two largest units that matter: "45s", "31m",
# "2h 5m", "3d 4h".
def duration(span: timedelta | float) -> str:
    seconds = int(span.total_seconds() if isinstance(span, timedelta) else span)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = [(days, "d"), (hours, "h"), (minutes, "m"), (seconds, "s")]
    shown = [f"{value}{unit}" for value, unit in parts if value][:2]
    return " ".join(shown) or "0s"
