from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

Sessionmaker = async_sessionmaker[AsyncSession]


def create_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


# expire_on_commit=False keeps loaded rows readable after a commit, which the
# request handlers and tasks rely on when they build a response or enqueue
# work from objects they just saved.
def create_sessionmaker(engine: AsyncEngine) -> Sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


# Builds an Alembic config that works from an installed package, without an
# alembic.ini on disk. The URL travels as an attribute rather than a config
# option because ConfigParser would try to interpolate any "%" in a password.
def alembic_config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", "tocsin:migrations")
    config.attributes["url"] = url
    return config


# Brings the schema up to date. Alembic's async env calls asyncio.run, so this
# must be called from synchronous code (or a worker thread), never from inside
# a running event loop.
def migrate(url: str) -> None:
    command.upgrade(alembic_config(url), "head")
