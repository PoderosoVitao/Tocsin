from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.helpers import FakeSender
from tocsin.alerts import DeliveryKey
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

    try:
        import pgserver
    except ImportError:
        pytest.fail("set TOCSIN_TEST_DATABASE_URL (pgserver is only installed on Python 3.12)")

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


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


# Same idea as database_url: TOCSIN_TEST_REDIS_URL in CI, otherwise the
# redis-server binary bundled in the redislite wheel.
@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    url = os.environ.get("TOCSIN_TEST_REDIS_URL")
    if url:
        yield url
        return

    try:
        import redislite
    except ImportError:
        pytest.fail("set TOCSIN_TEST_REDIS_URL (redislite is only installed on Python 3.12)")

    port = _free_port()
    server = subprocess.Popen(
        [redislite.__redis_executable__, "--port", str(port), "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL,
    )
    url = f"redis://127.0.0.1:{port}/0"
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
            break
        except OSError:
            server.poll()
            if server.returncode is not None:
                raise RuntimeError("redis-server exited during startup") from None
            time.sleep(0.05)
    yield url
    server.terminate()
    server.wait()


@pytest_asyncio.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url)
    await client.flushdb()
    yield client
    await client.aclose()


# Stands in for the task queue: remembers what would have been queued, so a
# test can run the deliveries itself and look at every step.
class RecordingDispatcher:
    def __init__(self) -> None:
        self.keys: list[DeliveryKey] = []

    async def deliver(self, keys: Sequence[DeliveryKey]) -> None:
        self.keys.extend(keys)


@pytest.fixture
def dispatcher() -> RecordingDispatcher:
    return RecordingDispatcher()


@pytest.fixture
def sender() -> FakeSender:
    return FakeSender()


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
    settings: Settings,
    sessionmaker: Sessionmaker,
    redis: Redis,
    dispatcher: RecordingDispatcher,
    sender: FakeSender,
    api_key: str,
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings, sessionmaker, redis, dispatcher, {"telegram": sender})
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(
        transport=transport, base_url="https://tocsin.test", headers=headers
    ) as client:
        yield client
