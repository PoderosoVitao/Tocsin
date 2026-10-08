from __future__ import annotations

import html
from typing import Any

import httpx

from tocsin.channels import Alert, PermanentError, TransientError
from tocsin.models import State

ICONS = {State.DOWN: "🔴", State.UP: "🟢"}


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    token = config.get("bot_token")
    chat_id = config.get("chat_id")
    if not isinstance(token, str) or ":" not in token:
        raise ValueError("bot_token must be the token BotFather gave you (it contains a colon)")
    if not isinstance(chat_id, (str, int)) or str(chat_id).strip() == "":
        raise ValueError("chat_id is required")
    return {"bot_token": token, "chat_id": str(chat_id)}


def render(alert: Alert) -> str:
    icon = ICONS.get(State(alert.to_state), "⚪")
    lines = [f"{icon} <b>{html.escape(alert.headline)}</b>", html.escape(alert.reason)]
    if alert.output_excerpt:
        lines.append(f"<pre>{html.escape(alert.output_excerpt)}</pre>")
    if alert.link:
        lines.append(f'<a href="{html.escape(alert.link, quote=True)}">Open in tocsin</a>')
    return "\n".join(lines)


class TelegramSender:
    def __init__(self, http: httpx.AsyncClient, api_url: str = "https://api.telegram.org") -> None:
        self.http = http
        self.api_url = api_url.rstrip("/")

    async def send(self, config: dict[str, Any], alert: Alert) -> str | None:
        result = await self._call(
            config,
            "sendMessage",
            {
                "chat_id": config["chat_id"],
                "text": render(alert),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        return str(result["message_id"])

    # Calls the Bot API. The bot token is part of the URL, and httpx puts the
    # URL in its exception messages, so errors are rebuilt from the status
    # and Telegram's description: the token must never reach last_error,
    # which the dashboard shows.
    async def _call(self, config: dict[str, Any], method: str, payload: dict[str, Any]) -> Any:
        url = f"{self.api_url}/bot{config['bot_token']}/{method}"
        try:
            response = await self.http.post(url, json=payload, timeout=10)
        except httpx.HTTPError as exc:
            raise TransientError(f"Telegram unreachable ({type(exc).__name__})") from None

        try:
            body = response.json()
        except ValueError:
            body = {}
        description = body.get("description", "") if isinstance(body, dict) else ""
        if response.status_code == 429:
            retry_after = body.get("parameters", {}).get("retry_after")
            raise TransientError(f"Telegram rate limit: {description}", retry_after=retry_after)
        if response.status_code >= 500:
            raise TransientError(f"Telegram error {response.status_code}: {description}")
        if response.status_code >= 400 or not body.get("ok"):
            raise PermanentError(
                f"Telegram refused the message ({response.status_code}): {description}"
            )
        return body["result"]
