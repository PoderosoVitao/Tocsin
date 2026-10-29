from __future__ import annotations

import random
from datetime import UTC, datetime

import httpx

from tests.conftest import RecordingDispatcher
from tests.helpers import FakeSender, add_channel, create_check
from tocsin.alerts import deliver
from tocsin.channels import PermanentError, TransientError
from tocsin.config import Settings
from tocsin.db import Sessionmaker

NOW = datetime(2026, 5, 1, 3, 0, tzinfo=UTC)


# Two checks fail, each alerting one channel: the first alert is sent, the
# second can never be (a blocked bot).
async def two_alerts(
    client: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    dispatcher: RecordingDispatcher,
    settings: Settings,
) -> None:
    await add_channel(sessionmaker, "phone")
    for name in ("backup", "sync"):
        check = await create_check(client, name=name)
        await client.get(f"{check['ping_path']}/fail")
    sender = FakeSender("msg-1", PermanentError("Telegram refused the message (403): blocked"))
    for key in dispatcher.keys:
        await deliver(sessionmaker, {"telegram": sender}, settings, key, NOW, random.Random(0))


async def test_the_alert_log_lists_every_delivery_newest_first(
    client: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    dispatcher: RecordingDispatcher,
    settings: Settings,
) -> None:
    await two_alerts(client, sessionmaker, dispatcher, settings)

    alerts = (await client.get("/api/alerts")).json()

    assert [a["check_name"] for a in alerts] == ["sync", "backup"]
    sync, backup = alerts
    assert (backup["status"], backup["attempts"], backup["channel_name"]) == ("sent", 1, "phone")
    assert backup["reason"] == "the job reported a failure"
    assert (backup["from_state"], backup["to_state"]) == ("new", "down")
    assert (sync["status"], sync["attempts"]) == ("failed", 1)
    assert sync["last_error"] == "Telegram refused the message (403): blocked"
    assert sync["check_id"] != backup["check_id"]


async def test_the_alert_log_can_filter_by_status(
    client: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    dispatcher: RecordingDispatcher,
    settings: Settings,
) -> None:
    await two_alerts(client, sessionmaker, dispatcher, settings)

    failed = (await client.get("/api/alerts", params={"status": "failed"})).json()
    sent = (await client.get("/api/alerts", params={"status": "sent"})).json()
    retrying = (await client.get("/api/alerts", params={"status": "retrying"})).json()

    assert [a["check_name"] for a in failed] == ["sync"]
    assert [a["check_name"] for a in sent] == ["backup"]
    assert retrying == []
    assert (await client.get("/api/alerts", params={"status": "bogus"})).status_code == 422


async def test_the_alert_log_pages_with_before(
    client: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    dispatcher: RecordingDispatcher,
    settings: Settings,
) -> None:
    await two_alerts(client, sessionmaker, dispatcher, settings)

    first = (await client.get("/api/alerts", params={"limit": 1})).json()
    second = (await client.get("/api/alerts", params={"limit": 1, "before": first[0]["id"]})).json()

    assert [a["check_name"] for a in first] == ["sync"]
    assert [a["check_name"] for a in second] == ["backup"]
    assert (await client.get("/api/alerts", params={"before": second[0]["id"]})).json() == []


async def test_a_retrying_alert_shows_its_next_attempt(
    client: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    dispatcher: RecordingDispatcher,
    settings: Settings,
) -> None:
    await add_channel(sessionmaker)
    check = await create_check(client)
    await client.get(f"{check['ping_path']}/fail")
    sender = FakeSender(TransientError("Telegram error 502: Bad Gateway"))
    await deliver(sessionmaker, {"telegram": sender}, settings, dispatcher.keys[0], NOW)

    [alert] = (await client.get("/api/alerts")).json()

    assert alert["status"] == "retrying"
    assert alert["last_error"] == "Telegram error 502: Bad Gateway"
    assert alert["next_attempt_at"] is not None


async def test_the_alert_log_needs_a_key(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/alerts", headers={"Authorization": ""})).status_code == 401
