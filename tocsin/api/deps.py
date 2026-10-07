from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from tocsin.apikeys import authenticate
from tocsin.config import Settings
from tocsin.models import ApiKey


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def _presented_key(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token:
        return token.strip()
    return request.headers.get("x-api-key", "").strip()


async def require_api_key(request: Request, session: Session) -> ApiKey:
    key = await authenticate(session, _presented_key(request))
    if key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="a valid API key is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return key
