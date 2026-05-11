# syntax=docker/dockerfile:1.7
# Dashboard — FastAPI + Jinja2 + Alpine.js packaged as a Lambda container image.

ARG PYTHON_VERSION=3.14
ARG TAILWIND_VERSION=v4.2.4

# Build stage 1 — generate Tailwind CSS using the standalone v4 CLI (single
# static binary, no Node runtime). The Play CDN is unreliable when paired
# with Alpine.js (race conditions on DOM scan), so we build CSS at image
# build time and serve it as a static asset.
FROM --platform=linux/arm64 debian:trixie-slim AS assets
ARG TAILWIND_VERSION

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL -o /usr/local/bin/tailwindcss \
        "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-linux-arm64" \
    && chmod +x /usr/local/bin/tailwindcss

WORKDIR /app
COPY services/dashboard/src/dashboard/templates/ services/dashboard/src/dashboard/templates/
COPY services/dashboard/src/dashboard/static/tailwind.input.css services/dashboard/src/dashboard/static/tailwind.input.css

RUN tailwindcss \
        -i services/dashboard/src/dashboard/static/tailwind.input.css \
        -o services/dashboard/src/dashboard/static/tailwind.css \
        --minify

# Build stage 2 — Python deps via uv. Trixie variant ships uv 0.11.x (matches
# pyproject required-version >=0.11); the bookworm variant lags at uv 0.9.x.
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

# Build stage 3 — Lambda runtime. The AWS-provided base image bundles the
# Lambda Runtime Interface Client; CMD names the handler the RIC invokes.
# Mangum (registered as ``dashboard.app.handler``) translates the API
# Gateway proxy event into ASGI calls for FastAPI.
FROM --platform=linux/arm64 public.ecr.aws/lambda/python:${PYTHON_VERSION} AS runtime

# Site-packages from the uv-resolved venv land directly on PYTHONPATH; the
# dashboard package itself is installed editable, so we copy its source
# tree too. ``${LAMBDA_TASK_ROOT}`` is the Lambda working directory and is
# already on sys.path by default.
COPY --from=builder /app/.venv/lib/python${PYTHON_VERSION}/site-packages/ ${LAMBDA_TASK_ROOT}/
COPY --from=builder /app/packages/common/src/common ${LAMBDA_TASK_ROOT}/common
COPY --from=builder /app/services/dashboard/src/dashboard ${LAMBDA_TASK_ROOT}/dashboard
COPY --from=assets /app/services/dashboard/src/dashboard/static/tailwind.css \
                   ${LAMBDA_TASK_ROOT}/dashboard/static/tailwind.css

CMD ["dashboard.app.handler"]
