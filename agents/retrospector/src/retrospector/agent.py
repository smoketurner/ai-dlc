"""Strands Agent factory for the Retrospector.

The Retrospector uses Claude Haiku 4.5 across both modes — capture
is per-event and high-frequency (cheap model wins); consolidate is
once-per-destination-per-week and bounded in size (cheap model
still suffices since the input is a bullet list, not a deep code
review).

The agent loop runs entirely on gateway-routed tools —
``artifact_tool`` for ``read_memory_md`` / ``read_stack_profile_md``
/ ``get_artifact`` and ``repo_helper`` for the PR / issue / file
reads — and finishes by emitting either a
:class:`~retrospector.decision.CaptureDecision` (capture mode) or a
:class:`~retrospector.decision.ConsolidationPlan` (consolidate
mode) via Strands' ``structured_output_model`` parameter.
"""

from __future__ import annotations

import os
from typing import Literal

from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

from common.gateway_tools import gateway_tools
from common.memory import agent_memory_preamble
from common.routing import pick_variant
from common.runtime import default_retry_strategy, run_for_structured_output
from common.templating import make_template_env
from retrospector.decision import CaptureDecision, ConsolidationPlan
from retrospector.hooks import build_hooks
from retrospector.prompts import system_prompt_for_mode

DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

type Mode = Literal["capture", "consolidate"]

type CaptureEventKind = Literal[
    "RUN.COMPLETED",
    "RUN.FAILED",
    "RUN.CANCEL_REQUESTED",
    "IMPL_PR.OPENED",
    "REVIEW.READY",
    "CHECKS.PASSED",
    "CHECKS.FAILED",
    "IMPL.ITERATION_REQUESTED",
]

type Destination = Literal["target_repo", "platform"]


def model_id() -> str:
    """Bedrock model id, overridable via ``AIDLC_BEDROCK_MODEL_ID``."""
    return os.environ.get("AIDLC_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)


def build_agent(run_id: str, *, mode: Mode, mcp_client: MCPClient) -> Agent:
    """Build a fresh Strands Agent for one retrospective in ``mode``.

    The caller is responsible for starting ``mcp_client`` (typically via
    ``with gateway_mcp_client() as mcp_client:``) and keeping it open
    for the lifetime of the agent call.

    Variant tag from :func:`common.routing.pick_variant` still flows
    into the actor_id telemetry, but the Retrospector's prompts
    (capture vs consolidate) are selected by ``mode`` directly — A/B
    of either prompt would land via ``prompts_b.py`` in a future PR.
    """
    pick_variant(run_id, "retrospector")  # telemetry side-effect via actor_id
    bedrock_model_id = model_id()
    return Agent(
        model=BedrockModel(
            model_id=bedrock_model_id,
            region_name=os.environ["AWS_REGION"],
            temperature=0.2,
            max_tokens=4096,
            streaming=True,
        ),
        system_prompt=system_prompt_for_mode(mode),
        tools=list(gateway_tools(mcp_client)),
        hooks=build_hooks(),
        retry_strategy=default_retry_strategy(bedrock_model_id),
    )


def capture(  # noqa: PLR0913 -- 9 event fields are the input contract
    agent: Agent,
    *,
    event_type: CaptureEventKind,
    project_slug: str,
    target_repo: str,
    pr_url: str | None,
    issue_url: str | None,
    reason: str | None,
    verdict: str | None,
    pr_comment_body: str | None,
    revision_count: int = 0,
    validation_artifact_keys: tuple[str, ...] = (),
) -> CaptureDecision:
    """Run the agent against one PR-signal event and return zero or more bullets."""
    user_message = compose_capture_message(
        event_type=event_type,
        project_slug=project_slug,
        target_repo=target_repo,
        pr_url=pr_url,
        issue_url=issue_url,
        reason=reason,
        verdict=verdict,
        pr_comment_body=pr_comment_body,
        revision_count=revision_count,
        validation_artifact_keys=validation_artifact_keys,
    )
    return run_for_structured_output(
        agent,
        output_model=CaptureDecision,
        prompt=user_message,
    )


def consolidate(
    agent: Agent,
    *,
    destination: Destination,
    project_slug: str,
    target_repo: str,
    buffer_content: str,
) -> ConsolidationPlan:
    """Run the agent against one destination's buffer and return its plan."""
    user_message = compose_consolidate_message(
        destination=destination,
        project_slug=project_slug,
        target_repo=target_repo,
        buffer_content=buffer_content,
    )
    return run_for_structured_output(
        agent,
        output_model=ConsolidationPlan,
        prompt=user_message,
    )


def compose_capture_message(  # noqa: PLR0913 -- 9 event fields are the input contract
    *,
    event_type: CaptureEventKind,
    project_slug: str,
    target_repo: str,
    pr_url: str | None,
    issue_url: str | None,
    reason: str | None,
    verdict: str | None,
    pr_comment_body: str | None,
    revision_count: int = 0,
    validation_artifact_keys: tuple[str, ...] = (),
) -> str:
    """Compose the user-message prompt for capture mode."""
    template = make_template_env(__package__).get_template("capture_message.md.j2")
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
        verdict=verdict,
        pr_comment_body=pr_comment_body,
        revision_count=revision_count,
        validation_artifact_keys=validation_artifact_keys,
    )
    return body.rstrip("\n")


def compose_consolidate_message(
    *,
    destination: Destination,
    project_slug: str,
    target_repo: str,
    buffer_content: str,
) -> str:
    """Compose the user-message prompt for consolidate mode."""
    template = make_template_env(__package__).get_template("consolidate_message.md.j2")
    body = template.render(
        memory_preamble=agent_memory_preamble(
            project_slug=project_slug,
            query=f"consolidate lessons for {destination}",
        ),
        destination=destination,
        project_slug=project_slug,
        target_repo=target_repo,
        buffer_content=buffer_content,
    )
    return body.rstrip("\n")
