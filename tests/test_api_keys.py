from __future__ import annotations

import httpx

from tocsin.apikeys import hash_key


async def test_a_created_key_is_shown_once_and_works(client: httpx.AsyncClient) -> None:
    created = await client.post("/api/keys", json={"name": "ci runner"})
    assert created.status_code == 201
    body = created.json()
    assert body["key"].startswith("tsn_")
    assert body["prefix"] == body["key"][:10]
    assert body["revoked_at"] is None

    # The new key authenticates.
    me = await client.get("/api/checks", headers={"Authorization": f"Bearer {body['key']}"})
    assert me.status_code == 200

    # Listing never shows the key, its hash, or anything it could be rebuilt from.
    listing = await client.get("/api/keys")
    assert body["key"] not in listing.text
    assert hash_key(body["key"]) not in listing.text
    assert "key" not in listing.json()[0]
    assert {k["name"] for k in listing.json()} == {"tests", "ci runner"}


async def test_listing_shows_when_a_key_was_last_used(client: httpx.AsyncClient) -> None:
    keys = (await client.get("/api/keys")).json()
    [mine] = keys
    assert mine["name"] == "tests"
    assert mine["last_used_at"] is not None


async def test_a_revoked_key_stops_working_at_once(client: httpx.AsyncClient) -> None:
    created = (await client.post("/api/keys", json={"name": "laptop"})).json()
    headers = {"Authorization": f"Bearer {created['key']}"}
    assert (await client.get("/api/checks", headers=headers)).status_code == 200

    assert (await client.delete(f"/api/keys/{created['id']}")).status_code == 204
    assert (await client.get("/api/checks", headers=headers)).status_code == 401
    revoked = [k for k in (await client.get("/api/keys")).json() if k["id"] == created["id"]]
    assert revoked[0]["revoked_at"] is not None
    # Revoking again is harmless.
    assert (await client.delete(f"/api/keys/{created['id']}")).status_code == 204


async def test_the_key_in_use_cannot_be_revoked(client: httpx.AsyncClient) -> None:
    [mine] = (await client.get("/api/keys")).json()
    response = await client.delete(f"/api/keys/{mine['id']}")
    assert response.status_code == 409
    assert "key you are using" in response.json()["detail"]
    assert (await client.get("/api/checks")).status_code == 200


async def test_key_endpoints_validate_and_need_a_key(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/keys", json={"name": ""})).status_code == 422
    missing = "/api/keys/00000000-0000-0000-0000-000000000000"
    assert (await client.delete(missing)).status_code == 404
    anonymous = {"Authorization": ""}
    assert (await client.get("/api/keys", headers=anonymous)).status_code == 401
    assert (
        await client.post("/api/keys", json={"name": "x"}, headers=anonymous)
    ).status_code == 401
