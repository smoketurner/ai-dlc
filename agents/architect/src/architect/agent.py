"""Strands Agent factory for the Architect.

The Architect uses Claude Opus 4.7 on Bedrock with a strict-JSON output
contract: the agent's final message is parsed as a :class:`SpecBundle`.
We use Strands' structured-output entrypoint (``structured_output``) so
the Bedrock model is constrained to produce JSON matching the schema.
"""

from __future__ import annotations

import os

from strands import Agent
from strands.models import BedrockModel

from architect.prompts import SYSTEM_PROMPT
from architect.spec import SpecBundle
from architect.tools import read_memory_md_tool, write_spec_doc_tool

DEFAULT_MODEL_ID = "us.anthropic.claude-opus-4-7-20260301-v1:0"


def model_id() -> str:
    """Bedrock model id, overridable via ``AIDLC_ARCHITECT_MODEL_ID``."""
    return os.environ.get("AIDLC_ARCHITECT_MODEL_ID", DEFAULT_MODEL_ID)


def build_agent() -> Agent:
    """Build a fresh Strands Agent for one architect invocation."""
    return Agent(
        model=BedrockModel(
            model_id=model_id(),
            temperature=0.4,
            max_tokens=8192,
            streaming=True,
        ),
        system_prompt=SYSTEM_PROMPT,
        tools=[read_memory_md_tool, write_spec_doc_tool],
    )


def generate_spec(intent: str, *, project_slug: str, prior_feedback: str | None) -> SpecBundle:
    """Run the agent and return the validated SpecBundle.

    Args:
        intent: Free-text feature intent from the user.
        project_slug: Project the spec belongs to.
        prior_feedback: Reviewer feedback from a prior rejection, or ``None``.

    Returns:
        A validated :class:`SpecBundle` ready for Markdown rendering.
    """
    user_message = _compose_message(intent, project_slug, prior_feedback)
    agent = build_agent()
    return agent.structured_output(SpecBundle, user_message)


def _compose_message(intent: str, project_slug: str, prior_feedback: str | None) -> str:
    parts = [
        f"Project: {project_slug}",
        "",
        "Intent:",
        intent.strip(),
    ]
    if prior_feedback:
        parts += [
            "",
            "Reviewer feedback from a prior rejected spec — address every point:",
            prior_feedback.strip(),
        ]
    parts += [
        "",
        f"Read the project's MEMORY.md (project_slug={project_slug}) before "
        "you draft the spec; conform to every rule in its Conventions section.",
    ]
    return "\n".join(parts)
