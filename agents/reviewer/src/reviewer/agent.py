"""Strands Agent factory for the Reviewer.

The Reviewer uses Claude Sonnet 4.6 on Bedrock. Output is a
:class:`Review` constrained via Strands' ``structured_output``.
"""

from __future__ import annotations

import os

from strands import Agent
from strands.models import BedrockModel

from common.memory import agent_memory_preamble
from common.routing import load_system_prompt, pick_variant
from reviewer.hooks import build_hooks
from reviewer.review import Review
from reviewer.tools import read_memory_md_tool, read_spec_doc_tool, run_pr_in_sandbox_tool

DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-6"


def model_id() -> str:
    """Bedrock model id, overridable via ``AIDLC_BEDROCK_MODEL_ID``."""
    return os.environ.get("AIDLC_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)


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
            strict_tools=True,
        ),
        system_prompt=load_system_prompt("reviewer", variant),
        tools=[read_memory_md_tool, read_spec_doc_tool, run_pr_in_sandbox_tool],
        hooks=build_hooks(),
    )


def review_pr(
    agent: Agent,
    *,
    project_slug: str,
    spec_slug: str,
    task_id: str,
    pr_url: str,
    diff_summary: str,
) -> Review:
    """Run the agent and return the validated Review.

    Caller constructs the agent (via :func:`build_agent`) so the caller
    can read usage metrics off it after this returns.
    """
    user_message = compose_message(
        project_slug=project_slug,
        spec_slug=spec_slug,
        task_id=task_id,
        pr_url=pr_url,
        diff_summary=diff_summary,
    )
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
        agent_memory_preamble(project_slug=project_slug, query=diff_summary),
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
