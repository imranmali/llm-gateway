"""OpenAI Chat Completions wire models."""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Role = Literal["system", "user", "assistant"]


class ChatMessage(BaseModel):
    """A single chat turn. Only plain text content is supported."""

    model_config = ConfigDict(extra="ignore")

    role: Role
    content: str
    name: str | None = None


class ChatCompletionRequest(BaseModel):
    """Subset of the OpenAI Chat Completions request the gateway understands."""

    model_config = ConfigDict(extra="ignore")

    model: str = Field(min_length=1)
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, ge=1)
    stop: str | list[str] | None = None
    stream: bool = False
    user: str | None = None

    @field_validator("stop")
    @classmethod
    def _normalise_stop(cls, v: str | list[str] | None) -> list[str] | None:
        if v is None:
            return None
        return [v] if isinstance(v, str) else v

    def stop_sequences(self) -> list[str] | None:
        """Return ``stop`` as a list (the validator guarantees this shape)."""
        stop = self.stop
        if stop is None:
            return None
        return [stop] if isinstance(stop, str) else list(stop)


class Usage(BaseModel):
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)

    @classmethod
    def of(cls, prompt_tokens: int, completion_tokens: int) -> Usage:
        return cls(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        )


class AssistantMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str | None = ""


class Choice(BaseModel):
    index: int = 0
    message: AssistantMessage
    finish_reason: str | None = "stop"


def _completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex}"


class ChatCompletionResponse(BaseModel):
    """OpenAI ``chat.completion`` object."""

    id: str = Field(default_factory=_completion_id)
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[Choice]
    usage: Usage = Field(default_factory=Usage)


class ModelCard(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = 0
    owned_by: str = "llm-gateway"


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelCard]


class ErrorBody(BaseModel):
    message: str
    type: str
    code: str
    param: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)
