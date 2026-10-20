"""HTTP endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from llm_gateway.auth import Client
from llm_gateway.errors import GatewayError, RateLimitExceededError, StreamingNotSupportedError
from llm_gateway.schemas import ChatCompletionRequest, ChatCompletionResponse, ModelCard, ModelList
from llm_gateway.state import GatewayState

router = APIRouter()


def get_state(request: Request) -> GatewayState:
    state: GatewayState = request.app.state.gateway
    return state


def authenticate(request: Request) -> Client:
    client = get_state(request).authenticator.authenticate(request.headers)
    request.state.client_name = client.name
    return client


StateDep = Annotated[GatewayState, Depends(get_state)]
ClientDep = Annotated[Client, Depends(authenticate)]


@router.post("/v1/chat/completions", response_model=ChatCompletionResponse, tags=["openai"])
async def chat_completions(
    body: ChatCompletionRequest, response: Response, client: ClientDep, state: StateDep
) -> ChatCompletionResponse:
    decision = state.limiter.check(client.name, client.limits)
    quota_headers = decision.headers()
    if not decision.allowed:
        raise RateLimitExceededError(
            f"Rate limit exceeded for client {client.name!r} ({decision.limit_type} per "
            f"minute). Retry after {decision.retry_after_s}s.",
            headers=quota_headers,
        )
    try:
        if body.stream:
            raise StreamingNotSupportedError(
                "Streaming responses are not supported; send 'stream': false."
            )
        result = await state.router.complete(body)
    except GatewayError as exc:
        exc.headers = {**quota_headers, **exc.headers}
        raise

    usage = result.response.usage
    state.limiter.record_usage(client.name, client.limits, usage.total_tokens)

    response.headers.update(quota_headers)
    response.headers["X-Gateway-Provider"] = result.provider
    response.headers["X-Gateway-Upstream-Model"] = result.upstream_model
    response.headers["X-Gateway-Attempts"] = str(result.attempts)
    return result.response.model_copy(update={"model": body.model})


@router.get("/v1/models", response_model=ModelList, tags=["openai"])
async def list_models(_: ClientDep, state: StateDep) -> ModelList:
    return ModelList(data=[ModelCard(id=route.model) for route in state.config.routes])


@router.get("/healthz", tags=["ops"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", tags=["ops"])
async def readyz(state: StateDep) -> JSONResponse:
    routes = {r.model: state.router.is_route_available(r.model) for r in state.config.routes}
    ready = all(routes.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "not_ready",
            "routes": routes,
            "providers": state.router.provider_states(),
        },
    )


@router.get("/metrics", tags=["ops"], include_in_schema=False)
async def metrics(state: StateDep) -> Response:
    payload, content_type = state.metrics.render()
    return Response(content=payload, media_type=content_type)
