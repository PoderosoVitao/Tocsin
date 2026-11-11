from __future__ import annotations

import argparse
import asyncio
import json
import os
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
        argv = [sys.executable, "-m", "taskiq", command, target, *args.taskiq_args]
        os.execv(sys.executable, argv)

    return run


def _run(args: argparse.Namespace) -> int:
    from tocsin.wrapper import run

    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        print("tocsin run: give the command to run after --", file=sys.stderr)
        return 2
    if not args.check:
        print("tocsin run: --check (or TOCSIN_CHECK) is required", file=sys.stderr)
        return 2
    return run(command, _ping_url(args), tail_lines=args.tail_lines, timeout=args.timeout)


def _ping_url(args: argparse.Namespace) -> str:
    if args.check.startswith(("http://", "https://")):
        return str(args.check)
    return f"{args.server.rstrip('/')}/ping/{args.check}"


def _read_inputs(paths: Sequence[str]) -> list[bytes]:
    limit = 20_000_000
    documents = []
    for path in paths:
        if path == "-":
            data = sys.stdin.buffer.read(limit + 1)
        else:
            with open(path, "rb") as handle:
                data = handle.read(limit + 1)
        if len(data) > limit:
            raise ValueError(f"{path} is larger than {limit // 1_000_000} MB")
        documents.append(data)
    return documents


# Converts a test tool's results to a tocsin.eval/v1 report and sends them as
# the check's success ping. When the results can't be read, the check is told
# the job failed, with the reason, so the problem is an alert and not a silence.
def _report(args: argparse.Namespace) -> int:
    from tocsin.adapters import AdapterError
    from tocsin.adapters.junit import junit_report
    from tocsin.adapters.send import send_failure, send_report

    if not args.check and not args.dry_run:
        print("tocsin report: --check (or TOCSIN_CHECK) is required", file=sys.stderr)
        return 2
    labels = {}
    for item in args.label:
        name, separator, value = item.partition("=")
        if not separator or not name:
            print(f"tocsin report: --label wants name=value, not {item!r}", file=sys.stderr)
            return 2
        labels[name] = value
    try:
        documents = _read_inputs(args.files)
    except (OSError, ValueError) as exc:
        print(f"tocsin report: {exc}", file=sys.stderr)
        return 2

    try:
        report = junit_report(
            documents,
            suite=args.suite,
            labels=labels,
            report_url=args.report_url,
            group_by=args.group_by,
        )
    except AdapterError as exc:
        print(f"tocsin report: {exc}", file=sys.stderr)
        if not args.dry_run:
            send_failure(_ping_url(args), f"could not read the test results: {exc}", args.timeout)
        return 1

    if args.dry_run:
        print(json.dumps(report, indent=2))
        return 0
    sent = send_report(_ping_url(args), report, args.timeout)
    if sent.result == "accepted":
        runs = sum(scenario["runs"] for scenario in report["scenarios"])
        print(f"tocsin: reported {runs} runs in {len(report['scenarios'])} scenarios")
        return 0
    if sent.result == "rejected":
        print(f"tocsin report: tocsin refused the report ({sent.message})", file=sys.stderr)
    else:
        print(f"tocsin report: could not reach tocsin ({sent.message})", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tocsin", description="A job monitor that understands AI jobs."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    check_help = "the check's ping uuid, or its full ping URL (default: $TOCSIN_CHECK)"
    server_help = "tocsin's address, used with a bare uuid (default: $TOCSIN_URL or localhost:8000)"

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

    run = commands.add_parser(
        "run",
        help="run a command and report its start, exit code and output",
        usage="tocsin run --check <uuid or ping URL> [options] -- command [args...]",
    )
    run.add_argument("--check", default=os.environ.get("TOCSIN_CHECK"), help=check_help)
    run.add_argument(
        "--server", default=os.environ.get("TOCSIN_URL", "http://localhost:8000"), help=server_help
    )
    run.add_argument(
        "--tail-lines", type=int, default=100, help="output lines to send (default: 100)"
    )
    run.add_argument(
        "--timeout", type=float, default=10, help="seconds to wait for each ping (default: 10)"
    )
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(handler=_run)

    report = commands.add_parser(
        "report", help="send an evaluation report converted from another tool's results"
    ).add_subparsers(dest="report_format", required=True)
    junit = report.add_parser(
        "junit",
        help="send JUnit XML test results (pytest --junitxml, Maven Surefire, Jest, ...)",
        usage="tocsin report junit --check <uuid or ping URL> [options] results.xml [...]",
    )
    junit.add_argument(
        "files", nargs="+", metavar="results.xml", help="JUnit XML files, or - for stdin"
    )
    junit.add_argument("--check", default=os.environ.get("TOCSIN_CHECK"), help=check_help)
    junit.add_argument(
        "--server", default=os.environ.get("TOCSIN_URL", "http://localhost:8000"), help=server_help
    )
    junit.add_argument("--suite", help="the report's suite name (default: the suite's own name)")
    junit.add_argument(
        "--label",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="what was evaluated, e.g. --label model=claude-sonnet-5-5 (repeatable)",
    )
    junit.add_argument("--report-url", help="a link to the full report")
    junit.add_argument(
        "--group-by",
        choices=["test", "class"],
        default="test",
        help="a scenario per distinct test (default) or per class, for big suites",
    )
    junit.add_argument("--timeout", type=float, default=10, help="seconds to wait (default: 10)")
    junit.add_argument(
        "--dry-run", action="store_true", help="print the report instead of sending it"
    )
    junit.set_defaults(handler=_report)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler: Handler = args.handler
    return handler(args)
