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
import logging
import os
import uuid
from typing import Any

from bedrock_agentcore.identity.auth import requires_access_token
from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp import MCPClient
from strands.tools.mcp.mcp_agent_tool import MCPAgentTool

logger = logging.getLogger(__name__)


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

    Reads the workload access token from :class:`BedrockAgentCoreContext`,
    exchanges it via ``GetResourceOauth2Token`` for a Cognito JWT (M2M /
    client_credentials), and returns the JWT. The decorator caches.

    .. note::
       The ``WorkloadAccessToken`` lives in a :mod:`contextvars`
       ``ContextVar`` that does not auto-inherit into threads spawned via
       :class:`threading.Thread`. Callers that dispatch the agent loop
       onto a daemon thread MUST wrap the thread's target with
       :func:`contextvars.copy_context().run` so this function can resolve
       the token off the entrypoint's context.
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
    dict response as MCP content. Observed shapes in the wild:

    * ``structuredContent`` populated with the raw dict (preferred).
    * ``structuredContent`` populated with a JSON-string serialisation
      of the dict (some gateway configurations).
    * ``content[0].text`` containing the JSON-string envelope, when
      structured output isn't surfaced. Some ops leave ``text`` empty
      and rely on a subsequent block.
    * ``isError=True`` with a text block carrying the error message
      verbatim (not JSON).

    The helper walks all of these shapes, skipping blocks that don't
    yield a parseable dict, and only raises when nothing is recoverable.
    The full result is logged at WARNING so a future shape mismatch
    surfaces in CloudWatch instead of as an opaque traceback.

    Raises ``RuntimeError`` when no shape yields a parseable dict.
    """
    if isinstance(result, dict) and result.get("isError"):
        raise RuntimeError(_error_message(result))

    structured = result.get("structuredContent") if isinstance(result, dict) else None
    parsed = _coerce_to_dict(structured)
    if parsed is not None:
        return parsed

    blocks = result.get("content", []) if isinstance(result, dict) else []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        parsed = _coerce_to_dict(text)
        if parsed is not None:
            return parsed

    logger.warning(
        "gateway tool returned no parseable content",
        extra={"result": repr(result)[:4096]},
    )
    msg = f"gateway tool returned no parseable content: {repr(result)[:1024]}"
    raise RuntimeError(msg)


def _coerce_to_dict(value: Any) -> dict[str, Any] | None:
    """Return a dict if ``value`` already is one or is a JSON-encoded dict; else None."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def _error_message(result: dict[str, Any]) -> str:
    """Render an MCP error result into a single-line message for the RuntimeError."""
    blocks = result.get("content", [])
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    detail = " | ".join(parts) if parts else repr(result)[:512]
    return f"gateway tool returned isError=true: {detail}"
