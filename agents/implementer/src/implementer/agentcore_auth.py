"""AgentCore Identity helpers for the Implementer.

The Implementer authenticates to GitHub two ways:

  * **User on-behalf-of** (preferred when ``requestor_sub`` is set AND the
    user has authorized the App via the dashboard). Calls
    ``GetWorkloadAccessTokenForUserId`` + ``GetResourceOauth2Token`` to
    fetch the user's GitHub OAuth token from the AgentCore Identity Token
    Vault. Commits attribute to the user.
  * **App installation token** (fallback). Mints a fresh installation-scoped
    token from the App's private key in Secrets Manager, scoped to the
    target repo. Commits attribute to ``ai-dlc[bot]``. This is the path
    every run takes when there's no human in the loop (Triage-driven runs)
    or the user hasn't authorized the App.

We mirror :mod:`common.github_app`'s shape rather than sharing code because
the Implementer ships as its own container and the auth surface is small.

Required env vars:
  * ``AIDLC_GITHUB_APP_SECRET_ARN`` — Secrets Manager secret holding
    ``{"app_id": int, "private_key_base64": str}``.
  * ``AIDLC_GITHUB_OAUTH_PROVIDER_NAME`` — name of the AgentCore Identity
    OAuth2 credential provider for user-OBO.
  * ``AIDLC_AGENT_WORKLOAD_NAME`` — name of the AgentCore workload identity
    the Implementer runs under.
"""

from __future__ import annotations

import base64
import os
import time
from functools import cache
from typing import TYPE_CHECKING

import boto3
import httpx
import jwt
import structlog
from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict, Field, SecretStr

logger = structlog.get_logger()

if TYPE_CHECKING:
    from mypy_boto3_bedrock_agentcore.client import BedrockAgentCoreClient
    from mypy_boto3_secretsmanager.client import SecretsManagerClient

GITHUB_API = "https://api.github.com"
USER_AGENT = "ai-dlc-implementer/1.0"
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
ACCEPT_HEADER = "application/vnd.github+json"
API_VERSION = "2022-11-28"

JWT_TTL_SECONDS = 540  # GitHub max is 600s; mint a fresh JWT every 9 min
INSTALLATION_TOKEN_TTL_SECONDS = 3000  # mint a fresh install token every 50 min
JWT_REFRESH_MARGIN = 30  # rotate this many seconds before TTL


class AppCredentials(BaseModel):
    """Decoded App credentials read from Secrets Manager.

    The source-of-truth secret carries a wider schema (``client_id``,
    ``client_secret``, etc. for the AgentCore Identity OAuth provider);
    this Lambda only needs ``app_id`` + the PEM key. ``extra="ignore"``
    tolerates the extra fields without coupling.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    app_id: int = Field(ge=1)
    private_key_base64: SecretStr

    def private_key_pem(self) -> bytes:
        """Decode the base64-wrapped PEM into raw bytes for pyjwt."""
        return base64.b64decode(self.private_key_base64.get_secret_value())


@cache
def agentcore_client() -> BedrockAgentCoreClient:
    """Process-cached boto3 client for AgentCore Identity APIs."""
    return boto3.client("bedrock-agentcore")


@cache
def secrets_client() -> SecretsManagerClient:
    """Process-cached boto3 Secrets Manager client."""
    return boto3.client("secretsmanager")


@cache
def app_credentials() -> AppCredentials:
    """Read + parse the App credentials secret. Cached for the container's life."""
    secret_arn = os.environ["AIDLC_GITHUB_APP_SECRET_ARN"]
    response = secrets_client().get_secret_value(SecretId=secret_arn)
    return AppCredentials.model_validate_json(response["SecretString"])


def oauth_provider_name() -> str:
    """Name of the AgentCore Identity OAuth2 credential provider for GitHub."""
    return os.environ["AIDLC_GITHUB_OAUTH_PROVIDER_NAME"]


def workload_name() -> str:
    """Name of the AgentCore workload identity the Implementer runs under."""
    return os.environ["AIDLC_AGENT_WORKLOAD_NAME"]


jwt_cache: dict[str, tuple[str, float]] = {}


def app_jwt() -> str:
    """Return a fresh App-level JWT, cached for ``JWT_TTL_SECONDS`` minus a margin."""
    now = time.time()
    cached = jwt_cache.get("jwt")
    if cached is not None and cached[1] > now:
        return cached[0]
    creds = app_credentials()
    payload = {
        "iat": int(now) - 60,  # account for clock skew
        "exp": int(now) + JWT_TTL_SECONDS,
        # pyjwt 2.10+ rejects non-string ``iss``; GitHub's spec says App ID
        # (integer-shaped), but the JWT claim itself must be serialised as
        # a string.
        "iss": str(creds.app_id),
    }
    token = jwt.encode(payload, creds.private_key_pem(), algorithm="RS256")
    jwt_cache["jwt"] = (token, now + JWT_TTL_SECONDS - JWT_REFRESH_MARGIN)
    return token


installation_id_cache: dict[str, int] = {}


def installation_id_for_repo(repo: str) -> int:
    """Fetch (and cache) the App's installation id for ``owner/name``."""
    cached = installation_id_cache.get(repo)
    if cached is not None:
        return cached
    response = httpx.get(
        f"{GITHUB_API}/repos/{repo}/installation",
        headers=app_headers(),
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    installation_id = int(response.json()["id"])
    installation_id_cache[repo] = installation_id
    return installation_id


installation_token_cache: dict[str, tuple[str, float]] = {}


def installation_token_for_repo(repo: str) -> str:
    """Mint (or reuse) an installation token scoped to ``repo``."""
    now = time.time()
    cached = installation_token_cache.get(repo)
    if cached is not None and cached[1] > now:
        return cached[0]
    installation_id = installation_id_for_repo(repo)
    response = httpx.post(
        f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
        headers=app_headers(),
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    token = str(response.json()["token"])
    installation_token_cache[repo] = (token, now + INSTALLATION_TOKEN_TTL_SECONDS)
    return token


def app_headers() -> dict[str, str]:
    """Standard headers for App-JWT-authenticated calls."""
    return {
        "Accept": ACCEPT_HEADER,
        "Authorization": f"Bearer {app_jwt()}",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": API_VERSION,
    }


def user_oauth_token_for_requestor_sub(requestor_sub: str) -> str | None:
    """Fetch the requestor's GitHub OAuth token from the Token Vault.

    Returns ``None`` when the user hasn't authorized the App, the session
    is otherwise unavailable, or the call surfaces an IAM/network failure.
    User-OBO is best-effort — the caller always has the App installation
    token to fall back to, so an unexpected error here shouldn't blow up
    the whole run.
    """
    client = agentcore_client()
    try:
        workload_response = client.get_workload_access_token_for_user_id(
            workloadName=workload_name(),
            userId=requestor_sub,
        )
        workload_token = workload_response["workloadAccessToken"]
        resource_response = client.get_resource_oauth2_token(
            resourceCredentialProviderName=oauth_provider_name(),
            oauth2Flow="USER_FEDERATION",
            workloadIdentityToken=workload_token,
            scopes=[],
        )
    except client.exceptions.ResourceNotFoundException:
        return None
    except ClientError as exc:
        logger.warning(
            "user-OBO failed; falling back to installation token",
            requestor_sub=requestor_sub,
            error_code=exc.response.get("Error", {}).get("Code"),
        )
        return None
    access_token = resource_response.get("accessToken")
    return access_token if access_token else None
