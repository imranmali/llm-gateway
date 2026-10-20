"""Pure ASGI middleware for request IDs, access logging and HTTP metrics."""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from llm_gateway.observability.logging import request_id_var
from llm_gateway.observability.metrics import MetricsObserver

REQUEST_ID_HEADER = "X-Request-ID"
UNMATCHED_ROUTE = "unmatched"

_REQUEST_ID_HEADER_RAW = REQUEST_ID_HEADER.lower().encode("latin-1")
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _incoming_request_id(scope: Scope) -> str:
    for name, value in scope.get("headers", ()):
        if name.lower() == _REQUEST_ID_HEADER_RAW:
            candidate: str = value.decode("latin-1")
            if _VALID_REQUEST_ID.fullmatch(candidate):
                return candidate
            break
    return uuid.uuid4().hex


def _route_template(scope: Scope) -> str:
    path = getattr(scope.get("route"), "path", None)
    return path if isinstance(path, str) and path else UNMATCHED_ROUTE


class ObservabilityMiddleware:
    """Assigns a request ID, records HTTP metrics and emits one access log per request."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        metrics: MetricsObserver | None = None,
        logger_name: str = "llm_gateway.access",
    ) -> None:
        self.app = app
        self.metrics = metrics
        self.logger = logging.getLogger(logger_name)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope)
        state: dict[str, Any] = scope.setdefault("state", {})
        state["request_id"] = request_id
        header_value = request_id.encode("latin-1")
        status_code: int | None = None

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = [
                    (k, v)
                    for k, v in message.get("headers", ())
                    if k.lower() != _REQUEST_ID_HEADER_RAW
                ]
                headers.append((_REQUEST_ID_HEADER_RAW, header_value))
                message = {**message, "headers": headers}
            await send(message)

        token = request_id_var.set(request_id)
        start = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException:
            if status_code is None:
                status_code = 500
            raise
        finally:
            duration_s = time.perf_counter() - start
            self._record(scope, status_code or 500, duration_s)
            request_id_var.reset(token)

    def _record(self, scope: Scope, status: int, duration_s: float) -> None:
        method: str = scope.get("method", "")
        route = _route_template(scope)
        if self.metrics is not None:
            self.metrics.observe_http(
                method=method, route=route, status=status, duration_s=duration_s
            )
        self.logger.info(
            "request completed",
            extra={
                "method": method,
                "path": scope.get("path", ""),
                "route": route,
                "status": status,
                "duration_ms": round(duration_s * 1000, 2),
                "client": scope.get("state", {}).get("client_name", "-"),
            },
        )
