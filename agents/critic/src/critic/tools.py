"""Critic-local Strands tools.

``browse_url`` (AgentCore Browser) plus the S3-key helper for the
critique artifact. Gateway-routed tools (``artifact_tool`` reads /
writes) are spliced in by :func:`critic.agent.build_agent`.
"""

from __future__ import annotations

from strands import tool

from common.agentcore_browser import browse_url

browse_url_tool = tool(browse_url)


def critique_s3_key(run_id: str) -> str:
    """S3 key under the artifacts bucket for a run's critique."""
    return f"runs/{run_id}/critique.md"
