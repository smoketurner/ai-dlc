"""Strands Agent factory for the Architect.

The Architect uses Claude Opus 4.7 on Bedrock. The agent loop runs with
gateway-routed grounding tools (the ``artifact_tool`` ops for
``read_memory_md`` / ``read_stack_profile_md`` / ``put_artifact``) plus
the local repo-checkout readers (``list_repo_paths``, ``read_repo_file``)
and ``browse_url``. It finishes by calling the gateway's
``put_artifact`` op to persist a single markdown plan document to S3 at
``runs/{run_id}/plan.md``.

The output is plain markdown — no JSON wrapping. The platform reads
``plan.md`` back to populate the ``DESIGN.READY`` event.
"""

from __future__ import annotations

import os

from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

from architect.hooks import build_hooks
from architect.tools import (
    browse_url_tool,
    list_repo_paths_tool,
    read_repo_file_tool,
)
from common.gateway_tools import gateway_tools
from common.memory import agent_memory_preamble, agent_skills_preamble
from common.routing import load_system_prompt, pick_variant
from common.runtime import default_retry_strategy

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
    """Build a fresh Strands Agent for one architect invocation.

    The caller is responsible for starting ``mcp_client`` (typically via
    ``with gateway_mcp_client() as mcp_client:``) and keeping it open
    for the lifetime of the agent call. Tool definitions from the
    gateway catalogue are spliced into the agent's tool list alongside
    the local repo-checkout readers and :func:`browse_url`.

    The system prompt is selected via A/B routing — if
    ``architect.prompts_b`` exists, half of runs (deterministically
    picked from ``run_id``) use it.

    ``model_id_override`` lets the fallback path in
    :func:`common.runtime.invoke_with_fallback` rebuild this agent on a
    different model after a throttle. When ``None``, the env-var (or
    :data:`DEFAULT_MODEL_ID`) is used.
    """
    variant = pick_variant(run_id, "architect")
    bedrock_model_id = model_id_override or model_id()
    return Agent(
        model=BedrockModel(
            model_id=bedrock_model_id,
            region_name=os.environ["AWS_REGION"],
            temperature=0.4,
            max_tokens=8192,
            streaming=True,
        ),
        system_prompt=load_system_prompt("architect", variant),
        tools=[
            *gateway_tools(mcp_client),
            list_repo_paths_tool,
            read_repo_file_tool,
            browse_url_tool,
        ],
        hooks=build_hooks(),
        retry_strategy=default_retry_strategy(bedrock_model_id),
    )


def generate_plan(
    agent: Agent,
    *,
    project_slug: str,
    run_id: str,
    intent: str,
    triggering_comment_body: str | None = None,
    source_issue_url: str | None = None,
    source_issue_title: str | None = None,
    source_issue_body: str | None = None,
) -> None:
    """Run the agent so it writes ``plan.md`` to S3 via ``put_artifact``.

    No structured output: the agent's instructions tell it to call the
    gateway's ``put_artifact`` op exactly once with the run's plan key.
    The caller reads usage metrics off ``agent`` after this returns and
    fetches the persisted plan via ``get_artifact`` through the same
    gateway client.

    Args:
        agent: Strands ``Agent`` built via :func:`build_agent`.
        project_slug: Project the plan belongs to.
        run_id: The run UUID7 string (also the S3 path component).
        intent: Free-text feature intent from the user / issue body.
        triggering_comment_body: Free-text guidance from the
            ``@aidlc-bot <text>`` comment that minted this run, with the
            bot mention already stripped, or ``None`` if the run wasn't
            triggered by a guidance-bearing comment.
        source_issue_url: GitHub URL of the originating issue.
        source_issue_title: Title of the originating issue.
        source_issue_body: Body of the originating issue.
    """
    user_message = compose_message(
        intent=intent,
        project_slug=project_slug,
        run_id=run_id,
        triggering_comment_body=triggering_comment_body,
        source_issue_url=source_issue_url,
        source_issue_title=source_issue_title,
        source_issue_body=source_issue_body,
    )
    agent(user_message)


def compose_message(
    *,
    intent: str,
    project_slug: str,
    run_id: str,
    triggering_comment_body: str | None,
    source_issue_url: str | None,
    source_issue_title: str | None,
    source_issue_body: str | None,
) -> str:
    """Compose the user-message prompt handed to the architect."""
    parts = [
        agent_memory_preamble(project_slug=project_slug, query=intent),
        agent_skills_preamble(),
        f"Project: {project_slug}",
        f"Run id: {run_id}",
    ]
    if source_issue_url:
        parts.append(f"GitHub issue: {source_issue_url}")
    if source_issue_title:
        parts.append(f"Issue title: {source_issue_title}")
    parts += ["", "Intent:", intent.strip()]
    if source_issue_body:
        parts += ["", "Issue body:", source_issue_body.strip()]
    if triggering_comment_body:
        parts += [
            "",
            "Additional user guidance (from the @aidlc-bot comment that "
            "retriggered this run — treat as feedback to incorporate into the plan):",
            triggering_comment_body.strip(),
        ]
    parts += [
        "",
        f"Read the project's MEMORY.md (project_slug={project_slug}) before "
        "you draft the plan; conform to every rule in its Conventions section.",
        "",
        "Produce the plan as Markdown using the eight section headings the "
        "system prompt specifies, then call ``put_artifact(key="
        f"'runs/{run_id}/plan.md', content=...)`` once to persist it.",
    ]
    return "\n".join(parts)
