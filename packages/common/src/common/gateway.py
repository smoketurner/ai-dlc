"""MCP client to AgentCore Gateway.

Agents and tools hit the Gateway over Streamable HTTP MCP. We keep a thin
client here for non-agent code (e.g., the dashboard, tools running in
Lambdas, integration tests) that needs to call gateway-exposed tools without
pulling in a full MCP server stack.

For agent runtimes themselves, Strands and the Claude Agent SDK have their
own MCP-client integration — they consume the same gateway URL but go through
their framework's transport layer, not this module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from common.errors import GatewayError


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    """Inputs needed to talk to a single AgentCore Gateway."""

    url: str
    bearer_token: str
    timeout_s: float = 30.0


def _post_jsonrpc(config: GatewayConfig, method: str, params: dict[str, Any]) -> Any:
    """Issue a single MCP JSON-RPC call and return the ``result`` field."""
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }
    headers = {
        "Authorization": f"Bearer {config.bearer_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    try:
        response = httpx.post(
            config.url,
            json=request,
            headers=headers,
            timeout=config.timeout_s,
        )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise GatewayError("gateway request failed", url=config.url, method=method) from exc
    if not isinstance(body, dict):
        raise GatewayError("gateway returned non-object", url=config.url, method=method)
    if "error" in body:
        err = body["error"]
        msg = err.get("message", "unknown") if isinstance(err, dict) else str(err)
        raise GatewayError("gateway returned error", url=config.url, method=method, error=msg)
    return body.get("result")


def list_tools(config: GatewayConfig) -> list[dict[str, Any]]:
    """Return the gateway's tool catalog."""
    result = _post_jsonrpc(config, "tools/list", {})
    if not isinstance(result, dict):
        return []
    tools = result.get("tools", [])
    return tools if isinstance(tools, list) else []


def call_tool(config: GatewayConfig, /, *, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Invoke a single gateway tool and return the structured result."""
    result = _post_jsonrpc(
        config,
        "tools/call",
        {"name": name, "arguments": arguments},
    )
    if not isinstance(result, dict):
        raise GatewayError("non-object tool result", tool=name)
    return result
