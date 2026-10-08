from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select, update

from tests.helpers import create_check
from tocsin.db import Sessionmaker
from tocsin.models import Check, Event, Ping, Run
from tocsin.pings import output_tail


async def events(sessionmaker: Sessionmaker) -> list[tuple[str, str, str]]:
    async with sessionmaker() as session:
        rows = await session.scalars(select(Event).order_by(Event.id))
        return [(e.from_state, e.to_state, e.reason) for e in rows]


async def runs(sessionmaker: Sessionmaker) -> list[Run]:
    async with sessionmaker() as session:
        return list(await session.scalars(select(Run).order_by(Run.id)))


@pytest.mark.parametrize("method", ["GET", "POST", "HEAD"])
async def test_success_ping_brings_a_new_check_up(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker, method: str
) -> None:
    check = await create_check(client)
    response = await client.request(method, check["ping_path"], headers={"Authorization": ""})
    assert response.status_code == 200

    read = (await client.get(f"/api/checks/{check['id']}")).json()
    assert read["state"] == "up"
    assert read["last_ping_at"] is not None
    assert read["due_at"].endswith("03:00:00Z")
    assert await events(sessionmaker) == [("new", "up", "first ping")]
    [run] = await runs(sessionmaker)
    assert run.status == "success"


async def test_unknown_or_malformed_keys_are_not_found(client: httpx.AsyncClient) -> None:
    assert (await client.get("/ping/7d0c6c29-5a37-4bd3-9a43-3d3fa5d5d1a0")).status_code == 404
    assert (await client.get("/ping/not-a-uuid")).status_code == 404


async def test_fail_ping_takes_the_check_down(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check = await create_check(client)
    await client.get(check["ping_path"])
    assert (await client.get(f"{check['ping_path']}/fail")).status_code == 200
    # A second failure while down is not a new transition.
    await client.get(f"{check['ping_path']}/fail")

    assert (await events(sessionmaker))[1:] == [("up", "down", "the job reported a failure")]


async def test_exit_codes(client: httpx.AsyncClient, sessionmaker: Sessionmaker) -> None:
    check = await create_check(client)
    await client.get(f"{check['ping_path']}/0")
    await client.get(f"{check['ping_path']}/3")
    await client.get(f"{check['ping_path']}/0")

    assert await events(sessionmaker) == [
        ("new", "up", "first ping"),
        ("up", "down", "the job exited with code 3"),
        ("down", "up", "the job succeeded"),
    ]
    assert [(r.status, r.exit_code) for r in await runs(sessionmaker)] == [
        ("success", 0),
        ("fail", 3),
        ("success", 0),
    ]


@pytest.mark.parametrize("signal", ["256", "-1", "finish", "1.5"])
async def test_invalid_signals_are_rejected(client: httpx.AsyncClient, signal: str) -> None:
    check = await create_check(client)
    assert (await client.get(f"{check['ping_path']}/{signal}")).status_code == 400


async def test_post_body_is_stored_as_the_output_tail(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check = await create_check(client)
    await client.post(f"{check['ping_path']}/1", content=b"rsync: write failed: No space left")
    [run] = await runs(sessionmaker)
    assert run.output_tail == "rsync: write failed: No space left"


async def test_output_tail_keeps_the_end_of_a_long_body(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check = await create_check(client)
    body = b"x" * 50_000 + b"the error is at the end"
    await client.post(check["ping_path"], content=body)
    [run] = await runs(sessionmaker)
    assert run.output_tail is not None
    assert len(run.output_tail) == 10_000
    assert run.output_tail.endswith("the error is at the end")


def test_output_tail_survives_nul_bytes_and_split_characters() -> None:
    assert output_tail(b"", 10) is None
    assert output_tail(b"a\x00b", 10) == "a�b"
    # Cutting into the middle of a two-byte character must not raise.
    assert output_tail("é".encode() * 3, 5) == "�éé"


async def test_start_ping_is_recorded_without_changing_state(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check = await create_check(client)
    await client.get(f"{check['ping_path']}/start")
    read = (await client.get(f"/api/checks/{check['id']}")).json()
    assert read["state"] == "new"
    async with sessionmaker() as session:
        [ping] = await session.scalars(select(Ping))
    assert ping.kind == "start"
    assert ping.source_ip == "127.0.0.1"


async def test_a_ping_for_a_paused_check_does_not_change_its_state(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check = await create_check(client)
    await client.get(check["ping_path"])
    await client.patch(f"/api/checks/{check['id']}", json={"paused": True})

    assert (await client.get(f"{check['ping_path']}/fail")).status_code == 200
    read = (await client.get(f"/api/checks/{check['id']}")).json()
    assert read["state"] == "paused"
    assert [to for _, to, _ in await events(sessionmaker)] == ["up", "paused"]
    assert len(await runs(sessionmaker)) == 2


async def test_late_is_shown_between_due_time_and_deadline(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    check = await create_check(client, grace_seconds=3600)
    await client.get(check["ping_path"])
    async with sessionmaker() as session, session.begin():
        await session.execute(update(Check).values(due_at=datetime(2026, 1, 1, tzinfo=UTC)))
    read = (await client.get(f"/api/checks/{check['id']}")).json()
    assert read["state"] == "late"
