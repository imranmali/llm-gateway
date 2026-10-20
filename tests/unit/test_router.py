import asyncio
import logging
import random
from typing import Any

import pytest

from llm_gateway.config import GatewayConfig
from llm_gateway.errors import (
    AllProvidersFailedError,
    ModelNotFoundError,
    ProviderError,
    UpstreamBadRequestError,
    UpstreamTimeoutError,
)
from llm_gateway.events import AttemptOutcome, CircuitState, LimitType
from llm_gateway.providers import MockProvider, Provider
from llm_gateway.routing import Router
from llm_gateway.schemas import (
    AssistantMessage,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    Usage,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


class RecordingObserver:
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []

    def on_provider_attempt(
        self, *, provider: str, model: str, outcome: AttemptOutcome, duration_s: float
    ) -> None:
        self.events.append(("attempt", provider, model, outcome, duration_s))

    def on_fallback(self, *, from_provider: str, to_provider: str) -> None:
        self.events.append(("fallback", from_provider, to_provider))

    def on_circuit_state_change(self, *, provider: str, state: CircuitState) -> None:
        self.events.append(("circuit", provider, state))

    def on_tokens(
        self, *, provider: str, model: str, prompt_tokens: int, completion_tokens: int
    ) -> None:
        self.events.append(("tokens", provider, model, prompt_tokens, completion_tokens))

    def on_rate_limited(self, *, client: str, limit_type: LimitType) -> None:
        self.events.append(("rate_limited", client, limit_type))

    def of(self, kind: str) -> list[tuple[Any, ...]]:
        return [e[1:] for e in self.events if e[0] == kind]

    def outcomes(self) -> list[tuple[str, str]]:
        return [(e[0], e[2]) for e in self.of("attempt")]


def _response(model: str) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        model=model,
        choices=[Choice(message=AssistantMessage(content="ok"))],
        usage=Usage.of(3, 2),
    )


class ScriptedProvider(Provider):
    """Raises or answers according to a script; the last step repeats."""

    def __init__(
        self,
        name: str,
        script: list[BaseException | None],
        *,
        clock: FakeClock | None = None,
        step_s: float = 0.0,
        gate: asyncio.Event | None = None,
    ) -> None:
        super().__init__(name)
        self.script = script
        self.clock = clock
        self.step_s = step_s
        self.gate = gate
        self.calls = 0
        self.closed = False

    async def chat(
        self, request: ChatCompletionRequest, upstream_model: str
    ) -> ChatCompletionResponse:
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.clock is not None:
            self.clock.advance(self.step_s)
        if item is not None:
            raise item
        return _response(upstream_model)

    async def aclose(self) -> None:
        self.closed = True


def make_config(
    routes: dict[str, list[tuple[str, str]]] | None = None,
    *,
    providers: tuple[str, ...] = ("a", "b"),
    max_attempts: int = 2,
    threshold: int = 5,
    recovery: float = 30.0,
    half_open: int = 1,
    backoff_initial: float = 0.2,
    backoff_max: float = 2.0,
) -> GatewayConfig:
    routes = routes or {"public": [("a", "a-model"), ("b", "b-model")]}
    return GatewayConfig.from_mapping(
        {
            "providers": {name: {"type": "mock"} for name in providers},
            "routes": [
                {"model": model, "targets": [{"provider": p, "model": m} for p, m in targets]}
                for model, targets in routes.items()
            ],
            "routing": {
                "max_attempts_per_target": max_attempts,
                "backoff_initial_s": backoff_initial,
                "backoff_max_s": backoff_max,
                "circuit_breaker": {
                    "failure_threshold": threshold,
                    "recovery_timeout_s": recovery,
                    "half_open_max_calls": half_open,
                },
            },
        }
    )


class Harness:
    def __init__(self, config: GatewayConfig, providers: dict[str, Provider]) -> None:
        self.clock = FakeClock()
        self.sleep = FakeSleep()
        self.observer = RecordingObserver()
        self.router = Router(
            config,
            providers,
            observer=self.observer,
            clock=self.clock,
            sleep=self.sleep,
            rng=random.Random(0),  # noqa: S311 - deterministic jitter
        )

    async def complete(self, model: str = "public") -> Any:
        request = ChatCompletionRequest(model=model, messages=[{"role": "user", "content": "hi"}])
        return await self.router.complete(request)


def _mocks(a: MockProvider | None = None, b: MockProvider | None = None) -> dict[str, Provider]:
    return {"a": a or MockProvider("a"), "b": b or MockProvider("b")}


async def test_primary_succeeds_secondary_not_called() -> None:
    a, b = MockProvider("a"), MockProvider("b")
    h = Harness(make_config(), _mocks(a, b))
    result = await h.complete()
    assert result.provider == "a"
    assert result.upstream_model == "a-model"
    assert result.attempts == 1
    assert result.response.model == "a-model"  # not rewritten by the router
    assert (a.calls, b.calls) == (1, 0)
    assert h.sleep.delays == []
    assert h.observer.of("attempt") == [("a", "a-model", "success", 0.0)]
    usage = result.response.usage
    assert h.observer.of("tokens") == [
        ("a", "a-model", usage.prompt_tokens, usage.completion_tokens)
    ]
    assert h.observer.of("fallback") == []


async def test_transient_error_recovered_on_same_target() -> None:
    a = MockProvider("a", fail_times=1, fail_status=503)
    h = Harness(make_config(), _mocks(a))
    result = await h.complete()
    assert result.provider == "a"
    assert result.attempts == 2
    assert len(h.sleep.delays) == 1
    assert 0.1 <= h.sleep.delays[0] <= 0.2
    assert h.observer.outcomes() == [("a", "error"), ("a", "success")]
    assert h.router.breakers["a"].consecutive_failures == 0


async def test_primary_down_secondary_serves() -> None:
    a, b = MockProvider("a", fail_times=-1, fail_status=500), MockProvider("b")
    h = Harness(make_config(), _mocks(a, b))
    result = await h.complete()
    assert result.provider == "b"
    assert result.upstream_model == "b-model"
    assert result.attempts == 3
    assert (a.calls, b.calls) == (2, 1)
    assert len(h.sleep.delays) == 1  # no sleep before failing over
    assert h.observer.of("fallback") == [("a", "b")]


@pytest.mark.parametrize("status", [400, 422])
async def test_client_error_not_retried(status: int) -> None:
    a, b = MockProvider("a", fail_times=-1, fail_status=status), MockProvider("b")
    h = Harness(make_config(threshold=1), _mocks(a, b))
    with pytest.raises(UpstreamBadRequestError) as exc:
        await h.complete()
    assert exc.value.status_code == 400
    assert exc.value.code == "upstream_bad_request"
    assert exc.value.message == f"mock failure ({status})"
    assert (a.calls, b.calls) == (1, 0)
    assert h.sleep.delays == []
    assert h.observer.outcomes() == [("a", "client_error")]
    assert h.router.breakers["a"].state == "closed"  # client errors are not failures


async def test_everything_down() -> None:
    a = MockProvider("a", fail_times=-1, fail_status=503)
    b = MockProvider("b", fail_times=-1, fail_status=429)
    h = Harness(make_config(), _mocks(a, b))
    with pytest.raises(AllProvidersFailedError) as exc:
        await h.complete()
    assert type(exc.value) is AllProvidersFailedError
    assert exc.value.status_code == 502
    assert exc.value.code == "all_providers_failed"
    assert exc.value.message == (
        "all providers failed for model 'public': "
        "a/a-model: status=503 mock failure (503) (2 attempts); "
        "b/b-model: status=429 mock failure (429) (2 attempts)"
    )
    assert (a.calls, b.calls) == (2, 2)
    assert len(h.sleep.delays) == 2


async def test_all_timeouts_yield_upstream_timeout() -> None:
    a = MockProvider("a", fail_times=-1, fail_timeout=True)
    b = MockProvider("b", fail_times=-1, fail_timeout=True)
    h = Harness(make_config(), _mocks(a, b))
    with pytest.raises(UpstreamTimeoutError) as exc:
        await h.complete()
    assert exc.value.status_code == 504
    assert exc.value.code == "upstream_timeout"
    assert "a/a-model: timeout mock timeout" in exc.value.message
    assert {o for _, o in h.observer.outcomes()} == {"timeout"}


async def test_mixed_timeout_and_error_is_all_failed() -> None:
    a = MockProvider("a", fail_times=-1, fail_timeout=True)
    b = MockProvider("b", fail_times=-1, fail_status=500)
    h = Harness(make_config(), _mocks(a, b))
    with pytest.raises(AllProvidersFailedError) as exc:
        await h.complete()
    assert type(exc.value) is AllProvidersFailedError


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_auth_errors_fail_over_without_retry(status: int) -> None:
    a, b = MockProvider("a", fail_times=-1, fail_status=status), MockProvider("b")
    h = Harness(make_config(max_attempts=3), _mocks(a, b))
    result = await h.complete()
    assert result.provider == "b"
    assert result.attempts == 2
    assert (a.calls, b.calls) == (1, 1)
    assert h.sleep.delays == []
    assert h.observer.outcomes() == [("a", "error"), ("b", "success")]
    assert h.observer.of("fallback") == [("a", "b")]


async def test_circuit_opens_and_skips_provider() -> None:
    a, b = MockProvider("a", fail_times=-1, fail_status=503), MockProvider("b")
    h = Harness(make_config(threshold=2), _mocks(a, b))
    await h.complete()
    assert h.router.breakers["a"].state == "open"
    assert ("a", "open") in h.observer.of("circuit")
    h.observer.events.clear()

    result = await h.complete()
    assert result.provider == "b"
    assert result.attempts == 1  # skipped targets don't count
    assert a.calls == 2
    assert h.observer.of("attempt")[0] == ("a", "a-model", "skipped", 0.0)
    assert h.observer.of("fallback") == [("a", "b")]


async def test_retry_stops_when_circuit_opens() -> None:
    a, b = MockProvider("a", fail_times=-1, fail_status=503), MockProvider("b")
    h = Harness(make_config(threshold=1, max_attempts=3), _mocks(a, b))
    result = await h.complete()
    assert result.provider == "b"
    assert a.calls == 1
    assert h.sleep.delays == []


async def test_circuit_recovers_after_trial_success() -> None:
    a, b = MockProvider("a", fail_times=-1, fail_status=503), MockProvider("b")
    h = Harness(make_config(threshold=2, recovery=30.0), _mocks(a, b))
    await h.complete()
    assert h.router.breakers["a"].state == "open"
    h.clock.advance(30.0)
    a.fail_times = 0  # provider healthy again
    result = await h.complete()
    assert result.provider == "a"
    assert h.router.breakers["a"].state == "closed"
    assert h.observer.of("circuit") == [("a", "open"), ("a", "half_open"), ("a", "closed")]
    assert (await h.complete()).provider == "a"


async def test_failed_trial_reopens_circuit() -> None:
    a, b = MockProvider("a", fail_times=-1, fail_status=503), MockProvider("b")
    h = Harness(make_config(threshold=2, recovery=30.0), _mocks(a, b))
    await h.complete()
    h.clock.advance(30.0)
    result = await h.complete()
    assert result.provider == "b"
    assert a.calls == 3  # one trial, no retry once re-opened
    assert h.router.breakers["a"].state == "open"
    await h.complete()
    assert a.calls == 3


async def test_half_open_admits_limited_trials() -> None:
    gate = asyncio.Event()
    a = ScriptedProvider("a", [ProviderError.from_status("a", 503, "down"), None])
    b = MockProvider("b")
    h = Harness(make_config(threshold=1, recovery=10.0, half_open=1), {"a": a, "b": b})
    await h.complete()
    assert h.router.breakers["a"].state == "open"
    h.clock.advance(10.0)

    a.gate = gate
    trial = asyncio.create_task(h.complete())
    await asyncio.sleep(0)
    assert a.calls == 2  # trial in flight
    concurrent = await h.complete()
    assert concurrent.provider == "b"  # second caller skipped: no trial slots left
    assert a.calls == 2
    gate.set()
    assert (await trial).provider == "a"
    assert h.router.breakers["a"].state == "closed"


async def test_cancelled_trial_releases_slot() -> None:
    gate = asyncio.Event()
    a = ScriptedProvider("a", [ProviderError.from_status("a", 503, "down"), None])
    h = Harness(make_config(threshold=1, recovery=10.0), {"a": a, "b": MockProvider("b")})
    await h.complete()
    h.clock.advance(10.0)
    a.gate = gate
    trial = asyncio.create_task(h.complete())
    await asyncio.sleep(0)
    assert not h.router.breakers["a"].allow_request()
    trial.cancel()
    with pytest.raises(asyncio.CancelledError):
        await trial
    assert h.router.breakers["a"].state == "half_open"
    assert h.router.breakers["a"].allow_request()


async def test_backoff_is_exponential_jittered_and_bounded() -> None:
    a = MockProvider("a", fail_times=-1, fail_status=503)
    cfg = make_config(
        {"solo": [("a", "a-model")]},
        max_attempts=5,
        threshold=100,
        backoff_initial=1.0,
        backoff_max=2.5,
    )
    h = Harness(cfg, _mocks(a))
    with pytest.raises(AllProvidersFailedError):
        await h.complete("solo")
    rng = random.Random(0)  # noqa: S311 - deterministic jitter
    expected = [min(2.5, 1.0 * 2**n) * rng.uniform(0.5, 1.0) for n in range(4)]
    assert h.sleep.delays == pytest.approx(expected)
    assert all(d <= 2.5 for d in h.sleep.delays)
    assert 0.5 <= h.sleep.delays[0] <= 1.0
    assert 1.0 <= h.sleep.delays[1] <= 2.0


async def test_retry_abandoned_when_breaker_rejects_after_backoff() -> None:
    a, b = MockProvider("a", fail_times=-1, fail_status=503), MockProvider("b")
    h = Harness(make_config(threshold=3, max_attempts=3), _mocks(a, b))

    async def opening_sleep(delay: float) -> None:
        # Concurrent traffic opens the circuit while this request is backing off.
        for _ in range(3):
            h.router.breakers["a"].record_failure()

    h.router._sleep = opening_sleep
    result = await h.complete()
    assert result.provider == "b"
    assert a.calls == 1


async def test_attempt_duration_measured_with_clock() -> None:
    clock = FakeClock()
    a = ScriptedProvider("a", [None], clock=clock, step_s=1.5)
    observer = RecordingObserver()
    router = Router(
        make_config(),
        {"a": a, "b": MockProvider("b")},
        observer=observer,
        clock=clock,
        sleep=FakeSleep(),
    )
    request = ChatCompletionRequest(model="public", messages=[{"role": "user", "content": "x"}])
    await router.complete(request)
    assert observer.of("attempt") == [("a", "a-model", "success", 1.5)]


async def test_unexpected_exception_is_retried_and_not_leaked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    a = ScriptedProvider("a", [RuntimeError("secret-ish detail"), None])
    h = Harness(make_config(), {"a": a, "b": MockProvider("b")})
    with caplog.at_level(logging.WARNING, logger="llm_gateway.routing"):
        result = await h.complete()
    assert result.provider == "a"
    assert result.attempts == 2
    assert h.observer.outcomes() == [("a", "error"), ("a", "success")]
    assert any("unexpected error" in r.getMessage() for r in caplog.records)


async def test_unexpected_exception_summary_has_type_only() -> None:
    a = ScriptedProvider("a", [RuntimeError("do-not-leak")])
    h = Harness(make_config({"solo": [("a", "a-model")]}), {"a": a, "b": MockProvider("b")})
    with pytest.raises(AllProvidersFailedError) as exc:
        await h.complete("solo")
    assert "unexpected error (RuntimeError)" in exc.value.message
    assert "do-not-leak" not in exc.value.message


async def test_all_targets_skipped() -> None:
    a = MockProvider("a", fail_times=-1, fail_status=503)
    b = MockProvider("b", fail_times=-1, fail_status=503)
    h = Harness(make_config(threshold=1), _mocks(a, b))
    with pytest.raises(AllProvidersFailedError):
        await h.complete()
    with pytest.raises(AllProvidersFailedError) as exc:
        await h.complete()
    assert type(exc.value) is AllProvidersFailedError
    assert exc.value.message == (
        "all providers failed for model 'public': a/a-model: circuit open; b/b-model: circuit open"
    )
    assert (a.calls, b.calls) == (1, 1)


async def test_unknown_model() -> None:
    h = Harness(make_config(), _mocks())
    with pytest.raises(ModelNotFoundError) as exc:
        await h.complete("nope")
    assert "'nope'" in exc.value.message
    assert "public" in exc.value.message


async def test_route_availability_and_states() -> None:
    cfg = make_config(
        {"public": [("a", "a-model"), ("b", "b-model")], "solo": [("a", "a-model")]},
        threshold=1,
    )
    a = MockProvider("a", fail_times=-1, fail_status=500)
    h = Harness(cfg, _mocks(a))
    assert h.router.provider_states() == {"a": "closed", "b": "closed"}
    assert h.router.is_route_available("solo")
    await h.complete("public")
    assert h.router.provider_states() == {"a": "open", "b": "closed"}
    assert not h.router.is_route_available("solo")
    assert h.router.is_route_available("public")
    assert not h.router.is_route_available("unknown")
    h.clock.advance(30.0)
    assert h.router.is_route_available("solo")
    assert h.router.provider_states()["a"] == "half_open"


def test_missing_provider_instance_rejected() -> None:
    with pytest.raises(ValueError, match="'b'"):
        Router(make_config(), {"a": MockProvider("a")})


async def test_aclose_closes_every_provider(caplog: pytest.LogCaptureFixture) -> None:
    class Broken(ScriptedProvider):
        async def aclose(self) -> None:
            raise RuntimeError("close failed")

    a = Broken("a", [None])
    b = ScriptedProvider("b", [None])
    router = Router(make_config(), {"a": a, "b": b})
    with caplog.at_level(logging.WARNING, logger="llm_gateway.routing"):
        await router.aclose()
    assert b.closed
    assert any("failed to close provider a" in r.getMessage() for r in caplog.records)


async def test_default_dependencies_work() -> None:
    router = Router(make_config(), _mocks())
    request = ChatCompletionRequest(model="public", messages=[{"role": "user", "content": "x"}])
    assert (await router.complete(request)).provider == "a"
