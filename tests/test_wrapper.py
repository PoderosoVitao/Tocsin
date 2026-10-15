from __future__ import annotations

import socket
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tocsin.cli import main
from tocsin.wrapper import run

CHECK = "0b9e5bd4-77a4-4a2c-9a51-6b1f6b2d3c11"


class FakeTocsin:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[tuple[str, str, bytes]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _record(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                fake.requests.append((self.command, self.path, self.rfile.read(length)))
                self.send_response(fake.status)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"OK")

            do_GET = do_POST = _record

            def log_message(self, *args: object) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def paths(self) -> list[str]:
        return [path for _, path, _ in self.requests]


@pytest.fixture
def tocsin() -> Iterator[FakeTocsin]:
    fake = FakeTocsin()
    yield fake
    fake.server.shutdown()


def python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def unused_port_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}/ping/{CHECK}"


def test_a_successful_command_reports_start_and_exit_code(
    tocsin: FakeTocsin, capfd: pytest.CaptureFixture[str]
) -> None:
    status = run(python("print('copied 12 files')"), f"{tocsin.url}/ping/{CHECK}")

    assert status == 0
    assert tocsin.paths == [f"/ping/{CHECK}/start", f"/ping/{CHECK}/0"]
    assert tocsin.requests[1][2] == b"copied 12 files\n"
    assert capfd.readouterr().out == "copied 12 files\n"


def test_a_failing_command_reports_its_code_and_output(
    tocsin: FakeTocsin, capfd: pytest.CaptureFixture[str]
) -> None:
    code = "import sys; print('starting'); sys.stderr.write('No space left\\n'); sys.exit(3)"
    status = run(python(code), f"{tocsin.url}/ping/{CHECK}")

    assert status == 3
    method, path, body = tocsin.requests[-1]
    assert (method, path) == ("POST", f"/ping/{CHECK}/3")
    assert b"starting\n" in body and b"No space left\n" in body
    assert capfd.readouterr().err == "No space left\n"


def test_only_the_last_lines_are_sent(tocsin: FakeTocsin) -> None:
    run(python("for i in range(500): print(i)"), f"{tocsin.url}/ping/{CHECK}", tail_lines=3)
    assert tocsin.requests[-1][2] == b"497\n498\n499\n"


def test_a_silent_command_sends_no_body(tocsin: FakeTocsin) -> None:
    run(python("pass"), f"{tocsin.url}/ping/{CHECK}")
    assert tocsin.requests[-1] == ("GET", f"/ping/{CHECK}/0", b"")


# Section 16: "The CLI wrapper preserves the command's exit code when the
# server is down."
def test_the_exit_code_is_preserved_when_tocsin_is_down(
    tmp_path: pytest.TempPathFactory, capfd: pytest.CaptureFixture[str]
) -> None:
    marker = f"{tmp_path}/ran"
    code = f"open({marker!r}, 'w').write('yes'); raise SystemExit(4)"

    started = time.monotonic()
    status = run(python(code), unused_port_url(), timeout=1)

    assert status == 4
    assert open(marker).read() == "yes"
    assert "could not reach tocsin" in capfd.readouterr().err
    assert time.monotonic() - started < 10


def test_a_refused_ping_is_not_retried(capfd: pytest.CaptureFixture[str]) -> None:
    fake = FakeTocsin(status=404)
    try:
        assert run(python("pass"), f"{fake.url}/ping/{CHECK}") == 0
    finally:
        fake.server.shutdown()
    assert len(fake.requests) == 2
    assert "ping refused (404)" in capfd.readouterr().err


def test_a_command_that_does_not_exist_reports_127(tocsin: FakeTocsin) -> None:
    status = run(["/nonexistent/backup.sh"], f"{tocsin.url}/ping/{CHECK}")
    assert status == 127
    assert tocsin.paths[-1] == f"/ping/{CHECK}/127"


def test_a_command_killed_by_a_signal_reports_128_plus_the_signal(tocsin: FakeTocsin) -> None:
    code = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
    assert run(python(code), f"{tocsin.url}/ping/{CHECK}") == 137
    assert tocsin.paths[-1] == f"/ping/{CHECK}/137"


def test_cli_builds_the_ping_url_from_a_uuid_and_server(tocsin: FakeTocsin) -> None:
    status = main(["run", "--check", CHECK, "--server", tocsin.url, "--", *python("exit(2)")])
    assert status == 2
    assert tocsin.paths == [f"/ping/{CHECK}/start", f"/ping/{CHECK}/2"]


def test_cli_accepts_a_full_ping_url(tocsin: FakeTocsin) -> None:
    main(["run", "--check", f"{tocsin.url}/ping/{CHECK}", "--", *python("pass")])
    assert tocsin.paths[-1] == f"/ping/{CHECK}/0"


def test_cli_needs_a_command_and_a_check(capfd: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "--check", CHECK]) == 2
    assert main(["run", "--", "true"]) == 2
