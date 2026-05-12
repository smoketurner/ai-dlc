"""Critic-local tool wiring.

All S3 access for the critic now flows through the per-agent AgentCore
Gateway via MCP — ``common.gateway_tools`` builds the catalogue at
``build_agent`` time. The only direct tool that remains here is
``browse_url``, which hits AgentCore Browser (not a gateway target).
"""

from __future__ import annotations

from strands import tool

from common.agentcore_browser import browse_url

browse_url_tool = tool(browse_url)


def critique_s3_key(run_id: str) -> str:
    """S3 key under the artifacts bucket for a run's critique."""
    return f"runs/{run_id}/critique.md"
