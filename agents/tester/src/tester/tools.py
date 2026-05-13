"""Tester-local tool wiring.

S3 access for the tester (read MEMORY.md / stack profile / plan, write
the test-report artifact) now flows through the per-agent AgentCore
Gateway via MCP — ``common.gateway_tools`` builds the catalogue at
``build_agent`` time. The local tools that remain here are the ones
the gateway can't host: ``get_pr_diff`` (talks to GitHub directly),
``run_pr_in_sandbox`` (AgentCore Code Interpreter), and ``browse_url``
(AgentCore Browser).

Note: the gateway-routed ``artifact_tool.get_artifact`` and
``artifact_tool.read_memory_md`` ops raise when the underlying S3 key
is missing, whereas the previous local ``read_memory_md`` /
``read_plan_doc`` here returned an empty string. In practice both keys
are present by the time the tester runs (architect writes ``plan.md``
earlier in the state machine; MEMORY.md is bootstrapped at clone-sync),
so the tester sees the harder contract only on edge-cases and surfaces
the error as a tool-result the agent can react to.
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
