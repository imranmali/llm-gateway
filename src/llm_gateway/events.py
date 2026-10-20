"""Observer protocol decoupling core components from telemetry backends."""

from __future__ import annotations

from typing import Literal, Protocol

AttemptOutcome = Literal["success", "error", "timeout", "client_error", "skipped"]
CircuitState = Literal["closed", "half_open", "open"]
LimitType = Literal["requests", "tokens"]


class GatewayObserver(Protocol):
    """Receives domain events from the router, circuit breakers and rate limiter."""

    def on_provider_attempt(
        self,
        *,
        provider: str,
        model: str,
        outcome: AttemptOutcome,
        duration_s: float,
    ) -> None: ...

    def on_fallback(self, *, from_provider: str, to_provider: str) -> None: ...

    def on_circuit_state_change(self, *, provider: str, state: CircuitState) -> None: ...

    def on_tokens(
        self, *, provider: str, model: str, prompt_tokens: int, completion_tokens: int
    ) -> None: ...

    def on_rate_limited(self, *, client: str, limit_type: LimitType) -> None: ...


class NullObserver:
    """Observer that ignores every event."""

    def on_provider_attempt(
        self,
        *,
        provider: str,
        model: str,
        outcome: AttemptOutcome,
        duration_s: float,
    ) -> None:
        return None

    def on_fallback(self, *, from_provider: str, to_provider: str) -> None:
        return None

    def on_circuit_state_change(self, *, provider: str, state: CircuitState) -> None:
        return None

    def on_tokens(
        self, *, provider: str, model: str, prompt_tokens: int, completion_tokens: int
    ) -> None:
        return None

    def on_rate_limited(self, *, client: str, limit_type: LimitType) -> None:
        return None
