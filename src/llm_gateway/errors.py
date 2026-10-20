"""Gateway error hierarchy mapped onto the OpenAI error envelope."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from llm_gateway.schemas import ErrorBody, ErrorResponse


class GatewayError(Exception):
    """Base class for errors that become HTTP responses."""

    status_code: int = 500
    error_type: str = "server_error"
    code: str = "internal_error"

    def __init__(self, message: str, *, headers: Mapping[str, str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.headers: dict[str, str] = dict(headers or {})

    def to_response(self) -> ErrorResponse:
        return ErrorResponse(
            error=ErrorBody(message=self.message, type=self.error_type, code=self.code)
        )


class AuthenticationError(GatewayError):
    status_code = 401
    error_type = "authentication_error"
    code = "invalid_api_key"


class InvalidRequestError(GatewayError):
    status_code = 400
    error_type = "invalid_request_error"
    code = "invalid_request"


class StreamingNotSupportedError(InvalidRequestError):
    code = "streaming_not_supported"


class ModelNotFoundError(GatewayError):
    status_code = 404
    error_type = "invalid_request_error"
    code = "model_not_found"


class RateLimitExceededError(GatewayError):
    status_code = 429
    error_type = "rate_limit_error"
    code = "rate_limit_exceeded"


class UpstreamBadRequestError(GatewayError):
    status_code = 400
    error_type = "invalid_request_error"
    code = "upstream_bad_request"


class AllProvidersFailedError(GatewayError):
    status_code = 502
    error_type = "upstream_error"
    code = "all_providers_failed"


class UpstreamTimeoutError(AllProvidersFailedError):
    status_code = 504
    code = "upstream_timeout"


@dataclass
class ProviderError(Exception):
    """Raised by provider adapters.

    ``retryable`` failures (connection errors, timeouts, 429, 5xx) are retried on the
    same target. ``client_error`` failures (400/422) are returned to the caller without
    retry or failover. Anything else (e.g. 401/403/404) fails over to the next target.
    """

    provider: str
    message: str
    status_code: int | None = None
    retryable: bool = False
    timeout: bool = False
    client_error: bool = False
    extra: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def __str__(self) -> str:
        status = f" status={self.status_code}" if self.status_code is not None else ""
        return f"{self.provider}:{status} {self.message}".strip()

    @classmethod
    def from_status(cls, provider: str, status_code: int, message: str) -> ProviderError:
        """Classify an upstream HTTP status into retry/failover semantics."""
        return cls(
            provider=provider,
            message=message,
            status_code=status_code,
            retryable=status_code == 429 or status_code >= 500,
            client_error=status_code in (400, 422),
        )
