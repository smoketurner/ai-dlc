"""Strands MCPClient pointed at the agent's own AgentCore Gateway.

The gateway's CUSTOM_JWT authorizer trusts Cognito-issued JWTs whose
``client_id`` claim matches the Cognito M2M (client_credentials) app
client provisioned in module.auth. The agent runtime obtains those
JWTs from AgentCore Identity via :func:`fetch_gateway_token`, which
wraps the SDK's ``@requires_access_token(auth_flow="M2M")`` decorator
— that handles the data-plane ``GetResourceOauth2Token`` call, vault
lookup of the M2M client_id / client_secret, and the OAuth2
client_credentials exchange against Cognito's token endpoint.

ContextVars do not auto-inherit into threads spawned via
:class:`threading.Thread`, so request handlers that dispatch work into
a daemon thread MUST use :func:`contextvars.copy_context().run` to
carry the runtime's ``WorkloadAccessToken`` across the boundary.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

from bedrock_agentcore.identity.auth import requires_access_token
from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp import MCPClient
from strands.tools.mcp.mcp_agent_tool import MCPAgentTool


def gateway_url() -> str:
    """Return ``AIDLC_AGENT_GATEWAY_URL`` or raise."""
    url = os.environ.get("AIDLC_AGENT_GATEWAY_URL")
    if not url:
        msg = (
            "AIDLC_AGENT_GATEWAY_URL is unset; the agent's runtime must wire "
            "in its per-agent AgentCore Gateway URL."
        )
        raise OSError(msg)
    return url


def fetch_gateway_token() -> str:
    """Fetch a Cognito M2M JWT for the agent's gateway via AgentCore Identity.

    The decorator-wrapped inner function reads the workload access token
    from :class:`BedrockAgentCoreContext`, exchanges it via
    ``GetResourceOauth2Token`` for a Cognito JWT (M2M /
    client_credentials), and returns the JWT string. The decorator
    handles caching internally.
    """
    provider_name = os.environ.get("AIDLC_GATEWAY_OAUTH_PROVIDER_NAME")
    if not provider_name:
        msg = (
            "AIDLC_GATEWAY_OAUTH_PROVIDER_NAME is unset; the agent's runtime "
            "must point at the AgentCore Identity M2M credential provider "
            "wired to Cognito."
        )
        raise OSError(msg)
    scope = os.environ.get("AIDLC_GATEWAY_OAUTH_SCOPE", "")
    scopes = [scope] if scope else []

    @requires_access_token(provider_name=provider_name, auth_flow="M2M", scopes=scopes)
    def _inner(*, access_token: str) -> str:
        return access_token

    return _inner()


def gateway_mcp_client(*, access_token: str | None = None) -> MCPClient:
    """Build an unstarted Strands MCPClient for the agent's own gateway.

    Args:
        access_token: Bearer JWT to use when opening the MCP session.
            When ``None`` (production path), the transport calls
            :func:`fetch_gateway_token` lazily at session-start time so
            the token is fresh per MCPClient lifecycle. Tests pass an
            explicit token to bypass the AgentCore Identity round-trip.

    Returns:
        An unstarted :class:`strands.tools.mcp.MCPClient`. Caller is
        responsible for entering it as a context manager.

    Raises:
        EnvironmentError: If ``AIDLC_AGENT_GATEWAY_URL`` is unset.
    """
    url = gateway_url()
    captured = access_token

    def transport() -> Any:
        bearer = captured or fetch_gateway_token()
        return streamablehttp_client(url=url, headers={"Authorization": f"Bearer {bearer}"})

    return MCPClient(transport_callable=transport)


def gateway_tools(client: MCPClient) -> list[MCPAgentTool]:
    """Return the gateway's tool catalogue as Strands ``MCPAgentTool``s.

    Args:
        client: A started :class:`MCPClient`.

    Returns:
        The list of tools the gateway advertises. Drop directly into
        ``Agent(tools=[...])`` alongside local ``@tool`` functions.
    """
    return list(client.list_tools_sync())


def call_gateway_tool(
    client: MCPClient,
    *,
    name: str,
    arguments: dict[str, Any],
) -> Any:
    """Invoke a gateway tool by name out-of-band (post-agent / no Agent in scope).

    Args:
        client: A started :class:`MCPClient`.
        name: The MCP tool name as advertised by the gateway target
            (e.g. ``"artifact_tool"``).
        arguments: The tool input payload.

    Returns:
        The :class:`strands.tools.mcp.MCPToolResult` from the MCP call.
        Callers that need the structured response should pull from
        ``result.content`` (a list of content blocks).
    """
    return client.call_tool_sync(
        tool_use_id=str(uuid.uuid4()),
        name=name,
        arguments=arguments,
    )


def extract_envelope(result: Any) -> dict[str, Any]:
    """Pull a Lambda return envelope out of an MCPToolResult.

    AgentCore Gateway invokes a Lambda target and returns the Lambda's
    dict response as MCP content. The MCP server serialises dict
    returns into both ``structuredContent`` (the raw dict) and
    ``content[0].text`` (a JSON string of the same dict) per
    ``mcp.server.lowlevel.server``'s serialisation path. This helper
    prefers the structured form and falls back to parsing the first
    text block so it's robust to servers that haven't enabled
    structured output.

    Raises ``RuntimeError`` when neither shape yields a parseable dict.
    """
    structured = result.get("structuredContent") if isinstance(result, dict) else None
    if isinstance(structured, dict):
        return structured
    blocks = result.get("content", []) if isinstance(result, dict) else []
    for block in blocks:
        text = block.get("text") if isinstance(block, dict) else None
        if isinstance(text, str):
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
    msg = f"gateway tool returned no parseable content: {result!r}"
    raise RuntimeError(msg)
