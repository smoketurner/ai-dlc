"""Strands Agent factory for the Critic.

The Critic uses Claude Opus 4.7 on Bedrock with a strict-JSON output
contract: the agent loop runs with the spec-reading tools and finishes
by emitting a :class:`Critique` via Strands' ``structured_output_model``
parameter — that constrains the Bedrock model to produce JSON matching
the schema while still letting the agent call ``read_memory_md`` and
``read_spec_doc`` to ground its critique.
"""

from __future__ import annotations

import os

from strands import Agent
from strands.models import BedrockModel

from common.memory import agent_memory_preamble
from common.routing import load_system_prompt, pick_variant
from common.runtime import default_retry_strategy, run_for_structured_output
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
    bedrock_model_id = model_id()
    return Agent(
        model=BedrockModel(
            model_id=bedrock_model_id,
            region_name=os.environ["AWS_REGION"],
            temperature=0.3,
            max_tokens=8192,
            streaming=True,
        ),
        system_prompt=load_system_prompt("critic", variant),
        tools=[read_memory_md_tool, read_spec_doc_tool],
        hooks=build_hooks(),
        retry_strategy=default_retry_strategy(bedrock_model_id),
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
    return run_for_structured_output(agent, output_model=Critique, prompt=user_message)


def compose_message(*, project_slug: str, spec_slug: str, intent: str) -> str:
    """Compose the user-message prompt for the critic."""
    parts = [
        agent_memory_preamble(project_slug=project_slug, query=intent),
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
