"""Strands Agent factory for the Code-Critic.

The Code-Critic uses Claude Opus 4.7 on Bedrock to adversarially
review the integrated impl PR — logical gaps, missing edge cases,
drift from the spec's intent, integration-level concerns the
reviewer's task-level scan might miss. Emits a :class:`Critique` via
Strands' ``structured_output_model`` parameter.
"""

from __future__ import annotations

import os

from strands import Agent, tool
from strands.models import BedrockModel

from code_critic.critique import Critique
from code_critic.hooks import build_hooks
from code_critic.tools import (
    browse_url_tool,
    read_memory_md_tool,
    read_spec_doc_tool,
    read_stack_profile_md_tool,
)
from common.memory import agent_memory_preamble
from common.routing import load_system_prompt, pick_variant
from common.runtime import default_retry_strategy, run_for_structured_output
from common.sandbox import get_pr_diff

DEFAULT_MODEL_ID = "us.anthropic.claude-opus-4-7-v1"

get_pr_diff_tool = tool(get_pr_diff)


def model_id() -> str:
    """Bedrock model id, overridable via ``AIDLC_BEDROCK_MODEL_ID``."""
    return os.environ.get("AIDLC_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)


def build_agent(run_id: str) -> Agent:
    """Build a fresh Strands Agent for one code-critic invocation."""
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
            read_memory_md_tool,
            read_stack_profile_md_tool,
            read_spec_doc_tool,
            get_pr_diff_tool,
            browse_url_tool,
        ],
        hooks=build_hooks(),
        retry_strategy=default_retry_strategy(bedrock_model_id),
    )


def critique_pr(
    agent: Agent,
    *,
    project_slug: str,
    spec_slug: str,
    run_id: str,
    pr_url: str,
    revision_number: int,
) -> Critique:
    """Run the agent and return the validated Critique."""
    user_message = compose_message(
        project_slug=project_slug,
        spec_slug=spec_slug,
        run_id=run_id,
        pr_url=pr_url,
        revision_number=revision_number,
    )
    return run_for_structured_output(agent, output_model=Critique, prompt=user_message)


def compose_message(
    *,
    project_slug: str,
    spec_slug: str,
    run_id: str,
    pr_url: str,
    revision_number: int,
) -> str:
    """Compose the user-message prompt for the code-critic."""
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
        agent_memory_preamble(project_slug=project_slug, query=spec_slug),
        f"Project: {project_slug}",
        f"Spec slug: {spec_slug}",
        f"Run id: {run_id}",
        f"Impl PR: {pr_url}",
        f"Revision number: {revision_number}",
        "",
        revision_context,
        "",
        f"Read the three spec documents in order — requirements, design, tasks "
        f"(spec_slug={spec_slug}) — and the project's MEMORY.md "
        f"(project_slug={project_slug}). Fetch the impl PR diff with "
        "``get_pr_diff``. Then return a Critique JSON object focused on "
        "logical gaps, missing edge cases, and drift from the spec's intent.",
    ]
    return "\n".join(parts)
