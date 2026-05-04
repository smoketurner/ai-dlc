"""Resolve the GitHub repos a user can target via the AI-DLC App.

Calls AgentCore Identity to fetch the user's cached OAuth token, then walks
``GET /user/installations`` + ``GET /user/installations/{id}/repositories``
to build the list of ``owner/name`` strings the user can submit runs against.

Results are cached per-user with a TTL so we don't hammer GitHub on every
page load. Cache miss costs ~2 round trips to GitHub plus 2 to AgentCore;
cache hit is a dict lookup. Expired entries are silently refreshed on the
next access.
"""

from __future__ import annotations

import time
from functools import cache
from typing import TYPE_CHECKING

import boto3
import httpx
import structlog

from dashboard.deps import settings

if TYPE_CHECKING:
    from mypy_boto3_bedrock_agentcore.client import BedrockAgentCoreClient

logger = structlog.get_logger()

GITHUB_API = "https://api.github.com"
ACCEPT_HEADER = "application/vnd.github+json"
API_VERSION = "2022-11-28"
USER_AGENT = "ai-dlc-dashboard/1.0"
HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
CACHE_TTL_SECONDS = 60
PAGE_SIZE = 100

repo_cache: dict[str, tuple[list[str], float]] = {}


@cache
def agentcore_client() -> BedrockAgentCoreClient:
    """Process-cached AgentCore client (separate from auth_github's instance)."""
    return boto3.client("bedrock-agentcore")


def repos_for_user(user_sub: str) -> list[str]:
    """Return ``owner/name`` strings the user can target. Empty when not linked."""
    now = time.time()
    cached = repo_cache.get(user_sub)
    if cached is not None and cached[1] > now:
        return cached[0]
    token = user_oauth_token(user_sub)
    if token is None:
        repo_cache[user_sub] = ([], now + CACHE_TTL_SECONDS)
        return []
    repos = list_user_installation_repos(token)
    repo_cache[user_sub] = (repos, now + CACHE_TTL_SECONDS)
    return repos


def user_oauth_token(user_sub: str) -> str | None:
    """Fetch the user's GitHub OAuth token from AgentCore Identity."""
    cfg = settings()
    if not cfg.dashboard_workload_name or not cfg.github_oauth_provider_name:
        return None
    client = agentcore_client()
    try:
        workload_response = client.get_workload_access_token_for_user_id(
            workloadName=cfg.dashboard_workload_name,
            userId=user_sub,
        )
        resource_response = client.get_resource_oauth2_token(
            resourceCredentialProviderName=cfg.github_oauth_provider_name,
            oauth2Flow="USER_FEDERATION",
            workloadIdentityToken=workload_response["workloadAccessToken"],
            scopes=[],
            resourceOauth2ReturnUrl=cfg.dashboard_oauth_return_url,
        )
    except client.exceptions.ResourceNotFoundException:
        return None
    return resource_response.get("accessToken")


def list_user_installation_repos(token: str) -> list[str]:
    """Walk /user/installations + /user/installations/{id}/repositories."""
    headers = {
        "Accept": ACCEPT_HEADER,
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": API_VERSION,
    }
    repos: list[str] = []
    with httpx.Client(timeout=HTTP_TIMEOUT, headers=headers) as client:
        installations = paged_get(client, f"{GITHUB_API}/user/installations", "installations")
        for installation in installations:
            installation_id = installation["id"]
            for repo in paged_get(
                client,
                f"{GITHUB_API}/user/installations/{installation_id}/repositories",
                "repositories",
            ):
                repos.append(repo["full_name"])
    return sorted(set(repos))


def paged_get(client: httpx.Client, url: str, key: str) -> list[dict]:  # type: ignore[type-arg]
    """Iterate GitHub paginated endpoints. Stops at the first empty page."""
    items: list[dict] = []  # type: ignore[type-arg]
    page = 1
    while True:
        response = client.get(url, params={"per_page": PAGE_SIZE, "page": page})
        response.raise_for_status()
        body = response.json()
        chunk = body.get(key, [])
        if not chunk:
            break
        items.extend(chunk)
        if len(chunk) < PAGE_SIZE:
            break
        page += 1
    return items
