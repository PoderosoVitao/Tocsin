from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from tocsin.models import State


class DeliveryError(Exception):
    pass


# The channel may work if asked again later: a timeout, a 5xx, a rate limit.
# `retry_after` is the channel's own hint, in seconds, when it gives one.
class TransientError(DeliveryError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# Asking again won't help: a revoked token, a chat that doesn't exist. The
# delivery is marked failed at once instead of retrying for an hour.
class PermanentError(DeliveryError):
    pass


@dataclass(frozen=True)
class Alert:
    event_id: int
    check_id: uuid.UUID
    check_name: str
    from_state: str
    to_state: str
    reason: str
    occurred_at: datetime
    link: str | None = None
    output_excerpt: str | None = None

    @property
    def headline(self) -> str:
        if self.to_state == State.DOWN:
            return f"{self.check_name} is down"
        if self.to_state == State.UP:
            return f"{self.check_name} is up again"
        return f"{self.check_name} is {self.to_state}"


class Sender(Protocol):
    # Sends the alert and returns the id the channel gave the message, if any.
    async def send(self, config: dict[str, Any], alert: Alert) -> str | None: ...


# Checks a channel's config when it is created, so a typo shows up as a 422
# instead of as a failed delivery during an outage.
def validate_config(kind: str, config: dict[str, Any]) -> dict[str, Any]:
    from tocsin.channels import telegram

    validators = {"telegram": telegram.validate_config}
    if kind not in validators:
        raise ValueError(f"unknown channel kind: {kind}")
    return validators[kind](config)
