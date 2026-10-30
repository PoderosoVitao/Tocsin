from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select

from tocsin.api.deps import Session, require_api_key
from tocsin.api.schemas import KeyCreated, KeyIn, KeyOut
from tocsin.apikeys import create_key
from tocsin.models import ApiKey

router = APIRouter(prefix="/keys", tags=["keys"])

CurrentKey = Annotated[ApiKey, Depends(require_api_key)]


@router.get("")
async def list_keys(session: Session) -> list[KeyOut]:
    keys = await session.scalars(select(ApiKey).order_by(ApiKey.created_at.desc()))
    return [KeyOut.build(key) for key in keys]


# Creates a key. The response is the only time it is shown: only a hash is
# stored, so a lost key can't be recovered, only replaced.
@router.post("", status_code=status.HTTP_201_CREATED)
async def create(body: KeyIn, session: Session) -> KeyCreated:
    key, plaintext = await create_key(session, body.name)
    await session.commit()
    return KeyCreated(**KeyOut.build(key).model_dump(), key=plaintext)


# Revokes a key; it stops working at once. Revoking the key making the request
# is refused, so nobody can lock themselves out of the dashboard.
@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke(key_id: uuid.UUID, session: Session, current: CurrentKey) -> Response:
    key = await session.get(ApiKey, key_id)
    if key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such key")
    if key.id == current.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="this is the key you are using; sign in with another key to revoke it",
        )
    if key.revoked_at is None:
        key.revoked_at = datetime.now(UTC)
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
