from __future__ import annotations

import uuid
from datetime import datetime
from typing import Self

from pydantic import BaseModel, Field, model_validator

from tocsin.models import Check, CheckKind, State
from tocsin.schedule import ScheduleError, validate
from tocsin.transitions import displayed_state

MAX_GRACE_SECONDS = 7 * 24 * 3600


class CheckIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: CheckKind = CheckKind.JOB
    schedule: str = Field(min_length=1, max_length=200)
    timezone: str = "UTC"
    grace_seconds: int = Field(default=300, ge=0, le=MAX_GRACE_SECONDS)
    # The channels to alert. Left out, a new check alerts every channel, which
    # is what a single user with one Telegram chat wants.
    channel_ids: list[uuid.UUID] | None = None

    @model_validator(mode="after")
    def _schedule_is_valid(self) -> Self:
        try:
            validate(self.schedule, self.timezone)
        except ScheduleError as exc:
            raise ValueError(str(exc)) from exc
        return self


class CheckPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    schedule: str | None = Field(default=None, min_length=1, max_length=200)
    timezone: str | None = None
    grace_seconds: int | None = Field(default=None, ge=0, le=MAX_GRACE_SECONDS)
    channel_ids: list[uuid.UUID] | None = None
    paused: bool | None = None


class CheckOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: CheckKind
    schedule: str
    timezone: str
    grace_seconds: int
    state: State
    due_at: datetime | None
    next_expected_at: datetime | None
    last_ping_at: datetime | None
    ping_url: str
    channel_ids: list[uuid.UUID]
    created_at: datetime

    @classmethod
    def build(
        cls, check: Check, channel_ids: list[uuid.UUID], public_url: str, now: datetime
    ) -> CheckOut:
        return cls(
            id=check.id,
            name=check.name,
            kind=CheckKind(check.kind),
            schedule=check.schedule,
            timezone=check.timezone,
            grace_seconds=check.grace_seconds,
            state=displayed_state(check, now),
            due_at=check.due_at,
            next_expected_at=check.next_expected_at,
            last_ping_at=check.last_ping_at,
            ping_url=f"{public_url.rstrip('/')}/ping/{check.ping_key}",
            channel_ids=channel_ids,
            created_at=check.created_at,
        )
