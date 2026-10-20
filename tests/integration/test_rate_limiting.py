"""Rate limiting through the HTTP API (rate-limiting spec)."""

from __future__ import annotations

from .conftest import KEY_A, KEY_B, AppFactory, FakeClock, chat


def _limited(make_client: AppFactory, rpm: int = 0, tpm: int = 0):  # type: ignore[no-untyped-def]
    return make_client(
        clients=[
            {
                "name": "team-a",
                "key": KEY_A,
                "rate_limit": {"requests_per_minute": rpm, "tokens_per_minute": tpm},
            },
            {"name": "team-b", "key": KEY_B},
        ]
    )


def test_request_limit_and_refill(make_client: AppFactory, clock: FakeClock) -> None:
    client = _limited(make_client, rpm=2)
    assert chat(client).status_code == 200
    assert chat(client).status_code == 200
    resp = chat(client)
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "rate_limit_exceeded"
    assert int(resp.headers["Retry-After"]) >= 1
    assert resp.headers["X-RateLimit-Remaining-Requests"] == "0"

    clock.advance(30)  # 2 rpm -> one request refilled every 30s
    assert chat(client).status_code == 200


def test_rejected_request_does_not_call_provider(make_client: AppFactory) -> None:
    client = _limited(make_client, rpm=1)
    chat(client)
    chat(client)
    metrics = client.get("/metrics").text
    assert (
        'llmgw_provider_requests_total{model="p-1",outcome="success",provider="primary"} 1.0'
        in metrics
    )
    assert 'llmgw_rate_limited_total{client="team-a",limit="requests"} 1.0' in metrics


def test_remaining_headers(make_client: AppFactory) -> None:
    client = _limited(make_client, rpm=5, tpm=1000)
    resp = chat(client)
    assert resp.headers["X-RateLimit-Limit-Requests"] == "5"
    assert resp.headers["X-RateLimit-Remaining-Requests"] == "4"
    assert resp.headers["X-RateLimit-Limit-Tokens"] == "1000"
    assert "X-RateLimit-Remaining-Tokens" in resp.headers


def test_token_budget_exhausted(make_client: AppFactory, clock: FakeClock) -> None:
    client = _limited(make_client, tpm=5)
    first = chat(client, content="one two three four five six")
    assert first.status_code == 200
    assert first.json()["usage"]["total_tokens"] >= 5
    resp = chat(client)
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1
    clock.advance(600)
    assert chat(client).status_code == 200


def test_client_isolation(make_client: AppFactory) -> None:
    client = _limited(make_client, rpm=1)
    assert chat(client).status_code == 200
    assert chat(client).status_code == 429
    assert chat(client, key=KEY_B).status_code == 200


def test_headers_present_on_upstream_errors(make_client: AppFactory) -> None:
    client = _limited(make_client, rpm=10)
    resp = chat(client, "broken")
    assert resp.status_code == 502
    assert resp.headers["X-RateLimit-Limit-Requests"] == "10"
