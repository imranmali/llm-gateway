"""Fallback routing: ordered targets, retries with jittered backoff and circuit breakers."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from llm_gateway.config import CircuitBreakerConfig, GatewayConfig, RouteTarget
from llm_gateway.errors import (
    AllProvidersFailedError,
    ModelNotFoundError,
    ProviderError,
    UpstreamBadRequestError,
    UpstreamTimeoutError,
)
from llm_gateway.events import AttemptOutcome, CircuitState, GatewayObserver, NullObserver
from llm_gateway.providers.base import Provider
from llm_gateway.schemas import ChatCompletionRequest, ChatCompletionResponse

logger = logging.getLogger(__name__)

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class CircuitBreaker:
    """Per-provider circuit breaker: closed -> open -> half_open -> closed/open.

    The open -> half_open transition happens lazily when :attr:`state` is read after
    ``recovery_timeout_s`` has elapsed. In half-open state at most
    ``half_open_max_calls`` trial requests are admitted concurrently.
    """

    def __init__(
        self,
        provider: str,
        config: CircuitBreakerConfig,
        *,
        clock: Clock = time.monotonic,
        observer: GatewayObserver | None = None,
    ) -> None:
        self.provider = provider
        self._config = config
        self._clock = clock
        self._observer: GatewayObserver = observer or NullObserver()
        self._state: CircuitState = "closed"
        self._failures = 0
        self._opened_at = 0.0
        self._trials_in_flight = 0

    @property
    def state(self) -> CircuitState:
        if (
            self._state == "open"
            and self._clock() - self._opened_at >= self._config.recovery_timeout_s
        ):
            self._transition("half_open")
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._failures

    def allow_request(self) -> bool:
        """Return whether a call may be made now (reserving a half-open trial slot)."""
        state = self.state
        if state == "closed":
            return True
        if state == "open":
            return False
        if self._trials_in_flight < self._config.half_open_max_calls:
            self._trials_in_flight += 1
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        if self._state == "half_open":
            self._transition("closed")

    def record_failure(self) -> None:
        state = self.state
        if state == "half_open":
            self._open()
        elif state == "closed":
            self._failures += 1
            if self._failures >= self._config.failure_threshold:
                self._open()

    def release(self) -> None:
        """Give back a half-open trial slot for a call that ended without an outcome."""
        if self._state == "half_open" and self._trials_in_flight > 0:
            self._trials_in_flight -= 1

    def _open(self) -> None:
        self._opened_at = self._clock()
        self._transition("open")

    def _transition(self, state: CircuitState) -> None:
        previous, self._state = self._state, state
        self._trials_in_flight = 0
        self._failures = 0
        log = logger.warning if state == "open" else logger.info
        log("circuit for provider %s: %s -> %s", self.provider, previous, state)
        self._observer.on_circuit_state_change(provider=self.provider, state=state)


@dataclass(frozen=True)
class RoutedResult:
    response: ChatCompletionResponse
    provider: str
    upstream_model: str
    attempts: int


@dataclass
class _Tally:
    """Bookkeeping for one routed request."""

    attempts: int = 0
    timeouts: int = 0
    notes: list[str] = field(default_factory=list)


def _describe(exc: ProviderError) -> str:
    parts = ["timeout"] if exc.timeout else []
    if exc.status_code is not None:
        parts.append(f"status={exc.status_code}")
    parts.append(exc.message)
    return " ".join(parts)


class Router:
    """Routes a public model name over its ordered targets with retry and failover."""

    def __init__(
        self,
        config: GatewayConfig,
        providers: Mapping[str, Provider],
        *,
        observer: GatewayObserver | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        missing = sorted(set(config.providers) - set(providers))
        if missing:
            raise ValueError(f"no provider instance for configured provider(s): {missing}")
        self._config = config
        self._providers = dict(providers)
        self._observer: GatewayObserver = observer or NullObserver()
        self._clock = clock
        self._sleep = sleep
        # Jitter only; not security sensitive.
        self._rng = rng or random.Random()  # noqa: S311
        self.breakers: dict[str, CircuitBreaker] = {
            name: CircuitBreaker(
                name, config.routing.circuit_breaker, clock=clock, observer=self._observer
            )
            for name in config.providers
        }

    async def complete(self, request: ChatCompletionRequest) -> RoutedResult:
        """Serve ``request`` from the first healthy target of its route."""
        route = self._config.route_for(request.model)
        if route is None:
            available = ", ".join(sorted(r.model for r in self._config.routes))
            raise ModelNotFoundError(
                f"model {request.model!r} not found; available models: {available}"
            )

        tally = _Tally()
        targets = route.targets
        for index, target in enumerate(targets):
            response = await self._try_target(request, target, tally)
            if response is not None:
                return RoutedResult(
                    response=response,
                    provider=target.provider,
                    upstream_model=target.model,
                    attempts=tally.attempts,
                )
            if index + 1 < len(targets):
                self._observer.on_fallback(
                    from_provider=target.provider, to_provider=targets[index + 1].provider
                )

        message = f"all providers failed for model {request.model!r}: " + "; ".join(tally.notes)
        if tally.attempts and tally.timeouts == tally.attempts:
            raise UpstreamTimeoutError(message)
        raise AllProvidersFailedError(message)

    async def _try_target(
        self, request: ChatCompletionRequest, target: RouteTarget, tally: _Tally
    ) -> ChatCompletionResponse | None:
        """Attempt one target; return its response or ``None`` after recording why it failed."""
        label = f"{target.provider}/{target.model}"
        breaker = self.breakers[target.provider]
        provider = self._providers[target.provider]
        max_attempts = self._config.routing.max_attempts_per_target

        if not breaker.allow_request():
            self._attempt(target, "skipped", 0.0)
            tally.notes.append(f"{label}: circuit open")
            return None

        made = 0
        last = ""
        while True:
            made += 1
            tally.attempts += 1
            start = self._clock()
            try:
                response = await provider.chat(request, target.model)
            except ProviderError as exc:
                duration = self._clock() - start
                if exc.client_error:
                    breaker.record_success()
                    self._attempt(target, "client_error", duration)
                    raise UpstreamBadRequestError(exc.message) from exc
                breaker.record_failure()
                if exc.timeout:
                    tally.timeouts += 1
                self._attempt(target, "timeout" if exc.timeout else "error", duration)
                last = _describe(exc)
                retryable = exc.retryable
            except asyncio.CancelledError:
                breaker.release()
                raise
            except Exception as exc:
                duration = self._clock() - start
                breaker.record_failure()
                self._attempt(target, "error", duration)
                last = f"unexpected error ({type(exc).__name__})"
                retryable = True
                logger.warning("unexpected error from provider %s", target.provider, exc_info=True)
            else:
                breaker.record_success()
                self._attempt(target, "success", self._clock() - start)
                self._observer.on_tokens(
                    provider=target.provider,
                    model=target.model,
                    prompt_tokens=response.usage.prompt_tokens,
                    completion_tokens=response.usage.completion_tokens,
                )
                return response

            logger.warning("attempt %d/%d on %s failed: %s", made, max_attempts, label, last[:200])
            if not retryable or made >= max_attempts or breaker.state == "open":
                break
            await self._sleep(self._backoff(made - 1))
            if not breaker.allow_request():
                break

        tally.notes.append(f"{label}: {last} ({made} attempt{'s' if made != 1 else ''})")
        return None

    def _backoff(self, retry: int) -> float:
        routing = self._config.routing
        base = min(routing.backoff_max_s, routing.backoff_initial_s * 2.0**retry)
        return base * self._rng.uniform(0.5, 1.0)

    def _attempt(self, target: RouteTarget, outcome: AttemptOutcome, duration_s: float) -> None:
        self._observer.on_provider_attempt(
            provider=target.provider, model=target.model, outcome=outcome, duration_s=duration_s
        )

    def is_route_available(self, model: str) -> bool:
        """Whether ``model`` has at least one target whose circuit is not open."""
        route = self._config.route_for(model)
        if route is None:
            return False
        return any(self.breakers[t.provider].state != "open" for t in route.targets)

    def provider_states(self) -> dict[str, CircuitState]:
        return {name: breaker.state for name, breaker in self.breakers.items()}

    async def aclose(self) -> None:
        """Close every provider, continuing past individual failures."""
        for name, provider in self._providers.items():
            try:
                await provider.aclose()
            except Exception:
                logger.warning("failed to close provider %s", name, exc_info=True)
