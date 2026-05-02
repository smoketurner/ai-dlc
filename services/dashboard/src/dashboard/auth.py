"""Cognito OIDC auth via the ALB-injected ``x-amzn-oidc-data`` header.

The ALB validates the user's session against Cognito, signs a JWT containing
the user's claims with one of the AWS-published EC keys, and forwards it as
``x-amzn-oidc-data``. We only verify the signature (against the per-region
public-key endpoint) and return a typed :class:`User`. ALB has already done
the Cognito dance — we never touch refresh tokens or the OIDC flow itself.

For local dev (``AIDLC_AUTH=disabled``) the dependency returns a fake user.
"""

from __future__ import annotations

import base64
import json
from functools import lru_cache
from typing import Annotated

import httpx
import jwt
import structlog
from fastapi import Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from dashboard.deps import settings

logger = structlog.get_logger()


class User(BaseModel):
    """Authenticated principal extracted from ``x-amzn-oidc-data``."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    sub: str
    email: str | None = None
    groups: list[str] = []


@lru_cache(maxsize=64)
def alb_public_key(region: str, kid: str) -> str:
    """Fetch + cache the ALB's per-key public key."""
    url = f"https://public-keys.auth.elb.{region}.amazonaws.com/{kid}"
    resp = httpx.get(url, timeout=5.0)
    resp.raise_for_status()
    return resp.text


def decode_oidc_data(token: str, region: str) -> dict[str, str | list[str]]:
    """Decode + verify an ``x-amzn-oidc-data`` JWT and return its claims."""
    header_b64 = token.split(".", 1)[0]
    header = json.loads(base64.urlsafe_b64decode(header_b64 + "==="))
    kid = header["kid"]
    pem = alb_public_key(region, kid)
    return jwt.decode(token, pem, algorithms=["ES256"])


def get_current_user(
    request: Request,
    x_amzn_oidc_data: Annotated[str | None, Header()] = None,
) -> User:
    """FastAPI dependency that returns the authenticated user."""
    cfg = settings()
    if cfg.auth_disabled:
        return User(sub="dev-user", email="dev@example.com", groups=["dev"])
    if x_amzn_oidc_data is None:
        logger.warning("missing x-amzn-oidc-data header", path=str(request.url))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing x-amzn-oidc-data header.",
        )
    try:
        claims = decode_oidc_data(x_amzn_oidc_data, cfg.cognito_region)
    except (jwt.InvalidTokenError, httpx.HTTPError) as exc:
        logger.warning("oidc verification failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid x-amzn-oidc-data signature.",
        ) from exc
    return User(
        sub=str(claims.get("sub", "unknown")),
        email=str(claims.get("email")) if claims.get("email") else None,
        groups=list(claims.get("cognito:groups") or [])
        if isinstance(claims.get("cognito:groups"), list)
        else [],
    )


CurrentUser = Annotated[User, Depends(get_current_user)]
