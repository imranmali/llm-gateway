from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from llm_gateway.app import create_app
from llm_gateway.config import GatewayConfig


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def no_sleep(_: float) -> None:
    return None


KEY_A = "sk-test-a"
KEY_B = "sk-test-b"


def base_config(**overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "clients": [
            {"name": "team-a", "key": KEY_A},
            {"name": "team-b", "key": KEY_B},
        ],
        "default_rate_limit": {"requests_per_minute": 100, "tokens_per_minute": 0},
        "providers": {
            "primary": {"type": "mock"},
            "secondary": {"type": "mock"},
            "flaky": {"type": "mock", "fail_times": -1, "fail_status": 503},
            "slow": {"type": "mock", "fail_times": -1, "fail_timeout": True},
            "picky": {"type": "mock", "fail_times": -1, "fail_status": 400},
        },
        "routes": [
            {"model": "simple", "targets": [{"provider": "primary", "model": "p-1"}]},
            {
                "model": "failover",
                "targets": [
                    {"provider": "flaky", "model": "f-1"},
                    {"provider": "secondary", "model": "s-1"},
                ],
            },
            {"model": "broken", "targets": [{"provider": "flaky", "model": "f-1"}]},
            {"model": "timeout", "targets": [{"provider": "slow", "model": "t-1"}]},
            {
                "model": "bad-request",
                "targets": [
                    {"provider": "picky", "model": "x-1"},
                    {"provider": "secondary", "model": "s-1"},
                ],
            },
        ],
        "routing": {
            "max_attempts_per_target": 2,
            "backoff_initial_s": 0.1,
            "backoff_max_s": 1.0,
            "circuit_breaker": {"failure_threshold": 3, "recovery_timeout_s": 30},
        },
    }
    cfg.update(overrides)
    return cfg


AppFactory = Callable[..., TestClient]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def make_client(clock: FakeClock) -> Iterator[AppFactory]:
    clients: list[TestClient] = []

    def factory(**overrides: Any) -> TestClient:
        config = GatewayConfig.from_mapping(base_config(**overrides))
        app = create_app(config, clock=clock, sleep=no_sleep, environ={})
        client = TestClient(app, raise_server_exceptions=False)
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client: AppFactory) -> TestClient:
    return make_client()


def chat(
    client: TestClient,
    model: str = "simple",
    *,
    key: str | None = KEY_A,
    content: str = "hello gateway",
    **extra: Any,
) -> Any:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    body = {"model": model, "messages": [{"role": "user", "content": content}], **extra}
    return client.post("/v1/chat/completions", json=body, headers=headers)
