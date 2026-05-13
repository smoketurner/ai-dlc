"""Strands Agent factory for the Retrospector.

The Retrospector uses Claude Haiku 4.5 — synthesis is small (read a
PR + comments + MEMORY.md, decide one of two outcomes) and runs on
every terminal event, so the cheaper model is the right pick. The
agent loop runs entirely on gateway-routed tools — ``artifact_tool``
for ``read_memory_md`` / ``read_stack_profile_md`` / ``get_artifact``
and ``repo_helper`` for the PR / issue / file reads — and finishes by
emitting a :class:`RetrospectiveDecision` via Strands'
``structured_output_model`` parameter.
"""

from __future__ import annotations

import os
from typing import Literal

from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

from common.gateway_tools import gateway_tools
from common.memory import agent_memory_preamble
from common.routing import load_system_prompt, pick_variant
from common.runtime import default_retry_strategy, run_for_structured_output
from common.templating import make_template_env
from retrospector.decision import RetrospectiveDecision

DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def model_id() -> str:
    """Bedrock model id, overridable via ``AIDLC_BEDROCK_MODEL_ID``."""
    return os.environ.get("AIDLC_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)


def build_agent(run_id: str, *, mcp_client: MCPClient) -> Agent:
    """Build a fresh Strands Agent for one retrospective.

    The caller is responsible for starting ``mcp_client`` (typically via
    ``with gateway_mcp_client() as mcp_client:``) and keeping it open
    for the lifetime of the agent call. Tool definitions from the
    gateway catalogue are spliced in — the retrospector has no
    local-only tools.

    Prompt variant routed via :func:`common.routing.pick_variant` so
    A/B'ing the retrospector prompt follows the same convention as
    every other Strands agent in the project.
    """
    variant = pick_variant(run_id, "retrospector")
    bedrock_model_id = model_id()
    return Agent(
        model=BedrockModel(
            model_id=bedrock_model_id,
            region_name=os.environ["AWS_REGION"],
            temperature=0.2,
            max_tokens=4096,
            streaming=True,
        ),
        system_prompt=load_system_prompt("retrospector", variant),
        tools=list(gateway_tools(mcp_client)),
        retry_strategy=default_retry_strategy(bedrock_model_id),
    )


type EventKind = Literal[
    "RUN.COMPLETED",
    "RUN.FAILED",
    "RUN.CANCEL_REQUESTED",
]


def retrospect(  # noqa: PLR0913 -- 6 event fields + 2 cap-hit fields + mcp_client
    agent: Agent,
    *,
    event_type: EventKind,
    project_slug: str,
    target_repo: str,
    pr_url: str | None,
    issue_url: str | None,
    reason: str | None,
    revision_count: int = 0,
    validation_artifact_keys: tuple[str, ...] = (),
) -> RetrospectiveDecision:
    """Run the agent against one terminal event and return its decision."""
    user_message = compose_message(
        event_type=event_type,
        project_slug=project_slug,
        target_repo=target_repo,
        pr_url=pr_url,
        issue_url=issue_url,
        reason=reason,
        revision_count=revision_count,
        validation_artifact_keys=validation_artifact_keys,
    )
    return run_for_structured_output(
        agent,
        output_model=RetrospectiveDecision,
        prompt=user_message,
    )


def compose_message(
    *,
    event_type: EventKind,
    project_slug: str,
    target_repo: str,
    pr_url: str | None,
    issue_url: str | None,
    reason: str | None,
    revision_count: int = 0,
    validation_artifact_keys: tuple[str, ...] = (),
) -> str:
    """Compose the user-message prompt for one retrospective."""
    template = make_template_env(__package__).get_template("retrospective_message.md.j2")
    body = template.render(
        memory_preamble=agent_memory_preamble(
            project_slug=project_slug,
            query=f"retrospective on {event_type}",
        ),
        event_type=event_type,
        project_slug=project_slug,
        target_repo=target_repo,
        pr_url=pr_url,
        issue_url=issue_url,
        reason=reason,
        revision_count=revision_count,
        validation_artifact_keys=validation_artifact_keys,
    )
    return body.rstrip("\n")
