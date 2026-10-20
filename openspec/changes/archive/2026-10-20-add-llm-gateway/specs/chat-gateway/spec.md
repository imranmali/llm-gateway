# Spec Delta

## Purpose

Provides a single OpenAI-compatible HTTP API through which clients send chat
completion requests to any configured LLM provider, with authentication,
validation, a uniform error format and health endpoints.

## ADDED Requirements

### Requirement: OpenAI-compatible chat completions endpoint
The gateway SHALL accept `POST /v1/chat/completions` with an OpenAI Chat Completions request body (`model`, `messages`, and optional `temperature`, `top_p`, `max_tokens`, `stop`, `user`) and SHALL respond with an OpenAI Chat Completions response body containing `id`, `object` = `chat.completion`, `created`, `model`, `choices` and `usage`.

#### Scenario: Successful completion
- **WHEN** an authenticated client posts a valid request for a configured model
- **THEN** the gateway responds `200` with a `chat.completion` object whose `model` is the requested public model name and whose `usage.total_tokens` equals `prompt_tokens + completion_tokens`

#### Scenario: Unknown model
- **WHEN** a client requests a model name that no route defines
- **THEN** the gateway responds `404` with error code `model_not_found`

### Requirement: Request validation
The gateway SHALL reject malformed requests with status `400` (or `422` for schema violations) before contacting any provider. A request with an empty `messages` list, or a message whose `role` is not one of `system`, `user`, `assistant`, SHALL be rejected.

#### Scenario: Empty messages
- **WHEN** a client posts a request whose `messages` array is empty
- **THEN** the gateway responds with a `4xx` error and no provider is called

#### Scenario: Streaming requested
- **WHEN** a client posts a request with `stream: true`
- **THEN** the gateway responds `400` with error code `streaming_not_supported`

### Requirement: Uniform error format
Every error response produced by the gateway SHALL use the OpenAI error envelope `{"error": {"message": <string>, "type": <string>, "code": <string>}}` and SHALL never include upstream provider credentials.

#### Scenario: Error envelope
- **WHEN** any request fails (authentication, validation, rate limit, routing or upstream failure)
- **THEN** the response body is a JSON object with an `error` object containing `message`, `type` and `code`

### Requirement: API key authentication
When client API keys are configured, the gateway SHALL require a valid key on every `/v1/*` request, supplied as `Authorization: Bearer <key>` or `X-API-Key: <key>`, and SHALL compare keys in constant time. When no client keys are configured the gateway SHALL treat all callers as a single `anonymous` client.

#### Scenario: Missing key
- **WHEN** keys are configured and a request has no credentials
- **THEN** the gateway responds `401` with error code `invalid_api_key`

#### Scenario: Valid key via X-API-Key
- **WHEN** a request supplies a configured key in the `X-API-Key` header
- **THEN** the request is processed as that client

### Requirement: Model listing
The gateway SHALL expose `GET /v1/models` returning an OpenAI list object (`object` = `list`) whose `data` contains one entry per configured public model name.

#### Scenario: List configured models
- **WHEN** an authenticated client calls `GET /v1/models`
- **THEN** the response lists every configured route's public model name with `object` = `model`

### Requirement: Health and readiness endpoints
The gateway SHALL expose unauthenticated `GET /healthz` that returns `200` while the process is serving, and `GET /readyz` that returns `200` when at least one target of every route is available (circuit not open) and `503` otherwise, including per-provider status in the body.

#### Scenario: Liveness
- **WHEN** an orchestrator calls `GET /healthz`
- **THEN** the gateway responds `200` with `{"status": "ok"}`

#### Scenario: Not ready when all targets unavailable
- **WHEN** every target of some route has an open circuit
- **THEN** `GET /readyz` responds `503`

### Requirement: Provider translation
The gateway SHALL translate requests and responses between the OpenAI format and each supported upstream API: OpenAI-compatible endpoints (pass-through), Anthropic Messages API (system messages moved to the `system` field, `max_tokens` defaulted when absent, `stop_reason` mapped to `finish_reason`), and a built-in mock provider that returns deterministic content without network access.

#### Scenario: Anthropic system prompt mapping
- **WHEN** a request containing a `system` message is routed to an Anthropic target
- **THEN** the upstream request carries that text in its top-level `system` field and the `messages` array contains only `user`/`assistant` turns

#### Scenario: Mock provider
- **WHEN** a request is routed to a mock target
- **THEN** the gateway returns a completion without any network call and with non-zero token usage
