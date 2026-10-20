"""Structured JSON logging with a request-ID context variable."""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, TextIO

request_id_var: ContextVar[str | None] = ContextVar("llmgw_request_id", default=None)

REDACTED = "[REDACTED]"
SENSITIVE_KEYS = frozenset({"authorization", "api_key", "x-api-key", "password", "secret", "key"})

# Attributes present on every LogRecord; anything else arrived via ``extra=``.
_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", logging.INFO, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "request_id",
    "taskName",
    "color_message",  # uvicorn's ANSI-coloured duplicate of ``message``
}


class RequestIdFilter(logging.Filter):
    """Attach the current request ID (or ``"-"``) to every record as ``request_id``."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or "-"
        return True


class JsonFormatter(logging.Formatter):
    """Render each record as one JSON object per line, redacting sensitive extras."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=UTC)
        payload: dict[str, Any] = {
            "ts": ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None) or request_id_var.get() or "-",
        }
        for name, value in vars(record).items():
            if name in _STANDARD_ATTRS or name in payload:
                continue
            payload[name] = REDACTED if name.lower() in SENSITIVE_KEYS else value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exc_info"] = record.exc_text
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class _GatewayHandler(logging.StreamHandler[TextIO]):
    """Marker subclass so :func:`configure_logging` can recognise its own handler."""


def configure_logging(level: str = "INFO") -> None:
    """Install one JSON stderr handler on the root logger; safe to call repeatedly."""
    root = logging.getLogger()
    for existing in [h for h in root.handlers if isinstance(h, _GatewayHandler)]:
        root.removeHandler(existing)
        existing.close()
    handler = _GatewayHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())
    root.addHandler(handler)
    root.setLevel(level.upper())

    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    # The gateway emits its own access log; drop uvicorn's duplicate.
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True
