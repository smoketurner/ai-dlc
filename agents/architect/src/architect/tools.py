"""Architect-local Strands tools.

Local-filesystem readers for the cloned target repo plus ``browse_url``
(AgentCore Browser). Gateway-routed tools (``artifact_tool`` reads /
writes) are spliced in by :func:`architect.agent.build_agent` via
``common.gateway_tools.gateway_tools()``.
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
