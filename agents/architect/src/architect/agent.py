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

from architect.hooks import build_hooks
from architect.spec import SpecBundle
from architect.tools import read_memory_md_tool, write_spec_doc_tool
from common.routing import load_system_prompt, pick_variant

DEFAULT_MODEL_ID = "us.anthropic.claude-opus-4-6-v1"


def model_id() -> str:
    """Bedrock model id, overridable via ``AIDLC_BEDROCK_MODEL_ID``."""
    return os.environ.get("AIDLC_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)


def build_agent(run_id: str) -> Agent:
    """Build a fresh Strands Agent for one architect invocation.

    The system prompt is selected via A/B routing — if ``architect.prompts_b``
    exists, half of runs (deterministically picked from ``run_id``) use it.
    """
    variant = pick_variant(run_id, "architect")
    return Agent(
        model=BedrockModel(
            model_id=model_id(),
            region_name=os.environ["AWS_REGION"],
            temperature=0.4,
            max_tokens=8192,
            streaming=True,
        ),
        system_prompt=load_system_prompt("architect", variant),
        tools=[read_memory_md_tool, write_spec_doc_tool],
        hooks=build_hooks(),
    )


def generate_spec(
    intent: str, *, project_slug: str, prior_feedback: str | None, run_id: str
) -> SpecBundle:
    """Run the agent and return the validated SpecBundle.

    Args:
        intent: Free-text feature intent from the user.
        project_slug: Project the spec belongs to.
        prior_feedback: Reviewer feedback from a prior rejection, or ``None``.
        run_id: Run UUID7 — drives prompt-variant selection.

    Returns:
        A validated :class:`SpecBundle` ready for Markdown rendering.
    """
    user_message = _compose_message(intent, project_slug, prior_feedback)
    agent = build_agent(run_id)
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
