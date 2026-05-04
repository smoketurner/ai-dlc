"""Strands Agent factory for the Reviewer.

The Reviewer uses Claude Sonnet 4.6 on Bedrock. Output is a
:class:`Review` constrained via Strands' ``structured_output``.
"""

from __future__ import annotations

import os

from strands import Agent
from strands.models import BedrockModel

from common.routing import load_system_prompt, pick_variant
from reviewer.review import Review
from reviewer.tools import read_memory_md_tool, read_spec_doc_tool

DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-6-20260301-v1:0"


def model_id() -> str:
    """Bedrock model id, overridable via ``AIDLC_REVIEWER_MODEL_ID``."""
    return os.environ.get("AIDLC_REVIEWER_MODEL_ID", DEFAULT_MODEL_ID)


def build_agent(run_id: str) -> Agent:
    """Build a fresh Strands Agent for one reviewer invocation.

    Prompt variant routed via :func:`common.routing.pick_variant`.
    """
    variant = pick_variant(run_id, "reviewer")
    return Agent(
        model=BedrockModel(
            model_id=model_id(),
            region_name=os.environ["AWS_REGION"],
            temperature=0.2,
            max_tokens=8192,
            streaming=True,
        ),
        system_prompt=load_system_prompt("reviewer", variant),
        tools=[read_memory_md_tool, read_spec_doc_tool],
    )


def review_pr(
    *,
    project_slug: str,
    spec_slug: str,
    task_id: str,
    pr_url: str,
    diff_summary: str,
    run_id: str,
) -> Review:
    """Run the agent and return the validated Review.

    Args:
        project_slug: Project the PR belongs to.
        spec_slug: Slug of the parent spec.
        task_id: Identifier of the task the PR implements.
        pr_url: GitHub PR URL.
        diff_summary: Diff summary the Implementer produced.
        run_id: Run UUID7 — drives prompt-variant selection.

    Returns:
        A validated :class:`Review` ready for Markdown rendering.
    """
    user_message = compose_message(
        project_slug=project_slug,
        spec_slug=spec_slug,
        task_id=task_id,
        pr_url=pr_url,
        diff_summary=diff_summary,
    )
    agent = build_agent(run_id)
    return agent.structured_output(Review, user_message)


def compose_message(
    *,
    project_slug: str,
    spec_slug: str,
    task_id: str,
    pr_url: str,
    diff_summary: str,
) -> str:
    """Compose the user-message prompt for the reviewer."""
    parts = [
        f"Project: {project_slug}",
        f"Spec slug: {spec_slug}",
        f"Task id: {task_id}",
        f"PR: {pr_url}",
        "",
        "Diff summary the Implementer produced:",
        diff_summary.strip(),
        "",
        f"Read the project's MEMORY.md (project_slug={project_slug}) to apply "
        f"its conventions. Read the three spec documents (spec_slug={spec_slug}) "
        f"to know what the task is supposed to accomplish — focus your review "
        f"on whether the diff implements task {task_id} correctly. Return a "
        "Review JSON object.",
    ]
    return "\n".join(parts)
