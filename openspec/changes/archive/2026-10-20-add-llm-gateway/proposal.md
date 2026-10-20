# Proposal

## Why

Applications that call LLMs directly are coupled to one vendor's API, have no
protection against a single noisy client exhausting shared quota, fail hard
when a provider has an outage, and offer little visibility into cost and
latency. A small, self-hostable gateway that speaks the OpenAI Chat
Completions dialect lets every client use one API while the platform team
controls quotas, failover and telemetry centrally.

## What Changes

- New Python service (`llm_gateway`) exposing an OpenAI-compatible
  `POST /v1/chat/completions` and `GET /v1/models` API, with API-key
  authentication and health/readiness endpoints.
- Provider adapters for OpenAI-compatible APIs, Anthropic Messages API and a
  deterministic mock provider (for local development, demos and tests).
- Per-API-key rate limiting (requests/minute and tokens/minute) using token
  buckets, returning `429` with `Retry-After` and `X-RateLimit-*` headers.
- Model routing: a public model alias maps to an ordered list of
  provider/model targets with retries, exponential backoff, failover and a
  per-provider circuit breaker.
- Observability: Prometheus metrics at `/metrics`, structured JSON logs with
  request-ID correlation, and response headers describing the routing outcome.
- Configuration via a YAML file plus `LLMGW_`-prefixed environment variables;
  provider secrets only via environment variables.
- Delivery: Dockerfile, docker-compose example, GitHub Actions workflows for
  CI (lint, type-check, tests with coverage, spec validation, image smoke test)
  and deployment (container image publish to GHCR, GitHub release on tags),
  README, contribution guide and MIT license.

## Out of Scope

- Streaming responses (`stream: true` is rejected with a clear 400 error).
- Shared/distributed state (Redis etc.) for rate limits and circuit breakers;
  state is per process in v1 behind interfaces that allow a later backend.
- Embeddings, image, audio and legacy completions endpoints.
- Response caching, prompt/response persistence, billing and a management UI.
- Tool/function-calling translation between provider dialects beyond plain
  text chat messages.

## Capabilities

### New Capabilities

- `chat-gateway`: OpenAI-compatible chat completion and model listing API,
  client authentication, request validation, error format and health
  endpoints, plus translation to/from upstream provider APIs.
- `rate-limiting`: Per-client request and token quotas, enforcement responses
  and quota headers.
- `fallback-routing`: Mapping of public model names to ordered provider
  targets, retry/backoff, failover and circuit-breaking behaviour.
- `observability`: Metrics exposition, structured logging, request
  correlation and routing-outcome headers.

### Modified Capabilities

None — this is the first change in the repository.

## Impact

- New code under `src/llm_gateway/`, tests under `tests/`.
- New runtime dependencies: FastAPI, Uvicorn, httpx, pydantic,
  pydantic-settings, PyYAML, prometheus-client.
- New CI/CD workflows under `.github/workflows/` and a container image
  published to `ghcr.io/imranmali/llm-gateway`.
