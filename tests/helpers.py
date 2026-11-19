from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpx

from tocsin.channels import Alert
from tocsin.db import Sessionmaker
from tocsin.models import Channel, Check, Event
from tocsin.pings import Signal, record_ping
from tocsin.sweep import sweep


async def add_channel(sessionmaker: Sessionmaker, name: str = "phone") -> str:
    async with sessionmaker() as session, session.begin():
        channel = Channel(kind="telegram", name=name, config={"bot_token": "1:x", "chat_id": "1"})
        session.add(channel)
        await session.flush()
        return str(channel.id)


async def create_check(client: httpx.AsyncClient, **fields: Any) -> dict[str, Any]:
    body = {"name": "backup", "schedule": "0 3 * * *", **fields}
    response = await client.post("/api/checks", json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    created["ping_path"] = created["ping_url"].removeprefix("https://tocsin.test")
    return created


# A channel that records what it was asked to send. Each call takes the next
# entry of `outcomes`: an exception to raise, or a message id to return.
class FakeSender:
    def __init__(self, *outcomes: Exception | str, delay: float = 0) -> None:
        self.outcomes = list(outcomes)
        self.delay = delay
        self.calls: list[Alert] = []
        self.sent: list[Alert] = []
        # (message id, alert as edited), for edits.
        self.edits: list[tuple[str, Alert]] = []
        self.edit_error: Exception | None = None

    async def send(self, config: dict[str, Any], alert: Alert) -> str | None:
        self.calls.append(alert)
        if self.delay:
            await asyncio.sleep(self.delay)
        outcome = self.outcomes.pop(0) if self.outcomes else str(len(self.calls))
        if isinstance(outcome, Exception):
            raise outcome
        self.sent.append(alert)
        return outcome

    async def edit(self, config: dict[str, Any], external_id: str, alert: Alert) -> None:
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append((external_id, alert))


# Sends a ping as if it arrived at `at`, going through the same code as the
# HTTP endpoint, so tests can play out hours of pings in an instant.
async def ping_at(
    sessionmaker: Sessionmaker, check_id: str, signal: str | None, at: datetime, body: bytes = b""
) -> None:
    async with sessionmaker() as session, session.begin():
        check = await session.get_one(Check, check_id)
        await record_ping(session, check.ping_key, Signal.parse(signal), body, None, at, 10_000)


async def sweep_at(sessionmaker: Sessionmaker, at: datetime) -> list[Event]:
    async with sessionmaker() as session, session.begin():
        return await sweep(session, at)


# A stand-in for a tocsin server on a real port, for the job-side tools that
# talk to it over HTTP: records every request it gets.
class FakeTocsin:
    def __init__(self, status: int = 200, reply: bytes = b"OK") -> None:
        self.status = status
        self.reply = reply
        self.requests: list[tuple[str, str, bytes]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _record(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                fake.requests.append((self.command, self.path, self.rfile.read(length)))
                self.send_response(fake.status)
                self.send_header("Content-Length", str(len(fake.reply)))
                self.end_headers()
                self.wfile.write(fake.reply)

            do_GET = do_POST = _record

            def log_message(self, *args: object) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def paths(self) -> list[str]:
        return [path for _, path, _ in self.requests]


# A stand-in for a model provider on a real port, speaking enough of Anthropic's
# Messages API and OpenAI's chat completions API for the providers to talk to.
# Records every request, so tests can look at exactly what was sent.
class FakeLLMServer:
    def __init__(self, text: str = "disk full on /mnt/backup", status: int = 200) -> None:
        self.text = text
        self.status = status
        # Replaces the generated reply when set: (status, body).
        self.raw: tuple[int, bytes] | None = None
        self.requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                headers = {key.lower(): value for key, value in self.headers.items()}
                fake.requests.append((self.path, headers, body))
                status, payload = fake.reply(self.path)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        ).start()

    def reply(self, path: str) -> tuple[int, bytes]:
        if self.raw is not None:
            return self.raw
        if self.status != 200:
            return self.status, json.dumps({"error": {"message": "nope"}}).encode()
        if path.endswith("/messages"):
            body: dict[str, Any] = {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": self.text}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 120, "output_tokens": 9},
            }
        else:
            body = {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": self.text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 9},
            }
        return 200, json.dumps(body).encode()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
