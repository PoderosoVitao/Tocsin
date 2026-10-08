from __future__ import annotations

from datetime import UTC, datetime

import httpx
from sqlalchemy import select, update

from tests.helpers import add_channel
from tocsin.db import Sessionmaker
from tocsin.models import ApiKey, Check, Event

BACKUP = {"name": "backup", "schedule": "0 3 * * *", "timezone": "Europe/Lisbon"}


async def test_api_requires_a_key(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/checks", headers={"Authorization": ""})
    assert response.status_code == 401
    response = await client.get("/api/checks", headers={"Authorization": "Bearer tsn_wrong"})
    assert response.status_code == 401


async def test_api_accepts_the_key_in_x_api_key(client: httpx.AsyncClient, api_key: str) -> None:
    response = await client.get("/api/checks", headers={"Authorization": "", "X-Api-Key": api_key})
    assert response.status_code == 200


async def test_a_revoked_key_is_refused(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    async with sessionmaker() as session, session.begin():
        await session.execute(update(ApiKey).values(revoked_at=datetime.now(UTC)))
    assert (await client.get("/api/checks")).status_code == 401


async def test_create_check(client: httpx.AsyncClient, sessionmaker: Sessionmaker) -> None:
    response = await client.post("/api/checks", json=BACKUP)
    assert response.status_code == 201
    body = response.json()

    assert body["state"] == "new"
    assert body["kind"] == "job"
    assert body["grace_seconds"] == 300
    assert body["next_expected_at"] is None
    async with sessionmaker() as session:
        check = await session.scalar(select(Check))
    assert check is not None
    assert body["ping_url"] == f"https://tocsin.test/ping/{check.ping_key}"


async def test_a_new_check_alerts_every_channel_by_default(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    first = await add_channel(sessionmaker, "phone")
    second = await add_channel(sessionmaker, "team")

    everything = (await client.post("/api/checks", json=BACKUP)).json()
    assert sorted(everything["channel_ids"]) == sorted([first, second])

    only_one = (await client.post("/api/checks", json={**BACKUP, "channel_ids": [first]})).json()
    assert only_one["channel_ids"] == [first]


async def test_create_check_rejects_bad_input(client: httpx.AsyncClient) -> None:
    for bad in (
        {**BACKUP, "schedule": "0 3 * *"},
        {**BACKUP, "timezone": "Nowhere/Special"},
        {**BACKUP, "grace_seconds": -1},
        {**BACKUP, "channel_ids": ["00000000-0000-0000-0000-000000000000"]},
    ):
        assert (await client.post("/api/checks", json=bad)).status_code == 422


async def test_list_and_read_checks(client: httpx.AsyncClient) -> None:
    created = (await client.post("/api/checks", json=BACKUP)).json()
    await client.post("/api/checks", json={**BACKUP, "name": "archive"})

    listed = (await client.get("/api/checks")).json()
    assert [check["name"] for check in listed] == ["archive", "backup"]
    read = (await client.get(f"/api/checks/{created['id']}")).json()
    assert read == created
    missing = await client.get("/api/checks/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404


async def test_update_check(client: httpx.AsyncClient) -> None:
    created = (await client.post("/api/checks", json=BACKUP)).json()
    response = await client.patch(
        f"/api/checks/{created['id']}", json={"name": "nightly backup", "schedule": "@every 2h"}
    )
    assert response.status_code == 200
    assert response.json()["name"] == "nightly backup"
    assert response.json()["schedule"] == "@every 2h"

    bad = await client.patch(f"/api/checks/{created['id']}", json={"schedule": "never"})
    assert bad.status_code == 422


async def test_pause_and_resume_record_events(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    created = (await client.post("/api/checks", json=BACKUP)).json()
    async with sessionmaker() as session, session.begin():
        await session.execute(
            update(Check).values(state="up", last_ping_at=datetime(2026, 5, 1, tzinfo=UTC))
        )

    paused = (await client.patch(f"/api/checks/{created['id']}", json={"paused": True})).json()
    assert paused["state"] == "paused"
    assert paused["next_expected_at"] is None

    resumed = (await client.patch(f"/api/checks/{created['id']}", json={"paused": False})).json()
    assert resumed["state"] == "up"
    assert resumed["next_expected_at"] is not None

    async with sessionmaker() as session:
        events = list(await session.scalars(select(Event).order_by(Event.id)))
    assert [(e.from_state, e.to_state) for e in events] == [("up", "paused"), ("paused", "up")]


async def test_a_never_pinged_check_resumes_as_new(client: httpx.AsyncClient) -> None:
    created = (await client.post("/api/checks", json=BACKUP)).json()
    await client.patch(f"/api/checks/{created['id']}", json={"paused": True})
    resumed = (await client.patch(f"/api/checks/{created['id']}", json={"paused": False})).json()
    assert resumed["state"] == "new"
    assert resumed["next_expected_at"] is None


async def test_delete_check(client: httpx.AsyncClient) -> None:
    created = (await client.post("/api/checks", json=BACKUP)).json()
    assert (await client.delete(f"/api/checks/{created['id']}")).status_code == 204
    assert (await client.get(f"/api/checks/{created['id']}")).status_code == 404
