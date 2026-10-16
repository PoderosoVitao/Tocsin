from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select

from tests.helpers import add_channel, create_check, ping_at, sweep_at
from tocsin.channels import Alert
from tocsin.db import Sessionmaker
from tocsin.models import Delivery, Event
from tocsin.sweep import sweep

T0 = datetime(2026, 5, 1, 3, 0, tzinfo=UTC)


def minutes(n: float) -> datetime:
    return T0 + timedelta(minutes=n)


async def alerts_sent(sessionmaker: Sessionmaker) -> int:
    async with sessionmaker() as session:
        return await session.scalar(select(func.count()).select_from(Delivery)) or 0


async def events(sessionmaker: Sessionmaker) -> list[Event]:
    async with sessionmaker() as session:
        return list(await session.scalars(select(Event).order_by(Event.id)))


# A check that was up, failed at T0 (alerted), then flapped inside its
# ten-minute cooldown, ending up.
async def flapping_check(client: httpx.AsyncClient, sessionmaker: Sessionmaker) -> str:
    await add_channel(sessionmaker)
    check = await create_check(client, schedule="@hourly", cooldown_seconds=600)
    await ping_at(sessionmaker, check["id"], None, minutes(-60))
    await ping_at(sessionmaker, check["id"], "fail", minutes(0))
    await ping_at(sessionmaker, check["id"], None, minutes(1))
    await ping_at(sessionmaker, check["id"], "fail", minutes(2))
    await ping_at(sessionmaker, check["id"], None, minutes(3))
    return str(check["id"])


async def test_changes_inside_the_cooldown_are_recorded_but_not_sent(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    await flapping_check(client, sessionmaker)

    assert [(e.to_state, e.suppressed) for e in await events(sessionmaker)] == [
        ("up", False),
        ("down", False),
        ("up", True),
        ("down", True),
        ("up", True),
    ]
    assert await alerts_sent(sessionmaker) == 1


async def test_one_alert_reports_where_a_flapping_check_settled(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    await flapping_check(client, sessionmaker)

    assert await sweep_at(sessionmaker, minutes(9)) == []
    [event] = await sweep_at(sessionmaker, minutes(10))
    assert (event.from_state, event.to_state) == ("down", "up")
    assert event.reason == (
        "the job succeeded (it changed state 3 times during the 10m cooldown after the last alert)"
    )
    assert await alerts_sent(sessionmaker) == 2
    assert await sweep_at(sessionmaker, minutes(30)) == []


async def test_no_catch_up_when_it_settles_where_the_last_alert_left_it(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check_id = await flapping_check(client, sessionmaker)
    await ping_at(sessionmaker, check_id, "fail", minutes(4))

    assert await sweep_at(sessionmaker, minutes(11)) == []
    assert await alerts_sent(sessionmaker) == 1


async def test_a_change_after_the_cooldown_alerts_at_once(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    await add_channel(sessionmaker)
    check = await create_check(client, schedule="@hourly", cooldown_seconds=600)
    await ping_at(sessionmaker, check["id"], "fail", minutes(0))
    await ping_at(sessionmaker, check["id"], None, minutes(11))

    assert [e.suppressed for e in await events(sessionmaker)] == [False, False]
    assert await alerts_sent(sessionmaker) == 2


# Two sweeps at once after a flap: the first holds the row, the second skips
# it, and once the first commits the check no longer matches.
async def test_concurrent_sweeps_send_one_catch_up(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    await flapping_check(client, sessionmaker)

    async with sessionmaker() as first, first.begin():
        assert len(await sweep(first, minutes(10))) == 1
        async with sessionmaker() as second, second.begin():
            assert await sweep(second, minutes(10)) == []
    assert await sweep_at(sessionmaker, minutes(10)) == []
    assert await alerts_sent(sessionmaker) == 2


async def test_pausing_forgets_the_last_alert(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check_id = await flapping_check(client, sessionmaker)
    await client.patch(f"/api/checks/{check_id}", json={"paused": True})
    await client.patch(f"/api/checks/{check_id}", json={"paused": False})

    assert await sweep_at(sessionmaker, minutes(15)) == []


async def test_reminders_repeat_while_the_check_stays_down(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    await add_channel(sessionmaker)
    check = await create_check(client, schedule="0 3 * * *", reminder_seconds=3600)
    await ping_at(sessionmaker, check["id"], "fail", minutes(0))

    assert await sweep_at(sessionmaker, minutes(59)) == []
    [reminder] = await sweep_at(sessionmaker, minutes(61))
    assert (reminder.from_state, reminder.to_state) == ("down", "down")
    assert reminder.reason == "down since 2026-05-01 03:00 UTC: the job reported a failure"
    assert await sweep_at(sessionmaker, minutes(62)) == []
    assert len(await sweep_at(sessionmaker, minutes(122))) == 1

    await ping_at(sessionmaker, check["id"], None, minutes(130))
    assert await sweep_at(sessionmaker, minutes(300)) == []
    assert await alerts_sent(sessionmaker) == 4


async def test_reminders_are_off_unless_asked_for(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    await add_channel(sessionmaker)
    check = await create_check(client, schedule="0 3 * * *")
    await ping_at(sessionmaker, check["id"], "fail", minutes(0))

    assert await sweep_at(sessionmaker, minutes(24 * 60)) == []


async def test_cooldown_and_reminders_through_the_api(client: httpx.AsyncClient) -> None:
    check = await create_check(client)
    assert (check["cooldown_seconds"], check["reminder_seconds"]) == (300, None)

    updated = await client.patch(
        f"/api/checks/{check['id']}", json={"cooldown_seconds": 0, "reminder_seconds": 7200}
    )
    assert (updated.json()["cooldown_seconds"], updated.json()["reminder_seconds"]) == (0, 7200)
    off = await client.patch(f"/api/checks/{check['id']}", json={"reminder_seconds": None})
    assert off.json()["reminder_seconds"] is None
    too_often = await client.patch(f"/api/checks/{check['id']}", json={"reminder_seconds": 30})
    assert too_often.status_code == 422


def test_a_reminder_reads_as_still_down() -> None:
    reminder = Alert(1, uuid.uuid4(), "backup", "down", "down", "down since …", T0)
    assert reminder.headline == "backup is still down"
