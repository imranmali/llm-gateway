"""Gateway configuration: process settings from env and routing config from YAML."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RateLimitConfig(_Strict):
    """Per-client limits. ``0`` means unlimited."""

    requests_per_minute: int = Field(default=0, ge=0)
    tokens_per_minute: int = Field(default=0, ge=0)


class ClientKeyConfig(_Strict):
    """A client allowed to call the gateway.

    Exactly one of ``key`` (literal, for local development) or ``key_env`` (name of an
    environment variable holding the key) must be set.
    """

    name: str = Field(min_length=1)
    key: str | None = None
    key_env: str | None = None
    rate_limit: RateLimitConfig | None = None

    @model_validator(mode="after")
    def _one_key_source(self) -> ClientKeyConfig:
        if (self.key is None) == (self.key_env is None):
            raise ValueError(f"client {self.name!r}: set exactly one of 'key' or 'key_env'")
        return self

    def resolve_key(self, environ: dict[str, str] | None = None) -> str:
        env = os.environ if environ is None else environ
        if self.key is not None:
            return self.key
        assert self.key_env is not None
        value = env.get(self.key_env)
        if not value:
            raise ValueError(f"client {self.name!r}: environment variable {self.key_env} is unset")
        return value


class MockProviderConfig(_Strict):
    type: Literal["mock"]
    latency_s: float = Field(default=0.0, ge=0)
    # Deterministic failure injection: fail the first ``fail_times`` calls (``-1`` = always).
    fail_times: int = Field(default=0, ge=-1)
    fail_status: int = Field(default=503, ge=400, le=599)
    fail_timeout: bool = False


class HttpProviderConfig(_Strict):
    type: Literal["openai", "anthropic"]
    base_url: str | None = None
    api_key_env: str | None = None
    timeout_s: float = Field(default=30.0, gt=0)
    headers: dict[str, str] = Field(default_factory=dict)


ProviderConfig = Annotated[MockProviderConfig | HttpProviderConfig, Field(discriminator="type")]


class RouteTarget(_Strict):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)


class RouteConfig(_Strict):
    model: str = Field(min_length=1, description="Public model name clients request")
    targets: list[RouteTarget] = Field(min_length=1)


class CircuitBreakerConfig(_Strict):
    failure_threshold: int = Field(default=5, ge=1)
    recovery_timeout_s: float = Field(default=30.0, gt=0)
    half_open_max_calls: int = Field(default=1, ge=1)


class RoutingConfig(_Strict):
    max_attempts_per_target: int = Field(default=2, ge=1)
    backoff_initial_s: float = Field(default=0.2, ge=0)
    backoff_max_s: float = Field(default=2.0, ge=0)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)


class GatewayConfig(_Strict):
    """Top-level YAML configuration."""

    clients: list[ClientKeyConfig] = Field(default_factory=list)
    default_rate_limit: RateLimitConfig = Field(
        default_factory=lambda: RateLimitConfig(requests_per_minute=60)
    )
    providers: dict[str, ProviderConfig]
    routes: list[RouteConfig] = Field(min_length=1)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)

    @model_validator(mode="after")
    def _check_references(self) -> GatewayConfig:
        seen: set[str] = set()
        for route in self.routes:
            if route.model in seen:
                raise ValueError(f"duplicate route for model {route.model!r}")
            seen.add(route.model)
            for target in route.targets:
                if target.provider not in self.providers:
                    raise ValueError(
                        f"route {route.model!r} references unknown provider {target.provider!r}"
                    )
        names = [c.name for c in self.clients]
        if len(names) != len(set(names)):
            raise ValueError("client names must be unique")
        return self

    def route_for(self, model: str) -> RouteConfig | None:
        return next((r for r in self.routes if r.model == model), None)

    def limits_for(self, client: ClientKeyConfig | None) -> RateLimitConfig:
        if client is not None and client.rate_limit is not None:
            return client.rate_limit
        return self.default_rate_limit

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> GatewayConfig:
        return cls.model_validate(data)

    @classmethod
    def from_yaml(cls, path: str | Path) -> GatewayConfig:
        with Path(path).open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path}: top-level YAML must be a mapping")
        return cls.from_mapping(data)

    @classmethod
    def default(cls) -> GatewayConfig:
        """Mock-only configuration used when no config file exists."""
        return cls.from_mapping(
            {
                "providers": {"mock": {"type": "mock"}},
                "routes": [
                    {"model": "mock-model", "targets": [{"provider": "mock", "model": "mock-1"}]}
                ],
            }
        )


class Settings(BaseSettings):
    """Process-level settings read from ``LLMGW_*`` environment variables."""

    model_config = SettingsConfigDict(env_prefix="LLMGW_", extra="ignore")

    config: Path = Path("config.yaml")
    host: str = "0.0.0.0"  # noqa: S104 - container default; override with LLMGW_HOST
    port: int = 8080
    log_level: str = "INFO"

    def load_gateway_config(self) -> GatewayConfig:
        if self.config.is_file():
            return GatewayConfig.from_yaml(self.config)
        return GatewayConfig.default()
