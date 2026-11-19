from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from taskiq import InMemoryBroker

from tests.conftest import RecordingDispatcher
from tests.helpers import FakeLLMServer, FakeSender, add_channel, create_check
from tocsin.channels import TransientError
from tocsin.config import Settings
from tocsin.db import Sessionmaker
from tocsin.events import CHANNEL
from tocsin.models import Delivery
from tocsin.worker import DELIVER, RELAY_DELIVERIES, SUMMARIZE, TaskiqDispatcher, register_tasks


# Runs the real task functions on an in-memory broker: the worker opens its
# own resources at startup, and a queued delivery reaches the channel.
async def test_queued_delivery_runs_on_a_worker(
    client: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    dispatcher: RecordingDispatcher,
    settings: Settings,
    database_url: str,
) -> None:
    await add_channel(sessionmaker)
    check = await create_check(client)
    await client.get(f"{check['ping_path']}/fail")

    settings.database_url = database_url
    broker = InMemoryBroker(await_inplace=True)
    register_tasks(broker, settings)
    assert {DELIVER, RELAY_DELIVERIES} <= set(broker.get_all_tasks())
    await broker.startup()
    sender = FakeSender()
    broker.state.resources.senders = {"telegram": sender}
    try:
        await TaskiqDispatcher(broker).deliver(dispatcher.keys)
    finally:
        await broker.shutdown()

    assert len(sender.sent) == 1
    async with sessionmaker() as session:
        [delivery] = await session.scalars(select(Delivery))
    assert delivery.status == "sent"


# A delivery changing happens in the worker, so it announces itself: the
# alert log and the check's page refresh when a retry fails or succeeds.
async def test_a_delivery_announces_that_it_changed(
    client: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    dispatcher: RecordingDispatcher,
    settings: Settings,
    database_url: str,
    redis_url: str,
    redis: Redis,
) -> None:
    await add_channel(sessionmaker)
    check = await create_check(client)
    await client.get(f"{check['ping_path']}/fail")

    settings.database_url = database_url
    settings.redis_url = redis_url
    broker = InMemoryBroker(await_inplace=True)
    register_tasks(broker, settings)
    await broker.startup()
    broker.state.resources.senders = {"telegram": FakeSender(TransientError("timeout"))}
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    await pubsub.subscribe(CHANNEL)
    try:
        await TaskiqDispatcher(broker).deliver(dispatcher.keys)
        async with asyncio.timeout(5):
            message = None
            while message is None:
                message = await pubsub.get_message(timeout=1.0)
    finally:
        await broker.shutdown()
        await pubsub.aclose()

    assert json.loads(message["data"]) == {"type": "alert.updated", "check_id": check["id"]}


# The summary task, run for real on an in-memory broker: the worker builds its
# model from the settings, asks it, and edits the alert that already went out.
async def test_a_queued_summary_reaches_the_alert_that_went_out_without_it(
    client: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    dispatcher: RecordingDispatcher,
    settings: Settings,
    database_url: str,
    redis_url: str,
) -> None:
    server = FakeLLMServer("Disk full on /mnt/backup")
    await add_channel(sessionmaker)
    check = await create_check(client, summary_enabled=True)
    await client.get(f"{check['ping_path']}/start")
    await client.post(f"{check['ping_path']}/1", content=b"No space left on device\n")

    settings.database_url = database_url
    settings.redis_url = redis_url
    settings.llm_provider = "openai-compatible"
    settings.llm_model = "m"
    settings.llm_base_url = server.url
    broker = InMemoryBroker(await_inplace=True)
    register_tasks(broker, settings)
    assert SUMMARIZE in broker.get_all_tasks()
    await broker.startup()
    sender = FakeSender()
    broker.state.resources.senders = {"telegram": sender}
    try:
        await TaskiqDispatcher(broker).deliver(dispatcher.keys)
        assert dispatcher.summaries
        await TaskiqDispatcher(broker).summarize(dispatcher.summaries)
    finally:
        await broker.shutdown()
        server.close()

    assert sender.sent[0].summary is None
    assert [alert.summary for _, alert in sender.edits] == ["Disk full on /mnt/backup"]
    [(path, _, body)] = server.requests
    assert path == "/chat/completions" and "No space left on device" in json.dumps(body)


async def test_a_worker_with_no_model_set_up_leaves_alerts_alone(
    client: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    dispatcher: RecordingDispatcher,
    settings: Settings,
    database_url: str,
    redis_url: str,
) -> None:
    await add_channel(sessionmaker)
    check = await create_check(client, summary_enabled=True)
    await client.post(f"{check['ping_path']}/1", content=b"boom\n")

    settings.database_url = database_url
    settings.redis_url = redis_url
    broker = InMemoryBroker(await_inplace=True)
    register_tasks(broker, settings)
    await broker.startup()
    sender = FakeSender()
    broker.state.resources.senders = {"telegram": sender}
    try:
        assert broker.state.resources.llm is None
        await TaskiqDispatcher(broker).deliver(dispatcher.keys)
        await TaskiqDispatcher(broker).summarize(dispatcher.summaries)
    finally:
        await broker.shutdown()
    assert len(sender.sent) == 1 and sender.edits == []


async def test_settings_that_cannot_work_turn_summaries_off_with_a_clear_log(
    settings: Settings,
    database_url: str,
    redis_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings.database_url = database_url
    settings.redis_url = redis_url
    settings.llm_provider = "openai"  # no key, no model
    broker = InMemoryBroker(await_inplace=True)
    register_tasks(broker, settings)
    await broker.startup()
    try:
        assert broker.state.resources.llm is None
    finally:
        await broker.shutdown()
    assert "failure summaries are off" in caplog.text
