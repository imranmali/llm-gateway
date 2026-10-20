from typing import Any

import httpx
import pytest

from llm_gateway.config import GatewayConfig
from llm_gateway.providers import MockProvider
from llm_gateway.providers.anthropic import AnthropicProvider
from llm_gateway.providers.openai import OpenAIProvider
from llm_gateway.providers.registry import build_providers
from llm_gateway.schemas import ChatCompletionRequest

COMPLETION = {
    "model": "m",
    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
}


def _config(providers: dict[str, Any]) -> GatewayConfig:
    name = next(iter(providers))
    return GatewayConfig.from_mapping(
        {
            "providers": providers,
            "routes": [{"model": "m", "targets": [{"provider": name, "model": "u"}]}],
        }
    )


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
        return httpx.Response(200, json=COMPLETION)


def _req() -> ChatCompletionRequest:
    return ChatCompletionRequest(model="m", messages=[{"role": "user", "content": "hi"}])


def test_mock_provider_settings() -> None:
    cfg = _config(
        {
            "fake": {
                "type": "mock",
                "latency_s": 0.5,
                "fail_times": 2,
                "fail_status": 500,
                "fail_timeout": True,
            }
        }
    )
    provider = build_providers(cfg, environ={})["fake"]
    assert isinstance(provider, MockProvider)
    assert provider.name == "fake"
    assert (provider.latency_s, provider.fail_times) == (0.5, 2)
    assert (provider.fail_status, provider.fail_timeout) == (500, True)


async def test_http_providers_built_from_config_and_env() -> None:
    cfg = GatewayConfig.from_mapping(
        {
            "providers": {
                "oa": {
                    "type": "openai",
                    "api_key_env": "OA_KEY",
                    "base_url": "https://oa.example.test/v1",
                    "timeout_s": 5,
                    "headers": {"x-team": "core"},
                },
                "an": {"type": "anthropic", "api_key_env": "AN_KEY"},
                "local": {"type": "openai"},
            },
            "routes": [{"model": "m", "targets": [{"provider": "oa", "model": "u"}]}],
        }
    )
    recorder = Recorder()
    providers = build_providers(
        cfg,
        environ={"OA_KEY": "sk-oa", "AN_KEY": "sk-an"},
        transport=httpx.MockTransport(recorder),
    )
    oa, an, local = providers["oa"], providers["an"], providers["local"]
    assert isinstance(oa, OpenAIProvider)
    assert isinstance(an, AnthropicProvider)
    assert isinstance(local, OpenAIProvider)
    assert oa.base_url == "https://oa.example.test/v1"
    assert an.base_url == "https://api.anthropic.com"
    assert local.base_url == "https://api.openai.com/v1"
    assert oa._client.timeout.read == 5

    for provider in providers.values():
        await provider.chat(_req(), "u")
        await provider.aclose()
    oa_req, an_req, local_req = recorder.requests
    assert oa_req.headers["authorization"] == "Bearer sk-oa"
    assert oa_req.headers["x-team"] == "core"
    assert an_req.headers["x-api-key"] == "sk-an"
    assert "authorization" not in local_req.headers


@pytest.mark.parametrize("environ", [{}, {"OA_KEY": ""}])
def test_missing_api_key_fails_fast(environ: dict[str, str]) -> None:
    cfg = _config({"oa": {"type": "openai", "api_key_env": "OA_KEY"}})
    with pytest.raises(ValueError, match=r"'oa'.*OA_KEY"):
        build_providers(cfg, environ=environ)


def test_environ_defaults_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AN_KEY_FROM_OS", "sk-os")
    cfg = _config({"an": {"type": "anthropic", "api_key_env": "AN_KEY_FROM_OS"}})
    provider = build_providers(cfg)["an"]
    assert isinstance(provider, AnthropicProvider)
    assert provider._headers["x-api-key"] == "sk-os"
