import math

import pytest

from llm_gateway.config import RateLimitConfig
from llm_gateway.events import LimitType
from llm_gateway.ratelimit import (
    InMemoryRateLimiter,
    RateLimitDecision,
    RateLimiter,
    TokenBucket,
)


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingObserver:
    def __init__(self) -> None:
        self.rate_limited: list[tuple[str, LimitType]] = []

    def on_provider_attempt(self, **_: object) -> None:
        return None

    def on_fallback(self, **_: object) -> None:
        return None

    def on_circuit_state_change(self, **_: object) -> None:
        return None

    def on_tokens(self, **_: object) -> None:
        return None

    def on_rate_limited(self, *, client: str, limit_type: LimitType) -> None:
        self.rate_limited.append((client, limit_type))


def _limits(rpm: int = 0, tpm: int = 0) -> RateLimitConfig:
    return RateLimitConfig(requests_per_minute=rpm, tokens_per_minute=tpm)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def limiter(clock: FakeClock) -> InMemoryRateLimiter:
    return InMemoryRateLimiter(clock=clock)


# --- TokenBucket ---------------------------------------------------------------------------


def test_bucket_starts_full_and_consumes(clock: FakeClock) -> None:
    bucket = TokenBucket(3, 1.0, clock)
    assert bucket.capacity == 3
    assert bucket.available() == 3
    assert bucket.try_consume()
    assert bucket.try_consume(2)
    assert not bucket.try_consume()
    assert bucket.available() == 0


def test_bucket_refills_continuously_and_caps_at_capacity(clock: FakeClock) -> None:
    bucket = TokenBucket(10, 2.0, clock)
    bucket.consume(10)
    clock.advance(1.5)
    assert bucket.available() == pytest.approx(3.0)
    clock.advance(100)
    assert bucket.available() == 10


def test_bucket_debt_and_seconds_until(clock: FakeClock) -> None:
    bucket = TokenBucket(10, 2.0, clock)
    assert bucket.seconds_until(5) == 0
    bucket.consume(14)
    assert bucket.available() == pytest.approx(-4.0)
    assert not bucket.try_consume(0.5)
    assert bucket.seconds_until(1) == pytest.approx(2.5)
    clock.advance(2.5)
    assert bucket.try_consume(1)


def test_bucket_ignores_clock_going_backwards(clock: FakeClock) -> None:
    bucket = TokenBucket(2, 1.0, clock)
    bucket.consume(2)
    clock.advance(-5)
    assert bucket.available() == 0


def test_bucket_without_refill_never_recovers(clock: FakeClock) -> None:
    bucket = TokenBucket(1, 0.0, clock)
    bucket.consume(1)
    assert bucket.seconds_until(1) == math.inf


@pytest.mark.parametrize(("capacity", "refill"), [(0, 1.0), (-1, 1.0), (1, -0.1)])
def test_bucket_rejects_invalid_parameters(capacity: float, refill: float) -> None:
    with pytest.raises(ValueError, match="must"):
        TokenBucket(capacity, refill)


# --- Request limit -------------------------------------------------------------------------


def test_within_limit_both_requests_allowed(limiter: InMemoryRateLimiter) -> None:
    limits = _limits(rpm=2)
    assert limiter.check("a", limits).allowed
    assert limiter.check("a", limits).allowed


def test_limit_exceeded_rejects_with_requests_type(limiter: InMemoryRateLimiter) -> None:
    limits = _limits(rpm=2)
    limiter.check("a", limits)
    limiter.check("a", limits)
    decision = limiter.check("a", limits)
    assert not decision.allowed
    assert decision.limit_type == "requests"
    assert decision.request_remaining == 0
    # One request refills every 30 s at 2 requests/minute.
    assert decision.retry_after_s == 30


def test_refill_admits_next_request(limiter: InMemoryRateLimiter, clock: FakeClock) -> None:
    limits = _limits(rpm=2)
    limiter.check("a", limits)
    limiter.check("a", limits)
    assert not limiter.check("a", limits).allowed
    clock.advance(29)
    assert not limiter.check("a", limits).allowed
    clock.advance(1)
    decision = limiter.check("a", limits)
    assert decision.allowed
    assert decision.request_remaining == 0


def test_rejection_does_not_consume(limiter: InMemoryRateLimiter, clock: FakeClock) -> None:
    limits = _limits(rpm=1)
    limiter.check("a", limits)
    for _ in range(5):
        assert not limiter.check("a", limits).allowed
    clock.advance(60)
    assert limiter.check("a", limits).allowed


def test_retry_after_is_at_least_one_second(limiter: InMemoryRateLimiter) -> None:
    limits = _limits(rpm=6000)  # refills one request every 10 ms
    for _ in range(6000):
        assert limiter.check("a", limits).allowed
    decision = limiter.check("a", limits)
    assert not decision.allowed
    assert decision.retry_after_s == 1
    assert decision.headers()["Retry-After"] == "1"


# --- Token limit ---------------------------------------------------------------------------


def test_token_budget_exhausted_then_recovers(
    limiter: InMemoryRateLimiter, clock: FakeClock
) -> None:
    limits = _limits(tpm=600)  # 10 tokens/second
    first = limiter.check("a", limits)
    assert first.allowed
    assert first.token_remaining == 600
    limiter.record_usage("a", limits, 650)  # 50 tokens of debt

    decision = limiter.check("a", limits)
    assert not decision.allowed
    assert decision.limit_type == "tokens"
    assert decision.token_remaining == 0
    # Recover the 50-token debt plus one whole token at 10 tokens/s.
    assert decision.retry_after_s == math.ceil(51 / 10)

    clock.advance(5)
    assert not limiter.check("a", limits).allowed
    clock.advance(1)
    decision = limiter.check("a", limits)
    assert decision.allowed
    assert decision.token_remaining == 10


def test_request_admitted_while_token_bucket_positive(limiter: InMemoryRateLimiter) -> None:
    limits = _limits(tpm=100)
    limiter.record_usage("a", limits, 99)
    assert limiter.check("a", limits).allowed
    limiter.record_usage("a", limits, 1)
    assert not limiter.check("a", limits).allowed


def test_record_usage_ignores_zero_and_unlimited(limiter: InMemoryRateLimiter) -> None:
    limiter.record_usage("a", _limits(tpm=100), 0)
    assert limiter.check("a", _limits(tpm=100)).token_remaining == 100
    limiter.record_usage("b", _limits(), 10**9)
    assert limiter.check("b", _limits()).allowed


def test_requests_checked_before_tokens_and_retry_covers_both(
    limiter: InMemoryRateLimiter,
) -> None:
    limits = _limits(rpm=1, tpm=60)  # 1 request/min, 1 token/s
    assert limiter.check("a", limits).allowed
    limiter.record_usage("a", limits, 159)  # 99 tokens of debt -> 100 s to reach 1 token
    decision = limiter.check("a", limits)
    assert not decision.allowed
    assert decision.limit_type == "requests"
    assert decision.retry_after_s == 100


def test_tokens_exhausted_does_not_consume_request(
    limiter: InMemoryRateLimiter, clock: FakeClock
) -> None:
    limits = _limits(rpm=2, tpm=60)
    limiter.record_usage("a", limits, 60)
    decision = limiter.check("a", limits)
    assert decision.limit_type == "tokens"
    assert decision.request_remaining == 2
    clock.advance(1)
    decision = limiter.check("a", limits)
    assert decision.allowed
    assert decision.request_remaining == 1


# --- Isolation, unlimited, config changes, observer ---------------------------------------


def test_clients_are_isolated(limiter: InMemoryRateLimiter) -> None:
    limits = _limits(rpm=1, tpm=10)
    assert limiter.check("a", limits).allowed
    assert not limiter.check("a", limits).allowed
    limiter.record_usage("a", limits, 100)
    decision = limiter.check("b", limits)
    assert decision.allowed
    assert decision.token_remaining == 10


def test_zero_limits_mean_unlimited(limiter: InMemoryRateLimiter) -> None:
    limits = _limits()
    for _ in range(1000):
        decision = limiter.check("a", limits)
        assert decision.allowed
    limiter.record_usage("a", limits, 10**9)
    decision = limiter.check("a", limits)
    assert decision == RateLimitDecision(
        allowed=True,
        limit_type=None,
        retry_after_s=0,
        request_limit=0,
        request_remaining=0,
        token_limit=0,
        token_remaining=0,
    )
    assert decision.headers() == {}


def test_unlimited_dimension_never_rejects(limiter: InMemoryRateLimiter) -> None:
    limits = _limits(rpm=0, tpm=10)
    for _ in range(100):
        assert limiter.check("a", limits).allowed
    decision = limiter.check("a", _limits(rpm=1, tpm=0))
    limiter.record_usage("a", _limits(rpm=1, tpm=0), 10**6)
    assert decision.allowed
    assert not limiter.check("a", _limits(rpm=1, tpm=0)).allowed


def test_limit_change_rebuilds_bucket(limiter: InMemoryRateLimiter) -> None:
    assert limiter.check("a", _limits(rpm=1)).allowed
    assert not limiter.check("a", _limits(rpm=1)).allowed
    decision = limiter.check("a", _limits(rpm=5))
    assert decision.allowed
    assert decision.request_limit == 5
    assert decision.request_remaining == 4


def test_observer_notified_on_rejection_only(clock: FakeClock) -> None:
    observer = RecordingObserver()
    limiter = InMemoryRateLimiter(clock=clock, observer=observer)
    limits = _limits(rpm=1, tpm=10)
    limiter.check("a", limits)
    assert observer.rate_limited == []
    limiter.check("a", limits)
    clock.advance(60)
    limiter.record_usage("b", limits, 10)
    limiter.check("b", limits)
    assert observer.rate_limited == [("a", "requests"), ("b", "tokens")]


def test_satisfies_protocol(limiter: InMemoryRateLimiter) -> None:
    protocol_typed: RateLimiter = limiter
    assert protocol_typed.check("a", _limits()).allowed


# --- Headers -------------------------------------------------------------------------------


def test_first_request_headers_report_remaining(limiter: InMemoryRateLimiter) -> None:
    decision = limiter.check("a", _limits(rpm=5))
    assert decision.headers() == {
        "X-RateLimit-Limit-Requests": "5",
        "X-RateLimit-Remaining-Requests": "4",
    }


def test_headers_on_rejection_include_all_enforced_limits_and_retry_after(
    limiter: InMemoryRateLimiter,
) -> None:
    limits = _limits(rpm=1, tpm=1000)
    limiter.check("a", limits)
    limiter.record_usage("a", limits, 250)
    headers = limiter.check("a", limits).headers()
    assert headers == {
        "X-RateLimit-Limit-Requests": "1",
        "X-RateLimit-Remaining-Requests": "0",
        "X-RateLimit-Limit-Tokens": "1000",
        "X-RateLimit-Remaining-Tokens": "750",
        "Retry-After": "60",
    }
    assert int(headers["Retry-After"]) >= 1


def test_headers_token_only() -> None:
    decision = RateLimitDecision(
        allowed=True,
        limit_type=None,
        retry_after_s=0,
        request_limit=0,
        request_remaining=0,
        token_limit=100,
        token_remaining=42,
    )
    assert decision.headers() == {
        "X-RateLimit-Limit-Tokens": "100",
        "X-RateLimit-Remaining-Tokens": "42",
    }
