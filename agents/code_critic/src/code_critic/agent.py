"""Strands Agent factory for the Code-Critic.

The Code-Critic uses Claude Opus 4.6 on Bedrock to adversarially
review the integrated impl PR against the **original GitHub issue**.
Its primary lens is "does this PR solve the user's stated problem".
It also flags drift from the architect's plan and missing edge cases.

The agent loop runs with gateway-routed grounding tools
(``artifact_tool`` ops for ``read_memory_md`` / ``read_stack_profile_md``
/ ``get_artifact``) plus the local ``get_pr_diff`` (shared with
reviewer/tester via :mod:`common.sandbox`) and ``browse_url``. It
finishes by emitting a :class:`Critique` via Strands'
``structured_output_model`` parameter.
"""

from __future__ import annotations

import os

from strands import Agent, tool
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

from code_critic.critique import Critique
from code_critic.hooks import build_hooks
from code_critic.tools import browse_url_tool
from common.gateway_tools import gateway_tools
from common.memory import agent_memory_preamble
from common.routing import load_system_prompt, pick_variant
from common.runtime import default_retry_strategy, run_for_structured_output
from common.sandbox import get_pr_diff

DEFAULT_MODEL_ID = "us.anthropic.claude-opus-4-6-v1"

get_pr_diff_tool = tool(get_pr_diff)


def model_id() -> str:
    """Bedrock model id, overridable via ``AIDLC_BEDROCK_MODEL_ID``."""
    return os.environ.get("AIDLC_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)


def build_agent(run_id: str, *, mcp_client: MCPClient) -> Agent:
    """Build a fresh Strands Agent for one code-critic invocation.

    The caller is responsible for starting ``mcp_client`` (typically via
    ``with gateway_mcp_client() as mcp_client:``) and keeping it open
    for the lifetime of the agent call. Tool definitions from the
    gateway catalogue are spliced into the agent's tool list alongside
    the local ``get_pr_diff`` and ``browse_url`` tools.
    """
    variant = pick_variant(run_id, "code_critic")
    bedrock_model_id = model_id()
    return Agent(
        model=BedrockModel(
            model_id=bedrock_model_id,
            region_name=os.environ["AWS_REGION"],
            temperature=0.3,
            max_tokens=8192,
            streaming=True,
        ),
        system_prompt=load_system_prompt("code_critic", variant),
        tools=[
            *gateway_tools(mcp_client),
            get_pr_diff_tool,
            browse_url_tool,
        ],
        hooks=build_hooks(),
        retry_strategy=default_retry_strategy(bedrock_model_id),
    )


def critique_pr(  # noqa: PLR0913 -- structured input + 3 issue context fields
    agent: Agent,
    *,
    project_slug: str,
    plan_s3_key: str,
    run_id: str,
    pr_url: str,
    revision_number: int,
    source_issue_url: str | None,
    source_issue_title: str | None,
    source_issue_body: str | None,
) -> Critique:
    """Run the agent and return the validated Critique."""
    user_message = compose_message(
        project_slug=project_slug,
        plan_s3_key=plan_s3_key,
        run_id=run_id,
        pr_url=pr_url,
        revision_number=revision_number,
        source_issue_url=source_issue_url,
        source_issue_title=source_issue_title,
        source_issue_body=source_issue_body,
    )
    return run_for_structured_output(agent, output_model=Critique, prompt=user_message)


def compose_message(
    *,
    project_slug: str,
    plan_s3_key: str,
    run_id: str,
    pr_url: str,
    revision_number: int,
    source_issue_url: str | None,
    source_issue_title: str | None,
    source_issue_body: str | None,
) -> str:
    """Compose the user-message prompt for the code-critic.

    The prompt leads with the **source issue** because the code-critic's
    primary job is to grade the diff against the user's original ask
    (not just the architect's plan).
    """
    revision_context = (
        "This is the first validation pass."
        if revision_number == 0
        else (
            f"This is revision pass #{revision_number} — the implementer revised "
            "the impl branch in response to the reviewer's prior verdict. Look "
            "for new gaps introduced by the fixes, not just the pre-existing ones."
        )
    )
    parts = [
        agent_memory_preamble(project_slug=project_slug, query=pr_url),
        f"Project: {project_slug}",
        f"Run id: {run_id}",
        f"Impl PR: {pr_url}",
        f"Revision number: {revision_number}",
        f"Plan S3 key: {plan_s3_key}",
    ]
    if source_issue_url:
        parts.append(f"Source issue: {source_issue_url}")
    if source_issue_title:
        parts.append(f"Issue title: {source_issue_title}")
    parts += ["", revision_context]
    if source_issue_body:
        parts += ["", "## Original issue body", "", source_issue_body.strip()]
    parts += [
        "",
        f"Read the architect's plan via ``get_artifact(key='{plan_s3_key}')`` "
        f"and the project's MEMORY.md (project_slug={project_slug}). Fetch the "
        "impl PR diff with ``get_pr_diff``. Then return a Critique JSON object — "
        "grade the diff against the issue body above (lens [issue→diff], "
        "[user-problem]) and against the plan (lens [plan-drift]), and flag "
        "logical gaps / missing edge cases (lens [edge-case]).",
    ]
    return "\n".join(parts)
