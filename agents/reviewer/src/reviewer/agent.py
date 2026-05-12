"""Strands Agent factory for the Reviewer.

The Reviewer uses Claude Sonnet 4.6 on Bedrock. The agent loop runs
with spec/memory readers plus a sandbox runner and finishes by emitting
a :class:`Review` via Strands' ``structured_output_model`` parameter —
that constrains the model to produce JSON matching the schema while
still letting it call grounding tools.
"""

from __future__ import annotations

import os

from strands import Agent
from strands.models import BedrockModel

from common.memory import agent_memory_preamble
from common.routing import load_system_prompt, pick_variant
from common.runtime import default_retry_strategy, run_for_structured_output
from reviewer.hooks import build_hooks
from reviewer.review import Review
from reviewer.tools import (
    browse_url_tool,
    get_pr_diff_tool,
    read_memory_md_tool,
    read_spec_doc_tool,
    read_stack_profile_md_tool,
    run_pr_in_sandbox_tool,
)

DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-6"


def model_id() -> str:
    """Bedrock model id, overridable via ``AIDLC_BEDROCK_MODEL_ID``."""
    return os.environ.get("AIDLC_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)


def build_agent(run_id: str) -> Agent:
    """Build a fresh Strands Agent for one reviewer invocation.

    Prompt variant routed via :func:`common.routing.pick_variant`.
    """
    variant = pick_variant(run_id, "reviewer")
    bedrock_model_id = model_id()
    return Agent(
        model=BedrockModel(
            model_id=bedrock_model_id,
            region_name=os.environ["AWS_REGION"],
            temperature=0.2,
            max_tokens=8192,
            streaming=True,
        ),
        system_prompt=load_system_prompt("reviewer", variant),
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


def review_pr(
    agent: Agent,
    *,
    project_slug: str,
    spec_slug: str,
    run_id: str,
    pr_url: str,
    revision_number: int,
) -> Review:
    """Run the agent and return the validated Review.

    Targets the integrated impl PR — the reviewer reads ``get_pr_diff``
    for the full diff (every task plus any prior revision) and produces
    a single coherent verdict.
    """
    user_message = compose_message(
        project_slug=project_slug,
        spec_slug=spec_slug,
        run_id=run_id,
        pr_url=pr_url,
        revision_number=revision_number,
    )
    return run_for_structured_output(agent, output_model=Review, prompt=user_message)


def compose_message(
    *,
    project_slug: str,
    spec_slug: str,
    run_id: str,
    pr_url: str,
    revision_number: int,
) -> str:
    """Compose the user-message prompt for the reviewer."""
    revision_context = (
        "This is the first validation pass."
        if revision_number == 0
        else (
            f"This is revision pass #{revision_number} — the implementer revised "
            "the impl branch in response to your prior request_changes verdict. "
            "Check whether the previously-flagged issues are resolved; flag any "
            "new ones introduced by the fixes."
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
        f"Read the project's MEMORY.md (project_slug={project_slug}) to apply "
        f"its conventions. Read the three spec documents (spec_slug={spec_slug}) "
        f"to know what the run is supposed to accomplish. Fetch the impl PR "
        f"diff with ``get_pr_diff`` and produce a single coherent verdict over "
        "the integrated diff. Return a Review JSON object.",
    ]
    return "\n".join(parts)
