"""Prometheus implementation of :class:`~llm_gateway.events.GatewayObserver`."""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from llm_gateway import __version__
from llm_gateway.events import AttemptOutcome, CircuitState, LimitType

LATENCY_BUCKETS: tuple[float, ...] = (
    0.01,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
)

CIRCUIT_STATE_VALUES: dict[CircuitState, int] = {"closed": 0, "half_open": 1, "open": 2}


class MetricsObserver:
    """Records HTTP and gateway domain events into a dedicated Prometheus registry."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry if registry is not None else CollectorRegistry()
        reg = self.registry
        # Counter names omit ``_total``; prometheus_client appends it on exposition.
        self._http_requests = Counter(
            "llmgw_http_requests",
            "HTTP requests handled by the gateway.",
            ["method", "route", "status"],
            registry=reg,
        )
        self._http_duration = Histogram(
            "llmgw_http_request_duration_seconds",
            "HTTP request latency in seconds.",
            ["method", "route"],
            buckets=LATENCY_BUCKETS,
            registry=reg,
        )
        self._provider_requests = Counter(
            "llmgw_provider_requests",
            "Upstream provider attempts by outcome.",
            ["provider", "model", "outcome"],
            registry=reg,
        )
        self._provider_duration = Histogram(
            "llmgw_provider_request_duration_seconds",
            "Upstream provider attempt latency in seconds.",
            ["provider", "model"],
            buckets=LATENCY_BUCKETS,
            registry=reg,
        )
        self._tokens = Counter(
            "llmgw_tokens",
            "Tokens reported by upstream providers.",
            ["provider", "model", "type"],
            registry=reg,
        )
        self._fallbacks = Counter(
            "llmgw_fallbacks",
            "Fail-overs from one provider to the next.",
            ["from_provider", "to_provider"],
            registry=reg,
        )
        self._rate_limited = Counter(
            "llmgw_rate_limited",
            "Requests rejected by the rate limiter.",
            ["client", "limit"],
            registry=reg,
        )
        self._circuit_state = Gauge(
            "llmgw_circuit_breaker_state",
            "Circuit breaker state per provider (0 closed, 1 half-open, 2 open).",
            ["provider"],
            registry=reg,
        )
        self._build_info = Gauge(
            "llmgw_build_info",
            "Gateway build information.",
            ["version"],
            registry=reg,
        )
        self._build_info.labels(version=__version__).set(1)

    def observe_http(self, *, method: str, route: str, status: int, duration_s: float) -> None:
        """Record one completed HTTP request."""
        self._http_requests.labels(method=method, route=route, status=str(status)).inc()
        self._http_duration.labels(method=method, route=route).observe(duration_s)

    def set_circuit_state(self, provider: str, state: CircuitState) -> None:
        """Set the circuit breaker gauge for ``provider``."""
        self._circuit_state.labels(provider=provider).set(CIRCUIT_STATE_VALUES[state])

    def render(self) -> tuple[bytes, str]:
        """Return the exposition payload and its content type."""
        return generate_latest(self.registry), CONTENT_TYPE_LATEST

    # GatewayObserver ---------------------------------------------------------------

    def on_provider_attempt(
        self,
        *,
        provider: str,
        model: str,
        outcome: AttemptOutcome,
        duration_s: float,
    ) -> None:
        self._provider_requests.labels(provider=provider, model=model, outcome=outcome).inc()
        if outcome != "skipped":
            self._provider_duration.labels(provider=provider, model=model).observe(duration_s)

    def on_fallback(self, *, from_provider: str, to_provider: str) -> None:
        self._fallbacks.labels(from_provider=from_provider, to_provider=to_provider).inc()

    def on_circuit_state_change(self, *, provider: str, state: CircuitState) -> None:
        self.set_circuit_state(provider, state)

    def on_tokens(
        self, *, provider: str, model: str, prompt_tokens: int, completion_tokens: int
    ) -> None:
        for token_type, count in (("prompt", prompt_tokens), ("completion", completion_tokens)):
            if count > 0:
                self._tokens.labels(provider=provider, model=model, type=token_type).inc(count)

    def on_rate_limited(self, *, client: str, limit_type: LimitType) -> None:
        self._rate_limited.labels(client=client, limit=limit_type).inc()
