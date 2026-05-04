"""``GET /auth/github`` and ``GET /auth/github/callback`` routes.

These bridge the dashboard's Cognito-authenticated session with AgentCore
Identity's ``USER_FEDERATION`` flow on the ``GithubOauth2`` credential
provider:

  1. ``/auth/github``: dashboard calls
     ``bedrock-agentcore:GetWorkloadAccessTokenForUserId`` (using the
     user's Cognito ``sub``) to derive a workload-bound token, then
     ``GetResourceOauth2Token`` with ``oauth2Flow=USER_FEDERATION``.
     If AgentCore returns an ``accessToken`` directly (user already
     linked), the page shows "already connected". Otherwise AgentCore
     returns an ``authorizationUrl`` + ``sessionUri``; we store the
     ``sessionUri`` in a short-lived signed cookie, redirect the user
     to the URL, and let GitHub handle the consent.
  2. ``/auth/github/callback``: AgentCore redirects here after the
     user authorizes. We pull the ``sessionUri`` cookie and call
     ``CompleteResourceTokenAuth`` to finalize the federation. The
     access token now lives in the AgentCore Token Vault keyed by
     the user's identity — subsequent agent runs fetch it via the
     same flow with no further user interaction.
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING

import boto3
import structlog
from fastapi import APIRouter, Cookie, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from dashboard.auth import CurrentUser
from dashboard.deps import settings

if TYPE_CHECKING:
    from mypy_boto3_bedrock_agentcore.client import BedrockAgentCoreClient

logger = structlog.get_logger()
router = APIRouter()
templates = Jinja2Templates(directory="src/dashboard/templates")

SESSION_URI_COOKIE = "aidlc_obo_session_uri"
SESSION_URI_COOKIE_TTL = 600  # 10 minutes — only needed for the OAuth round-trip


@cache
def agentcore_client() -> BedrockAgentCoreClient:
    """Process-cached boto3 client for AgentCore Identity APIs."""
    return boto3.client("bedrock-agentcore")


@router.get("/auth/github", response_class=HTMLResponse)
async def start_github_auth(request: Request, user: CurrentUser) -> Response:
    """Initiate (or short-circuit) the user's GitHub authorization flow."""
    cfg = settings()
    client = agentcore_client()
    workload_response = client.get_workload_access_token_for_user_id(
        workloadName=cfg.dashboard_workload_name,
        userId=user.sub,
    )
    workload_token = workload_response["workloadAccessToken"]
    resource_response = client.get_resource_oauth2_token(
        resourceCredentialProviderName=cfg.github_oauth_provider_name,
        oauth2Flow="USER_FEDERATION",
        workloadIdentityToken=workload_token,
        scopes=[],
        resourceOauth2ReturnUrl=cfg.dashboard_oauth_return_url,
    )

    if resource_response.get("accessToken"):
        return templates.TemplateResponse(
            request,
            "connect_github.html",
            {"user": user, "status": "linked"},
        )

    authorization_url = resource_response.get("authorizationUrl")
    session_uri = resource_response.get("sessionUri")
    if not authorization_url or not session_uri:
        msg = (
            "AgentCore did not return either an accessToken or an "
            f"authorizationUrl/sessionUri pair: {resource_response!r}"
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=msg)

    redirect = RedirectResponse(authorization_url, status_code=status.HTTP_303_SEE_OTHER)
    redirect.set_cookie(
        key=SESSION_URI_COOKIE,
        value=session_uri,
        max_age=SESSION_URI_COOKIE_TTL,
        secure=True,
        httponly=True,
        samesite="lax",
    )
    logger.info("github auth started", user=user.sub)
    return redirect


@router.get("/auth/github/callback", response_class=HTMLResponse)
async def github_auth_callback(
    request: Request,
    user: CurrentUser,
    aidlc_obo_session_uri: str | None = Cookie(default=None),
) -> Response:
    """Finalize the user federation after AgentCore redirects back."""
    if aidlc_obo_session_uri is None:
        return templates.TemplateResponse(
            request,
            "connect_github.html",
            {"user": user, "status": "missing_session"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    client = agentcore_client()
    try:
        client.complete_resource_token_auth(
            userIdentifier={"userId": user.sub},
            sessionUri=aidlc_obo_session_uri,
        )
    except Exception as exc:
        logger.warning("complete_resource_token_auth failed", err=str(exc))
        response = templates.TemplateResponse(
            request,
            "connect_github.html",
            {"user": user, "status": "callback_failed", "error": str(exc)},
            status_code=status.HTTP_502_BAD_GATEWAY,
        )
        response.delete_cookie(SESSION_URI_COOKIE)
        return response
    response: Response = templates.TemplateResponse(
        request,
        "connect_github.html",
        {"user": user, "status": "just_linked"},
    )
    response.delete_cookie(SESSION_URI_COOKIE)
    logger.info("github auth completed", user=user.sub)
    return response
