"""Triage-local Strands tools — currently just AgentCore Browser.

The triage agent reads no S3 artifacts or MEMORY.md through the gateway,
so unlike the architect it has no MCP client wiring. The only tool it
carries is :func:`browse_url`, used when an issue body links to
load-bearing external context (spec, RFC, PR, README) the model wants
to read before classifying.
"""

from __future__ import annotations

from strands import tool

from common.agentcore_browser import browse_url

browse_url_tool = tool(browse_url)
