"""Code-Critic-local Strands tools.

``browse_url`` (AgentCore Browser) plus the S3-key helper for the
critique artifact. Gateway-routed tools (``artifact_tool`` reads /
writes) are spliced in by :func:`code_critic.agent.build_agent`;
``get_pr_diff`` is added there too from :mod:`common.sandbox` (shared
with reviewer / tester, invokes ``repo_helper`` directly).
"""

from __future__ import annotations

from strands import tool

from common.agentcore_browser import browse_url

browse_url_tool = tool(browse_url)


def critique_s3_key(*, run_id: str, revision_number: int) -> str:
    """S3 key under the artifacts bucket for a run's code-critique artifact."""
    return f"runs/{run_id}/validation/critique-r{revision_number}.md"
