"""Proposer-local tool wiring.

S3 access (read MEMORY.md / stack profile) and the Lambda invocation
for ``list_issue_comments`` now flow through the per-agent AgentCore
Gateway via MCP — ``common.gateway_tools`` builds the catalogue at
``build_agent`` time. The only local tool that remains here is
``browse_url`` (AgentCore Browser, not a gateway target).
"""

from __future__ import annotations

from strands import tool

from common.agentcore_browser import browse_url

browse_url_tool = tool(browse_url)
