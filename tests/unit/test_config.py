from pathlib import Path

import pytest
from pydantic import ValidationError

from llm_gateway.config import (
    ClientKeyConfig,
    GatewayConfig,
    MockProviderConfig,
    RateLimitConfig,
    Settings,
)

BASE = {
    "providers": {"mock": {"type": "mock"}, "oa": {"type": "openai", "api_key_env": "X"}},
    "routes": [{"model": "m", "targets": [{"provider": "mock", "model": "m1"}]}],
}


def test_minimal_config_defaults() -> None:
    cfg = GatewayConfig.from_mapping(BASE)
    assert isinstance(cfg.providers["mock"], MockProviderConfig)
    assert cfg.routing.max_attempts_per_target == 2
    assert cfg.default_rate_limit.requests_per_minute == 60
    assert cfg.route_for("m") is not None
    assert cfg.route_for("nope") is None


def test_unknown_provider_reference_rejected() -> None:
    data = {**BASE, "routes": [{"model": "m", "targets": [{"provider": "ghost", "model": "x"}]}]}
    with pytest.raises(ValidationError, match="unknown provider"):
        GatewayConfig.from_mapping(data)


def test_duplicate_route_rejected() -> None:
    route = {"model": "m", "targets": [{"provider": "mock", "model": "m1"}]}
    with pytest.raises(ValidationError, match="duplicate route"):
        GatewayConfig.from_mapping({**BASE, "routes": [route, route]})


def test_empty_targets_rejected() -> None:
    with pytest.raises(ValidationError):
        GatewayConfig.from_mapping({**BASE, "routes": [{"model": "m", "targets": []}]})


def test_unknown_provider_type_rejected() -> None:
    with pytest.raises(ValidationError):
        GatewayConfig.from_mapping({**BASE, "providers": {"mock": {"type": "nope"}}})


def test_duplicate_client_names_rejected() -> None:
    clients = [{"name": "a", "key": "k1"}, {"name": "a", "key": "k2"}]
    with pytest.raises(ValidationError, match="unique"):
        GatewayConfig.from_mapping({**BASE, "clients": clients})


def test_client_key_sources() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        ClientKeyConfig(name="a")
    with pytest.raises(ValidationError, match="exactly one"):
        ClientKeyConfig(name="a", key="k", key_env="E")
    assert ClientKeyConfig(name="a", key="lit").resolve_key({}) == "lit"
    assert ClientKeyConfig(name="a", key_env="E").resolve_key({"E": "env"}) == "env"
    with pytest.raises(ValueError, match="unset"):
        ClientKeyConfig(name="a", key_env="E").resolve_key({})


def test_limits_for_client_override() -> None:
    cfg = GatewayConfig.from_mapping(BASE)
    custom = ClientKeyConfig(name="a", key="k", rate_limit=RateLimitConfig(requests_per_minute=5))
    assert cfg.limits_for(custom).requests_per_minute == 5
    assert cfg.limits_for(ClientKeyConfig(name="b", key="k")) == cfg.default_rate_limit
    assert cfg.limits_for(None) == cfg.default_rate_limit


def test_from_yaml(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(
        "providers:\n  mock: {type: mock}\n"
        "routes:\n  - model: m\n    targets: [{provider: mock, model: m1}]\n",
        encoding="utf-8",
    )
    assert GatewayConfig.from_yaml(path).routes[0].model == "m"


def test_from_yaml_rejects_non_mapping(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        GatewayConfig.from_yaml(path)


def test_settings_env_and_default_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LLMGW_PORT", "9999")
    monkeypatch.setenv("LLMGW_CONFIG", str(tmp_path / "missing.yaml"))
    settings = Settings()
    assert settings.port == 9999
    cfg = settings.load_gateway_config()
    assert cfg.routes[0].model == "mock-model"
