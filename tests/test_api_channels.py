from __future__ import annotations

import httpx

from tests.helpers import FakeSender, create_check
from tocsin.channels import PermanentError

TOKEN = "123456789:AAE-secret-part-of-the-token"
PHONE = {"kind": "telegram", "name": "phone", "config": {"bot_token": TOKEN, "chat_id": 42}}


async def test_create_channel_masks_the_token(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/channels", json=PHONE)
    assert response.status_code == 201
    body = response.json()
    assert body["config"] == {"bot_token": "1234…", "chat_id": "42"}
    assert TOKEN not in response.text
    assert TOKEN not in (await client.get("/api/channels")).text


async def test_a_new_channel_is_attached_to_existing_checks(client: httpx.AsyncClient) -> None:
    check = await create_check(client)
    channel = (await client.post("/api/channels", json=PHONE)).json()
    detached = (
        await client.post("/api/channels", json={**PHONE, "attach_to_existing_checks": False})
    ).json()

    read = (await client.get(f"/api/checks/{check['id']}")).json()
    assert read["channel_ids"] == [channel["id"]]
    assert detached["id"] not in read["channel_ids"]


async def test_create_channel_rejects_bad_config(client: httpx.AsyncClient) -> None:
    bad_token = {**PHONE, "config": {"bot_token": "nope", "chat_id": 1}}
    assert (await client.post("/api/channels", json=bad_token)).status_code == 422
    unknown = {**PHONE, "kind": "pigeon"}
    assert (await client.post("/api/channels", json=unknown)).status_code == 422


async def test_delete_channel_detaches_it(client: httpx.AsyncClient) -> None:
    channel = (await client.post("/api/channels", json=PHONE)).json()
    check = await create_check(client)
    assert (await client.delete(f"/api/channels/{channel['id']}")).status_code == 204
    assert (await client.get("/api/channels")).json() == []
    assert (await client.get(f"/api/checks/{check['id']}")).json()["channel_ids"] == []


async def test_send_a_test_message(client: httpx.AsyncClient, sender: FakeSender) -> None:
    channel = (await client.post("/api/channels", json=PHONE)).json()
    response = await client.post(f"/api/channels/{channel['id']}/test")
    assert response.json() == {"status": "sent"}
    [alert] = sender.sent
    assert "test message" in alert.reason


async def test_a_failing_test_message_reports_why(
    client: httpx.AsyncClient, sender: FakeSender
) -> None:
    sender.outcomes.append(PermanentError("Telegram refused the message (400): chat not found"))
    channel = (await client.post("/api/channels", json=PHONE)).json()
    response = await client.post(f"/api/channels/{channel['id']}/test")
    assert response.status_code == 502
    assert "chat not found" in response.json()["detail"]
