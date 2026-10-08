from __future__ import annotations

import asyncio
from typing import Any

import httpx

from tocsin.channels import Alert
from tocsin.db import Sessionmaker
from tocsin.models import Channel


async def add_channel(sessionmaker: Sessionmaker, name: str = "phone") -> str:
    async with sessionmaker() as session, session.begin():
        channel = Channel(kind="telegram", name=name, config={"bot_token": "1:x", "chat_id": "1"})
        session.add(channel)
        await session.flush()
        return str(channel.id)


async def create_check(client: httpx.AsyncClient, **fields: Any) -> dict[str, Any]:
    body = {"name": "backup", "schedule": "0 3 * * *", **fields}
    response = await client.post("/api/checks", json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    created["ping_path"] = created["ping_url"].removeprefix("https://tocsin.test")
    return created


# A channel that records what it was asked to send. Each call takes the next
# entry of `outcomes`: an exception to raise, or a message id to return.
class FakeSender:
    def __init__(self, *outcomes: Exception | str, delay: float = 0) -> None:
        self.outcomes = list(outcomes)
        self.delay = delay
        self.calls: list[Alert] = []
        self.sent: list[Alert] = []

    async def send(self, config: dict[str, Any], alert: Alert) -> str | None:
        self.calls.append(alert)
        if self.delay:
            await asyncio.sleep(self.delay)
        outcome = self.outcomes.pop(0) if self.outcomes else str(len(self.calls))
        if isinstance(outcome, Exception):
            raise outcome
        self.sent.append(alert)
        return outcome
