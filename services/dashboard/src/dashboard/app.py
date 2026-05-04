"""FastAPI app entrypoint."""

from __future__ import annotations

from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from dashboard.routes import auth_github, pages, runs, stream, webhooks

logger = structlog.get_logger()

app = FastAPI(title="ai-dlc dashboard")

# Tailwind CSS is built at container build time by the `tailwind` Docker stage
# and dropped into this directory; uv installs the dashboard package in
# editable mode, so __file__ resolves to the source tree at runtime.
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)

app.include_router(pages.router)
app.include_router(runs.router)
app.include_router(stream.router)
app.include_router(webhooks.router)
app.include_router(auth_github.router)
