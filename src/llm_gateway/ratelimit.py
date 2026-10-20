"""Per-client request and token rate limiting with lazily refilled token buckets."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from llm_gateway.config import RateLimitConfig
from llm_gateway.events import GatewayObserver, LimitType

Clock = Callable[[], float]
"""Returns monotonic seconds."""

# Absorbs float noise from continuous refill (e.g. 0.9999999999999999 instead of 1.0).
_EPSILON = 1e-9


class TokenBucket:
    """Continuously refilling bucket; ``consume`` may drive it negative ("debt")."""

    def __init__(self, capacity: float, refill_per_s: float, clock: Clock = time.monotonic) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_per_s < 0:
            raise ValueError("refill_per_s must not be negative")
        self.capacity = capacity
        self.refill_per_s = refill_per_s
        self._clock = clock
        self._tokens = capacity
        self._updated = clock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._updated = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_s)

    def available(self) -> float:
        """Current balance after refilling; never above capacity, negative while in debt."""
        self._refill()
        return self._tokens

    def try_consume(self, amount: float = 1.0) -> bool:
        """Consume ``amount`` only if that much is available."""
        if self.available() + _EPSILON < amount:
            return False
        self._tokens -= amount
        return True

    def consume(self, amount: float) -> None:
        """Consume ``amount`` unconditionally, possibly going into debt."""
        self._refill()
        self._tokens -= amount

    def seconds_until(self, amount: float) -> float:
        """Seconds until ``amount`` is available (``0`` if it already is, ``inf`` if never)."""
        missing = amount - self.available()
        if missing <= _EPSILON:
            return 0.0
        if self.refill_per_s == 0:
            return math.inf
        return missing / self.refill_per_s


@dataclass(frozen=True)
class RateLimitDecision:
    """Outcome of a rate-limit check. Limits of ``0`` mean unlimited (not reported)."""

    allowed: bool
    limit_type: LimitType | None
    retry_after_s: int
    request_limit: int
    request_remaining: int
    token_limit: int
    token_remaining: int

    def headers(self) -> dict[str, str]:
        """Rate-limit response headers for the enforced limits, plus ``Retry-After``."""
        out: dict[str, str] = {}
        if self.request_limit:
            out["X-RateLimit-Limit-Requests"] = str(self.request_limit)
            out["X-RateLimit-Remaining-Requests"] = str(self.request_remaining)
        if self.token_limit:
            out["X-RateLimit-Limit-Tokens"] = str(self.token_limit)
            out["X-RateLimit-Remaining-Tokens"] = str(self.token_remaining)
        if not self.allowed:
            out["Retry-After"] = str(self.retry_after_s)
        return out


class RateLimiter(Protocol):
    """Admission control per client name."""

    def check(self, client: str, limits: RateLimitConfig) -> RateLimitDecision: ...

    def record_usage(self, client: str, limits: RateLimitConfig, tokens: int) -> None: ...


@dataclass
class _ClientBuckets:
    requests: TokenBucket | None = field(default=None)
    tokens: TokenBucket | None = field(default=None)


def _remaining(bucket: TokenBucket | None) -> int:
    return 0 if bucket is None else math.floor(max(0.0, bucket.available()))


def _retry_after(seconds: float) -> int:
    return max(1, math.ceil(seconds - _EPSILON))


class InMemoryRateLimiter:
    """Process-local :class:`RateLimiter` keeping one pair of buckets per client.

    Requests cost 1 each from a bucket of ``requests_per_minute``. A request is admitted
    on the token dimension while the token bucket is positive; actual usage is deducted
    afterwards via :meth:`record_usage` and may leave the bucket in debt. All mutations are
    synchronous, so no lock is needed within a single event loop.
    """

    def __init__(
        self, *, clock: Clock = time.monotonic, observer: GatewayObserver | None = None
    ) -> None:
        self._clock = clock
        self._observer = observer
        self._clients: dict[str, _ClientBuckets] = {}

    def _bucket(self, current: TokenBucket | None, per_minute: int) -> TokenBucket | None:
        """Return the bucket for ``per_minute``, rebuilding it if the limit changed."""
        if per_minute == 0:
            return None
        if current is not None and current.capacity == per_minute:
            return current
        return TokenBucket(per_minute, per_minute / 60, self._clock)

    def _buckets(self, client: str, limits: RateLimitConfig) -> _ClientBuckets:
        state = self._clients.setdefault(client, _ClientBuckets())
        state.requests = self._bucket(state.requests, limits.requests_per_minute)
        state.tokens = self._bucket(state.tokens, limits.tokens_per_minute)
        return state

    def check(self, client: str, limits: RateLimitConfig) -> RateLimitDecision:
        """Admit or reject one request; consumes a request slot only when admitted.

        ``retry_after_s`` covers every exhausted dimension: one whole request for the
        request bucket, and one whole token (so the balance is strictly positive and at
        least ``1`` is reported as remaining) for the token bucket.
        """
        state = self._buckets(client, limits)
        waits: dict[LimitType, float] = {}
        if state.requests is not None and (wait := state.requests.seconds_until(1.0)) > 0:
            waits["requests"] = wait
        if state.tokens is not None and state.tokens.available() <= 0:
            waits["tokens"] = state.tokens.seconds_until(1.0)

        limit_type: LimitType | None = next(iter(waits), None)
        if limit_type is None:
            if state.requests is not None:
                state.requests.consume(1.0)
            retry_after_s = 0
        else:
            retry_after_s = _retry_after(max(waits.values()))
            if self._observer is not None:
                self._observer.on_rate_limited(client=client, limit_type=limit_type)

        return RateLimitDecision(
            allowed=limit_type is None,
            limit_type=limit_type,
            retry_after_s=retry_after_s,
            request_limit=limits.requests_per_minute,
            request_remaining=_remaining(state.requests),
            token_limit=limits.tokens_per_minute,
            token_remaining=_remaining(state.tokens),
        )

    def record_usage(self, client: str, limits: RateLimitConfig, tokens: int) -> None:
        """Deduct actual token usage after a completion; may leave the bucket in debt."""
        bucket = self._buckets(client, limits).tokens
        if bucket is not None and tokens > 0:
            bucket.consume(tokens)
