import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from llm_gateway.errors import ProviderError
from llm_gateway.providers.anthropic import AnthropicProvider
from llm_gateway.schemas import ChatCompletionRequest

Handler = Callable[[httpx.Request], httpx.Response]

MESSAGE = {
    "id": "msg_01",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-4-5-20250929",
    "content": [
        {"type": "text", "text": "Hello"},
        {"type": "tool_use", "id": "t1", "name": "f", "input": {}},
        {"type": "text", "text": ", world"},
    ],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 12, "output_tokens": 4, "cache_read_input_tokens": 0},
}


def _req(**kwargs: Any) -> ChatCompletionRequest:
    data: dict[str, Any] = {
        "model": "public",
        "messages": [
            {"role": "system", "content": "You are terse."},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "system", "content": "Answer in English."},
            {"role": "user", "content": "how are you?"},
        ],
    }
    data.update(kwargs)
    return ChatCompletionRequest.model_validate(data)


class Recorder:
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def _provider(handler: Handler, **kwargs: Any) -> tuple[AnthropicProvider, Recorder]:
    recorder = Recorder(handler)
    kwargs.setdefault("api_key", "sk-ant-test")
    provider = AnthropicProvider("anth", transport=httpx.MockTransport(recorder), **kwargs)
    return provider, recorder


def _json(status: int, body: Any) -> Handler:
    return lambda _: httpx.Response(status, json=body)


async def test_request_translation_moves_system_messages() -> None:
    provider, rec = _provider(_json(200, MESSAGE), headers={"x-extra": "1"})
    await provider.chat(_req(temperature=0.3, top_p=0.9, stop=["\n\n", "END"]), "claude-x")
    sent = rec.requests[0]
    assert sent.method == "POST"
    assert str(sent.url) == "https://api.anthropic.com/v1/messages"
    assert sent.headers["x-api-key"] == "sk-ant-test"
    assert sent.headers["anthropic-version"] == "2023-06-01"
    assert sent.headers["content-type"] == "application/json"
    assert sent.headers["x-extra"] == "1"
    assert "authorization" not in sent.headers
    assert json.loads(sent.content) == {
        "model": "claude-x",
        "max_tokens": 1024,
        "system": "You are terse.\n\nAnswer in English.",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "how are you?"},
        ],
        "temperature": 0.3,
        "top_p": 0.9,
        "stop_sequences": ["\n\n", "END"],
    }
    await provider.aclose()


async def test_minimal_request_omits_optional_fields() -> None:
    provider, rec = _provider(
        _json(200, MESSAGE),
        base_url="https://proxy.example.test/anthropic/",
        default_max_tokens=256,
        api_version="2024-01-01",
        api_key=None,
    )
    request = ChatCompletionRequest(model="p", messages=[{"role": "user", "content": "hi"}])
    await provider.chat(request, "claude-x")
    sent = rec.requests[0]
    assert str(sent.url) == "https://proxy.example.test/anthropic/v1/messages"
    assert sent.headers["anthropic-version"] == "2024-01-01"
    assert "x-api-key" not in sent.headers
    assert json.loads(sent.content) == {
        "model": "claude-x",
        "max_tokens": 256,
        "messages": [{"role": "user", "content": "hi"}],
    }


async def test_explicit_max_tokens_passed_through() -> None:
    provider, rec = _provider(_json(200, MESSAGE))
    await provider.chat(_req(max_tokens=42), "claude-x")
    assert json.loads(rec.requests[0].content)["max_tokens"] == 42


async def test_response_translation() -> None:
    provider, _ = _provider(_json(200, MESSAGE))
    resp = await provider.chat(_req(), "claude-x")
    assert resp.id == "msg_01"
    assert resp.object == "chat.completion"
    assert resp.model == "claude-sonnet-4-5-20250929"
    assert resp.choices[0].message.role == "assistant"
    assert resp.choices[0].message.content == "Hello, world"
    assert resp.choices[0].finish_reason == "stop"
    assert (resp.usage.prompt_tokens, resp.usage.completion_tokens) == (12, 4)
    assert resp.usage.total_tokens == 16


@pytest.mark.parametrize(
    ("stop_reason", "finish_reason"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("tool_use", "tool_calls"),
        ("pause_turn", "pause_turn"),
        (None, None),
    ],
)
async def test_stop_reason_mapping(stop_reason: str | None, finish_reason: str | None) -> None:
    provider, _ = _provider(_json(200, {**MESSAGE, "stop_reason": stop_reason}))
    resp = await provider.chat(_req(), "claude-x")
    assert resp.choices[0].finish_reason == finish_reason


async def test_sparse_response_uses_defaults() -> None:
    provider, _ = _provider(_json(200, {"content": []}))
    resp = await provider.chat(_req(), "claude-x")
    assert resp.id.startswith("chatcmpl-")
    assert resp.model == "claude-x"
    assert resp.choices[0].message.content == ""
    assert resp.choices[0].finish_reason is None
    assert resp.usage.total_tokens == 0


@pytest.mark.parametrize(
    ("status", "retryable", "client_error"),
    [
        (529, True, False),
        (500, True, False),
        (429, True, False),
        (400, False, True),
        (401, False, False),
        (404, False, False),
    ],
)
async def test_error_classification(status: int, retryable: bool, client_error: bool) -> None:
    body = {"type": "error", "error": {"type": "some_error", "message": f"anthropic {status}"}}
    provider, _ = _provider(_json(status, body))
    with pytest.raises(ProviderError) as exc:
        await provider.chat(_req(), "claude-x")
    assert exc.value.provider == "anth"
    assert exc.value.status_code == status
    assert exc.value.retryable is retryable
    assert exc.value.client_error is client_error
    assert exc.value.message == f"anthropic {status}"


async def test_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    provider, _ = _provider(handler)
    with pytest.raises(ProviderError) as exc:
        await provider.chat(_req(), "claude-x")
    assert exc.value.timeout
    assert exc.value.retryable


async def test_connect_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    provider, _ = _provider(handler)
    with pytest.raises(ProviderError) as exc:
        await provider.chat(_req(), "claude-x")
    assert exc.value.retryable
    assert not exc.value.timeout


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=["a", "list"]),
        httpx.Response(200, json={"id": "msg", "content": "not a list"}),
    ],
)
async def test_invalid_responses_are_retryable_502(response: httpx.Response) -> None:
    provider, _ = _provider(lambda _: response)
    with pytest.raises(ProviderError) as exc:
        await provider.chat(_req(), "claude-x")
    assert exc.value.status_code == 502
    assert exc.value.retryable
