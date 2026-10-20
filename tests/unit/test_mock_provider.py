import pytest

from llm_gateway.errors import ProviderError
from llm_gateway.events import NullObserver
from llm_gateway.providers import MockProvider
from llm_gateway.schemas import ChatCompletionRequest


def _req(max_tokens: int | None = None) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="public",
        messages=[
            {"role": "system", "content": "be nice"},
            {"role": "user", "content": "hello world"},
        ],
        max_tokens=max_tokens,
    )


async def test_echoes_last_user_message_with_usage() -> None:
    provider = MockProvider()
    resp = await provider.chat(_req(), "mock-1")
    assert resp.model == "mock-1"
    assert resp.choices[0].message.content == "[mock-1] hello world"
    assert resp.usage.prompt_tokens == 4
    assert resp.usage.completion_tokens == 3
    assert resp.usage.total_tokens == 7
    assert provider.calls == 1
    await provider.aclose()


async def test_max_tokens_truncates() -> None:
    resp = await MockProvider().chat(_req(max_tokens=1), "m")
    assert resp.choices[0].message.content == "[m]"


async def test_fail_times_then_recovers() -> None:
    provider = MockProvider(fail_times=1, fail_status=500)
    with pytest.raises(ProviderError) as exc:
        await provider.chat(_req(), "m")
    assert exc.value.retryable
    assert exc.value.status_code == 500
    assert (await provider.chat(_req(), "m")).choices


async def test_always_fail_timeout() -> None:
    provider = MockProvider(fail_times=-1, fail_timeout=True, latency_s=0.001)
    for _ in range(3):
        with pytest.raises(ProviderError) as exc:
            await provider.chat(_req(), "m")
        assert exc.value.timeout


def test_null_observer_accepts_all_events() -> None:
    obs = NullObserver()
    obs.on_provider_attempt(provider="p", model="m", outcome="success", duration_s=0.1)
    obs.on_fallback(from_provider="a", to_provider="b")
    obs.on_circuit_state_change(provider="p", state="open")
    obs.on_tokens(provider="p", model="m", prompt_tokens=1, completion_tokens=1)
    obs.on_rate_limited(client="c", limit_type="requests")
