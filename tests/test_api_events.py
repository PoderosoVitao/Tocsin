from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import httpx

from tests.conftest import RecordingDispatcher
from tests.helpers import FakeSender, add_channel, create_check, ping_at
from tocsin.alerts import deliver
from tocsin.channels import TransientError
from tocsin.config import Settings
from tocsin.db import Sessionmaker

T0 = datetime(2026, 5, 1, 3, 0, tzinfo=UTC)


async def test_events_are_listed_newest_first_with_their_reason(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check = await create_check(client, cooldown_seconds=0)
    await ping_at(sessionmaker, check["id"], None, T0)
    await ping_at(sessionmaker, check["id"], "2", T0 + timedelta(hours=1))

    events = (await client.get(f"/api/checks/{check['id']}/events")).json()

    assert [(e["from_state"], e["to_state"]) for e in events] == [("up", "down"), ("new", "up")]
    assert events[0]["reason"] == "the job exited with code 2"
    assert events[0]["run_id"] is not None
    assert events[1]["deliveries"] == []
    assert not any(e["suppressed"] for e in events)


# One failure alerted to two channels: Telegram took it for one and was down
# for the other, so the second is waiting for a retry.
async def test_events_show_each_delivery_with_its_attempts_and_next_retry(
    client: httpx.AsyncClient,
    sessionmaker: Sessionmaker,
    dispatcher: RecordingDispatcher,
    settings: Settings,
) -> None:
    await add_channel(sessionmaker, "phone")
    await add_channel(sessionmaker, "team")
    check = await create_check(client)
    await client.get(f"{check['ping_path']}/fail")
    assert len(dispatcher.keys) == 2
    sender = FakeSender("msg-1", TransientError("Telegram error 502: Bad Gateway"))
    for key in dispatcher.keys:
        await deliver(sessionmaker, {"telegram": sender}, settings, key, T0, random.Random(0))

    [event] = (await client.get(f"/api/checks/{check['id']}/events")).json()

    by_status = {d["status"]: d for d in event["deliveries"]}
    assert sorted(by_status) == ["retrying", "sent"]
    assert by_status["sent"]["attempts"] == 1
    assert by_status["sent"]["sent_at"] is not None
    assert by_status["sent"]["last_error"] is None
    assert by_status["retrying"]["attempts"] == 1
    assert by_status["retrying"]["last_error"] == "Telegram error 502: Bad Gateway"
    assert by_status["retrying"]["next_attempt_at"] is not None
    assert {d["channel_name"] for d in event["deliveries"]} == {"phone", "team"}


async def test_events_inside_a_cooldown_are_marked_suppressed(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    await add_channel(sessionmaker)
    check = await create_check(client, cooldown_seconds=600)
    await ping_at(sessionmaker, check["id"], "fail", T0)
    await ping_at(sessionmaker, check["id"], None, T0 + timedelta(minutes=1))

    events = (await client.get(f"/api/checks/{check['id']}/events")).json()

    assert [(e["to_state"], e["suppressed"], len(e["deliveries"])) for e in events] == [
        ("up", True, 0),
        ("down", False, 1),
    ]


async def test_events_page_with_before(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check = await create_check(client, cooldown_seconds=0)
    for minute in range(4):
        await ping_at(
            sessionmaker,
            check["id"],
            "fail" if minute % 2 else None,
            T0 + timedelta(minutes=minute),
        )

    url = f"/api/checks/{check['id']}/events"
    first = (await client.get(url, params={"limit": 2})).json()
    second = (await client.get(url, params={"before": first[-1]["id"], "limit": 2})).json()

    assert len(first) == 2 and len(second) == 2
    assert first[-1]["id"] > second[0]["id"]
    assert [e["id"] for e in first + second] == sorted(
        (e["id"] for e in first + second), reverse=True
    )


async def test_events_of_one_check_do_not_include_another(client: httpx.AsyncClient) -> None:
    other = await create_check(client, name="other")
    await client.get(other["ping_path"])
    mine = await create_check(client, name="mine")
    assert (await client.get(f"/api/checks/{mine['id']}/events")).json() == []


async def test_events_for_an_unknown_check_or_without_a_key(client: httpx.AsyncClient) -> None:
    unknown = "/api/checks/00000000-0000-0000-0000-000000000000/events"
    assert (await client.get(unknown)).status_code == 404
    assert (await client.get(unknown, headers={"Authorization": ""})).status_code == 401
