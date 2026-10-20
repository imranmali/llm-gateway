from __future__ import annotations

import io
import json
import logging
import re
from collections.abc import Iterator
from typing import Any

import pytest

from llm_gateway.observability import (
    JsonFormatter,
    RequestIdFilter,
    configure_logging,
    request_id_var,
)


@pytest.fixture
def capture() -> Iterator[tuple[logging.Logger, io.StringIO]]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())
    logger = logging.getLogger("test.observability.json")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    yield logger, stream
    logger.handlers = []


def _records(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_emits_one_json_object_per_line_with_core_fields(
    capture: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = capture
    logger.info("hello %s", "world")
    logger.warning("second")
    first, second = _records(stream)
    assert first["message"] == "hello world"
    assert first["level"] == "INFO"
    assert first["logger"] == "test.observability.json"
    assert first["request_id"] == "-"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", first["ts"])
    assert second["level"] == "WARNING"


def test_includes_extra_fields(capture: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = capture
    logger.info(
        "request completed",
        extra={"method": "GET", "path": "/x", "status": 200, "duration_ms": 1.25, "client": "a"},
    )
    (record,) = _records(stream)
    assert record["method"] == "GET"
    assert record["path"] == "/x"
    assert record["status"] == 200
    assert record["duration_ms"] == 1.25
    assert record["client"] == "a"
    for internal in ("args", "msg", "levelno", "pathname", "exc_text"):
        assert internal not in record


def test_request_id_from_contextvar(capture: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = capture
    token = request_id_var.set("req-42")
    try:
        logger.info("inside")
    finally:
        request_id_var.reset(token)
    logger.info("outside")
    inside, outside = _records(stream)
    assert inside["request_id"] == "req-42"
    assert outside["request_id"] == "-"


def test_formatter_reads_contextvar_without_filter() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
    token = request_id_var.set("ctx-id")
    try:
        payload = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)
    assert payload["request_id"] == "ctx-id"


@pytest.mark.parametrize(
    "key", ["authorization", "Authorization", "api_key", "x-api-key", "password", "secret", "key"]
)
def test_redacts_sensitive_extras(capture: tuple[logging.Logger, io.StringIO], key: str) -> None:
    logger, stream = capture
    logger.info("creds", extra={key: "sk-super-secret"})
    raw = stream.getvalue()
    assert "sk-super-secret" not in raw
    assert _records(stream)[0][key] == "[REDACTED]"


def test_exc_info_rendered(capture: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = capture
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("failed")
    (record,) = _records(stream)
    assert record["level"] == "ERROR"
    assert isinstance(record["exc_info"], str)
    assert "ValueError: boom" in record["exc_info"]


def test_cached_exc_text_and_stack_info() -> None:
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "msg", None, None)
    record.exc_text = "Traceback: cached"
    record.stack_info = "Stack (most recent call last): here"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["exc_info"] == "Traceback: cached"
    assert "here" in payload["stack_info"]


def test_non_serialisable_values_use_str(capture: tuple[logging.Logger, io.StringIO]) -> None:
    class Thing:
        def __str__(self) -> str:
            return "thing!"

    logger, stream = capture
    logger.info("odd", extra={"obj": Thing(), "items": {1, 2}})
    (record,) = _records(stream)
    assert record["obj"] == "thing!"
    assert isinstance(record["items"], str)


def test_filter_always_passes_and_sets_attribute() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
    assert RequestIdFilter().filter(record) is True
    assert record.request_id == "-"


@pytest.fixture
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    names = ("uvicorn", "uvicorn.error", "uvicorn.access")
    saved = {
        n: (list(lg.handlers), lg.propagate, lg.disabled)
        for n in names
        for lg in [logging.getLogger(n)]
    }
    yield
    root.handlers = saved_handlers
    root.setLevel(saved_level)
    for n, (handlers, propagate, disabled) in saved.items():
        lg = logging.getLogger(n)
        lg.handlers, lg.propagate, lg.disabled = handlers, propagate, disabled


def _ours(root: logging.Logger) -> list[logging.Handler]:
    return [h for h in root.handlers if isinstance(h.formatter, JsonFormatter)]


@pytest.mark.usefixtures("restore_logging")
def test_configure_logging_is_idempotent() -> None:
    uvicorn_error = logging.getLogger("uvicorn.error")
    uvicorn_error.addHandler(logging.NullHandler())
    uvicorn_error.propagate = False
    logging.getLogger("uvicorn.access").addHandler(logging.NullHandler())

    configure_logging("debug")
    configure_logging("WARNING")

    root = logging.getLogger()
    ours = _ours(root)
    assert len(ours) == 1
    assert any(isinstance(f, RequestIdFilter) for f in ours[0].filters)
    assert isinstance(ours[0], logging.StreamHandler)
    assert root.level == logging.WARNING
    for name in ("uvicorn", "uvicorn.error"):
        assert logging.getLogger(name).handlers == []
        assert logging.getLogger(name).propagate is True
    access = logging.getLogger("uvicorn.access")
    assert access.handlers == []
    assert access.propagate is False
    assert access.disabled is True


@pytest.mark.usefixtures("restore_logging")
def test_configure_logging_keeps_foreign_handlers() -> None:
    foreign = logging.NullHandler()
    logging.getLogger().addHandler(foreign)
    configure_logging()
    configure_logging()
    root = logging.getLogger()
    assert foreign in root.handlers
    assert len(_ours(root)) == 1
    assert root.level == logging.INFO


def test_uvicorn_color_message_is_dropped() -> None:
    record = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, "Started", None, None)
    record.color_message = "\x1b[36mStarted\x1b[0m"
    payload = json.loads(JsonFormatter().format(record))
    assert "color_message" not in payload
    assert payload["message"] == "Started"
