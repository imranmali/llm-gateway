"""Anthropic Messages API adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from llm_gateway.providers.openai import HttpProvider
from llm_gateway.schemas import (
    AssistantMessage,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    Usage,
)

_FINISH_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
}


def _token_count(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


class AnthropicProvider(HttpProvider):
    """Translates OpenAI chat requests to ``POST /v1/messages`` and back."""

    def __init__(
        self,
        name: str,
        *,
        api_key: str | None,
        base_url: str = "https://api.anthropic.com",
        timeout_s: float = 30.0,
        headers: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        default_max_tokens: int = 1024,
        api_version: str = "2023-06-01",
    ) -> None:
        super().__init__(
            name, base_url=base_url, timeout_s=timeout_s, headers=headers, transport=transport
        )
        self.default_max_tokens = default_max_tokens
        self._headers = {"anthropic-version": api_version, "content-type": "application/json"}
        if api_key:
            self._headers["x-api-key"] = api_key

    def build_body(self, request: ChatCompletionRequest, upstream_model: str) -> dict[str, Any]:
        """Translate an OpenAI request into a Messages API body."""
        body: dict[str, Any] = {
            "model": upstream_model,
            "max_tokens": request.max_tokens or self.default_max_tokens,
            "messages": [
                {"role": m.role, "content": m.content}
                for m in request.messages
                if m.role != "system"
            ],
        }
        system = [m.content for m in request.messages if m.role == "system"]
        if system:
            body["system"] = "\n\n".join(system)
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.top_p is not None:
            body["top_p"] = request.top_p
        stop = request.stop_sequences()
        if stop:
            body["stop_sequences"] = stop
        return body

    async def chat(
        self, request: ChatCompletionRequest, upstream_model: str
    ) -> ChatCompletionResponse:
        data = await self.post_json(
            "/v1/messages", self.build_body(request, upstream_model), self._headers
        )
        if not isinstance(data, dict):
            raise self.invalid_response("expected a JSON object")
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise self.invalid_response("missing 'content' list")
        text = "".join(
            block["text"]
            for block in blocks
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
        stop_reason = data.get("stop_reason")
        finish_reason = (
            _FINISH_REASONS.get(stop_reason, stop_reason) if isinstance(stop_reason, str) else None
        )
        usage = data.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        extra: dict[str, Any] = {}
        if isinstance(data.get("id"), str):
            extra["id"] = data["id"]
        model = data.get("model")
        return ChatCompletionResponse(
            model=model if isinstance(model, str) and model else upstream_model,
            choices=[Choice(message=AssistantMessage(content=text), finish_reason=finish_reason)],
            usage=Usage.of(
                _token_count(usage.get("input_tokens")), _token_count(usage.get("output_tokens"))
            ),
            **extra,
        )
