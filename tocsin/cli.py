from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
from typing import Any

# The server commands import their dependencies inside the handler, so the
# base install (the job-side wrapper, with no dependencies) can still load
# this module and print help.

Handler = Callable[[argparse.Namespace], int]


def _migrate(args: argparse.Namespace) -> int:
    from tocsin.config import get_settings
    from tocsin.db import migrate

    migrate(get_settings().database_url)
    print("database is up to date")
    return 0


async def _with_session(work: Callable[[Any], Any]) -> Any:
    from tocsin.config import get_settings
    from tocsin.db import create_engine, create_sessionmaker

    engine = create_engine(get_settings().database_url)
    try:
        async with create_sessionmaker(engine)() as session:
            result = await work(session)
            await session.commit()
            return result
    finally:
        await engine.dispose()


def _keys_create(args: argparse.Namespace) -> int:
    from tocsin.apikeys import create_key

    _, plaintext = asyncio.run(_with_session(lambda session: create_key(session, args.name)))
    print(plaintext)
    print("Store this key now; it is not shown again.", file=sys.stderr)
    return 0


def _keys_list(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from tocsin.models import ApiKey

    async def work(session: Any) -> list[ApiKey]:
        return list(await session.scalars(select(ApiKey).order_by(ApiKey.created_at)))

    for key in asyncio.run(_with_session(work)):
        status = "revoked" if key.revoked_at else "active"
        used = key.last_used_at.isoformat(timespec="seconds") if key.last_used_at else "never"
        print(f"{key.prefix}…  {status:8}  last used {used:25}  {key.name}")
    return 0


def _keys_revoke(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from sqlalchemy import update

    from tocsin.models import ApiKey

    async def work(session: Any) -> int:
        result = await session.execute(
            update(ApiKey)
            .where(ApiKey.prefix == args.prefix[:10], ApiKey.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        return int(result.rowcount)

    revoked = asyncio.run(_with_session(work))
    print(f"revoked {revoked} key(s)")
    return 0 if revoked else 1


def _api(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "tocsin.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips=args.forwarded_allow_ips,
    )
    return 0


# The worker and scheduler are taskiq's own runners pointed at tocsin's broker;
# extra arguments (such as --workers 2) are passed through to taskiq.
def _taskiq(command: str, target: str) -> Handler:
    def run(args: argparse.Namespace) -> int:
        import os

        argv = [sys.executable, "-m", "taskiq", command, target, *args.taskiq_args]
        os.execv(sys.executable, argv)

    return run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tocsin", description="A job monitor that understands AI jobs."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    migrate = commands.add_parser("migrate", help="bring the database schema up to date")
    migrate.set_defaults(handler=_migrate)

    keys = commands.add_parser("keys", help="manage API keys").add_subparsers(
        dest="keys_command", required=True
    )
    create = keys.add_parser("create", help="create a key and print it once")
    create.add_argument("name", help="a label to tell keys apart, e.g. laptop")
    create.set_defaults(handler=_keys_create)
    keys.add_parser("list", help="list keys").set_defaults(handler=_keys_list)
    revoke = keys.add_parser("revoke", help="revoke a key by the prefix shown in the list")
    revoke.add_argument("prefix")
    revoke.set_defaults(handler=_keys_revoke)

    api = commands.add_parser("api", help="run the HTTP API")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)
    api.add_argument(
        "--forwarded-allow-ips",
        default="127.0.0.1",
        help="proxies trusted to set X-Forwarded-For (the ping path records the source IP)",
    )
    api.set_defaults(handler=_api)

    worker = commands.add_parser("worker", help="run a worker (alert delivery, sweeps)")
    worker.add_argument("taskiq_args", nargs=argparse.REMAINDER)
    worker.set_defaults(handler=_taskiq("worker", "tocsin.worker:broker"))

    scheduler = commands.add_parser("scheduler", help="run the scheduler for periodic tasks")
    scheduler.add_argument("taskiq_args", nargs=argparse.REMAINDER)
    scheduler.set_defaults(handler=_taskiq("scheduler", "tocsin.worker:scheduler"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler: Handler = args.handler
    return handler(args)
