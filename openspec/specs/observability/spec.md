# observability Specification

## Purpose
Gives operators visibility into gateway traffic, latency, token usage,
failover and rate limiting through metrics, structured logs and correlated
request identifiers.

## Requirements

### Requirement: Prometheus metrics endpoint
The gateway SHALL expose `GET /metrics` in Prometheus text exposition format without authentication.

#### Scenario: Scrape metrics
- **WHEN** a scraper calls `GET /metrics`
- **THEN** the gateway responds `200` with content type `text/plain` in Prometheus exposition format

### Requirement: Traffic and latency metrics
The gateway SHALL record HTTP request counts by method, route template and status, and HTTP request latency histograms by method and route template, using the route template (not the raw path) to bound label cardinality.

#### Scenario: Request counted
- **WHEN** a client completes a request to `/v1/chat/completions`
- **THEN** `llmgw_http_requests_total` with that route and status increases by 1

### Requirement: Provider, token and failover metrics
The gateway SHALL record, per provider and upstream model: attempt counts by outcome, attempt latency histograms, and prompt/completion token counters. It SHALL count fallbacks between providers, rate-limit rejections by limit type, and expose each provider's circuit state as a gauge (0 closed, 1 half-open, 2 open).

#### Scenario: Fallback counted
- **WHEN** a request fails over from provider A to provider B
- **THEN** `llmgw_fallbacks_total{from_provider="A",to_provider="B"}` increases by 1

#### Scenario: Tokens counted
- **WHEN** a completion reports 10 prompt and 5 completion tokens
- **THEN** `llmgw_tokens_total` increases by 10 for type `prompt` and by 5 for type `completion` for that provider and model

#### Scenario: Rate limit counted
- **WHEN** a request is rejected by the requests-per-minute limit
- **THEN** `llmgw_rate_limited_total{limit="requests"}` increases by 1

### Requirement: Request correlation ID
The gateway SHALL accept an incoming `X-Request-ID` header (up to 128 characters of `[A-Za-z0-9._-]`) or generate a new unique ID otherwise, return it in the `X-Request-ID` response header, and include it in every log record emitted while handling that request.

#### Scenario: Propagated ID
- **WHEN** a client sends `X-Request-ID: abc-123`
- **THEN** the response carries `X-Request-ID: abc-123`

#### Scenario: Generated ID
- **WHEN** a client sends no or an invalid `X-Request-ID`
- **THEN** the response carries a newly generated non-empty `X-Request-ID`

### Requirement: Structured access logs
The gateway SHALL emit one JSON log record per HTTP request containing at least timestamp, level, request ID, method, path, status, duration in milliseconds and client name, and SHALL NOT log API keys, provider credentials or message content.

#### Scenario: Access log emitted
- **WHEN** a request completes
- **THEN** exactly one access log record in JSON with the fields above is written and it contains no API key value

### Requirement: Routing outcome headers
Successful completion responses SHALL include `X-Gateway-Provider` (provider that served the request), `X-Gateway-Upstream-Model` (upstream model used) and `X-Gateway-Attempts` (total upstream attempts made).

#### Scenario: Outcome headers after fallback
- **WHEN** a request is served by the secondary target after one failed primary attempt
- **THEN** `X-Gateway-Provider` names the secondary provider and `X-Gateway-Attempts` is at least 2
