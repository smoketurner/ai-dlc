"""Tester-local Strands tools.

``get_pr_diff`` (invokes ``repo_helper`` Lambda directly for the diff),
``run_pr_in_sandbox`` (AgentCore Code Interpreter), and ``browse_url``
(AgentCore Browser). Gateway-routed tools are spliced in by
:func:`tester.agent.build_agent`. Note: ``artifact_tool.get_artifact``
raises on missing keys (it does not fall back to empty); the agent
sees this as a tool error and continues with whatever context it has.
"""

from __future__ import annotations

from strands import tool

from common.agentcore_browser import browse_url
from common.sandbox import get_pr_diff, run_pr_in_sandbox

get_pr_diff_tool = tool(get_pr_diff)
run_pr_in_sandbox_tool = tool(run_pr_in_sandbox)
browse_url_tool = tool(browse_url)


def report_s3_key(*, run_id: str, revision_number: int) -> str:
    """S3 key under the artifacts bucket for a run's test report."""
    return f"runs/{run_id}/validation/test_report-r{revision_number}.md"
