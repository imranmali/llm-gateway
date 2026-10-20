"""Deterministic in-process provider for development, demos and tests."""

from __future__ import annotations

import asyncio

from llm_gateway.errors import ProviderError
from llm_gateway.providers.base import Provider
from llm_gateway.schemas import (
    AssistantMessage,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    Usage,
)


def _count_tokens(text: str) -> int:
    """Rough whitespace token count, never zero for non-empty input."""
    return max(1, len(text.split())) if text else 0


class MockProvider(Provider):
    """Echoes the last user message.

    Failure injection: the first ``fail_times`` calls fail (``-1`` = every call), either
    with HTTP ``fail_status`` semantics or as a timeout when ``fail_timeout`` is set.
    """

    def __init__(
        self,
        name: str = "mock",
        *,
        latency_s: float = 0.0,
        fail_times: int = 0,
        fail_status: int = 503,
        fail_timeout: bool = False,
    ) -> None:
        super().__init__(name)
        self.latency_s = latency_s
        self.fail_times = fail_times
        self.fail_status = fail_status
        self.fail_timeout = fail_timeout
        self.calls = 0

    def _should_fail(self) -> bool:
        return self.fail_times < 0 or self.calls <= self.fail_times

    async def chat(
        self, request: ChatCompletionRequest, upstream_model: str
    ) -> ChatCompletionResponse:
        self.calls += 1
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        if self._should_fail():
            if self.fail_timeout:
                raise ProviderError(
                    provider=self.name, message="mock timeout", retryable=True, timeout=True
                )
            raise ProviderError.from_status(
                self.name, self.fail_status, f"mock failure ({self.fail_status})"
            )

        last_user = next((m.content for m in reversed(request.messages) if m.role == "user"), "")
        content = f"[{upstream_model}] {last_user}".strip()
        if request.max_tokens is not None:
            content = " ".join(content.split()[: request.max_tokens])
        prompt_tokens = sum(_count_tokens(m.content) for m in request.messages)
        return ChatCompletionResponse(
            model=upstream_model,
            choices=[Choice(message=AssistantMessage(content=content), finish_reason="stop")],
            usage=Usage.of(prompt_tokens, _count_tokens(content)),
        )
