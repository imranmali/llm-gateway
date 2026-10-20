"""Fallback routing and circuit breaking through the HTTP API (fallback-routing spec)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import AppFactory, FakeClock, chat


def test_failover_to_secondary(client: TestClient) -> None:
    resp = chat(client, "failover")
    assert resp.status_code == 200
    assert resp.json()["model"] == "failover"
    assert resp.headers["X-Gateway-Provider"] == "secondary"
    assert resp.headers["X-Gateway-Upstream-Model"] == "s-1"
    assert int(resp.headers["X-Gateway-Attempts"]) >= 2

    metrics = client.get("/metrics").text
    assert 'llmgw_fallbacks_total{from_provider="flaky",to_provider="secondary"} 1.0' in metrics
    assert 'llmgw_tokens_total{model="s-1",provider="secondary",type="prompt"}' in metrics


def test_all_providers_failed(client: TestClient) -> None:
    resp = chat(client, "broken")
    assert resp.status_code == 502
    error = resp.json()["error"]
    assert error["code"] == "all_providers_failed"
    assert "flaky" in error["message"]


def test_all_timeouts(client: TestClient) -> None:
    resp = chat(client, "timeout")
    assert resp.status_code == 504
    assert resp.json()["error"]["code"] == "upstream_timeout"


def test_upstream_bad_request_not_retried(client: TestClient) -> None:
    resp = chat(client, "bad-request")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "upstream_bad_request"
    picky = client.app.state.gateway.router  # type: ignore[attr-defined]
    assert picky.provider_states()["secondary"] == "closed"
    metrics = client.get("/metrics").text
    assert (
        'provider="secondary"'
        not in metrics.split("llmgw_provider_requests_total")[1].split("# HELP")[0]
    )


def test_circuit_opens_and_recovers(make_client: AppFactory, clock: FakeClock) -> None:
    client = make_client()
    # threshold 3, 2 attempts per request -> open after the second request
    chat(client, "broken")
    chat(client, "broken")
    ready = client.get("/readyz")
    assert ready.status_code == 503
    assert ready.json()["providers"]["flaky"] == "open"
    assert ready.json()["routes"]["simple"] is True

    metrics = client.get("/metrics").text
    assert 'llmgw_circuit_breaker_state{provider="flaky"} 2.0' in metrics

    # failover route now skips the open provider without calling it
    resp = chat(client, "failover")
    assert resp.status_code == 200
    assert resp.headers["X-Gateway-Attempts"] == "1"

    clock.advance(31)
    assert client.get("/readyz").status_code == 200


def test_access_log_and_request_id(client: TestClient, caplog) -> None:  # type: ignore[no-untyped-def]
    caplog.set_level("INFO", logger="llm_gateway.access")
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "simple", "messages": [{"role": "user", "content": "secret prompt"}]},
        headers={"Authorization": "Bearer sk-test-a", "X-Request-ID": "abc-123"},
    )
    assert resp.headers["X-Request-ID"] == "abc-123"
    records = [r for r in caplog.records if r.name == "llm_gateway.access"]
    assert len(records) == 1
    record = records[0]
    assert record.__dict__["client"] == "team-a"
    assert record.__dict__["status"] == 200
    assert record.__dict__["route"] == "/v1/chat/completions"
    assert "sk-test-a" not in caplog.text
    assert "secret prompt" not in caplog.text


def test_invalid_request_id_replaced(client: TestClient) -> None:
    resp = client.get("/healthz", headers={"X-Request-ID": "bad id with spaces"})
    assert resp.headers["X-Request-ID"] != "bad id with spaces"
    assert len(resp.headers["X-Request-ID"]) == 32


def test_metrics_endpoint(client: TestClient) -> None:
    chat(client)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert (
        'llmgw_http_requests_total{method="POST",route="/v1/chat/completions",status="200"} 1.0'
        in resp.text
    )
    assert "llmgw_build_info" in resp.text
