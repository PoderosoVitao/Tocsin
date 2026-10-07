from __future__ import annotations

from typing import Any

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from tocsin.db import Sessionmaker
from tocsin.models import Base, Check, State


# Fails when a model changes without a migration that makes the same change,
# which would otherwise only show up as a broken deploy.
async def test_migrations_match_the_models(engine: AsyncEngine) -> None:
    def diff(connection: Connection) -> list[Any]:
        context = MigrationContext.configure(connection)
        return list(compare_metadata(context, Base.metadata))

    async with engine.connect() as conn:
        assert await conn.run_sync(diff) == []


async def test_a_new_check_gets_its_defaults(sessionmaker: Sessionmaker) -> None:
    async with sessionmaker() as session, session.begin():
        session.add(Check(name="backup", schedule="0 3 * * *"))

    async with sessionmaker() as session:
        check = (await session.execute(select(Check))).scalar_one()

    assert check.state == State.NEW
    assert check.timezone == "UTC"
    assert check.grace_seconds == 300
    assert check.ping_key != check.id
    assert check.next_expected_at is None
