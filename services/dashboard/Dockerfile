# syntax=docker/dockerfile:1.7
# Dashboard — FastAPI + Jinja2 + Alpine.js. Runs uvicorn on :8080.

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
COPY services/dashboard/pyproject.toml services/dashboard/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --package dashboard

COPY services/dashboard/ services/dashboard/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --package dashboard

FROM --platform=linux/arm64 python:${PYTHON_VERSION}-slim-trixie AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1000 web \
    && useradd --system --uid 1000 --gid web --create-home web

WORKDIR /app

COPY --from=builder --chown=web:web /app /app

USER web
EXPOSE 8080

CMD ["uvicorn", "dashboard.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--loop", "uvloop", "--http", "httptools"]
