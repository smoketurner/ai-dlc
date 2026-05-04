"""AgentCore Identity helpers for the Implementer.

The Implementer authenticates to GitHub two ways:

  * **User on-behalf-of** (preferred when ``requestor_sub`` is set). Calls
    ``GetWorkloadAccessTokenForUserId`` + ``GetResourceOauth2Token`` to
    fetch the user's previously-authorized GitHub OAuth token from the
    AgentCore Identity Token Vault. Commits attribute to the user.
  * **Installation token** (fallback). The container's
    ``AIDLC_GITHUB_TOKEN`` env var supplies an App installation token —
    set by the platform's bootstrap path. Commits attribute to
    ``ai-dlc[bot]``.

We mirror :mod:`repo_helper.auth`'s shape rather than sharing code because
the Implementer ships as its own container and the auth surface is small.
"""

from __future__ import annotations

import os
from functools import cache
from typing import TYPE_CHECKING

import boto3

if TYPE_CHECKING:
    from mypy_boto3_bedrock_agentcore.client import BedrockAgentCoreClient


@cache
def agentcore_client() -> BedrockAgentCoreClient:
    """Process-cached boto3 client for AgentCore Identity APIs."""
    return boto3.client("bedrock-agentcore")


def oauth_provider_name() -> str:
    """Name of the AgentCore Identity OAuth2 credential provider for GitHub."""
    return os.environ["AIDLC_GITHUB_OAUTH_PROVIDER_NAME"]


def workload_name() -> str:
    """Name of the AgentCore workload identity the Implementer runs under."""
    return os.environ["AIDLC_AGENT_WORKLOAD_NAME"]


def user_oauth_token_for_requestor_sub(requestor_sub: str) -> str | None:
    """Fetch the requestor's GitHub OAuth token from the Token Vault.

    Returns ``None`` when the user hasn't authorized the App or the
    session is otherwise unavailable — the caller falls back to the
    installation-token path.
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
    access_token = resource_response.get("accessToken")
    return access_token if access_token else None


def installation_token_fallback() -> str:
    """Bootstrap-time installation token from the container env."""
    return os.environ["AIDLC_GITHUB_TOKEN"]
