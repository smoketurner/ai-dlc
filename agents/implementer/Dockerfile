# syntax=docker/dockerfile:1.7
# Implementer agent — Claude Agent SDK + Bedrock + AgentCore Runtime entrypoint.
#
# The Claude Agent SDK spawns the Claude Code CLI as a subprocess, so the
# runtime image needs Node.js, the @anthropic-ai/claude-code npm package,
# and git for repo operations.

ARG PYTHON_VERSION=3.14
ARG UV_VERSION=0.11.8
ARG NODE_MAJOR=22
ARG CLAUDE_CODE_VERSION=2.0.39

FROM --platform=linux/arm64 ghcr.io/astral-sh/uv:${UV_VERSION}-python${PYTHON_VERSION}-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
COPY packages/common/pyproject.toml packages/common/
COPY agents/implementer/pyproject.toml agents/implementer/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --package implementer

COPY packages/common/ packages/common/
COPY agents/implementer/ agents/implementer/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --package implementer

FROM --platform=linux/arm64 python:${PYTHON_VERSION}-slim-bookworm AS runtime

ARG NODE_MAJOR
ARG CLAUDE_CODE_VERSION

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:/usr/local/bin:$PATH" \
    AIDLC_WORKSPACE_ROOT=/workspace \
    AIDLC_WORKSPACE=/workspace/repo \
    CLAUDE_CODE_USE_BEDROCK=1

# git for repo ops; nodejs for the Claude Code CLI; ca-certificates so HTTPS works.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1000 agent \
    && useradd --system --uid 1000 --gid agent --create-home agent \
    && mkdir -p /workspace \
    && chown -R agent:agent /workspace

WORKDIR /app

COPY --from=builder --chown=agent:agent /app /app

USER agent
EXPOSE 8080

CMD ["python", "-m", "implementer.app"]
