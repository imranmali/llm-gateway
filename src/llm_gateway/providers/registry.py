"""Build provider adapters from configuration, resolving secrets from the environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TypedDict

import httpx

from llm_gateway.config import GatewayConfig, HttpProviderConfig, MockProviderConfig
from llm_gateway.providers.anthropic import AnthropicProvider
from llm_gateway.providers.base import Provider
from llm_gateway.providers.mock import MockProvider
from llm_gateway.providers.openai import OpenAIProvider


class _BaseUrl(TypedDict, total=False):
    base_url: str


def _api_key(name: str, config: HttpProviderConfig, environ: Mapping[str, str]) -> str | None:
    if config.api_key_env is None:
        return None
    value = environ.get(config.api_key_env)
    if not value:
        raise ValueError(
            f"provider {name!r}: environment variable {config.api_key_env} is unset or empty"
        )
    return value


def build_providers(
    config: GatewayConfig,
    *,
    environ: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Provider]:
    """Instantiate one provider per configured name; fail fast on missing secrets."""
    env: Mapping[str, str] = os.environ if environ is None else environ
    # Resolve every secret before opening any HTTP client so a missing key leaks nothing.
    keys = {
        name: _api_key(name, cfg, env)
        for name, cfg in config.providers.items()
        if isinstance(cfg, HttpProviderConfig)
    }
    providers: dict[str, Provider] = {}
    for name, cfg in config.providers.items():
        if isinstance(cfg, MockProviderConfig):
            providers[name] = MockProvider(
                name,
                latency_s=cfg.latency_s,
                fail_times=cfg.fail_times,
                fail_status=cfg.fail_status,
                fail_timeout=cfg.fail_timeout,
            )
            continue
        factory: type[OpenAIProvider] | type[AnthropicProvider] = (
            OpenAIProvider if cfg.type == "openai" else AnthropicProvider
        )
        base_url: _BaseUrl = {"base_url": cfg.base_url} if cfg.base_url else {}
        providers[name] = factory(
            name,
            api_key=keys[name],
            timeout_s=cfg.timeout_s,
            headers=cfg.headers,
            transport=transport,
            **base_url,
        )
    return providers
