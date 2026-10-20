# fallback-routing Specification

## Purpose
Keeps chat completions available during upstream failures by routing each
public model name to an ordered list of provider targets with retries,
failover and circuit breaking.

## Requirements

### Requirement: Ordered route targets
Each public model name SHALL map to an ordered, non-empty list of targets, each naming a configured provider and the upstream model to request. The gateway SHALL try targets in the configured order and return the first successful response.

#### Scenario: Primary succeeds
- **WHEN** the first target of a route responds successfully
- **THEN** the client receives that response and no other target is called

### Requirement: Retry with exponential backoff
For retryable failures — connection errors, timeouts, upstream `429` and upstream `5xx` — the gateway SHALL retry the same target up to the configured attempts per target, waiting an exponentially increasing, jittered delay bounded by a configured maximum between attempts.

#### Scenario: Transient error recovered
- **WHEN** a target fails once with `503` and then succeeds, with 2 attempts per target configured
- **THEN** the client receives the successful response and the attempt count is 2

### Requirement: Failover to next target
When a target's attempts are exhausted with retryable failures, or it fails with an upstream authentication/authorization/not-found error (`401`, `403`, `404`), the gateway SHALL fail over to the next target in the route.

#### Scenario: Primary down, secondary serves
- **WHEN** the primary target keeps failing with `500` and the secondary succeeds
- **THEN** the client receives the secondary's response and the response identifies the secondary provider as the one that served it

### Requirement: Non-retryable client errors
When a target rejects the request as invalid (upstream `400` or `422`), the gateway SHALL NOT retry or fail over and SHALL return a `400` error with code `upstream_bad_request` to the client.

#### Scenario: Invalid request not retried
- **WHEN** the primary target answers `400`
- **THEN** the client receives `400` and no further attempts are made

### Requirement: All targets failed
When every target of a route has failed or been skipped, the gateway SHALL respond `502` with error code `all_providers_failed` (or `504` with `upstream_timeout` when every attempt timed out) and a message summarising the attempts without exposing credentials.

#### Scenario: Everything down
- **WHEN** every target of a route fails with retryable errors
- **THEN** the client receives `502` with error code `all_providers_failed`

### Requirement: Per-provider circuit breaker
The gateway SHALL maintain a circuit breaker per provider. After the configured number of consecutive failed attempts the circuit SHALL open and the provider SHALL be skipped without being called. After the recovery timeout the circuit SHALL become half-open and admit a limited number of trial calls; a successful trial SHALL close it and a failed trial SHALL re-open it. Client errors (`400`/`422`) SHALL NOT count as failures.

#### Scenario: Circuit opens
- **WHEN** a provider fails the configured threshold of consecutive attempts
- **THEN** subsequent requests skip that provider without calling it until the recovery timeout elapses

#### Scenario: Circuit recovers
- **WHEN** the recovery timeout has elapsed and the trial call succeeds
- **THEN** the circuit closes and the provider receives traffic again
