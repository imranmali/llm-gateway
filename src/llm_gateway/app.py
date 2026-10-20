"""Application factory wiring configuration, providers and middleware."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from llm_gateway import __version__
from llm_gateway.api import router as api_router
from llm_gateway.auth import Authenticator
from llm_gateway.config import GatewayConfig, Settings
from llm_gateway.errors import GatewayError, InvalidRequestError
from llm_gateway.observability import MetricsObserver, ObservabilityMiddleware
from llm_gateway.providers.base import Provider
from llm_gateway.providers.registry import build_providers
from llm_gateway.ratelimit import Clock, InMemoryRateLimiter
from llm_gateway.routing import Router, Sleep
from llm_gateway.schemas import ErrorBody, ErrorResponse
from llm_gateway.state import GatewayState

logger = logging.getLogger(__name__)


def _error(status: int, message: str, error_type: str, code: str) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(message=message, type=error_type, code=code))
    return JSONResponse(status_code=status, content=body.to_dict())


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(GatewayError)
    async def _gateway_error(_: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_response().to_dict(),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg', 'invalid')}"
            for err in exc.errors()
        )
        return _error(
            400,
            f"Invalid request: {details}",
            InvalidRequestError.error_type,
            InvalidRequestError.code,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return _error(exc.status_code, str(exc.detail), "invalid_request_error", code)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error", exc_info=exc)
        return _error(500, "Internal server error.", "server_error", "internal_error")


def create_app(
    config: GatewayConfig | None = None,
    *,
    settings: Settings | None = None,
    providers: Mapping[str, Provider] | None = None,
    environ: Mapping[str, str] | None = None,
    clock: Clock = time.monotonic,
    sleep: Sleep = asyncio.sleep,
) -> FastAPI:
    """Build the gateway application.

    ``providers``, ``environ``, ``clock`` and ``sleep`` exist so tests can run the full
    stack deterministically without network access or real waiting.
    """
    if config is None:
        config = (settings or Settings()).load_gateway_config()

    metrics = MetricsObserver()
    provider_map = (
        dict(providers) if providers is not None else build_providers(config, environ=environ)
    )
    gateway_router = Router(config, provider_map, observer=metrics, clock=clock, sleep=sleep)
    for name in gateway_router.breakers:
        metrics.set_circuit_state(name, "closed")

    state = GatewayState(
        config=config,
        authenticator=Authenticator(config.clients, config.default_rate_limit, environ=environ),
        limiter=InMemoryRateLimiter(clock=clock, observer=metrics),
        router=gateway_router,
        metrics=metrics,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "gateway started",
            extra={"routes": [r.model for r in config.routes], "providers": list(provider_map)},
        )
        try:
            yield
        finally:
            await gateway_router.aclose()

    app = FastAPI(
        title="llm-gateway",
        version=__version__,
        description="OpenAI-compatible LLM gateway with rate limiting, fallback routing "
        "and observability.",
        lifespan=lifespan,
    )
    app.state.gateway = state
    app.include_router(api_router)
    _install_error_handlers(app)
    app.add_middleware(ObservabilityMiddleware, metrics=metrics)
    return app
