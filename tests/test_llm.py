from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr

from tests.helpers import FakeLLMServer
from tocsin.config import Settings
from tocsin.llm import LLMConfigError, LLMError, OpenAILLM, build_llm

KEY = "fake-llm-key-for-tests"


@pytest.fixture
def server() -> Iterator[FakeLLMServer]:
    fake = FakeLLMServer()
    yield fake
    fake.close()


@pytest_asyncio.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


def settings(server: FakeLLMServer, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "llm_provider": "anthropic",
        "llm_api_key": SecretStr(KEY),
        "llm_base_url": server.url,
        **overrides,
    }
    return Settings(**values)  # type: ignore[arg-type]


async def test_claude_is_asked_through_the_messages_api(
    server: FakeLLMServer, http: httpx.AsyncClient
) -> None:
    llm = build_llm(settings(server), http)
    assert llm is not None and llm.provider == "anthropic" and llm.model == "claude-opus-5-5"
    completion = await llm.complete("be brief", "why did it fail?", 500)
    await llm.close()

    assert (completion.text, completion.input_tokens, completion.output_tokens) == (
        "disk full on /mnt/backup",
        120,
        9,
    )
    [(path, headers, body)] = server.requests
    assert path == "/v1/messages"
    assert headers["authorization"] == f"Bearer {KEY}"
    assert body["model"] == "claude-opus-5-5" and body["max_tokens"] == 500
    assert body["system"] == "be brief"
    assert body["messages"] == [{"role": "user", "content": "why did it fail?"}]
    assert body["output_config"] == {"effort": "low"}
    # Nothing that would turn thinking off or force a tool, which this model rejects.
    assert not {"thinking", "tools", "tool_choice", "temperature"} & body.keys()


@pytest.mark.parametrize(
    ("model", "takes_effort"),
    [
        ("claude-opus-5-5", True),
        ("claude-sonnet-5-5", True),
        ("claude-haiku-5-5", True),
        ("claude-opus-4-8", True),
        ("claude-sonnet-4-6", True),
        ("claude-haiku-4-5", False),
    ],
)
async def test_the_effort_setting_is_only_sent_to_models_that_take_it(
    server: FakeLLMServer, http: httpx.AsyncClient, model: str, takes_effort: bool
) -> None:
    llm = build_llm(settings(server, llm_model=model), http)
    assert llm is not None
    await llm.complete("s", "p", 100)
    assert ("output_config" in server.requests[0][2]) is takes_effort


async def test_an_openai_server_is_asked_through_chat_completions(
    server: FakeLLMServer, http: httpx.AsyncClient
) -> None:
    llm = build_llm(
        settings(server, llm_provider="openai", llm_model="gpt-x", llm_base_url=server.url), http
    )
    assert llm is not None and llm.provider == "openai"
    completion = await llm.complete("be brief", "why did it fail?", 500)

    assert (completion.text, completion.input_tokens, completion.output_tokens) == (
        "disk full on /mnt/backup",
        120,
        9,
    )
    [(path, headers, body)] = server.requests
    assert path == "/chat/completions"
    assert headers["authorization"] == f"Bearer {KEY}"
    assert body["model"] == "gpt-x"
    assert body["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "why did it fail?"},
    ]
    # Other servers only know max_tokens; OpenAI's own URL gets the newer name.
    assert body["max_tokens"] == 500 and "max_completion_tokens" not in body


async def test_openai_itself_gets_the_newer_token_limit_name(server: FakeLLMServer) -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
        llm = build_llm(
            Settings(llm_provider="openai", llm_api_key=SecretStr(KEY), llm_model="gpt-x"), http
        )
        assert llm is not None
        await llm.complete("s", "p", 50)
    assert str(seen[0].url) == "https://api.openai.com/v1/chat/completions"
    assert b'"max_completion_tokens":50' in seen[0].content.replace(b" ", b"")


async def test_a_compatible_server_may_need_no_key(
    server: FakeLLMServer, http: httpx.AsyncClient
) -> None:
    llm = build_llm(
        settings(
            server,
            llm_provider="openai-compatible",
            llm_model="llama3",
            llm_api_key=None,
        ),
        http,
    )
    assert llm is not None and llm.provider == "openai-compatible"
    await llm.complete("s", "p", 50)
    assert "authorization" not in server.requests[0][1]


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, "rejected the API key"),
        (403, "may not use this model|rejected the API key"),
        (404, "does not know the model"),
        (429, "rate limit"),
        (500, "error 500"),
    ],
)
async def test_provider_failures_become_one_safe_error(
    server: FakeLLMServer, http: httpx.AsyncClient, provider: str, status: int, message: str
) -> None:
    server.status = status
    llm = build_llm(settings(server, llm_provider=provider, llm_model="m"), http)
    assert llm is not None
    with pytest.raises(LLMError, match=message) as caught:
        await llm.complete("s", "the prompt text", 50)
    # What was sent, and the key it was sent with, stay out of the message.
    assert KEY not in str(caught.value) and "the prompt text" not in str(caught.value)


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
async def test_an_unreachable_server_is_an_error_not_a_crash(
    server: FakeLLMServer, http: httpx.AsyncClient, provider: str
) -> None:
    llm = build_llm(settings(server, llm_provider=provider, llm_model="m"), http)
    server.close()
    assert llm is not None
    with pytest.raises(LLMError, match="unreachable"):
        await llm.complete("s", "p", 50)


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
async def test_a_stalled_server_times_out(
    server: FakeLLMServer, http: httpx.AsyncClient, provider: str
) -> None:
    import socket

    with socket.socket() as stalled:
        stalled.bind(("127.0.0.1", 0))
        stalled.listen(1)
        url = f"http://127.0.0.1:{stalled.getsockname()[1]}"
        llm = build_llm(
            settings(
                server,
                llm_provider=provider,
                llm_model="m",
                llm_base_url=url,
                llm_timeout_seconds=0.3,
            ),
            http,
        )
        assert llm is not None
        with pytest.raises(LLMError, match="timed out"):
            await llm.complete("s", "p", 50)


async def test_an_empty_or_malformed_answer_is_an_error(
    server: FakeLLMServer, http: httpx.AsyncClient
) -> None:
    llm = OpenAILLM(http, None, "m", server.url, 5)
    for body in (b"not json", b'{"choices": []}', b'{"choices":[{"message":{"content":null}}]}'):
        server.raw = (200, body)
        with pytest.raises(LLMError):
            await llm.complete("s", "p", 50)


async def test_a_refusal_from_claude_is_an_error(
    server: FakeLLMServer, http: httpx.AsyncClient
) -> None:
    server.raw = (
        200,
        b'{"id":"m","type":"message","role":"assistant","model":"claude-opus-5-5",'
        b'"content":[],"stop_reason":"refusal","stop_sequence":null,'
        b'"usage":{"input_tokens":1,"output_tokens":0}}',
    )
    llm = build_llm(settings(server), http)
    assert llm is not None
    with pytest.raises(LLMError, match="declined"):
        await llm.complete("s", "p", 50)


def test_nothing_is_built_until_a_provider_is_named(http: httpx.AsyncClient) -> None:
    assert build_llm(Settings(), http) is None
    # A compose file passes unset variables through as empty strings.
    empty = Settings(llm_provider="", llm_api_key=SecretStr(""), llm_model="", llm_base_url="")
    assert build_llm(empty, http) is None


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"llm_provider": "gemini"}, "must be one of"),
        ({"llm_provider": "openai", "llm_model": "m"}, "TOCSIN_LLM_API_KEY is required"),
        ({"llm_provider": "openai", "llm_api_key": SecretStr("k")}, "TOCSIN_LLM_MODEL"),
        (
            {"llm_provider": "openai-compatible", "llm_model": "m"},
            "TOCSIN_LLM_BASE_URL is required",
        ),
    ],
)
def test_settings_that_cannot_work_say_why(
    http: httpx.AsyncClient, values: dict[str, object], message: str
) -> None:
    with pytest.raises(LLMConfigError, match=message):
        build_llm(Settings(**values), http)  # type: ignore[arg-type]


def test_the_api_key_does_not_show_in_the_settings_text() -> None:
    text = repr(Settings(llm_api_key=SecretStr(KEY)))
    assert KEY not in text
