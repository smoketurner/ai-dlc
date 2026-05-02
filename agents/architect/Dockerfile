# syntax=docker/dockerfile:1.7
# Architect agent — Strands + Bedrock + AgentCore Runtime entrypoint.
# Two-stage uv build: deps installed in a cached layer, source copied separately.

ARG PYTHON_VERSION=3.14
ARG UV_VERSION=0.11.8

FROM --platform=linux/arm64 ghcr.io/astral-sh/uv:${UV_VERSION}-python${PYTHON_VERSION}-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
COPY packages/common/pyproject.toml packages/common/
COPY agents/architect/pyproject.toml agents/architect/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --package architect

COPY packages/common/ packages/common/
COPY agents/architect/ agents/architect/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --package architect

FROM --platform=linux/arm64 python:${PYTHON_VERSION}-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1000 agent \
    && useradd --system --uid 1000 --gid agent --create-home agent

WORKDIR /app

COPY --from=builder --chown=agent:agent /app /app

USER agent
EXPOSE 8080

# AgentCore Runtime expects POST /invocations + GET /ping on :8080.
CMD ["python", "-m", "architect.app"]
