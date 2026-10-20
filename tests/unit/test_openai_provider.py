import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from llm_gateway.errors import ProviderError
from llm_gateway.providers.openai import OpenAIProvider
from llm_gateway.schemas import ChatCompletionRequest

Handler = Callable[[httpx.Request], httpx.Response]

COMPLETION = {
    "id": "chatcmpl-abc",
    "object": "chat.completion",
    "created": 1700000000,
    "model": "gpt-4o-2024-08-06",
    "system_fingerprint": "fp_123",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello!", "refusal": None},
            "logprobs": None,
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 9,
        "completion_tokens": 3,
        "total_tokens": 12,
        "completion_tokens_details": {"reasoning_tokens": 0},
    },
}


def _req(**kwargs: Any) -> ChatCompletionRequest:
    data: dict[str, Any] = {
        "model": "public",
        "messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"},
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


def _provider(handler: Handler, **kwargs: Any) -> tuple[OpenAIProvider, Recorder]:
    recorder = Recorder(handler)
    kwargs.setdefault("api_key", "sk-test")
    provider = OpenAIProvider("oa", transport=httpx.MockTransport(recorder), **kwargs)
    return provider, recorder


def _json(status: int, body: Any) -> Handler:
    return lambda _: httpx.Response(status, json=body)


async def test_request_translation() -> None:
    provider, rec = _provider(
        _json(200, COMPLETION),
        base_url="https://llm.example.test/v1/",
        headers={"OpenAI-Organization": "org-1"},
    )
    await provider.chat(_req(temperature=0.5, stop="END", stream=True, user="u1"), "gpt-4o")
    sent = rec.requests[0]
    assert sent.method == "POST"
    assert str(sent.url) == "https://llm.example.test/v1/chat/completions"
    assert sent.headers["authorization"] == "Bearer sk-test"
    assert sent.headers["openai-organization"] == "org-1"
    assert json.loads(sent.content) == {
        "model": "gpt-4o",
        "messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"},
        ],
        "temperature": 0.5,
        "stop": ["END"],
        "user": "u1",
        "stream": False,
    }
    await provider.aclose()


async def test_default_base_url_and_no_auth_without_key() -> None:
    provider, rec = _provider(_json(200, COMPLETION), api_key=None)
    await provider.chat(_req(), "gpt-4o")
    sent = rec.requests[0]
    assert str(sent.url) == "https://api.openai.com/v1/chat/completions"
    assert "authorization" not in sent.headers


async def test_response_translation() -> None:
    provider, _ = _provider(_json(200, COMPLETION))
    resp = await provider.chat(_req(), "gpt-4o")
    assert resp.id == "chatcmpl-abc"
    assert resp.model == "gpt-4o-2024-08-06"
    assert resp.created == 1700000000
    assert resp.choices[0].message.content == "Hello!"
    assert resp.choices[0].finish_reason == "stop"
    assert (resp.usage.prompt_tokens, resp.usage.completion_tokens) == (9, 3)
    assert resp.usage.total_tokens == 12


@pytest.mark.parametrize("usage", ["missing", None])
async def test_missing_usage_defaults_to_zero(usage: Any) -> None:
    body = {k: v for k, v in COMPLETION.items() if k != "usage"}
    if usage != "missing":
        body["usage"] = usage
    provider, _ = _provider(_json(200, body))
    resp = await provider.chat(_req(), "gpt-4o")
    assert resp.usage.total_tokens == 0


@pytest.mark.parametrize(
    ("status", "retryable", "client_error"),
    [
        (429, True, False),
        (500, True, False),
        (503, True, False),
        (400, False, True),
        (422, False, True),
        (401, False, False),
        (403, False, False),
        (404, False, False),
    ],
)
async def test_error_classification(status: int, retryable: bool, client_error: bool) -> None:
    body = {"error": {"message": f"upstream says {status}", "type": "x", "code": None}}
    provider, _ = _provider(_json(status, body))
    with pytest.raises(ProviderError) as exc:
        await provider.chat(_req(), "gpt-4o")
    err = exc.value
    assert err.provider == "oa"
    assert err.status_code == status
    assert err.retryable is retryable
    assert err.client_error is client_error
    assert not err.timeout
    assert err.message == f"upstream says {status}"


async def test_error_message_from_text_is_truncated() -> None:
    provider, _ = _provider(lambda _: httpx.Response(502, text="x" * 2000))
    with pytest.raises(ProviderError) as exc:
        await provider.chat(_req(), "gpt-4o")
    assert exc.value.message == "x" * 500
    assert exc.value.retryable


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"error": "plain string"}, "plain string"),
        ({"message": "top-level"}, "top-level"),
        ({"detail": "other"}, '{"detail":"other"}'),
    ],
)
async def test_error_message_shapes(body: Any, expected: str) -> None:
    provider, _ = _provider(_json(400, body))
    with pytest.raises(ProviderError) as exc:
        await provider.chat(_req(), "gpt-4o")
    assert exc.value.message == expected


async def test_empty_error_body() -> None:
    provider, _ = _provider(lambda _: httpx.Response(500))
    with pytest.raises(ProviderError) as exc:
        await provider.chat(_req(), "gpt-4o")
    assert exc.value.message == "HTTP 500"


async def test_timeout_is_retryable_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    provider, _ = _provider(handler)
    with pytest.raises(ProviderError) as exc:
        await provider.chat(_req(), "gpt-4o")
    assert exc.value.timeout
    assert exc.value.retryable
    assert exc.value.status_code is None


async def test_connect_error_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    provider, _ = _provider(handler)
    with pytest.raises(ProviderError) as exc:
        await provider.chat(_req(), "gpt-4o")
    assert exc.value.retryable
    assert not exc.value.timeout
    assert "ConnectError" in exc.value.message


async def test_invalid_json_is_retryable_502() -> None:
    provider, _ = _provider(lambda _: httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(ProviderError) as exc:
        await provider.chat(_req(), "gpt-4o")
    assert exc.value.status_code == 502
    assert exc.value.retryable
    assert not exc.value.client_error


@pytest.mark.parametrize("body", [{"unexpected": True}, ["not", "an", "object"]])
async def test_schema_mismatch_is_retryable_502(body: Any) -> None:
    provider, _ = _provider(_json(200, body))
    with pytest.raises(ProviderError) as exc:
        await provider.chat(_req(), "gpt-4o")
    assert exc.value.status_code == 502
    assert exc.value.retryable


async def test_api_key_not_in_error_messages() -> None:
    provider, _ = _provider(_json(401, {"error": {"message": "Incorrect API key provided"}}))
    with pytest.raises(ProviderError) as exc:
        await provider.chat(_req(), "gpt-4o")
    assert "sk-test" not in str(exc.value)


async def test_aclose_closes_client() -> None:
    provider, _ = _provider(_json(200, COMPLETION))
    assert not provider._client.is_closed
    await provider.aclose()
    assert provider._client.is_closed
