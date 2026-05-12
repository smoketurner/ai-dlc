"""Strands Agent factory for the Retrospector.

The Retrospector uses Claude Haiku 4.5 — synthesis is small (read a
PR + comments + MEMORY.md, decide one of two outcomes) and runs on
every terminal event, so the cheaper model is the right pick. The
agent loop runs with the read tools and finishes by emitting a
:class:`RetrospectiveDecision` via Strands' ``structured_output_model``
parameter.
"""

from __future__ import annotations

import os
from typing import Literal

from strands import Agent
from strands.models import BedrockModel

from common.memory import agent_memory_preamble
from common.routing import load_system_prompt, pick_variant
from common.runtime import default_retry_strategy, run_for_structured_output
from retrospector.decision import RetrospectiveDecision
from retrospector.tools import (
    get_issue_tool,
    get_pr_tool,
    list_issue_comments_tool,
    list_pr_comments_tool,
    list_pr_review_comments_tool,
    read_memory_md_tool,
    read_stack_profile_md_tool,
)

DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def model_id() -> str:
    """Bedrock model id, overridable via ``AIDLC_BEDROCK_MODEL_ID``."""
    return os.environ.get("AIDLC_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)


def build_agent(run_id: str) -> Agent:
    """Build a fresh Strands Agent for one retrospective.

    Prompt variant routed via :func:`common.routing.pick_variant` so
    A/B'ing the retrospector prompt follows the same convention as
    every other Strands agent in the project.
    """
    variant = pick_variant(run_id, "retrospector")
    bedrock_model_id = model_id()
    return Agent(
        model=BedrockModel(
            model_id=bedrock_model_id,
            region_name=os.environ["AWS_REGION"],
            temperature=0.2,
            max_tokens=4096,
            streaming=True,
        ),
        system_prompt=load_system_prompt("retrospector", variant),
        tools=[
            read_memory_md_tool,
            read_stack_profile_md_tool,
            get_pr_tool,
            list_pr_comments_tool,
            list_pr_review_comments_tool,
            get_issue_tool,
            list_issue_comments_tool,
        ],
        retry_strategy=default_retry_strategy(bedrock_model_id),
    )


type EventKind = Literal[
    "RUN.COMPLETED",
    "RUN.FAILED",
    "RUN.CANCEL_REQUESTED",
]


def retrospect(
    *,
    event_type: EventKind,
    project_slug: str,
    target_repo: str,
    run_id: str,
    pr_url: str | None,
    issue_url: str | None,
    reason: str | None,
) -> RetrospectiveDecision:
    """Run the agent against one terminal event and return its decision."""
    user_message = compose_message(
        event_type=event_type,
        project_slug=project_slug,
        target_repo=target_repo,
        pr_url=pr_url,
        issue_url=issue_url,
        reason=reason,
    )
    agent = build_agent(run_id)
    return run_for_structured_output(
        agent,
        output_model=RetrospectiveDecision,
        prompt=user_message,
    )


def compose_message(
    *,
    event_type: EventKind,
    project_slug: str,
    target_repo: str,
    pr_url: str | None,
    issue_url: str | None,
    reason: str | None,
) -> str:
    """Compose the user-message prompt for one retrospective."""
    parts = [
        agent_memory_preamble(
            project_slug=project_slug,
            query=f"retrospective on {event_type}",
        ),
        f"Project: {project_slug}",
        f"Target repo: {target_repo}",
        f"Event: {event_type}",
    ]
    if pr_url:
        parts.append(f"Impl PR: {pr_url}")
    if issue_url:
        parts.append(f"Source issue: {issue_url}")
    if reason:
        parts += ["", "Reason / context (from the platform):", reason.strip()]
    parts += [
        "",
        "Steps:",
        "  1. read_memory_md to see what's already recorded — DO NOT propose duplicates.",
        "  2. If an impl PR is involved, get_pr + list_pr_comments + list_pr_review_comments.",
        "  3. If a source issue is involved, get_issue + list_issue_comments.",
        "  4. Decide whether the trace contains a reusable lesson worth appending "
        "to MEMORY.md. Return a RetrospectiveDecision JSON.",
    ]
    return "\n".join(parts)
