"""Strands Agent factory for the Critic.

The Critic uses Claude Opus 4.7 on Bedrock with a strict-JSON output
contract: the agent loop runs with the gateway-routed plan-reading
tools plus :func:`browse_url` and finishes by emitting a
:class:`Critique` via Strands' ``structured_output_model`` parameter
— that constrains the Bedrock model to produce JSON matching the
schema while still letting the agent call ``artifact_tool`` (via the
gateway) and ``browse_url`` to ground its critique.
"""

from __future__ import annotations

import os

from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

from common.gateway_tools import gateway_tools
from common.memory import agent_memory_preamble
from common.routing import load_system_prompt, pick_variant
from common.runtime import default_retry_strategy, run_for_structured_output
from critic.critique import Critique
from critic.hooks import build_hooks
from critic.tools import browse_url_tool

DEFAULT_MODEL_ID = "us.anthropic.claude-opus-4-6-v1"


def model_id() -> str:
    """Bedrock model id, overridable via ``AIDLC_BEDROCK_MODEL_ID``."""
    return os.environ.get("AIDLC_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)


def fallback_model_id() -> str | None:
    """Optional fallback model id read from ``AIDLC_BEDROCK_FALLBACK_MODEL_ID``.

    Returns ``None`` when unset or empty so :func:`common.runtime.invoke_with_fallback`
    skips the retry path. Used to cope with daily-token-quota throttles on
    Opus by transparently re-running the agent on a smaller model.
    """
    return os.environ.get("AIDLC_BEDROCK_FALLBACK_MODEL_ID") or None


def build_agent(
    run_id: str,
    *,
    mcp_client: MCPClient,
    model_id_override: str | None = None,
) -> Agent:
    """Build a fresh Strands Agent for one critic invocation.

    The caller is responsible for starting ``mcp_client`` (typically via
    ``with gateway_mcp_client(token=...) as mcp_client:``) and keeping
    it open for the lifetime of the agent call. Tool definitions from
    the gateway catalogue are spliced into the agent's tool list
    alongside the local :func:`browse_url` tool.

    Prompt variant routed via :func:`common.routing.pick_variant`.

    ``model_id_override`` lets :func:`common.runtime.invoke_with_fallback`
    rebuild this agent on a different model after a throttle.
    """
    variant = pick_variant(run_id, "critic")
    bedrock_model_id = model_id_override or model_id()
    return Agent(
        model=BedrockModel(
            model_id=bedrock_model_id,
            region_name=os.environ["AWS_REGION"],
            temperature=0.3,
            max_tokens=8192,
            streaming=True,
        ),
        system_prompt=load_system_prompt("critic", variant),
        tools=[
            *gateway_tools(mcp_client),
            browse_url_tool,
        ],
        hooks=build_hooks(),
        retry_strategy=default_retry_strategy(bedrock_model_id),
    )


def critique_plan(
    agent: Agent,
    *,
    project_slug: str,
    run_id: str,
    plan_s3_key: str,
    intent: str,
    source_issue_url: str | None = None,
    source_issue_title: str | None = None,
    source_issue_body: str | None = None,
) -> Critique:
    """Run the agent and return the validated Critique.

    Caller constructs the agent (via :func:`build_agent`) so the caller
    can read usage metrics off it after this returns.
    """
    user_message = compose_message(
        project_slug=project_slug,
        run_id=run_id,
        plan_s3_key=plan_s3_key,
        intent=intent,
        source_issue_url=source_issue_url,
        source_issue_title=source_issue_title,
        source_issue_body=source_issue_body,
    )
    return run_for_structured_output(agent, output_model=Critique, prompt=user_message)


def compose_message(
    *,
    project_slug: str,
    run_id: str,
    plan_s3_key: str,
    intent: str,
    source_issue_url: str | None,
    source_issue_title: str | None,
    source_issue_body: str | None,
) -> str:
    """Compose the user-message prompt for the critic."""
    parts = [
        agent_memory_preamble(project_slug=project_slug, query=intent),
        f"Project: {project_slug}",
        f"Run id: {run_id}",
        f"Plan S3 key: {plan_s3_key}",
    ]
    if source_issue_url:
        parts.append(f"GitHub issue: {source_issue_url}")
    if source_issue_title:
        parts.append(f"Issue title: {source_issue_title}")
    parts += ["", "Original intent:", intent.strip()]
    if source_issue_body:
        parts += ["", "Issue body:", source_issue_body.strip()]
    parts += [
        "",
        f"Read the architect's plan via ``get_artifact(key='{plan_s3_key}')`` and "
        f"the project's MEMORY.md (project_slug={project_slug}). Then return "
        "a Critique JSON object — adversarial review of the plan focused on "
        "missing edge cases, weak assumptions, architectural risk, and gaps "
        "in the plan's Verification section.",
    ]
    return "\n".join(parts)
