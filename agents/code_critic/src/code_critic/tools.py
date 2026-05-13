"""Code-Critic-local tool wiring.

S3 access for the code-critic (read the architect's plan and the
project's MEMORY.md / stack profile, write the critique artifact)
now flows through the per-agent AgentCore Gateway via MCP —
``common.gateway_tools`` builds the catalogue at ``build_agent`` time.
The only local tool that remains here is ``browse_url`` (AgentCore
Browser, not a gateway target). ``get_pr_diff`` is added locally in
:mod:`code_critic.agent` — it talks to GitHub via the repo_helper
Lambda directly through :mod:`common.sandbox` and is shared with
reviewer / tester.
"""

from __future__ import annotations

from strands import tool

from common.agentcore_browser import browse_url

browse_url_tool = tool(browse_url)


def critique_s3_key(*, run_id: str, revision_number: int) -> str:
    """S3 key under the artifacts bucket for a run's code-critique artifact."""
    return f"runs/{run_id}/validation/critique-r{revision_number}.md"
