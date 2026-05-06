# syntax=docker/dockerfile:1.7
# Implementer agent — Claude Agent SDK + Bedrock + AgentCore Runtime entrypoint.
#
# The Claude Agent SDK spawns the Claude Code CLI as a subprocess, so the
# runtime image needs Node.js, the @anthropic-ai/claude-code npm package,
# and git for repo operations.

ARG PYTHON_VERSION=3.14
ARG NODE_MAJOR=22
ARG CLAUDE_CODE_VERSION=2.1.126

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
COPY agents/implementer/pyproject.toml agents/implementer/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --package implementer

COPY agents/implementer/ agents/implementer/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --package implementer

FROM --platform=linux/arm64 python:${PYTHON_VERSION}-slim-trixie AS runtime

ARG NODE_MAJOR
ARG CLAUDE_CODE_VERSION

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:/usr/local/bin:$PATH" \
    AIDLC_WORKSPACE_ROOT=/workspace \
    AIDLC_WORKSPACE=/workspace/repo \
    CLAUDE_CODE_USE_BEDROCK=1

# git for repo ops; nodejs for the Claude Code CLI; ca-certificates so HTTPS
# works; curl + jq for endpoint probing and JSON inspection; make as the
# canonical lint/test entrypoint many project Makefiles encode; ripgrep + fd
# because Claude Code's instructions tell it to prefer ``rg`` over ``grep``
# and ``fd`` over ``find`` — having them present unlocks the faster paths.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fd-find \
        git \
        gnupg \
        jq \
        make \
        ripgrep \
    && curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && apt-get purge -y --auto-remove gnupg \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/bin/fdfind /usr/local/bin/fd \
    && groupadd --system --gid 1000 agent \
    && useradd --system --uid 1000 --gid agent --create-home agent \
    && mkdir -p /workspace \
    && chown -R agent:agent /workspace

# uv is the project repos' build/test driver — the agent is told to run
# ``uv sync`` and ``uv run ruff/ty/pytest`` in /workspace/repo, so it must
# be on PATH here. The astral builder image already has both binaries.
COPY --from=builder /usr/local/bin/uv /usr/local/bin/uvx /usr/local/bin/

WORKDIR /app

COPY --from=builder --chown=agent:agent /app /app

USER agent
EXPOSE 8080

CMD ["opentelemetry-instrument", "python", "-m", "implementer.app"]
