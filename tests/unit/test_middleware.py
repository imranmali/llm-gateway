from __future__ import annotations

import io
import json
import logging
import re

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from llm_gateway.observability import (
    REQUEST_ID_HEADER,
    JsonFormatter,
    MetricsObserver,
    ObservabilityMiddleware,
    RequestIdFilter,
    request_id_var,
)

ACCESS_LOGGER = "llm_gateway.access"


@pytest.fixture
def metrics() -> MetricsObserver:
    return MetricsObserver()


@pytest.fixture
def client(metrics: MetricsObserver) -> TestClient:
    app = FastAPI()

    @app.get("/items/{item_id}")
    async def get_item(item_id: str) -> dict[str, str]:
        return {"item_id": item_id, "ctx_request_id": request_id_var.get() or ""}

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("kaboom")

    @app.get("/whoami")
    async def whoami(request: Request) -> dict[str, str]:
        request.state.client_name = "team-a"
        return {"request_id": request.state.request_id}

    @app.get("/own-header")
    async def own_header() -> PlainTextResponse:
        return PlainTextResponse("x", headers={REQUEST_ID_HEADER: "from-app"})

    app.add_middleware(ObservabilityMiddleware, metrics=metrics)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def access_logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER)
    return caplog


def _access_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == ACCESS_LOGGER]


def _http_count(metrics: MetricsObserver, method: str, route: str, status: str) -> float | None:
    return metrics.registry.get_sample_value(
        "llmgw_http_requests_total", {"method": method, "route": route, "status": status}
    )


def test_propagates_valid_incoming_request_id(client: TestClient) -> None:
    resp = client.get("/items/1", headers={REQUEST_ID_HEADER: "abc-123"})
    assert resp.headers[REQUEST_ID_HEADER] == "abc-123"
    assert resp.json()["ctx_request_id"] == "abc-123"


@pytest.mark.parametrize("bad", ["has space", "x" * 129, "semi;colon", ""])
def test_replaces_invalid_request_id(client: TestClient, bad: str) -> None:
    resp = client.get("/items/1", headers={REQUEST_ID_HEADER: bad})
    rid = resp.headers[REQUEST_ID_HEADER]
    assert rid != bad
    assert re.fullmatch(r"[0-9a-f]{32}", rid)


def test_generates_request_id_when_missing(client: TestClient) -> None:
    first = client.get("/items/1").headers[REQUEST_ID_HEADER]
    second = client.get("/items/1").headers[REQUEST_ID_HEADER]
    assert re.fullmatch(r"[0-9a-f]{32}", first)
    assert first != second


def test_request_id_exposed_on_request_state(client: TestClient) -> None:
    resp = client.get("/whoami", headers={REQUEST_ID_HEADER: "state.id_1"})
    assert resp.json() == {"request_id": "state.id_1"}


def test_single_request_id_header_even_if_app_sets_one(client: TestClient) -> None:
    resp = client.get("/own-header", headers={REQUEST_ID_HEADER: "mine"})
    assert resp.headers.get_list(REQUEST_ID_HEADER) == ["mine"]


def test_contextvar_reset_after_request(client: TestClient) -> None:
    client.get("/items/1", headers={REQUEST_ID_HEADER: "abc"})
    assert request_id_var.get() is None


def test_metrics_use_route_template(client: TestClient, metrics: MetricsObserver) -> None:
    client.get("/items/1")
    client.get("/items/2")
    assert _http_count(metrics, "GET", "/items/{item_id}", "200") == 2
    assert _http_count(metrics, "GET", "/items/1", "200") is None
    assert (
        metrics.registry.get_sample_value(
            "llmgw_http_request_duration_seconds_count",
            {"method": "GET", "route": "/items/{item_id}"},
        )
        == 2
    )


def test_unmatched_route_label(client: TestClient, metrics: MetricsObserver) -> None:
    assert client.get("/nope/123").status_code == 404
    client.get("/also/missing")
    assert _http_count(metrics, "GET", "unmatched", "404") == 2


def test_access_log_fields(client: TestClient, access_logs: pytest.LogCaptureFixture) -> None:
    client.get("/items/7?api_key=sk-secret", headers={REQUEST_ID_HEADER: "rid-1"})
    (record,) = _access_records(access_logs)
    assert record.levelno == logging.INFO
    assert record.getMessage() == "request completed"
    assert record.__dict__["method"] == "GET"
    assert record.__dict__["path"] == "/items/7"
    assert record.__dict__["route"] == "/items/{item_id}"
    assert record.__dict__["status"] == 200
    assert record.__dict__["client"] == "-"
    duration = record.__dict__["duration_ms"]
    assert isinstance(duration, float)
    assert duration >= 0
    assert round(duration, 2) == duration
    assert "sk-secret" not in str(record.__dict__)


def test_access_log_json_has_request_id_and_no_secrets(client: TestClient) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())
    logger = logging.getLogger(ACCESS_LOGGER)
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        client.get(
            "/items/1",
            headers={REQUEST_ID_HEADER: "ctx-rid", "Authorization": "Bearer sk-top-secret"},
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["request_id"] == "ctx-rid"
    for field in ("ts", "level", "method", "path", "status", "duration_ms", "client"):
        assert field in payload
    assert "sk-top-secret" not in lines[0]


def test_access_log_includes_client_name(
    client: TestClient, access_logs: pytest.LogCaptureFixture
) -> None:
    client.get("/whoami")
    (record,) = _access_records(access_logs)
    assert record.__dict__["client"] == "team-a"
    assert record.__dict__["route"] == "/whoami"


def test_error_route_logs_500(
    client: TestClient, metrics: MetricsObserver, access_logs: pytest.LogCaptureFixture
) -> None:
    resp = client.get("/boom")
    assert resp.status_code == 500
    records = _access_records(access_logs)
    assert len(records) == 1
    assert records[0].__dict__["status"] == 500
    assert records[0].__dict__["route"] == "/boom"
    assert _http_count(metrics, "GET", "/boom", "500") == 1


async def test_exception_before_response_is_reraised_with_500(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def failing_app(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("no response")

    metrics = MetricsObserver()
    mw = ObservabilityMiddleware(failing_app, metrics=metrics)
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER)

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        raise AssertionError("nothing should be sent")

    scope: Scope = {"type": "http", "method": "POST", "path": "/x", "headers": []}
    with pytest.raises(RuntimeError, match="no response"):
        await mw(scope, receive, send)
    (record,) = _access_records(caplog)
    assert record.__dict__["status"] == 500
    assert record.__dict__["route"] == "unmatched"
    assert _http_count(metrics, "POST", "unmatched", "500") == 1
    assert request_id_var.get() is None


async def test_exception_after_response_started_keeps_status(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 201, "headers": []})
        raise RuntimeError("mid-stream")

    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER)
    mw = ObservabilityMiddleware(app)
    scope: Scope = {"type": "http", "method": "GET", "path": "/s", "headers": []}
    with pytest.raises(RuntimeError):
        await mw(scope, receive, send)
    (record,) = _access_records(caplog)
    assert record.__dict__["status"] == 201
    assert any(k == b"x-request-id" for k, _ in sent[0]["headers"])


async def test_non_http_scope_passes_through(caplog: pytest.LogCaptureFixture) -> None:
    seen: list[Scope] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope)

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        return None

    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER)
    metrics = MetricsObserver()
    scope: Scope = {"type": "lifespan"}
    await ObservabilityMiddleware(app, metrics=metrics)(scope, receive, send)
    assert seen == [{"type": "lifespan"}]
    assert "state" not in scope
    assert _access_records(caplog) == []


def test_lifespan_through_test_client(metrics: MetricsObserver) -> None:
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware, metrics=metrics)
    with TestClient(app) as tc:
        assert tc.get("/missing").status_code == 404
    assert _http_count(metrics, "GET", "unmatched", "404") == 1


def test_custom_logger_name_and_no_metrics(caplog: pytest.LogCaptureFixture) -> None:
    app = FastAPI()

    @app.get("/ping")
    async def ping() -> str:
        return "pong"

    app.add_middleware(ObservabilityMiddleware, logger_name="custom.access")
    caplog.set_level(logging.INFO, logger="custom.access")
    TestClient(app).get("/ping")
    assert [r.name for r in caplog.records if r.name.endswith("access")] == ["custom.access"]
