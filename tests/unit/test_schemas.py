import pytest
from pydantic import ValidationError

from llm_gateway.errors import (
    AllProvidersFailedError,
    ProviderError,
    RateLimitExceededError,
    UpstreamTimeoutError,
)
from llm_gateway.schemas import ChatCompletionRequest, ChatCompletionResponse, Usage


def _req(**kw: object) -> ChatCompletionRequest:
    data: dict[str, object] = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    data.update(kw)
    return ChatCompletionRequest.model_validate(data)


def test_request_parses_and_ignores_unknown_fields() -> None:
    req = _req(frequency_penalty=1.0, temperature=0.5)
    assert req.temperature == 0.5
    assert not req.stream


def test_request_rejects_empty_messages() -> None:
    with pytest.raises(ValidationError):
        _req(messages=[])


def test_request_rejects_bad_role() -> None:
    with pytest.raises(ValidationError):
        _req(messages=[{"role": "tool", "content": "x"}])


def test_stop_normalised_to_list() -> None:
    assert _req(stop="END").stop_sequences() == ["END"]
    assert _req(stop=["a", "b"]).stop_sequences() == ["a", "b"]
    assert _req().stop_sequences() is None


def test_usage_total() -> None:
    assert Usage.of(3, 4).total_tokens == 7


def test_response_defaults() -> None:
    resp = ChatCompletionResponse(model="m", choices=[])
    assert resp.id.startswith("chatcmpl-")
    assert resp.object == "chat.completion"
    assert resp.created > 0


def test_error_envelope() -> None:
    err = RateLimitExceededError("slow down", headers={"Retry-After": "3"})
    body = err.to_response().to_dict()
    assert body == {
        "error": {"message": "slow down", "type": "rate_limit_error", "code": "rate_limit_exceeded"}
    }
    assert err.status_code == 429
    assert err.headers["Retry-After"] == "3"
    assert UpstreamTimeoutError("t").status_code == 504
    assert isinstance(UpstreamTimeoutError("t"), AllProvidersFailedError)


@pytest.mark.parametrize(
    ("status", "retryable", "client_error"),
    [
        (429, True, False),
        (500, True, False),
        (503, True, False),
        (400, False, True),
        (422, False, True),
        (401, False, False),
        (404, False, False),
    ],
)
def test_provider_error_classification(status: int, retryable: bool, client_error: bool) -> None:
    err = ProviderError.from_status("p", status, "boom")
    assert err.retryable is retryable
    assert err.client_error is client_error
    assert "p" in str(err)
    assert f"status={status}" in str(err)
