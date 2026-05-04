"""Strands Agent factory for the Tester.

The Tester uses Claude Haiku 4.5 on Bedrock — gap analysis is a focused,
bounded task, so the smaller/cheaper model is appropriate. Output is a
:class:`Report` constrained via Strands' ``structured_output``.
"""

from __future__ import annotations

import os

from strands import Agent
from strands.models import BedrockModel

from common.routing import load_system_prompt, pick_variant
from tester.report import Report
from tester.tools import read_memory_md_tool, read_spec_doc_tool

DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def model_id() -> str:
    """Bedrock model id, overridable via ``AIDLC_BEDROCK_MODEL_ID``."""
    return os.environ.get("AIDLC_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)


def build_agent(run_id: str) -> Agent:
    """Build a fresh Strands Agent for one tester invocation.

    Prompt variant routed via :func:`common.routing.pick_variant`.
    """
    variant = pick_variant(run_id, "tester")
    return Agent(
        model=BedrockModel(
            model_id=model_id(),
            region_name=os.environ["AWS_REGION"],
            temperature=0.2,
            max_tokens=4096,
            streaming=True,
        ),
        system_prompt=load_system_prompt("tester", variant),
        tools=[read_memory_md_tool, read_spec_doc_tool],
    )


def analyze_gaps(
    *,
    project_slug: str,
    spec_slug: str,
    task_id: str,
    pr_url: str,
    diff_summary: str,
    run_id: str,
) -> Report:
    """Run the agent and return the validated Report.

    Args:
        project_slug: Project the PR belongs to.
        spec_slug: Slug of the parent spec.
        task_id: Identifier of the task the PR implements.
        pr_url: GitHub PR URL.
        diff_summary: Diff summary the Implementer produced.
        run_id: Run UUID7 — drives prompt-variant selection.

    Returns:
        A validated :class:`Report` ready for Markdown rendering.
    """
    user_message = compose_message(
        project_slug=project_slug,
        spec_slug=spec_slug,
        task_id=task_id,
        pr_url=pr_url,
        diff_summary=diff_summary,
    )
    agent = build_agent(run_id)
    return agent.structured_output(Report, user_message)


def compose_message(
    *,
    project_slug: str,
    spec_slug: str,
    task_id: str,
    pr_url: str,
    diff_summary: str,
) -> str:
    """Compose the user-message prompt for the tester."""
    parts = [
        f"Project: {project_slug}",
        f"Spec slug: {spec_slug}",
        f"Task id: {task_id}",
        f"PR: {pr_url}",
        "",
        "Diff summary the Implementer produced:",
        diff_summary.strip(),
        "",
        f"Read the project's MEMORY.md (project_slug={project_slug}) for "
        f"testing conventions. Read the three spec documents "
        f"(spec_slug={spec_slug}) — focus on the acceptance criteria the "
        f"task ({task_id}) claims to implement. Map each AC to a test that "
        "exercises it. Where no such test exists, list a gap and suggest a "
        "concrete test. Return a Report JSON object.",
    ]
    return "\n".join(parts)
