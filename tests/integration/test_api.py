"""End-to-end behaviour of the HTTP API (chat-gateway spec)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from llm_gateway.app import create_app
from llm_gateway.config import GatewayConfig, Settings
from llm_gateway.providers import MockProvider

from .conftest import KEY_A, KEY_B, AppFactory, chat


def _assert_envelope(body: dict[str, object], code: str) -> None:
    error = body["error"]
    assert isinstance(error, dict)
    assert error["code"] == code
    assert isinstance(error["message"], str)
    assert error["message"]
    assert isinstance(error["type"], str)
    assert error["type"]


def test_successful_completion(client: TestClient) -> None:
    resp = chat(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "simple"
    assert body["id"].startswith("chatcmpl-")
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "[p-1] hello gateway"}
    usage = body["usage"]
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"] > 0
    assert resp.headers["X-Gateway-Provider"] == "primary"
    assert resp.headers["X-Gateway-Upstream-Model"] == "p-1"
    assert resp.headers["X-Gateway-Attempts"] == "1"
    assert resp.headers["X-Request-ID"]


def test_unknown_model(client: TestClient) -> None:
    resp = chat(client, "does-not-exist")
    assert resp.status_code == 404
    _assert_envelope(resp.json(), "model_not_found")


@pytest.mark.parametrize(
    "body",
    [
        {"model": "simple", "messages": []},
        {"model": "simple", "messages": [{"role": "tool", "content": "x"}]},
        {"messages": [{"role": "user", "content": "x"}]},
    ],
)
def test_invalid_request_rejected_before_provider(make_client: AppFactory, body: dict) -> None:
    provider = MockProvider("primary")
    config = GatewayConfig.from_mapping(
        {
            "providers": {"primary": {"type": "mock"}},
            "routes": [{"model": "simple", "targets": [{"provider": "primary", "model": "p"}]}],
        }
    )
    with TestClient(create_app(config, providers={"primary": provider})) as client:
        resp = client.post("/v1/chat/completions", json=body)
    assert resp.status_code == 400
    _assert_envelope(resp.json(), "invalid_request")
    assert provider.calls == 0


def test_streaming_not_supported(client: TestClient) -> None:
    resp = chat(client, stream=True)
    assert resp.status_code == 400
    _assert_envelope(resp.json(), "streaming_not_supported")


def test_missing_and_invalid_keys(client: TestClient) -> None:
    resp = chat(client, key=None)
    assert resp.status_code == 401
    _assert_envelope(resp.json(), "invalid_api_key")
    resp = chat(client, key="sk-wrong")
    assert resp.status_code == 401
    assert "sk-wrong" not in resp.text


def test_x_api_key_header(client: TestClient) -> None:
    body = {"model": "simple", "messages": [{"role": "user", "content": "hi"}]}
    resp = client.post("/v1/chat/completions", json=body, headers={"X-API-Key": KEY_B})
    assert resp.status_code == 200


def test_anonymous_mode(make_client: AppFactory) -> None:
    client = make_client(clients=[])
    assert chat(client, key=None).status_code == 200
    assert client.get("/v1/models").status_code == 200


def test_list_models(client: TestClient) -> None:
    assert client.get("/v1/models").status_code == 401
    resp = client.get("/v1/models", headers={"Authorization": f"Bearer {KEY_A}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert {m["id"] for m in body["data"]} == {
        "simple",
        "failover",
        "broken",
        "timeout",
        "bad-request",
    }
    assert all(m["object"] == "model" for m in body["data"])


def test_healthz(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz_ok(client: TestClient) -> None:
    resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["providers"]["primary"] == "closed"
    assert body["routes"]["simple"] is True


def test_unknown_path_and_method_use_envelope(client: TestClient) -> None:
    resp = client.get("/nope")
    assert resp.status_code == 404
    _assert_envelope(resp.json(), "not_found")
    resp = client.get("/v1/chat/completions")
    assert resp.status_code == 405
    _assert_envelope(resp.json(), "method_not_allowed")


def test_unhandled_error_returns_500_envelope(make_client: AppFactory) -> None:
    client = make_client()
    state = client.app.state.gateway  # type: ignore[attr-defined]

    async def boom(*_: object, **__: object) -> None:
        raise RuntimeError("kaboom")

    state.router.complete = boom
    resp = chat(client)
    assert resp.status_code == 500
    _assert_envelope(resp.json(), "internal_error")
    assert "kaboom" not in resp.text


def test_create_app_from_settings_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "gw.yaml"
    path.write_text(
        "providers:\n  m: {type: mock}\n"
        "routes:\n  - model: from-file\n    targets: [{provider: m, model: m1}]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LLMGW_CONFIG", str(path))
    with TestClient(create_app()) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "from-file", "messages": [{"role": "user", "content": "x"}]},
        )
    assert resp.status_code == 200
    assert Settings().config == path


def test_main_runs_uvicorn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import llm_gateway.__main__ as entry

    captured: dict[str, object] = {}

    def fake_run(app: object, **kwargs: object) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(entry.uvicorn, "run", fake_run)
    monkeypatch.setenv("LLMGW_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("LLMGW_PORT", "9123")
    entry.main()
    assert captured["port"] == 9123
    assert captured["log_config"] is None
    assert captured["app"] is not None
