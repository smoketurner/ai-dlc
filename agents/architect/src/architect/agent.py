"""Strands Agent factory for the Architect.

The Architect uses Claude Opus 4.7 on Bedrock with a strict-JSON output
contract: the agent loop runs with the four grounding/output tools and
finishes by emitting a :class:`SpecBundle` via Strands'
``structured_output_model`` parameter — that constrains the Bedrock
model to produce JSON matching the schema while still letting the agent
call tools (``read_memory_md``, ``list_repo_paths``, ``read_repo_file``)
to ground itself in the project before drafting.
"""

from __future__ import annotations

import os

from strands import Agent
from strands.models import BedrockModel

from architect.hooks import build_hooks
from architect.spec import SpecBundle
from architect.tools import (
    browse_url_tool,
    list_repo_paths_tool,
    read_memory_md_tool,
    read_repo_file_tool,
    read_stack_profile_md_tool,
    write_spec_doc_tool,
)
from common.memory import agent_memory_preamble
from common.routing import load_system_prompt, pick_variant
from common.runtime import default_retry_strategy, run_for_structured_output

DEFAULT_MODEL_ID = "us.anthropic.claude-opus-4-6-v1"


def model_id() -> str:
    """Bedrock model id, overridable via ``AIDLC_BEDROCK_MODEL_ID``."""
    return os.environ.get("AIDLC_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)


def build_agent(run_id: str) -> Agent:
    """Build a fresh Strands Agent for one architect invocation.

    The system prompt is selected via A/B routing — if ``architect.prompts_b``
    exists, half of runs (deterministically picked from ``run_id``) use it.
    """
    variant = pick_variant(run_id, "architect")
    bedrock_model_id = model_id()
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
            read_memory_md_tool,
            read_stack_profile_md_tool,
            write_spec_doc_tool,
            list_repo_paths_tool,
            read_repo_file_tool,
            browse_url_tool,
        ],
        hooks=build_hooks(),
        retry_strategy=default_retry_strategy(bedrock_model_id),
    )


def generate_spec(
    agent: Agent,
    intent: str,
    *,
    project_slug: str,
    prior_feedback: str | None,
    triggering_comment_body: str | None = None,
) -> SpecBundle:
    """Run the agent and return the validated SpecBundle.

    The caller constructs the agent (so it can read usage metrics off of
    it after this returns) and passes it in.

    Args:
        agent: Strands ``Agent`` built via :func:`build_agent`.
        intent: Free-text feature intent from the user.
        project_slug: Project the spec belongs to.
        prior_feedback: Spec feedback to address — either a prior
            rejection's reviewer notes or accumulated PR-comment
            iteration. ``None`` for fresh runs.
        triggering_comment_body: Free-text guidance from the
            ``@aidlc-bot <text>`` comment that minted this run, with the
            bot mention already stripped, or ``None`` if the run wasn't
            triggered by a guidance-bearing comment.

    Returns:
        A validated :class:`SpecBundle` ready for Markdown rendering.
    """
    user_message = compose_message(
        intent,
        project_slug,
        prior_feedback,
        triggering_comment_body,
    )
    return run_for_structured_output(agent, output_model=SpecBundle, prompt=user_message)


def compose_message(
    intent: str,
    project_slug: str,
    prior_feedback: str | None,
    triggering_comment_body: str | None,
) -> str:
    """Compose the user-message prompt handed to the architect."""
    parts = [
        agent_memory_preamble(project_slug=project_slug, query=intent),
        f"Project: {project_slug}",
        "",
        "Intent:",
        intent.strip(),
    ]
    if triggering_comment_body:
        parts += [
            "",
            "Additional user guidance (from the @aidlc-bot comment that retriggered this run "
            "— treat as feedback to incorporate into the spec):",
            triggering_comment_body.strip(),
        ]
    if prior_feedback:
        parts += [
            "",
            "Spec feedback to address — apply every point. (Source: prior "
            "rejection or PR-comment iteration; treat both as authoritative.)",
            prior_feedback.strip(),
        ]
    parts += [
        "",
        f"Read the project's MEMORY.md (project_slug={project_slug}) before "
        "you draft the spec; conform to every rule in its Conventions section.",
    ]
    return "\n".join(parts)
