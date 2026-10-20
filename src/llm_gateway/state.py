"""Per-application runtime components shared by the HTTP layer."""

from __future__ import annotations

from dataclasses import dataclass

from llm_gateway.auth import Authenticator
from llm_gateway.config import GatewayConfig
from llm_gateway.observability import MetricsObserver
from llm_gateway.ratelimit import RateLimiter
from llm_gateway.routing import Router


@dataclass
class GatewayState:
    config: GatewayConfig
    authenticator: Authenticator
    limiter: RateLimiter
    router: Router
    metrics: MetricsObserver
