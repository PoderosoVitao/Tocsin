from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from tocsin.channels import Alert, PermanentError, TransientError, validate_config
from tocsin.channels.telegram import TelegramSender

TOKEN = "123456:SECRET-bot-token"
CONFIG = {"bot_token": TOKEN, "chat_id": "-100200"}
ALERT = Alert(
    event_id=7,
    check_id=uuid.uuid4(),
    check_name="backup <db>",
    from_state="up",
    to_state="down",
    reason="the job exited with code 2",
    occurred_at=datetime(2026, 5, 1, tzinfo=UTC),
    link="https://dash.test/#/checks/1",
    output_excerpt="cp: cannot write: <No space> & no hope",
)


def sender_returning(status: int, body: Any, seen: list[httpx.Request]) -> TelegramSender:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body)

    return TelegramSender(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_send_posts_an_html_message() -> None:
    seen: list[httpx.Request] = []
    sender = sender_returning(200, {"ok": True, "result": {"message_id": 99}}, seen)

    assert await sender.send(CONFIG, ALERT) == "99"

    [request] = seen
    assert request.url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = json.loads(request.content)
    assert payload["chat_id"] == "-100200"
    assert payload["parse_mode"] == "HTML"
    assert payload["text"] == (
        "🔴 <b>backup &lt;db&gt; is down</b>\n"
        "the job exited with code 2\n"
        "<pre>cp: cannot write: &lt;No space&gt; &amp; no hope</pre>\n"
        '<a href="https://dash.test/#/checks/1">Open in tocsin</a>'
    )


async def test_rate_limit_is_transient_with_retry_after() -> None:
    body = {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 33}}
    sender = sender_returning(429, body, [])
    with pytest.raises(TransientError) as caught:
        await sender.send(CONFIG, ALERT)
    assert caught.value.retry_after == 33


async def test_server_errors_are_transient() -> None:
    sender = sender_returning(502, {"ok": False, "description": "Bad Gateway"}, [])
    with pytest.raises(TransientError, match="502"):
        await sender.send(CONFIG, ALERT)


async def test_client_errors_are_permanent_and_never_contain_the_token() -> None:
    body = {"ok": False, "description": "Forbidden: bot was blocked by the user"}
    sender = sender_returning(403, body, [])
    with pytest.raises(PermanentError) as caught:
        await sender.send(CONFIG, ALERT)
    assert "bot was blocked" in str(caught.value)
    assert TOKEN not in str(caught.value)


async def test_network_errors_are_transient_and_never_contain_the_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"could not connect to {request.url}")

    sender = TelegramSender(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(TransientError) as caught:
        await sender.send(CONFIG, ALERT)
    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


def test_config_validation() -> None:
    assert validate_config("telegram", {"bot_token": TOKEN, "chat_id": -100}) == {
        "bot_token": TOKEN,
        "chat_id": "-100",
    }
    with pytest.raises(ValueError, match="bot_token"):
        validate_config("telegram", {"bot_token": "nope", "chat_id": 1})
    with pytest.raises(ValueError, match="chat_id"):
        validate_config("telegram", {"bot_token": TOKEN})
    with pytest.raises(ValueError, match="unknown channel kind"):
        validate_config("pager", {})
