from llm_gateway.config import CircuitBreakerConfig
from llm_gateway.events import AttemptOutcome, CircuitState, LimitType
from llm_gateway.routing import CircuitBreaker


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingObserver:
    def __init__(self) -> None:
        self.states: list[tuple[str, CircuitState]] = []

    def on_provider_attempt(
        self, *, provider: str, model: str, outcome: AttemptOutcome, duration_s: float
    ) -> None:
        return None

    def on_fallback(self, *, from_provider: str, to_provider: str) -> None:
        return None

    def on_circuit_state_change(self, *, provider: str, state: CircuitState) -> None:
        self.states.append((provider, state))

    def on_tokens(
        self, *, provider: str, model: str, prompt_tokens: int, completion_tokens: int
    ) -> None:
        return None

    def on_rate_limited(self, *, client: str, limit_type: LimitType) -> None:
        return None


def _breaker(
    threshold: int = 3, recovery: float = 10.0, half_open: int = 1
) -> tuple[CircuitBreaker, FakeClock, RecordingObserver]:
    clock = FakeClock()
    obs = RecordingObserver()
    cfg = CircuitBreakerConfig(
        failure_threshold=threshold, recovery_timeout_s=recovery, half_open_max_calls=half_open
    )
    return CircuitBreaker("p", cfg, clock=clock, observer=obs), clock, obs


def test_starts_closed_and_allows() -> None:
    breaker, _, obs = _breaker()
    assert breaker.state == "closed"
    assert breaker.allow_request()
    assert obs.states == []


def test_opens_after_consecutive_failures() -> None:
    breaker, _, obs = _breaker(threshold=3)
    for _ in range(2):
        breaker.record_failure()
    assert breaker.state == "closed"
    assert breaker.consecutive_failures == 2
    breaker.record_failure()
    assert breaker.state == "open"
    assert not breaker.allow_request()
    assert obs.states == [("p", "open")]


def test_success_resets_consecutive_failures() -> None:
    breaker, _, _ = _breaker(threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    assert breaker.consecutive_failures == 0
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "closed"


def test_half_open_after_recovery_timeout() -> None:
    breaker, clock, obs = _breaker(threshold=1, recovery=10.0)
    breaker.record_failure()
    clock.advance(9.9)
    assert breaker.state == "open"
    assert not breaker.allow_request()
    clock.advance(0.1)
    assert breaker.state == "half_open"
    assert obs.states == [("p", "open"), ("p", "half_open")]


def test_half_open_limits_trial_calls() -> None:
    breaker, clock, _ = _breaker(threshold=1, recovery=1.0, half_open=2)
    breaker.record_failure()
    clock.advance(1.0)
    assert breaker.allow_request()
    assert breaker.allow_request()
    assert not breaker.allow_request()
    breaker.release()
    assert breaker.allow_request()


def test_half_open_success_closes() -> None:
    breaker, clock, obs = _breaker(threshold=1, recovery=1.0)
    breaker.record_failure()
    clock.advance(1.0)
    assert breaker.allow_request()
    breaker.record_success()
    assert breaker.state == "closed"
    assert breaker.allow_request()
    assert obs.states == [("p", "open"), ("p", "half_open"), ("p", "closed")]


def test_half_open_failure_reopens_and_restarts_timer() -> None:
    breaker, clock, obs = _breaker(threshold=1, recovery=5.0)
    breaker.record_failure()
    clock.advance(5.0)
    assert breaker.allow_request()
    breaker.record_failure()
    assert breaker.state == "open"
    clock.advance(4.0)
    assert breaker.state == "open"
    clock.advance(1.0)
    assert breaker.state == "half_open"
    assert [s for _, s in obs.states] == ["open", "half_open", "open", "half_open"]


def test_failure_while_open_is_ignored() -> None:
    breaker, clock, obs = _breaker(threshold=1, recovery=5.0)
    breaker.record_failure()
    clock.advance(3.0)
    breaker.record_failure()  # late result of a call started before the circuit opened
    clock.advance(2.0)
    assert breaker.state == "half_open"
    assert len(obs.states) == 2


def test_release_outside_half_open_is_noop() -> None:
    breaker = CircuitBreaker("p", CircuitBreakerConfig())
    breaker.release()
    assert breaker.state == "closed"
    assert breaker.allow_request()
