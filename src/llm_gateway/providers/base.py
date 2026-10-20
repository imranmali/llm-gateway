"""Provider adapter contract."""

from __future__ import annotations

from abc import ABC, abstractmethod

from llm_gateway.schemas import ChatCompletionRequest, ChatCompletionResponse


class Provider(ABC):
    """An upstream LLM API.

    Implementations translate an OpenAI-format request into the upstream dialect and
    the upstream reply back into an OpenAI ``chat.completion``. They MUST raise
    :class:`llm_gateway.errors.ProviderError` for every upstream failure (including
    timeouts and connection errors) so the router can classify it.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    @abstractmethod
    async def chat(
        self, request: ChatCompletionRequest, upstream_model: str
    ) -> ChatCompletionResponse:
        """Perform a chat completion against ``upstream_model``."""

    async def aclose(self) -> None:  # noqa: B027 - optional hook, default no-op
        """Release network resources."""
