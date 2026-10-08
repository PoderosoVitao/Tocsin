from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TOCSIN_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://tocsin:tocsin@localhost:5432/tocsin"
    redis_url: str = "redis://localhost:6379/0"

    # The address jobs and people reach tocsin at. Used to build ping URLs and
    # the links inside alert messages.
    public_url: str = "http://localhost:8000"

    # How much of a job's output (the body of a finish ping) to keep per run.
    # The end of the output is kept, since that is where errors are.
    output_tail_bytes: int = 10_000

    # Pings accepted per check per minute. A job sending start and finish
    # pings every minute uses two.
    ping_rate_limit: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
