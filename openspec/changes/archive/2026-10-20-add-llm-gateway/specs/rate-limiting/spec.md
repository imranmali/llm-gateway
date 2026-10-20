# Spec Delta

## Purpose

Protects shared upstream quota and keeps the gateway fair by enforcing
per-client request and token budgets and telling clients how much budget
remains.

## ADDED Requirements

### Requirement: Per-client request rate limit
The gateway SHALL enforce a requests-per-minute limit per client API key using a token bucket whose capacity equals the configured limit and which refills continuously at `limit / 60` requests per second. A limit of `0` SHALL mean unlimited. Limits SHALL be configurable per key with a global default.

#### Scenario: Within limit
- **WHEN** a client with a limit of 2 requests/minute sends 2 requests in quick succession
- **THEN** both requests are processed

#### Scenario: Limit exceeded
- **WHEN** the same client sends a third request immediately afterwards
- **THEN** the gateway responds `429` with error code `rate_limit_exceeded` and no provider is called

#### Scenario: Refill
- **WHEN** enough time has elapsed for at least one request to be refilled
- **THEN** the next request from that client is processed

### Requirement: Per-client token rate limit
The gateway SHALL enforce a tokens-per-minute limit per client API key. A request SHALL be admitted only while the client's token bucket is positive; after a successful completion the actual `usage.total_tokens` SHALL be deducted, which may leave the bucket negative until refilled. A limit of `0` SHALL mean unlimited.

#### Scenario: Token budget exhausted
- **WHEN** a client's completed requests have consumed its whole tokens-per-minute budget
- **THEN** its next request is rejected with `429` and error code `rate_limit_exceeded`

### Requirement: Client isolation
Rate limit state SHALL be tracked independently per client API key so that one client exhausting its budget does not affect any other client.

#### Scenario: Independent buckets
- **WHEN** client A has exhausted its request budget and client B sends a request
- **THEN** client B's request is processed

### Requirement: Rate limit response headers
Every `/v1/chat/completions` response to an authenticated client whose request body passed validation, including `429` and upstream error responses, SHALL include `X-RateLimit-Limit-Requests`, `X-RateLimit-Remaining-Requests`, `X-RateLimit-Limit-Tokens` and `X-RateLimit-Remaining-Tokens` headers for enforced limits. Every `429` response SHALL include a `Retry-After` header with the whole number of seconds (minimum 1) until the request would be admitted.

#### Scenario: Retry-After on rejection
- **WHEN** a request is rejected for exceeding a rate limit
- **THEN** the response includes `Retry-After` with an integer value of at least 1

#### Scenario: Remaining requests reported
- **WHEN** a client with a limit of 5 requests/minute makes its first request
- **THEN** the response carries `X-RateLimit-Limit-Requests: 5` and `X-RateLimit-Remaining-Requests: 4`
