# Design

## Context

Greenfield repository (see proposal.md — Why). The service must be easy to run
locally (`docker compose up` with only the mock provider), testable without
network access, and structured so that rate limiting, routing and
observability can be built by separate contributors in parallel against
stable internal interfaces.

## Goals / Non-Goals

**Goals:**
- One async process, horizontally scalable behind a load balancer (with the
  documented caveat that limits/circuits are per process).
- Clear seams: provider adapters, rate limiter, router, observer — each
  replaceable and unit-testable in isolation with injected clocks/sleepers.
- Deterministic tests: no real sleeps, no real network.

**Non-Goals:**
- Streaming, distributed state, persistence (see proposal.md — Out of Scope).
- Exact tokenisation for quota pre-checks; actual usage reported by the
  provider is the source of truth.

## Decisions

### D1. Python 3.11+, FastAPI, httpx
FastAPI gives request validation via pydantic and OpenAPI docs for free;
httpx provides an async client with `MockTransport` for hermetic tests.
*Alternatives:* Go (faster, but slower to iterate and the team context is
Python), aiohttp (less ergonomic validation), LiteLLM as a dependency
(heavy, hides the behaviours this project exists to own).

### D2. Package layout (`src/llm_gateway/`)

| Module | Owner group | Responsibility |
|---|---|---|
| `config.py` | core | `Settings` (env, prefix `LLMGW_`) and `GatewayConfig` (YAML) models |
| `schemas.py` | core | OpenAI request/response pydantic models |
| `errors.py` | core | `GatewayError` hierarchy + OpenAI error envelope |
| `events.py` | core | `GatewayObserver` protocol, `NullObserver` |
| `providers/base.py` | core | `Provider` ABC, `ProviderError` semantics |
| `providers/mock.py` | core | deterministic mock provider |
| `providers/openai.py`, `providers/anthropic.py` | routing | HTTP adapters |
| `providers/registry.py` | routing | build providers from config |
| `ratelimit.py` | rate-limiting | token buckets, `RateLimiter` |
| `routing.py` | routing | `CircuitBreaker`, `Router` |
| `observability/{metrics,logging,middleware}.py` | observability | Prometheus observer, JSON logs, request-ID/access-log middleware |
| `auth.py`, `api.py`, `app.py`, `__main__.py` | integration | HTTP surface and wiring |

### D3. Internal contracts (stable seams)

- `Provider.chat(request: ChatCompletionRequest, upstream_model: str) -> ChatCompletionResponse`
  raises `ProviderError(provider, message, status_code: int | None, retryable: bool, timeout: bool)`.
  Adapters classify: connection error/timeout/429/5xx → retryable;
  401/403/404 → non-retryable but fail-over-able; 400/422 → `client_error`.
- `RateLimiter.check(client: str, limits: RateLimitConfig) -> RateLimitDecision`
  (allowed, retry_after_s, limit/remaining per dimension, `limit_type` on
  rejection), and `RateLimiter.record_usage(client, limits, tokens)`.
- `Router.complete(request) -> RoutedResult(response, provider, upstream_model, attempts)`
  raises `ModelNotFoundError`, `UpstreamBadRequestError`, `AllProvidersFailedError`.
- `GatewayObserver` callbacks: `on_provider_attempt`, `on_fallback`,
  `on_circuit_state_change`, `on_tokens`, `on_rate_limited`. Core components
  only depend on this protocol; Prometheus is one implementation. This keeps
  the routing and rate-limiting code free of metrics imports and lets the
  three capability groups be built in parallel.

### D4. Token-bucket rate limiting with "debt" for tokens
Requests: classic bucket, cost 1 per request, capacity = RPM.
Tokens: admit while the bucket is > 0, deduct actual `usage.total_tokens`
after the response (bucket can go negative). *Alternative:* pre-estimating
tokens (chars/4 + max_tokens) — rejected because estimates are wrong in both
directions and would reject legitimate requests; debt-based accounting is
exact and self-correcting within a minute. Buckets are created lazily per
client and guarded by a single `asyncio.Lock`-free design (all mutations are
synchronous within one event loop tick, so no lock is needed).

### D5. Retry, failover and circuit breaker
Per target: up to `max_attempts_per_target` attempts with delay
`min(backoff_max_s, backoff_initial_s * 2**n) * uniform(0.5, 1.0)`; then next
target. Circuit breaker per provider (not per target) because outages are
usually provider-wide; states closed → open (after N consecutive failures) →
half-open (after recovery timeout, admitting `half_open_max_calls`) → closed
on success / open on failure. Clock and sleep functions are injected for
deterministic tests. *Alternative:* per-(provider, model) breakers — finer
but slower to detect provider-wide outages; may be revisited.

### D6. Observability
`prometheus-client` with a per-app `CollectorRegistry` (avoids duplicate
registration across tests and app instances). HTTP metrics use the matched
route template as label. Logging uses stdlib `logging` with a JSON formatter
and a `contextvars` request ID injected by a filter — no extra dependency.
The request-ID/access-log middleware is a pure ASGI middleware so it also
wraps error responses.

### D7. Configuration
YAML file path from `LLMGW_CONFIG` (default `config.yaml`, falling back to a
built-in mock-only config if missing so `docker run` works out of the box).
Secrets are referenced by env var name (`api_key_env`, `key_env`) and resolved
at startup; literal client keys (`key`) are allowed for local development.

### D8. Delivery pipeline
GitHub Actions: `ci.yml` (ruff, mypy, pytest matrix 3.11–3.13 with coverage
gate, `openspec validate --strict`, Docker build + container smoke test) on
PRs and pushes; `release.yml` publishes the image to GHCR on pushes to `main`
(`edge`, `sha-*` tags) and on `v*` tags (semver tags + GitHub release).
Multi-stage Dockerfile running as non-root.

## Risks / Trade-offs

- [Per-process state] Limits multiply by replica count and circuits are not
  shared → documented; `RateLimiter` is an interface so a Redis backend can be
  added later.
- [Token debt] A single huge completion can push a client far negative and
  block it for longer than a minute → acceptable and explicit in the spec;
  operators size TPM accordingly.
- [Provider dialect drift] Upstream APIs evolve → adapters are thin and
  covered by contract tests using recorded-shape fixtures.
- [Retry amplification] Retries multiply load during outages → bounded
  attempts, jittered backoff and circuit breakers cap amplification.

## Migration Plan

Greenfield: deploy the container image, mount `config.yaml`, provide provider
keys as environment variables. Rollback is redeploying the previous image tag.
