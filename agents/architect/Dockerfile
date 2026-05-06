# syntax=docker/dockerfile:1.7
# Architect agent — Strands + Bedrock + AgentCore Runtime entrypoint.
# Two-stage uv build: deps installed in a cached layer, source copied separately.

ARG PYTHON_VERSION=3.14

# Trixie variant ships uv 0.11.x (matches pyproject required-version >=0.11);
# the bookworm variant lags at uv 0.9.x.
FROM --platform=linux/arm64 ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-trixie-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
# Workspace members must be present in full (uv builds them); only the leaf
# project's source can be deferred to a separate, source-cacheable layer.
COPY packages/common/ packages/common/
COPY agents/architect/pyproject.toml agents/architect/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --package architect

COPY agents/architect/ agents/architect/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --package architect

FROM --platform=linux/arm64 python:${PYTHON_VERSION}-slim-trixie AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1000 agent \
    && useradd --system --uid 1000 --gid agent --create-home agent \
    && mkdir -p /workspace \
    && chown -R agent:agent /workspace

ENV AIDLC_WORKSPACE_ROOT=/workspace

WORKDIR /app

COPY --from=builder --chown=agent:agent /app /app

USER agent
EXPOSE 8080

# AgentCore Runtime expects POST /invocations + GET /ping on :8080.
CMD ["opentelemetry-instrument", "python", "-m", "architect.app"]
