"""Cognito OIDC auth via Authlib + Starlette ``SessionMiddleware``.

The FastAPI app handles the OAuth code flow itself: ``/auth/login``
redirects to the Cognito hosted UI, ``/auth/callback`` exchanges the
code for tokens, and ``/auth/logout`` clears the session and bounces
through Cognito's hosted-UI logout. Authenticated user claims live in
``request.session`` (a signed HttpOnly cookie via ``SessionMiddleware``).

For local dev (``AIDLC_AUTH=disabled``) the dependency returns a fake
user so routes work without a Cognito round-trip.
"""

from __future__ import annotations

from functools import cache
from typing import Annotated
from urllib.parse import urlencode

import boto3
import structlog
from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict

from dashboard.deps import settings

logger = structlog.get_logger()
router = APIRouter()


class User(BaseModel):
    """Authenticated principal extracted from the Cognito ID token."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    sub: str
    email: str | None = None
    groups: list[str] = []


class AuthRedirectError(Exception):
    """Sentinel exception telling ``app.py`` to redirect to ``/auth/login``."""


@cache
def cognito_client_secret() -> str:
    """Fetch and cache the Cognito user-pool app-client secret."""
    cfg = settings()
    if cfg.auth_disabled:
        return ""
    sm = boto3.client("secretsmanager", region_name=cfg.region)
    return str(sm.get_secret_value(SecretId=cfg.cognito_client_secret_id)["SecretString"])


@cache
def session_secret() -> str:
    """Fetch and cache the ``SessionMiddleware`` signing key."""
    cfg = settings()
    if cfg.auth_disabled:
        return "dev-session-secret-not-secure"
    sm = boto3.client("secretsmanager", region_name=cfg.region)
    return str(sm.get_secret_value(SecretId=cfg.session_secret_id)["SecretString"])


@cache
def oauth_client() -> OAuth:
    """Process-cached Authlib OAuth client registered against Cognito."""
    cfg = settings()
    client = OAuth()
    client.register(
        name="cognito",
        server_metadata_url=cfg.cognito_discovery_url,
        client_id=cfg.cognito_client_id,
        client_secret=cognito_client_secret(),
        client_kwargs={"scope": "openid email profile"},
    )
    return client


def get_current_user(request: Request) -> User:
    """FastAPI dependency returning the authenticated user.

    Browser pages and JSON routes both depend on this; missing or
    expired sessions raise :class:`AuthRedirectError`, which ``app.py``
    translates into a 303 redirect to ``/auth/login``.
    """
    cfg = settings()
    if cfg.auth_disabled:
        return User(sub="dev-user", email="dev@example.com", groups=["dev"])
    data = request.session.get("user")
    if not data:
        raise AuthRedirectError
    return User(**data)


CurrentUser = Annotated[User, Depends(get_current_user)]


@router.get("/auth/login")
async def login(request: Request) -> RedirectResponse:
    """Kick off the Cognito OAuth code flow."""
    callback = request.url_for("auth_callback")
    return await oauth_client().cognito.authorize_redirect(request, str(callback))


@router.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request) -> RedirectResponse:
    """Finalise the OAuth exchange and persist claims into the session."""
    try:
        token = await oauth_client().cognito.authorize_access_token(request)
    except OAuthError as exc:
        logger.warning("oauth callback failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="oauth_callback_failed",
        ) from exc
    claims = token.get("userinfo") or {}
    groups_claim = claims.get("cognito:groups")
    request.session["user"] = {
        "sub": str(claims.get("sub", "")),
        "email": str(claims["email"]) if claims.get("email") else None,
        "groups": list(groups_claim) if isinstance(groups_claim, list) else [],
    }
    logger.info("oauth login", sub=request.session["user"]["sub"])
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/auth/logout")
async def logout(request: Request) -> RedirectResponse:
    """Clear the session and redirect through Cognito hosted-UI logout."""
    cfg = settings()
    request.session.clear()
    params = urlencode(
        {
            "client_id": cfg.cognito_client_id,
            "logout_uri": cfg.cognito_logout_redirect_url,
        }
    )
    return RedirectResponse(
        url=f"https://{cfg.cognito_domain}.auth.{cfg.region}.amazoncognito.com/logout?{params}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
