from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from tests.helpers import create_check, ping_at
from tocsin.db import Sessionmaker

T0 = datetime(2026, 5, 1, 3, 0, tzinfo=UTC)


async def test_the_dashboard_page_and_assets_are_served_without_a_key(
    client: httpx.AsyncClient,
) -> None:
    anonymous = {"Authorization": ""}
    page = await client.get("/", headers=anonymous)
    assert page.status_code == 200
    assert "<title>Tocsin</title>" in page.text
    assert page.headers["cache-control"] == "no-cache"

    # Every asset the page loads exists: the two it names, and the module
    # the script imports.
    script = (await client.get("/static/app.js", headers=anonymous)).text
    assert "/static/app.js" in page.text and "/static/style.css" in page.text
    assert "/static/logic.js" in script
    for path in ("/static/app.js", "/static/logic.js", "/static/style.css"):
        response = await client.get(path, headers=anonymous)
        assert response.status_code == 200, path
        assert response.headers["cache-control"] == "no-cache"


async def test_the_overview_needs_an_api_key(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/overview", headers={"Authorization": ""})
    assert response.status_code == 401


async def test_the_overview_lists_every_check_with_its_recent_runs_oldest_first(
    client: httpx.AsyncClient, sessionmaker: Sessionmaker
) -> None:
    busy = await create_check(client, name="busy")
    await create_check(client, name="idle")
    for i in range(25):
        await ping_at(sessionmaker, busy["id"], "start", T0 + timedelta(minutes=i))
        await ping_at(
            sessionmaker, busy["id"], "1" if i == 24 else "0", T0 + timedelta(minutes=i, seconds=30)
        )

    items = {item["name"]: item for item in (await client.get("/api/overview")).json()}

    assert list(items) == ["busy", "idle"]
    assert items["idle"]["recent_runs"] == []
    runs = items["busy"]["recent_runs"]
    assert len(runs) == 20
    assert [run["id"] for run in runs] == sorted(run["id"] for run in runs)
    assert runs[-1]["status"] == "fail" and runs[-1]["exit_code"] == 1
    assert runs[0]["duration_ms"] == 30_000
    assert "output_tail" not in runs[0]
    # It carries everything the checks list does.
    assert items["busy"]["ping_url"].startswith("https://tocsin.test/ping/")
    assert items["busy"]["state"] == "down"
