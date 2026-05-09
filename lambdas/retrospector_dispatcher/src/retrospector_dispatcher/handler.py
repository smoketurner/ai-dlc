"""Dispatcher Lambda — invokes the Retrospector for every terminal event.

Subscribed to EventBridge for the five terminal event types:

  * ``SPEC.APPROVED`` — spec PR merged.
  * ``SPEC.REJECTED`` — spec PR closed without merge.
  * ``TASK.APPROVED`` — task PR merged.
  * ``TASK.REJECTED`` — task PR closed without merge.
  * ``RUN.CANCEL_REQUESTED`` — issue closed or bot unassigned.

For each event we extract a normalised :class:`RetrospectorPayload`
and asynchronously invoke the Retrospector AgentCore Runtime. The
Lambda returns immediately; the agent runs as a daemon thread inside
its container and emits no follow-up event (lessons land as PRs
opened against ``docs/MEMORY.md``, not as platform events).

Events that don't carry the fields the agent needs (project_slug,
target_repo, etc.) are logged and dropped — the dispatcher is
best-effort.
"""

from __future__ import annotations

import json
import os
from functools import cache
from typing import TYPE_CHECKING, Any, cast

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.parser import ValidationError, parse
from aws_lambda_powertools.utilities.parser.envelopes import EventBridgeEnvelope
from aws_lambda_powertools.utilities.typing import LambdaContext

from common.events import UntypedEnvelope

if TYPE_CHECKING:
    from mypy_boto3_bedrock_agentcore.client import BedrockAgentCoreClient


logger = Logger(service="retrospector_dispatcher")
tracer = Tracer(service="retrospector_dispatcher")
metrics = Metrics(namespace="ai-dlc", service="retrospector_dispatcher")


TERMINAL_EVENT_TYPES = frozenset(
    {
        "SPEC.APPROVED",
        "SPEC.REJECTED",
        "TASK.APPROVED",
        "TASK.REJECTED",
        "RUN.CANCEL_REQUESTED",
    },
)

# A github.com path like "/owner/name/pull/42" splits into 3 parts after
# the host prefix; we need at least owner + name (the first two).
MIN_REPO_PATH_PARTS = 2


@cache
def agentcore_client() -> BedrockAgentCoreClient:
    """Process-cached AgentCore data-plane client (used to invoke the runtime)."""
    return boto3.client("bedrock-agentcore")


def retrospector_runtime_arn() -> str:
    """ARN of the Retrospector AgentCore Runtime."""
    return os.environ["AIDLC_RETROSPECTOR_RUNTIME_ARN"]


@logger.inject_lambda_context(log_event=False)
@tracer.capture_lambda_handler
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: LambdaContext) -> dict[str, Any]:
    """Validate the EventBridge event and fire the Retrospector once."""
    try:
        envelope = cast(
            "UntypedEnvelope",
            parse(event=normalise(event), model=UntypedEnvelope, envelope=EventBridgeEnvelope),
        )
    except ValidationError as exc:
        logger.warning("invalid event", extra={"errors": exc.errors()})
        return {"ok": False, "error": "validation_error"}

    event_type = envelope.type
    if event_type not in TERMINAL_EVENT_TYPES:
        logger.info("ignoring non-terminal event", extra={"type": event_type})
        return {"ok": True, "ignored": event_type}

    payload = build_retrospector_input(envelope)
    if payload is None:
        logger.info("event missing fields the retrospector needs", extra={"type": event_type})
        return {"ok": True, "ignored": "missing_fields"}

    invoke_runtime(payload=payload, run_id=str(envelope.run_id))
    metrics.add_metric(name="RetrospectivesDispatched", unit=MetricUnit.Count, value=1)
    return {"ok": True, "dispatched": event_type, "run_id": str(envelope.run_id)}


def normalise(event: dict[str, Any]) -> dict[str, Any]:
    """Decode ``detail`` if EventBridge ships it as a JSON string."""
    detail = event.get("detail")
    if isinstance(detail, str):
        return {**event, "detail": json.loads(detail)}
    return event


def build_retrospector_input(envelope: UntypedEnvelope) -> dict[str, Any] | None:
    """Translate a platform event envelope into the Retrospector's input shape.

    Returns ``None`` when the envelope is missing fields the agent
    needs (target_repo, project_slug, or both PR + issue identifiers).
    """
    payload = envelope.payload or {}
    project_slug = payload.get("project_slug")
    if not isinstance(project_slug, str) or not project_slug:
        return None
    pr_url = payload.get("pr_url") or ""
    issue_url = payload.get("source_issue_url") or payload.get("issue_url") or ""
    if not pr_url and not issue_url:
        return None
    target_repo = derive_target_repo(pr_url=pr_url, issue_url=issue_url)
    if not target_repo:
        return None
    return {
        "event_type": envelope.type,
        "project_slug": project_slug,
        "target_repo": target_repo,
        "pr_url": pr_url,
        "issue_url": issue_url,
        "spec_slug": payload.get("spec_slug") or "",
        "task_id": payload.get("task_id") or "",
        "reviewer": payload.get("reviewer") or payload.get("requestor") or "",
        "reason": payload.get("reason") or "",
        "run_id": str(envelope.run_id),
        "correlation_id": str(envelope.correlation_id),
        "actor_id": "retrospector_dispatcher",
    }


def derive_target_repo(*, pr_url: str, issue_url: str) -> str:
    """Pull ``owner/name`` out of a github.com URL."""
    source = pr_url or issue_url
    prefix = "https://github.com/"
    if not source.startswith(prefix):
        return ""
    rest = source[len(prefix) :]
    parts = rest.split("/", 2)
    if len(parts) < MIN_REPO_PATH_PARTS:
        return ""
    return f"{parts[0]}/{parts[1]}"


def invoke_runtime(*, payload: dict[str, Any], run_id: str) -> None:
    """Asynchronously invoke the Retrospector AgentCore Runtime.

    AgentCore Runtime invocations are synchronous request/response;
    we keep the Lambda fast by relying on the runtime's internal
    daemon-thread pattern (the runtime's ``handler`` returns
    ``{"status": "dispatched"}`` in ~100ms regardless of how long the
    underlying agent takes).
    """
    body = json.dumps(payload).encode("utf-8")
    agentcore_client().invoke_agent_runtime(
        agentRuntimeArn=retrospector_runtime_arn(),
        runtimeSessionId=f"retrospector-{run_id}",
        contentType="application/json",
        accept="application/json",
        payload=body,
    )
