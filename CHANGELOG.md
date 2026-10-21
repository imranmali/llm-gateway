# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-21

### Added
- OpenAI-compatible `POST /v1/chat/completions` and `GET /v1/models` with
  API-key authentication, `/healthz`, `/readyz` and `/metrics`.
- Provider adapters for OpenAI-compatible APIs, Anthropic Messages API and a
  deterministic mock provider.
- Per-client requests/minute and tokens/minute rate limiting with
  `X-RateLimit-*` and `Retry-After` headers.
- Fallback routing with retries, jittered exponential backoff and per-provider
  circuit breakers.
- Prometheus metrics, structured JSON logs and `X-Request-ID` correlation.
- Docker image, docker-compose stack with Prometheus, CI and release workflows;
  releases can be cut from the Actions tab with a manual `version` input.
