"""Client authentication via ``Authorization: Bearer`` or ``X-API-Key``."""

from __future__ import annotations

import hmac
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from llm_gateway.config import ClientKeyConfig, RateLimitConfig
from llm_gateway.errors import AuthenticationError

ANONYMOUS = "anonymous"


@dataclass(frozen=True)
class Client:
    name: str
    limits: RateLimitConfig


class Authenticator:
    """Resolves request credentials to a configured client.

    With no clients configured, every caller is the ``anonymous`` client.
    """

    def __init__(
        self,
        clients: Sequence[ClientKeyConfig],
        default_limits: RateLimitConfig,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        env = dict(environ) if environ is not None else None
        self._keys: list[tuple[bytes, Client]] = [
            (
                c.resolve_key(env).encode(),
                Client(name=c.name, limits=c.rate_limit or default_limits),
            )
            for c in clients
        ]
        self._anonymous = Client(name=ANONYMOUS, limits=default_limits)

    @property
    def enabled(self) -> bool:
        return bool(self._keys)

    @staticmethod
    def extract_key(headers: Mapping[str, str]) -> str | None:
        auth = headers.get("authorization")
        if auth:
            scheme, _, token = auth.partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                return token.strip()
        api_key = headers.get("x-api-key")
        return api_key.strip() if api_key and api_key.strip() else None

    def authenticate(self, headers: Mapping[str, str]) -> Client:
        if not self.enabled:
            return self._anonymous
        presented = self.extract_key(headers)
        if presented is None:
            raise AuthenticationError(
                "Missing API key. Send 'Authorization: Bearer <key>' or 'X-API-Key: <key>'."
            )
        candidate = presented.encode()
        match: Client | None = None
        # Compare against every key so timing does not reveal which key matched.
        for key, client in self._keys:
            if hmac.compare_digest(key, candidate) and match is None:
                match = client
        if match is None:
            raise AuthenticationError("Invalid API key.")
        return match
