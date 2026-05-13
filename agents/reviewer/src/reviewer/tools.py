"""Reviewer-local tool wiring.

S3 access for the reviewer (read MEMORY.md / stack profile / plan,
write the review artifact) now flows through the per-agent AgentCore
Gateway via MCP — ``common.gateway_tools`` builds the catalogue at
``build_agent`` time. The local tools that remain here are the ones
the gateway can't host: ``get_pr_diff`` (talks to GitHub directly),
``run_pr_in_sandbox`` (AgentCore Code Interpreter), and ``browse_url``
(AgentCore Browser).
"""

from __future__ import annotations

from strands import tool

from common.agentcore_browser import browse_url
from common.sandbox import get_pr_diff, run_pr_in_sandbox

get_pr_diff_tool = tool(get_pr_diff)
run_pr_in_sandbox_tool = tool(run_pr_in_sandbox)
browse_url_tool = tool(browse_url)


def review_s3_key(*, run_id: str, revision_number: int) -> str:
    """S3 key under the artifacts bucket for a run's review artifact."""
    return f"runs/{run_id}/validation/review-r{revision_number}.md"
