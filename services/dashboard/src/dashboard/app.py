"""FastAPI app entrypoint."""

from __future__ import annotations

from pathlib import Path

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from mangum import Mangum
from starlette.middleware.sessions import SessionMiddleware

from dashboard.auth import AuthRedirectError, session_secret
from dashboard.auth import router as auth_router
from dashboard.routes import auth_github, events, pages, runs, webhooks

logger = structlog.get_logger()

app = FastAPI(title="ai-dlc dashboard")

app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret(),
    https_only=True,
    same_site="lax",
)

# Tailwind CSS is built at container build time by the `tailwind` Docker stage
# and dropped into this directory; uv installs the dashboard package in
# editable mode, so __file__ resolves to the source tree at runtime.
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)


@app.exception_handler(AuthRedirectError)
async def auth_redirect_handler(_request: Request, _exc: AuthRedirectError) -> RedirectResponse:
    """Send unauthenticated browser navigation to the Cognito login flow."""
    return RedirectResponse(url="/auth/login", status_code=303)


app.include_router(auth_router)
app.include_router(pages.router)
app.include_router(runs.router)
app.include_router(events.router)
app.include_router(webhooks.router)
app.include_router(auth_github.router)

handler = Mangum(app, lifespan="off")
