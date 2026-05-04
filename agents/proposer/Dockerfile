# syntax=docker/dockerfile:1.7
# Proposer agent — Strands + Bedrock + AgentCore Runtime entrypoint.

ARG PYTHON_VERSION=3.14

FROM --platform=linux/arm64 ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-trixie-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
COPY packages/common/ packages/common/
COPY agents/proposer/pyproject.toml agents/proposer/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --package proposer

COPY agents/proposer/ agents/proposer/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --package proposer

FROM --platform=linux/arm64 python:${PYTHON_VERSION}-slim-trixie AS runtime

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

CMD ["python", "-m", "proposer.app"]
