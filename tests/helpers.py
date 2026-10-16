from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import httpx

from tocsin.channels import Alert
from tocsin.db import Sessionmaker
from tocsin.models import Channel, Check, Event
from tocsin.pings import Signal, record_ping
from tocsin.sweep import sweep


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


# Sends a ping as if it arrived at `at`, going through the same code as the
# HTTP endpoint, so tests can play out hours of pings in an instant.
async def ping_at(
    sessionmaker: Sessionmaker, check_id: str, signal: str | None, at: datetime, body: bytes = b""
) -> None:
    async with sessionmaker() as session, session.begin():
        check = await session.get_one(Check, check_id)
        await record_ping(session, check.ping_key, Signal.parse(signal), body, None, at, 10_000)


async def sweep_at(sessionmaker: Sessionmaker, at: datetime) -> list[Event]:
    async with sessionmaker() as session, session.begin():
        return await sweep(session, at)
