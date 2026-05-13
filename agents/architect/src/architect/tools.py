"""Architect-local tool wiring.

All S3 access for the architect now flows through the per-agent AgentCore
Gateway via MCP — ``common.gateway_tools`` builds the catalogue at
``build_agent`` time. The only direct tools that remain here are the
local-filesystem repo readers and ``browse_url`` (which hits AgentCore
Browser, not a gateway target).
"""

from __future__ import annotations

from strands import tool

from architect.repo_grounding import list_repo_paths, read_repo_file
from common.agentcore_browser import browse_url

list_repo_paths_tool = tool(list_repo_paths)
read_repo_file_tool = tool(read_repo_file)
browse_url_tool = tool(browse_url)


def plan_s3_key(run_id: str) -> str:
    """S3 key under the artifacts bucket for a run's plan document."""
    return f"runs/{run_id}/plan.md"
