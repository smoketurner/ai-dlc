"""GitHub auth — two paths, both routed through ``token_for_call``.

  * **User on-behalf-of** (preferred). The user authorizes the App once via
    AgentCore Identity's ``USER_FEDERATION`` flow on the ``GithubOauth2``
    credential provider; AgentCore caches the resulting GitHub OAuth token
    in the Token Vault keyed by the user's identity. At call time the
    Lambda calls ``bedrock-agentcore:GetWorkloadAccessTokenForJWT`` with
    the requestor's JWT, then ``bedrock-agentcore:GetResourceOauth2Token``
    with ``oauth2Flow=USER_FEDERATION`` to retrieve the cached user token.
    Commits and PRs attribute to the requestor in GitHub UI. AgentCore
    handles refresh + storage.
  * **Installation token** (fallback). For runs without a linked user
    (admin/bootstrap or before a user has authorized the App) the Lambda
    mints a fresh installation-scoped token from the App's private key
    in Secrets Manager. Commits attribute to ``ai-dlc[bot]``.

Required env vars:
  * ``AIDLC_GITHUB_APP_SECRET_ARN`` — Secrets Manager secret holding
    ``{"app_id": int, "private_key": str}`` (used by the installation path).
  * ``AIDLC_GITHUB_OAUTH_PROVIDER_NAME`` — name of the AgentCore Identity
    OAuth2 credential provider (``GithubOauth2`` vendor) to query for
    user-OBO tokens.
  * ``AIDLC_AGENT_WORKLOAD_NAME`` — name of the AgentCore workload identity
    the Lambda runs under (used as the workload-name argument to
    ``GetWorkloadAccessTokenForJWT``).

Caches are module-level globals because Lambda containers stay warm across
invocations.
"""

from __future__ import annotations

import os
import time
from functools import cache
from typing import TYPE_CHECKING

import boto3
import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field, SecretStr

if TYPE_CHECKING:
    from mypy_boto3_bedrock_agentcore.client import BedrockAgentCoreClient
    from mypy_boto3_secretsmanager.client import SecretsManagerClient

GITHUB_API = "https://api.github.com"
USER_AGENT = "ai-dlc-repo-helper/1.0"
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
ACCEPT_HEADER = "application/vnd.github+json"
API_VERSION = "2022-11-28"

JWT_TTL_SECONDS = 540  # mint a fresh JWT every 9 min (GitHub max is 10)
INSTALLATION_TOKEN_TTL_SECONDS = 3000  # mint a fresh token every 50 min (max 60)
JWT_REFRESH_MARGIN = 30  # rotate this many seconds before TTL


class AppCredentials(BaseModel):
    """Decoded App credentials read from Secrets Manager."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    app_id: int = Field(ge=1)
    private_key: SecretStr


@cache
def secrets_client() -> SecretsManagerClient:
    """Process-cached boto3 Secrets Manager client."""
    return boto3.client("secretsmanager")


@cache
def agentcore_client() -> BedrockAgentCoreClient:
    """Process-cached boto3 client for AgentCore Identity APIs."""
    return boto3.client("bedrock-agentcore")


@cache
def app_credentials() -> AppCredentials:
    """Read + parse the App credentials secret. Cached for the life of the container."""
    secret_arn = os.environ["AIDLC_GITHUB_APP_SECRET_ARN"]
    response = secrets_client().get_secret_value(SecretId=secret_arn)
    return AppCredentials.model_validate_json(response["SecretString"])


def oauth_provider_name() -> str:
    """Name of the AgentCore Identity OAuth2 credential provider for GitHub."""
    return os.environ["AIDLC_GITHUB_OAUTH_PROVIDER_NAME"]


def workload_name() -> str:
    """Name of the AgentCore workload identity this Lambda runs under."""
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
        "iss": creds.app_id,
    }
    token = jwt.encode(payload, creds.private_key.get_secret_value(), algorithm="RS256")
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
    token = response.json()["token"]
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


def user_oauth_token_for_requestor(requestor_jwt: str) -> str | None:
    """Resolve the requestor's GitHub OAuth token via AgentCore Identity.

    Calls ``GetWorkloadAccessTokenForJWT`` to convert the requestor's
    Cognito ID token into a workload-scoped access token, then
    ``GetResourceOauth2Token`` with ``oauth2Flow=USER_FEDERATION`` to fetch
    the user's GitHub OAuth token from the Token Vault. Returns ``None``
    if the user has not yet authorized the App (the dashboard's "Connect
    GitHub" flow has not been completed for this user).
    """
    client = agentcore_client()
    try:
        workload_token_response = client.get_workload_access_token_for_jwt(
            workloadName=workload_name(),
            userToken=requestor_jwt,
        )
        workload_token = workload_token_response["workloadAccessToken"]
        resource_response = client.get_resource_oauth2_token(
            resourceCredentialProviderName=oauth_provider_name(),
            oauth2Flow="USER_FEDERATION",
            workloadIdentityToken=workload_token,
            scopes=[],  # default scopes — GitHub Apps determine permissions at install time
        )
    except client.exceptions.ResourceNotFoundException:
        return None
    return resource_response["accessToken"]


def token_for_call(*, repo: str, requestor_jwt: str | None) -> str:
    """Return the right bearer token for a GitHub call.

    Prefers the user-on-behalf-of token from AgentCore Identity (commits
    attributed to the requestor); falls back to the App's installation
    token (commits attributed to ``ai-dlc[bot]``) when no user JWT is
    provided or the user hasn't completed the "Connect GitHub" flow.
    """
    if requestor_jwt is not None:
        user_token = user_oauth_token_for_requestor(requestor_jwt)
        if user_token is not None:
            return user_token
    return installation_token_for_repo(repo)
