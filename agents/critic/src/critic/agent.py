"""Strands Agent factory for the Critic.

The Critic uses Claude Opus 4.7 on Bedrock with a strict-JSON output
contract: the agent's final message is parsed as a :class:`Critique`. We
use Strands' ``structured_output`` so the model is constrained to produce
JSON matching the schema.
"""

from __future__ import annotations

import os

from strands import Agent
from strands.models import BedrockModel

from common.routing import load_system_prompt, pick_variant
from critic.critique import Critique
from critic.tools import read_memory_md_tool, read_spec_doc_tool

DEFAULT_MODEL_ID = "us.anthropic.claude-opus-4-7"


def model_id() -> str:
    """Bedrock model id, overridable via ``AIDLC_BEDROCK_MODEL_ID``."""
    return os.environ.get("AIDLC_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)


def build_agent(run_id: str) -> Agent:
    """Build a fresh Strands Agent for one critic invocation.

    Prompt variant routed via :func:`common.routing.pick_variant`.
    """
    variant = pick_variant(run_id, "critic")
    return Agent(
        model=BedrockModel(
            model_id=model_id(),
            region_name=os.environ["AWS_REGION"],
            temperature=0.3,
            max_tokens=8192,
            streaming=True,
        ),
        system_prompt=load_system_prompt("critic", variant),
        tools=[read_memory_md_tool, read_spec_doc_tool],
    )


def critique_spec(*, project_slug: str, spec_slug: str, intent: str, run_id: str) -> Critique:
    """Run the agent and return the validated Critique.

    Args:
        project_slug: Project the spec belongs to.
        spec_slug: Slug of the spec to review.
        intent: Original user intent that produced the spec.
        run_id: Run UUID7 — drives prompt-variant selection.

    Returns:
        A validated :class:`Critique` ready for Markdown rendering.
    """
    user_message = compose_message(project_slug=project_slug, spec_slug=spec_slug, intent=intent)
    agent = build_agent(run_id)
    return agent.structured_output(Critique, user_message)


def compose_message(*, project_slug: str, spec_slug: str, intent: str) -> str:
    """Compose the user-message prompt for the critic."""
    parts = [
        f"Project: {project_slug}",
        f"Spec slug: {spec_slug}",
        "",
        "Original intent:",
        intent.strip(),
        "",
        "Read the three spec documents in order — requirements, design, tasks "
        f"(spec_slug={spec_slug}) — and the project's MEMORY.md "
        f"(project_slug={project_slug}). Then return a Critique JSON object.",
    ]
    return "\n".join(parts)
