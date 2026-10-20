# Tasks

## 1. Core scaffolding (sequential, before parallel groups)

- [x] 1.1 Create `pyproject.toml` (hatchling, src layout, runtime + dev dependencies, ruff/mypy/pytest/coverage config) and verify `pip install -e ".[dev]"` succeeds
- [x] 1.2 Implement `config.py`, `schemas.py`, `errors.py`, `events.py` per design D2/D3 and verify with unit tests in `tests/unit/test_config.py`, `tests/unit/test_schemas.py`
- [x] 1.3 Implement `providers/base.py` and `providers/mock.py` (deterministic content, configurable failure modes) and verify with `tests/unit/test_mock_provider.py`

## 2. Rate limiting (parallel group A)

- [x] 2.1 Implement token bucket with injectable clock in `ratelimit.py` and verify capacity, refill and unlimited (`0`) behaviour with unit tests
- [x] 2.2 Implement `InMemoryRateLimiter.check`/`record_usage` with per-client isolation, token debt and `Retry-After` computation; verify with unit tests covering every scenario in `specs/rate-limiting/spec.md`
- [x] 2.3 Implement rate-limit header rendering helper and verify header names/values with unit tests

## 3. Fallback routing and provider adapters (parallel group B)

- [x] 3.1 Implement `CircuitBreaker` (closed/open/half-open, injectable clock, observer notifications) and verify transitions with unit tests
- [x] 3.2 Implement `Router` with ordered targets, jittered exponential backoff (injectable sleep), failover, bad-request short-circuit and all-failed/timeout errors; verify every scenario in `specs/fallback-routing/spec.md` with unit tests
- [x] 3.3 Implement OpenAI-compatible and Anthropic adapters with error classification; verify request/response translation and error mapping with `httpx.MockTransport` tests
- [x] 3.4 Implement `providers/registry.py` building providers from config (secrets from env) and verify with unit tests

## 4. Observability (parallel group C)

- [x] 4.1 Implement Prometheus `MetricsObserver` with per-app registry and all metrics named in `specs/observability/spec.md`; verify with unit tests reading the registry
- [x] 4.2 Implement JSON log formatter and request-ID context filter; verify output is valid JSON with request ID and no secrets
- [x] 4.3 Implement ASGI middleware for request-ID propagation/validation, access logging and HTTP metrics using route templates; verify with a minimal Starlette app in tests

## 5. HTTP surface and integration

- [x] 5.1 Implement `auth.py` (Bearer / X-API-Key, constant-time compare, anonymous mode) and verify with unit tests
- [x] 5.2 Implement `api.py` + `app.py` (`create_app`) wiring config, providers, limiter, router, observer, middleware and exception handlers; verify `/v1/chat/completions`, `/v1/models`, `/healthz`, `/readyz`, `/metrics` with integration tests in `tests/integration/`
- [x] 5.3 Add `__main__.py` entrypoint and `config.example.yaml`; verify `python -m llm_gateway` serves `/healthz` with the example config
- [x] 5.4 Run end-to-end scenario tests (rate limit → 429 headers, failover headers/metrics, circuit open → readyz 503) and verify coverage >= 85%

## 6. Delivery, docs and licensing

- [ ] 6.1 Add multi-stage non-root `Dockerfile`, `.dockerignore` and `docker-compose.yml`; verify `docker build` succeeds and the container answers `/healthz`
- [ ] 6.2 Add `.github/workflows/ci.yml` (lint, type-check, test matrix + coverage, OpenSpec validation, Docker smoke test) and verify it passes on the pull request
- [ ] 6.3 Add `.github/workflows/release.yml` (GHCR publish on `main` and `v*` tags, GitHub release on tags) plus Dependabot config and PR template; verify workflow syntax with the CI run
- [x] 6.4 Write `README.md` (features, quick start, configuration reference, API, metrics, architecture, development, deployment), `CONTRIBUTING.md` and `CHANGELOG.md`; confirm `LICENSE` (MIT) is referenced and README commands run as written

## Workflow follow-up

- Archive the change with `openspec archive add-llm-gateway` once CI is green, so the delta specs become the main specs under `openspec/specs/`.
- Verify `openspec validate --all --strict` passes after archiving, then merge the pull request into `main`.
