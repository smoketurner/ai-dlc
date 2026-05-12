"""Strands Agent factory for the Tester.

The Tester uses Claude Haiku 4.5 on Bedrock — gap analysis is a focused,
bounded task, so the smaller/cheaper model is appropriate. The agent
loop runs with spec/memory readers plus a sandbox runner and finishes
by emitting a :class:`Report` via Strands' ``structured_output_model``
parameter.
"""

from __future__ import annotations

import os

from strands import Agent
from strands.models import BedrockModel

from common.memory import agent_memory_preamble
from common.routing import load_system_prompt, pick_variant
from common.runtime import default_retry_strategy, run_for_structured_output
from tester.hooks import build_hooks
from tester.report import Report
from tester.tools import (
    browse_url_tool,
    get_pr_diff_tool,
    read_memory_md_tool,
    read_spec_doc_tool,
    read_stack_profile_md_tool,
    run_pr_in_sandbox_tool,
)

DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def model_id() -> str:
    """Bedrock model id, overridable via ``AIDLC_BEDROCK_MODEL_ID``."""
    return os.environ.get("AIDLC_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)


def build_agent(run_id: str) -> Agent:
    """Build a fresh Strands Agent for one tester invocation.

    Prompt variant routed via :func:`common.routing.pick_variant`.
    """
    variant = pick_variant(run_id, "tester")
    bedrock_model_id = model_id()
    return Agent(
        model=BedrockModel(
            model_id=bedrock_model_id,
            region_name=os.environ["AWS_REGION"],
            temperature=0.2,
            max_tokens=8192,
            streaming=True,
        ),
        system_prompt=load_system_prompt("tester", variant),
        tools=[
            read_memory_md_tool,
            read_stack_profile_md_tool,
            read_spec_doc_tool,
            get_pr_diff_tool,
            run_pr_in_sandbox_tool,
            browse_url_tool,
        ],
        hooks=build_hooks(),
        retry_strategy=default_retry_strategy(bedrock_model_id),
    )


def analyze_gaps(
    agent: Agent,
    *,
    project_slug: str,
    spec_slug: str,
    run_id: str,
    pr_url: str,
    revision_number: int,
) -> Report:
    """Run the agent and return the validated Report.

    Targets the integrated impl PR — the tester reads the full diff
    via ``get_pr_diff`` and identifies missing coverage across the
    entire run's contribution.
    """
    user_message = compose_message(
        project_slug=project_slug,
        spec_slug=spec_slug,
        run_id=run_id,
        pr_url=pr_url,
        revision_number=revision_number,
    )
    return run_for_structured_output(agent, output_model=Report, prompt=user_message)


def compose_message(
    *,
    project_slug: str,
    spec_slug: str,
    run_id: str,
    pr_url: str,
    revision_number: int,
) -> str:
    """Compose the user-message prompt for the tester."""
    revision_context = (
        "This is the first validation pass."
        if revision_number == 0
        else (
            f"This is revision pass #{revision_number} — the implementer revised "
            "the impl branch in response to the reviewer's prior verdict. Check "
            "whether new test gaps were introduced by the fixes."
        )
    )
    parts = [
        agent_memory_preamble(project_slug=project_slug, query=spec_slug),
        f"Project: {project_slug}",
        f"Spec slug: {spec_slug}",
        f"Run id: {run_id}",
        f"Impl PR: {pr_url}",
        f"Revision number: {revision_number}",
        "",
        revision_context,
        "",
        f"Read the project's MEMORY.md (project_slug={project_slug}) for "
        f"testing conventions. Read the three spec documents "
        f"(spec_slug={spec_slug}). Fetch the impl PR diff with "
        "``get_pr_diff``. Map each acceptance criterion across all tasks to "
        "a test that exercises it; where no such test exists, list a gap and "
        "suggest a concrete test. Return a Report JSON object.",
    ]
    return "\n".join(parts)
