from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tocsin.api.app import create_app
from tocsin.apikeys import create_key
from tocsin.config import Settings
from tocsin.db import Sessionmaker, create_engine, create_sessionmaker, migrate
from tocsin.models import Base

ROOT = Path(__file__).resolve().parent.parent
LOCAL_SERVERS = ROOT / ".tocsin-test"


# The integration tests run against a real Postgres, because the parts worth
# testing (row locks, SKIP LOCKED, unique constraints) are Postgres behaviour.
# CI provides one through TOCSIN_TEST_DATABASE_URL; locally, the pgserver
# wheel starts a private one so no Docker or system install is needed.
@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("TOCSIN_TEST_DATABASE_URL")
    if url:
        return url

    import pgserver

    LOCAL_SERVERS.mkdir(exist_ok=True)
    server = pgserver.get_server(LOCAL_SERVERS / "pg", cleanup_mode="stop")
    server.psql("DROP DATABASE IF EXISTS tocsin_test WITH (FORCE);")
    server.psql("CREATE DATABASE tocsin_test;")
    return f"postgresql+asyncpg://postgres@/tocsin_test?host={server.pgdata}"


@pytest_asyncio.fixture(scope="session")
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    await asyncio.to_thread(migrate, database_url)
    engine = create_engine(database_url)
    yield engine
    await engine.dispose()


# Empties every table before the test, so each test starts from a blank
# database without paying for a migration per test.
@pytest_asyncio.fixture
async def sessionmaker(engine: AsyncEngine) -> Sessionmaker:
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return create_sessionmaker(engine)


@pytest.fixture
def settings() -> Settings:
    return Settings(public_url="https://tocsin.test")


@pytest_asyncio.fixture
async def api_key(sessionmaker: Sessionmaker) -> str:
    async with sessionmaker() as session, session.begin():
        _, plaintext = await create_key(session, "tests")
    return plaintext


@pytest_asyncio.fixture
async def client(
    settings: Settings, sessionmaker: Sessionmaker, api_key: str
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings, sessionmaker)
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(
        transport=transport, base_url="https://tocsin.test", headers=headers
    ) as client:
        yield client
