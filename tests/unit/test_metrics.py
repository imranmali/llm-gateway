from __future__ import annotations

import pytest
from prometheus_client import REGISTRY, CollectorRegistry

from llm_gateway import __version__
from llm_gateway.events import CircuitState, GatewayObserver
from llm_gateway.observability import MetricsObserver


@pytest.fixture
def obs() -> MetricsObserver:
    return MetricsObserver()


def test_uses_private_registry_by_default(obs: MetricsObserver) -> None:
    assert obs.registry is not REGISTRY
    assert isinstance(obs.registry, CollectorRegistry)


def test_accepts_explicit_registry() -> None:
    registry = CollectorRegistry()
    assert MetricsObserver(registry).registry is registry


def test_instances_do_not_conflict() -> None:
    a, b = MetricsObserver(), MetricsObserver()
    a.on_fallback(from_provider="x", to_provider="y")
    labels = {"from_provider": "x", "to_provider": "y"}
    assert a.registry.get_sample_value("llmgw_fallbacks_total", labels) == 1
    assert b.registry.get_sample_value("llmgw_fallbacks_total", labels) is None


def test_build_info(obs: MetricsObserver) -> None:
    assert obs.registry.get_sample_value("llmgw_build_info", {"version": __version__}) == 1


def test_observe_http(obs: MetricsObserver) -> None:
    obs.observe_http(method="POST", route="/v1/chat/completions", status=200, duration_s=0.3)
    obs.observe_http(method="POST", route="/v1/chat/completions", status=200, duration_s=0.7)
    reg = obs.registry
    assert (
        reg.get_sample_value(
            "llmgw_http_requests_total",
            {"method": "POST", "route": "/v1/chat/completions", "status": "200"},
        )
        == 2
    )
    hist = {"method": "POST", "route": "/v1/chat/completions"}
    assert reg.get_sample_value("llmgw_http_request_duration_seconds_count", hist) == 2
    assert reg.get_sample_value("llmgw_http_request_duration_seconds_sum", hist) == pytest.approx(
        1.0
    )
    assert (
        reg.get_sample_value("llmgw_http_request_duration_seconds_bucket", {**hist, "le": "0.5"})
        == 1
    )


def test_provider_attempt(obs: MetricsObserver) -> None:
    obs.on_provider_attempt(provider="p", model="m", outcome="success", duration_s=1.5)
    obs.on_provider_attempt(provider="p", model="m", outcome="timeout", duration_s=30.0)
    reg = obs.registry
    for outcome in ("success", "timeout"):
        labels = {"provider": "p", "model": "m", "outcome": outcome}
        assert reg.get_sample_value("llmgw_provider_requests_total", labels) == 1
    hist = {"provider": "p", "model": "m"}
    assert reg.get_sample_value("llmgw_provider_request_duration_seconds_count", hist) == 2
    assert reg.get_sample_value("llmgw_provider_request_duration_seconds_sum", hist) == 31.5


def test_skipped_attempt_counts_but_not_timed(obs: MetricsObserver) -> None:
    obs.on_provider_attempt(provider="p", model="m", outcome="skipped", duration_s=0.0)
    reg = obs.registry
    labels = {"provider": "p", "model": "m", "outcome": "skipped"}
    assert reg.get_sample_value("llmgw_provider_requests_total", labels) == 1
    hist = {"provider": "p", "model": "m"}
    assert reg.get_sample_value("llmgw_provider_request_duration_seconds_count", hist) is None


def test_tokens(obs: MetricsObserver) -> None:
    obs.on_tokens(provider="p", model="m", prompt_tokens=10, completion_tokens=5)
    obs.on_tokens(provider="p", model="m", prompt_tokens=1, completion_tokens=0)
    reg = obs.registry
    base = {"provider": "p", "model": "m"}
    assert reg.get_sample_value("llmgw_tokens_total", {**base, "type": "prompt"}) == 11
    assert reg.get_sample_value("llmgw_tokens_total", {**base, "type": "completion"}) == 5


def test_zero_tokens_do_not_error(obs: MetricsObserver) -> None:
    obs.on_tokens(provider="p", model="m", prompt_tokens=0, completion_tokens=0)


def test_fallback(obs: MetricsObserver) -> None:
    obs.on_fallback(from_provider="a", to_provider="b")
    labels = {"from_provider": "a", "to_provider": "b"}
    assert obs.registry.get_sample_value("llmgw_fallbacks_total", labels) == 1


def test_rate_limited(obs: MetricsObserver) -> None:
    obs.on_rate_limited(client="team-a", limit_type="requests")
    obs.on_rate_limited(client="team-a", limit_type="tokens")
    obs.on_rate_limited(client="team-a", limit_type="requests")
    reg = obs.registry
    assert (
        reg.get_sample_value("llmgw_rate_limited_total", {"client": "team-a", "limit": "requests"})
        == 2
    )
    assert (
        reg.get_sample_value("llmgw_rate_limited_total", {"client": "team-a", "limit": "tokens"})
        == 1
    )


@pytest.mark.parametrize(("state", "value"), [("closed", 0.0), ("half_open", 1.0), ("open", 2.0)])
def test_circuit_state(obs: MetricsObserver, state: CircuitState, value: float) -> None:
    obs.on_circuit_state_change(provider="p", state=state)
    assert obs.registry.get_sample_value("llmgw_circuit_breaker_state", {"provider": "p"}) == value


def test_set_circuit_state_overwrites(obs: MetricsObserver) -> None:
    obs.set_circuit_state("p", "open")
    obs.set_circuit_state("p", "closed")
    assert obs.registry.get_sample_value("llmgw_circuit_breaker_state", {"provider": "p"}) == 0


def test_render(obs: MetricsObserver) -> None:
    obs.on_fallback(from_provider="a", to_provider="b")
    obs.observe_http(method="GET", route="/healthz", status=200, duration_s=0.001)
    body, content_type = obs.render()
    assert content_type.startswith("text/plain")
    text = body.decode()
    for name in (
        "llmgw_http_requests_total",
        "llmgw_http_request_duration_seconds_bucket",
        "llmgw_fallbacks_total",
        "llmgw_build_info",
    ):
        assert name in text


def test_satisfies_gateway_observer_protocol() -> None:
    observer: GatewayObserver = MetricsObserver()
    observer.on_provider_attempt(provider="p", model="m", outcome="error", duration_s=0.1)
    observer.on_fallback(from_provider="p", to_provider="q")
    observer.on_circuit_state_change(provider="p", state="open")
    observer.on_tokens(provider="q", model="m", prompt_tokens=1, completion_tokens=1)
    observer.on_rate_limited(client="c", limit_type="tokens")
