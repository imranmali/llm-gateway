# syntax=docker/dockerfile:1

# ---- build stage: produce a wheel -------------------------------------------
FROM python:3.15-slim AS build
WORKDIR /src
RUN pip install --no-cache-dir build==1.2.2
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m build --wheel --outdir /dist

# ---- runtime stage -----------------------------------------------------------
FROM python:3.15-slim AS runtime

LABEL org.opencontainers.image.title="llm-gateway" \
      org.opencontainers.image.description="OpenAI-compatible LLM gateway with rate limiting, fallback routing and observability" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/imranmali/llm-gateway"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    LLMGW_HOST=0.0.0.0 \
    LLMGW_PORT=8080 \
    LLMGW_CONFIG=/etc/llm-gateway/config.yaml

COPY --from=build /dist/*.whl /tmp/
RUN pip install /tmp/*.whl && rm /tmp/*.whl \
    && useradd --system --uid 10001 --no-create-home gateway \
    && mkdir -p /etc/llm-gateway

USER 10001
EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"LLMGW_PORT\",\"8080\")}/healthz',timeout=2)" || exit 1

ENTRYPOINT ["llm-gateway"]
