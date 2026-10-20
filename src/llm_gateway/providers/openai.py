"""OpenAI-compatible Chat Completions adapter (pass-through)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import ValidationError

from llm_gateway.errors import ProviderError
from llm_gateway.providers.base import Provider
from llm_gateway.schemas import ChatCompletionRequest, ChatCompletionResponse

MAX_ERROR_MESSAGE_CHARS = 500


def _error_message(response: httpx.Response) -> str:
    """Extract an upstream error message (``error.message`` when JSON), truncated."""
    message = ""
    try:
        data: Any = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            message = error["message"]
        elif isinstance(error, str):
            message = error
        elif isinstance(data.get("message"), str):
            message = data["message"]
    if not message:
        message = response.text.strip() or f"HTTP {response.status_code}"
    return message[:MAX_ERROR_MESSAGE_CHARS]


class HttpProvider(Provider):
    """Base for JSON-over-HTTP adapters: owns the client and classifies failures."""

    def __init__(
        self,
        name: str,
        *,
        base_url: str,
        timeout_s: float,
        headers: Mapping[str, str] | None,
        transport: httpx.AsyncBaseTransport | None,
    ) -> None:
        super().__init__(name)
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=timeout_s, headers=dict(headers or {}), transport=transport
        )

    def invalid_response(self, detail: str) -> ProviderError:
        return ProviderError(
            provider=self.name,
            message=f"invalid upstream response: {detail}"[:MAX_ERROR_MESSAGE_CHARS],
            status_code=502,
            retryable=True,
        )

    async def post_json(
        self, path: str, body: Mapping[str, Any], headers: Mapping[str, str]
    ) -> Any:
        """POST ``body`` and return the decoded JSON reply, raising :class:`ProviderError`."""
        try:
            response = await self._client.post(
                f"{self.base_url}{path}", json=dict(body), headers=dict(headers)
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                provider=self.name,
                message=f"upstream timeout ({type(exc).__name__})",
                retryable=True,
                timeout=True,
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderError(
                provider=self.name,
                message=f"upstream connection error ({type(exc).__name__})",
                retryable=True,
            ) from exc
        if not response.is_success:
            raise ProviderError.from_status(
                self.name, response.status_code, _error_message(response)
            )
        try:
            return response.json()
        except ValueError as exc:
            raise self.invalid_response("body is not valid JSON") from exc

    async def aclose(self) -> None:
        await self._client.aclose()


class OpenAIProvider(HttpProvider):
    """Forwards requests to an OpenAI-compatible ``/chat/completions`` endpoint."""

    def __init__(
        self,
        name: str,
        *,
        api_key: str | None,
        base_url: str = "https://api.openai.com/v1",
        timeout_s: float = 30.0,
        headers: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            name, base_url=base_url, timeout_s=timeout_s, headers=headers, transport=transport
        )
        self._auth = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def chat(
        self, request: ChatCompletionRequest, upstream_model: str
    ) -> ChatCompletionResponse:
        body = request.model_dump(exclude_none=True, exclude={"stream"})
        body["model"] = upstream_model
        body["stream"] = False
        data = await self.post_json("/chat/completions", body, self._auth)
        if isinstance(data, dict) and data.get("usage") is None:
            data.pop("usage", None)
        try:
            return ChatCompletionResponse.model_validate(data)
        except ValidationError as exc:
            raise self.invalid_response(
                f"{exc.error_count()} validation error(s) in chat.completion"
            ) from exc
