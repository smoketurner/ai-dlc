"""FastAPI app entrypoint."""

from __future__ import annotations

import structlog
from fastapi import FastAPI

from dashboard.routes import pages, runs, stream, webhooks

logger = structlog.get_logger()

app = FastAPI(title="ai-dlc dashboard")

app.include_router(pages.router)
app.include_router(runs.router)
app.include_router(stream.router)
app.include_router(webhooks.router)
