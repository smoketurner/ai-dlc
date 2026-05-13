"""Dispatcher Lambda — invokes the Retrospector for every terminal event.

Subscribed to EventBridge for the three run-terminal event types in the
single-PR-per-issue model:

  * ``RUN.COMPLETED`` — the impl PR merged and the run reached ``done``.
  * ``RUN.FAILED``    — a terminal failure projected the run to ``failed``.
  * ``RUN.CANCEL_REQUESTED`` — issue closed, bot unassigned, or the
    impl PR closed without merge. Cancellation projects the run to
    ``cancelled``.

For each event we extract a normalised ``RetrospectorInput`` and
asynchronously invoke the Retrospector AgentCore Runtime. The Lambda
returns immediately; the agent runs as a daemon thread inside its
container and emits no follow-up event (lessons land as PRs opened
against ``MEMORY.md``, not as platform events).

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
from botocore.exceptions import BotoCoreError, ClientError

from common.events import UntypedEnvelope
from common.identity import runtime_user_id
from common.trace_context import current_trace_context

if TYPE_CHECKING:
    from mypy_boto3_bedrock_agentcore.client import BedrockAgentCoreClient
    from mypy_boto3_dynamodb.client import DynamoDBClient


logger = Logger(service="retrospector_dispatcher")
tracer = Tracer(service="retrospector_dispatcher")
metrics = Metrics(namespace="ai-dlc", service="retrospector_dispatcher")


TERMINAL_EVENT_TYPES = frozenset(
    {
        "RUN.COMPLETED",
        "RUN.FAILED",
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


@cache
def ddb_client() -> DynamoDBClient:
    """Process-cached DynamoDB client (used to look up the run's requester)."""
    return boto3.client("dynamodb")


def retrospector_runtime_arn() -> str:
    """ARN of the Retrospector AgentCore Runtime."""
    return os.environ["AIDLC_RETROSPECTOR_RUNTIME_ARN"]


def lookup_run_requester(run_id: str) -> tuple[str | None, str | None]:
    """Read the run's STATE row and return ``(requestor_sub, requestor)``.

    Returns ``(None, None)`` when the row is missing, identity columns
    are absent, the table env var is unset, or the DDB call raises —
    the caller falls back to a synthetic ``system:`` identity rather
    than failing the dispatch (the retrospector is best-effort).
    """
    table = os.environ.get("AIDLC_RUNS_TABLE")
    if not table:
        return None, None
    try:
        resp = ddb_client().get_item(
            TableName=table,
            Key={"pk": {"S": f"RUN#{run_id}"}, "sk": {"S": "STATE"}},
            ProjectionExpression="requestor, requestor_sub",
            ConsistentRead=False,
        )
    except (BotoCoreError, ClientError) as exc:
        logger.warning("ddb lookup failed", extra={"run_id": run_id, "err": str(exc)})
        return None, None
    item = resp.get("Item") or {}
    requestor = item.get("requestor", {}).get("S")
    requestor_sub = item.get("requestor_sub", {}).get("S")
    return requestor_sub, requestor


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

    run_id = str(envelope.run_id)
    requestor_sub, requestor = lookup_run_requester(run_id)
    user_id = runtime_user_id(
        requestor_sub=requestor_sub,
        requestor=requestor,
        fallback="system:retrospector",
    )
    invoke_runtime(payload=payload, run_id=run_id, user_id=user_id)
    metrics.add_metric(name="RetrospectivesDispatched", unit=MetricUnit.Count, value=1)
    return {"ok": True, "dispatched": event_type, "run_id": run_id}


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

    On ``RUN.FAILED`` with ``revision_count > 0`` (cap-hit), enumerates
    the validator artifact S3 keys across every revision round so the
    retrospector can read each round's findings.
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
    revision_count = int(payload.get("revision_count") or 0)
    run_id = str(envelope.run_id)
    return {
        "event_type": envelope.type,
        "project_slug": project_slug,
        "target_repo": target_repo,
        "pr_url": pr_url,
        "issue_url": issue_url,
        "reason": payload.get("reason") or "",
        "revision_count": revision_count,
        "validation_artifact_keys": validation_artifact_keys(run_id, revision_count),
        "run_id": run_id,
        "correlation_id": str(envelope.correlation_id),
        "actor_id": "retrospector_dispatcher",
    }


VALIDATOR_KINDS = ("reviewer", "tester", "code_critic")


def validation_artifact_keys(run_id: str, revision_count: int) -> list[str]:
    """Enumerate S3 keys for every validator artifact across revisions.

    The state-router runs the three validators ``revision_count + 1``
    times (round 0 against the initial PR, then once after each
    implementer revision). Each validator writes
    ``runs/{run_id}/validation/{kind}-r{N}.md`` per round.
    """
    rounds = range(revision_count + 1)
    return [f"runs/{run_id}/validation/{kind}-r{n}.md" for n in rounds for kind in VALIDATOR_KINDS]


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


def invoke_runtime(*, payload: dict[str, Any], run_id: str, user_id: str) -> None:
    """Asynchronously invoke the Retrospector AgentCore Runtime.

    AgentCore Runtime invocations are synchronous request/response;
    we keep the Lambda fast by relying on the runtime's internal
    daemon-thread pattern (the runtime's ``handler`` returns
    ``{"status": "dispatched"}`` in ~100ms regardless of how long the
    underlying agent takes).

    ``user_id`` is forwarded as ``runtimeUserId``; see
    :mod:`common.identity` for the derivation rule. Without it the
    agent SDK can't bootstrap its workload access token and gateway
    MCP calls fail closed.
    """
    body = json.dumps(payload).encode("utf-8")
    agentcore_client().invoke_agent_runtime(
        agentRuntimeArn=retrospector_runtime_arn(),
        runtimeSessionId=f"retrospector-{run_id}",
        runtimeUserId=user_id,
        contentType="application/json",
        accept="application/json",
        payload=body,
        **current_trace_context(),
    )
