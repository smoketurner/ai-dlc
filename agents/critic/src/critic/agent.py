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

from critic.critique import Critique
from critic.prompts import SYSTEM_PROMPT
from critic.tools import read_memory_md_tool, read_spec_doc_tool

DEFAULT_MODEL_ID = "us.anthropic.claude-opus-4-7-20260301-v1:0"


def model_id() -> str:
    """Bedrock model id, overridable via ``AIDLC_CRITIC_MODEL_ID``."""
    return os.environ.get("AIDLC_CRITIC_MODEL_ID", DEFAULT_MODEL_ID)


def build_agent() -> Agent:
    """Build a fresh Strands Agent for one critic invocation."""
    return Agent(
        model=BedrockModel(
            model_id=model_id(),
            temperature=0.3,
            max_tokens=8192,
            streaming=True,
        ),
        system_prompt=SYSTEM_PROMPT,
        tools=[read_memory_md_tool, read_spec_doc_tool],
    )


def critique_spec(*, project_slug: str, spec_slug: str, intent: str) -> Critique:
    """Run the agent and return the validated Critique.

    Args:
        project_slug: Project the spec belongs to.
        spec_slug: Slug of the spec to review.
        intent: Original user intent that produced the spec.

    Returns:
        A validated :class:`Critique` ready for Markdown rendering.
    """
    user_message = compose_message(project_slug=project_slug, spec_slug=spec_slug, intent=intent)
    agent = build_agent()
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
