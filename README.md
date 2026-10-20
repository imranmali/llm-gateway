# llm-gateway

[![CI](https://github.com/imranmali/llm-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/imranmali/llm-gateway/actions/workflows/ci.yml)
[![Release](https://github.com/imranmali/llm-gateway/actions/workflows/release.yml/badge.svg)](https://github.com/imranmali/llm-gateway/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)

An **OpenAI-compatible LLM gateway** with **per-client rate limiting**,
**fallback routing** (retries, failover, circuit breakers) and
**observability** (Prometheus metrics, structured JSON logs, request IDs).

Point any OpenAI SDK at the gateway, and the platform team controls quotas,
provider failover and telemetry in one place.

```
            ┌──────────────────────────── llm-gateway ────────────────────────────┐
 client ──► │ request-ID/access-log ─► auth ─► rate limiter ─► router ─► provider │ ──► OpenAI
 (OpenAI    │   middleware              │        (token         │  retries,   adapters │ ──► Anthropic
  SDK)      │        │                  │         buckets)      │  failover,           │ ──► mock / vLLM / …
            │        ▼                  ▼                       ▼  circuit breakers     │
            │   JSON logs          401 / 429           /metrics (Prometheus)           │
            └─────────────────────────────────────────────────────────────────────────┘
```

## Features

| Area | What you get |
|---|---|
| **API** | `POST /v1/chat/completions`, `GET /v1/models` (OpenAI wire format and error envelope), `GET /healthz`, `GET /readyz`, `GET /metrics` |
| **Providers** | OpenAI and any OpenAI-compatible server (vLLM, Ollama, Together, …), Anthropic Messages API, and a deterministic `mock` provider |
| **Auth** | Client API keys via `Authorization: Bearer` or `X-API-Key`, constant-time comparison; anonymous mode when no keys are configured |
| **Rate limiting** | Per-client requests/minute and tokens/minute token buckets; `429` with `Retry-After` and `X-RateLimit-*` headers |
| **Fallback routing** | Public model alias → ordered provider targets; jittered exponential backoff; failover; per-provider circuit breaker (closed → open → half-open) |
| **Observability** | Prometheus metrics, one JSON access log per request, `X-Request-ID` propagation, `X-Gateway-*` routing headers |
| **Delivery** | Non-root Docker image, docker-compose with Prometheus, GitHub Actions CI + GHCR release pipeline |

## Quick start

### Docker Compose (gateway + Prometheus, no API keys needed)

```bash
docker compose up --build
# in another shell
curl -s http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-gw-demo" -H "Content-Type: application/json" \
  -d '{"model":"demo","messages":[{"role":"user","content":"Hello!"}]}' -i
```

The `demo` model routes to an always-failing mock first, so the response shows
failover in action (`X-Gateway-Provider: mock-primary`,
`X-Gateway-Attempts: 3`). Prometheus is at <http://localhost:9090>.

### Local Python

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
export LLMGW_CONFIG=config.example.yaml LLMGW_DEMO_KEY=sk-gw-demo
llm-gateway            # or: python -m llm_gateway
```

### With the OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8080/v1", api_key="sk-gw-demo")
reply = client.chat.completions.create(
    model="demo", messages=[{"role": "user", "content": "Hi"}]
)
print(reply.choices[0].message.content)
```

## Configuration

Process settings come from environment variables (prefix `LLMGW_`); routing
comes from a YAML file. Provider and client secrets are referenced **by
environment variable name** and never stored in the file.

| Variable | Default | Purpose |
|---|---|---|
| `LLMGW_CONFIG` | `config.yaml` (`/etc/llm-gateway/config.yaml` in Docker) | Path to the YAML config. If missing, a mock-only config serving `mock-model` is used. |
| `LLMGW_HOST` | `0.0.0.0` | Bind address |
| `LLMGW_PORT` | `8080` | Port |
| `LLMGW_LOG_LEVEL` | `INFO` | Root log level |

YAML reference (see [`config.example.yaml`](config.example.yaml) for a complete file):

```yaml
clients:                       # omit for anonymous mode
  - name: team-a
    key_env: TEAM_A_KEY        # or `key: <literal>` for local development only
    rate_limit: { requests_per_minute: 120, tokens_per_minute: 200000 }  # 0 = unlimited

default_rate_limit: { requests_per_minute: 60, tokens_per_minute: 100000 }

providers:
  openai:    { type: openai, api_key_env: OPENAI_API_KEY, timeout_s: 30 }
  anthropic: { type: anthropic, api_key_env: ANTHROPIC_API_KEY, timeout_s: 60 }
  vllm:      { type: openai, base_url: "http://vllm:8000/v1" }
  mock:      { type: mock, latency_s: 0, fail_times: 0, fail_status: 503, fail_timeout: false }

routes:
  - model: smart               # the name clients send
    targets:                   # tried in order
      - { provider: openai, model: gpt-4o }
      - { provider: anthropic, model: claude-sonnet-4-5 }

routing:
  max_attempts_per_target: 2
  backoff_initial_s: 0.2       # delay = min(max, initial * 2^n) * U(0.5, 1)
  backoff_max_s: 2.0
  circuit_breaker: { failure_threshold: 5, recovery_timeout_s: 30, half_open_max_calls: 1 }
```

The configuration is validated at startup: unknown providers in routes,
duplicate routes or client names, and missing secret environment variables
abort startup with a clear error.

## Behaviour

### Rate limiting
- Two token buckets per client: **requests** (cost 1) and **tokens**
  (actual `usage.total_tokens`, deducted after the response). A request is
  admitted while the token bucket is positive, so a large completion can put
  a client into "debt" until the bucket refills.
- Responses carry `X-RateLimit-Limit-Requests`, `X-RateLimit-Remaining-Requests`,
  `X-RateLimit-Limit-Tokens`, `X-RateLimit-Remaining-Tokens`; rejections are
  `429 rate_limit_exceeded` with `Retry-After` (seconds, ≥ 1).

### Fallback routing

| Upstream outcome | Retry same target | Fail over | Counts toward circuit |
|---|---|---|---|
| timeout, connection error, `429`, `5xx` | yes (with backoff) | after attempts are exhausted | yes |
| `401`, `403`, `404` | no | immediately | yes |
| `400`, `422` | no | no — client gets `400 upstream_bad_request` | no |

When every target fails the client gets `502 all_providers_failed`, or
`504 upstream_timeout` if every attempt timed out. A provider whose circuit is
open is skipped without being called; `/readyz` returns `503` while any route
has no available target.

### Response headers

| Header | Meaning |
|---|---|
| `X-Request-ID` | Echoes a valid incoming ID (`[A-Za-z0-9._-]{1,128}`) or a generated one |
| `X-Gateway-Provider` | Provider that served the response |
| `X-Gateway-Upstream-Model` | Upstream model used |
| `X-Gateway-Attempts` | Upstream calls made (including retries/failovers) |

### Errors
All errors use the OpenAI envelope:
`{"error": {"message": "...", "type": "...", "code": "..."}}` with codes
`invalid_api_key` (401), `invalid_request` / `streaming_not_supported` /
`upstream_bad_request` (400), `model_not_found` (404), `rate_limit_exceeded`
(429), `all_providers_failed` (502), `upstream_timeout` (504).

## Observability

### Metrics (`GET /metrics`)

| Metric | Type | Labels |
|---|---|---|
| `llmgw_http_requests_total` | counter | `method`, `route`, `status` |
| `llmgw_http_request_duration_seconds` | histogram | `method`, `route` |
| `llmgw_provider_requests_total` | counter | `provider`, `model`, `outcome` (`success`, `error`, `timeout`, `client_error`, `skipped`) |
| `llmgw_provider_request_duration_seconds` | histogram | `provider`, `model` |
| `llmgw_tokens_total` | counter | `provider`, `model`, `type` (`prompt`, `completion`) |
| `llmgw_fallbacks_total` | counter | `from_provider`, `to_provider` |
| `llmgw_rate_limited_total` | counter | `client`, `limit` (`requests`, `tokens`) |
| `llmgw_circuit_breaker_state` | gauge | `provider` (0 closed, 1 half-open, 2 open) |
| `llmgw_build_info` | gauge | `version` |

Example PromQL:

```promql
# p95 upstream latency per provider
histogram_quantile(0.95, sum by (le, provider) (rate(llmgw_provider_request_duration_seconds_bucket[5m])))
# failover rate
sum by (from_provider, to_provider) (rate(llmgw_fallbacks_total[5m]))
# tokens per minute by model
sum by (model) (rate(llmgw_tokens_total[1m])) * 60
```

### Logs
One JSON line per request on stderr (logger `llm_gateway.access`) with `ts`,
`level`, `request_id`, `method`, `path`, `route`, `status`, `duration_ms` and
`client`. API keys, provider credentials and message content are never logged.

## Development

The project is planned and tracked with
[OpenSpec](https://github.com/Fission-AI/OpenSpec): the living specification is
in [`openspec/specs/`](openspec/specs), and completed changes are archived in
[`openspec/changes/archive/`](openspec/changes/archive).

```bash
pip install -e ".[dev]"
ruff check src tests && ruff format --check src tests
mypy                       # strict
pytest --cov               # unit + integration tests, coverage gate ≥ 85%
npx @fission-ai/openspec validate --all --strict
```

Layout:

```
src/llm_gateway/
  app.py, api.py, auth.py, state.py   HTTP surface and wiring
  config.py, schemas.py, errors.py    configuration, wire models, error envelope
  events.py                           observer protocol (decouples telemetry)
  ratelimit.py                        token buckets and per-client limiter
  routing.py                          router, retries, circuit breakers
  providers/                          openai, anthropic, mock adapters + registry
  observability/                      metrics, JSON logging, ASGI middleware
tests/unit, tests/integration          hermetic tests (no network, fake clocks)
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the branch, commit and OpenSpec workflow.

## CI/CD

| Workflow | Trigger | Jobs |
|---|---|---|
| [`ci.yml`](.github/workflows/ci.yml) | pull requests, manual, called by release | ruff, mypy, pytest on 3.11/3.12/3.13 with coverage gate, strict OpenSpec validation, Docker build + container smoke test |
| [`release.yml`](.github/workflows/release.yml) | push to `main`, `v*.*.*` tags | runs CI, publishes `ghcr.io/imranmali/llm-gateway` (`edge`, `sha-*`, semver, `latest`), creates a GitHub release for tags, optional webhook deploy |

To enable automatic deployment, set the repository variable `DEPLOY_ENABLED=true`
and the secret `DEPLOY_WEBHOOK_URL` (for example a Render, Railway or Coolify
deploy hook) in the `production` environment.

## Limitations

- Streaming (`stream: true`) is not supported yet and returns `400`.
- Rate limit and circuit breaker state is per process: with N replicas the
  effective limit is N × the configured value.
- Only plain-text chat messages are translated between provider dialects.

## License

[MIT](LICENSE) © 2026 Imran Ali
