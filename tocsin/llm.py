from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from tocsin.config import Settings

log = logging.getLogger(__name__)

PROVIDERS = ("anthropic", "openai", "openai-compatible")
OPENAI_URL = "https://api.openai.com/v1"
# Claude's default when the operator names no model.
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"


# Anything that stops a completion: the provider is down, the key is wrong, the
# model declined, the answer was empty. The message is safe to log and to show
# (it never holds the key or the prompt).
class LLMError(Exception):
    pass


# The operator's settings can't produce a working provider.
class LLMConfigError(Exception):
    pass


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class LLM(Protocol):
    provider: str
    model: str

    async def complete(self, system: str, prompt: str, max_tokens: int) -> Completion: ...

    async def close(self) -> None: ...


# Claude, through the official SDK.
class AnthropicLLM:
    provider = "anthropic"

    def __init__(
        self, api_key: str | None, model: str, base_url: str | None, timeout: float
    ) -> None:
        import anthropic

        self.model = model
        self._anthropic = anthropic
        # No retries: a summary is a nicety, and a slow retry only delays
        # finding out that it won't come.
        self.client = anthropic.AsyncAnthropic(
            api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0
        )

    async def complete(self, system: str, prompt: str, max_tokens: int) -> Completion:
        anthropic = self._anthropic
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        # Reading a log for one line needs little thinking. Only the models that
        # take the setting get it; the others reject it.
        if _takes_effort(self.model):
            request["output_config"] = {"effort": "low"}
        try:
            response = await self.client.messages.create(**request)
        except anthropic.AuthenticationError:
            raise LLMError("Anthropic rejected the API key") from None
        except anthropic.PermissionDeniedError:
            raise LLMError("the Anthropic key may not use this model") from None
        except anthropic.NotFoundError:
            raise LLMError(f"Anthropic does not know the model {self.model!r}") from None
        except anthropic.RateLimitError:
            raise LLMError("Anthropic rate limit") from None
        except anthropic.APITimeoutError:
            raise LLMError("Anthropic timed out") from None
        except anthropic.APIConnectionError:
            raise LLMError("Anthropic is unreachable") from None
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic error {exc.status_code}") from None
        if response.stop_reason == "refusal":
            raise LLMError("the model declined to answer")
        text = "".join(block.text for block in response.content if block.type == "text")
        return Completion(text, response.usage.input_tokens, response.usage.output_tokens)

    async def close(self) -> None:
        await self.client.close()


# Claude models that accept output_config.effort.
_EFFORT_MODELS = re.compile(r"^claude-(?:opus|sonnet|haiku|fable|mythos)-(?:5|4-[678])(?:-|$)")


def _takes_effort(model: str) -> bool:
    return _EFFORT_MODELS.match(model) is not None


# OpenAI, and anything that speaks its chat completions API: OpenRouter, Groq,
# Together, vLLM, Ollama, LM Studio, Azure's v1 endpoint.
class OpenAILLM:
    def __init__(
        self,
        http: httpx.AsyncClient,
        api_key: str | None,
        model: str,
        base_url: str,
        timeout: float,
        provider: str = "openai",
    ) -> None:
        self.http = http
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.provider = provider

    async def complete(self, system: str, prompt: str, max_tokens: int) -> Completion:
        # OpenAI's newer models only take max_completion_tokens; most other
        # servers only know max_tokens.
        limit = "max_completion_tokens" if self.base_url == OPENAI_URL else "max_tokens"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            response = await self.http.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                timeout=self.timeout,
                json={
                    "model": self.model,
                    limit: max_tokens,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
        except httpx.TimeoutException:
            raise LLMError(f"{self.provider} timed out") from None
        except httpx.HTTPError:
            raise LLMError(f"{self.provider} is unreachable") from None

        if response.status_code in (401, 403):
            raise LLMError(f"{self.provider} rejected the API key")
        if response.status_code == 404:
            raise LLMError(f"{self.provider} does not know the model {self.model!r}")
        if response.status_code == 429:
            raise LLMError(f"{self.provider} rate limit")
        if response.status_code >= 400:
            raise LLMError(f"{self.provider} error {response.status_code}")
        try:
            body = response.json()
            choice = body["choices"][0]
            text = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMError(f"{self.provider} sent a reply tocsin could not read") from None
        if not isinstance(text, str):
            raise LLMError("the model declined to answer")
        usage = body.get("usage") if isinstance(body, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        return Completion(
            text, _count(usage.get("prompt_tokens")), _count(usage.get("completion_tokens"))
        )

    async def close(self) -> None:
        pass


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# The provider the settings ask for, or None when summaries are not set up
# (no provider named). Raises LLMConfigError for settings that name a provider
# but can't work, so the worker can say so once at startup instead of failing
# on every summary.
def build_llm(settings: Settings, http: httpx.AsyncClient) -> LLM | None:
    provider = settings.llm_provider
    if provider is None:
        return None
    if provider not in PROVIDERS:
        raise LLMConfigError(f"TOCSIN_LLM_PROVIDER must be one of {', '.join(PROVIDERS)}")
    key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else None
    timeout = settings.llm_timeout_seconds

    if provider == "anthropic":
        try:
            import anthropic  # noqa: F401
        except ImportError:
            raise LLMConfigError("the anthropic package is not installed") from None
        return AnthropicLLM(
            key, settings.llm_model or DEFAULT_ANTHROPIC_MODEL, settings.llm_base_url, timeout
        )

    if not settings.llm_model:
        raise LLMConfigError("TOCSIN_LLM_MODEL is required for this provider")
    if provider == "openai":
        if not key and not settings.llm_base_url:
            raise LLMConfigError("TOCSIN_LLM_API_KEY is required for OpenAI")
        return OpenAILLM(
            http, key, settings.llm_model, settings.llm_base_url or OPENAI_URL, timeout
        )
    if not settings.llm_base_url:
        raise LLMConfigError("TOCSIN_LLM_BASE_URL is required for openai-compatible")
    return OpenAILLM(
        http, key, settings.llm_model, settings.llm_base_url, timeout, "openai-compatible"
    )
