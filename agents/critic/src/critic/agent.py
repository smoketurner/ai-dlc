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
from critic.hooks import build_hooks
from critic.tools import read_memory_md_tool, read_spec_doc_tool

DEFAULT_MODEL_ID = "us.anthropic.claude-opus-4-6-v1"


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
        hooks=build_hooks(),
    )


def critique_spec(
    agent: Agent,
    *,
    project_slug: str,
    spec_slug: str,
    intent: str,
) -> Critique:
    """Run the agent and return the validated Critique.

    Caller constructs the agent (via :func:`build_agent`) so the caller
    can read usage metrics off it after this returns.
    """
    user_message = compose_message(project_slug=project_slug, spec_slug=spec_slug, intent=intent)
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
