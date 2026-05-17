"""Dispatcher Lambda — invokes the Retrospector in capture or consolidate mode.

Two trigger shapes:

* **EventBridge custom-bus events** → capture mode. One invocation per
  PR-signal event (terminal events plus ``IMPL_PR.OPENED`` /
  ``REVIEW.READY`` / ``CHECKS.PASSED`` / ``CHECKS.FAILED`` /
  ``IMPL.ITERATION_REQUESTED``). The Retrospector emits zero or more
  lesson bullets into AgentCore Memory.
* **Scheduled rule** → consolidate mode. Fanned out one invocation
  per destination: the platform destination (the ai-dlc repo
  itself) plus one ``target_repo`` invocation per distinct active
  ``project_slug`` discovered via a scan of the runs table. Each
  invocation reads its destination's pending events, runs the
  consolidator agent, opens up to two PRs, and deletes the
  shipped + discarded events.

The dispatcher returns immediately for both shapes; the Retrospector
runs as a daemon thread inside its AgentCore Runtime container and
emits no follow-up event.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
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


CAPTURE_EVENT_TYPES = frozenset(
    {
        "RUN.COMPLETED",
        "RUN.FAILED",
        "RUN.CANCEL_REQUESTED",
        "IMPL_PR.OPENED",
        "REVIEW.READY",
        "CHECKS.PASSED",
        "CHECKS.FAILED",
        "IMPL.ITERATION_REQUESTED",
    },
)
SCHEDULED_CONSOLIDATE_TYPE = "SCHEDULED.LESSONS_CONSOLIDATE"
PLATFORM_DEST_SLUG = "aidlc-platform"
MAX_PROJECTS_PER_FANOUT = 100

# A github.com path like "/owner/name/pull/42" splits into 3 parts after
# the host prefix; we need at least owner + name (the first two).
MIN_REPO_PATH_PARTS = 2


@cache
def agentcore_client() -> BedrockAgentCoreClient:
    """Process-cached AgentCore data-plane client (used to invoke the runtime)."""
    return boto3.client("bedrock-agentcore")


@cache
def ddb_client() -> DynamoDBClient:
    """Process-cached DynamoDB client (used to look up runs + project list)."""
    return boto3.client("dynamodb")


def retrospector_runtime_arn() -> str:
    """ARN of the Retrospector AgentCore Runtime."""
    return os.environ["AIDLC_RETROSPECTOR_RUNTIME_ARN"]


def platform_repo() -> str:
    """``owner/name`` of the ai-dlc platform repo, target for platform-destination PRs."""
    return os.environ["AIDLC_PLATFORM_REPO"]


def lookup_run_requester(run_id: str) -> tuple[str | None, str | None]:
    """Read the run's STATE row and return ``(requestor_sub, requestor)``."""
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
    """Route to capture (per event) or consolidate (fanout) based on the trigger."""
    if is_scheduled_consolidate(event):
        return handle_scheduled_consolidate()
    return handle_capture_event(event)


def is_scheduled_consolidate(event: dict[str, Any]) -> bool:
    """Detect the scheduled-consolidate trigger by its ``detail-type``."""
    return event.get("detail-type") == SCHEDULED_CONSOLIDATE_TYPE


def handle_capture_event(event: dict[str, Any]) -> dict[str, Any]:
    """Validate the EventBridge custom-bus event and invoke the Retrospector once."""
    try:
        envelope = cast(
            "UntypedEnvelope",
            parse(event=normalise(event), model=UntypedEnvelope, envelope=EventBridgeEnvelope),
        )
    except ValidationError as exc:
        logger.warning("invalid event", extra={"errors": exc.errors()})
        return {"ok": False, "error": "validation_error"}

    event_type = envelope.type
    if event_type not in CAPTURE_EVENT_TYPES:
        logger.info("ignoring unsupported event", extra={"type": event_type})
        return {"ok": True, "ignored": event_type}

    payload = build_capture_input(envelope)
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
    invoke_runtime(payload=payload, session_id=f"retrospector-{run_id}", user_id=user_id)
    metrics.add_metric(name="RetrospectivesDispatched", unit=MetricUnit.Count, value=1)
    return {"ok": True, "dispatched": event_type, "run_id": run_id}


def handle_scheduled_consolidate() -> dict[str, Any]:
    """Fan out one consolidate invocation per destination (platform + per project)."""
    invocations = list(consolidate_invocations())
    for inv in invocations:
        invoke_runtime(
            payload=inv["payload"],
            session_id=inv["session_id"],
            user_id="system:retrospector-consolidate",
        )
    metrics.add_metric(
        name="ConsolidationsDispatched",
        unit=MetricUnit.Count,
        value=len(invocations),
    )
    return {"ok": True, "dispatched_consolidates": len(invocations)}


def consolidate_invocations() -> Iterator[dict[str, Any]]:
    """Yield one payload per destination — platform first, then each active project."""
    yield build_consolidate_invocation(
        destination="platform",
        project_slug=PLATFORM_DEST_SLUG,
        target_repo=platform_repo(),
    )
    for slug, repo in active_projects():
        yield build_consolidate_invocation(
            destination="target_repo",
            project_slug=slug,
            target_repo=repo,
        )


def build_consolidate_invocation(
    *,
    destination: str,
    project_slug: str,
    target_repo: str,
) -> dict[str, Any]:
    """Compose the RetrospectorInput payload + session id for one consolidate fanout."""
    payload = {
        "mode": "consolidate",
        "event_type": SCHEDULED_CONSOLIDATE_TYPE,
        "destination": destination,
        "project_slug": project_slug,
        "target_repo": target_repo,
        "run_id": f"consolidate-{project_slug}",
        "correlation_id": f"consolidate-{project_slug}",
        "actor_id": "retrospector_dispatcher",
    }
    session_id = f"retrospector-consolidate-{destination}-{project_slug}"
    return {"payload": payload, "session_id": session_id}


def active_projects() -> Iterator[tuple[str, str]]:
    """Scan the runs table for distinct ``(project_slug, target_repo)`` tuples.

    Caps total emitted at :data:`MAX_PROJECTS_PER_FANOUT`. Skips items
    that don't carry both fields (e.g., research-mode rows or
    in-progress entries that haven't projected target_repo yet).
    """
    table = os.environ.get("AIDLC_RUNS_TABLE")
    if not table:
        return
    seen: set[tuple[str, str]] = set()
    paginator = ddb_client().get_paginator("scan")
    try:
        pages = paginator.paginate(
            TableName=table,
            FilterExpression="sk = :sk",
            ExpressionAttributeValues={":sk": {"S": "STATE"}},
            ProjectionExpression="project_slug, target_repo",
        )
        for page in pages:
            for item in page.get("Items", []):
                slug = item.get("project_slug", {}).get("S")
                repo = item.get("target_repo", {}).get("S")
                if not slug or not repo:
                    continue
                pair = (slug, repo)
                if pair in seen:
                    continue
                seen.add(pair)
                yield pair
                if len(seen) >= MAX_PROJECTS_PER_FANOUT:
                    return
    except (BotoCoreError, ClientError) as exc:
        logger.warning("ddb scan failed", extra={"err": str(exc)})


def normalise(event: dict[str, Any]) -> dict[str, Any]:
    """Decode ``detail`` if EventBridge ships it as a JSON string."""
    detail = event.get("detail")
    if isinstance(detail, str):
        return {**event, "detail": json.loads(detail)}
    return event


def build_capture_input(envelope: UntypedEnvelope) -> dict[str, Any] | None:
    """Translate a platform event envelope into a capture-mode RetrospectorInput.

    Returns ``None`` when the envelope is missing fields the agent needs
    (target_repo, project_slug, or both PR + issue identifiers).

    Hydrates ``verdict`` from ``REVIEW.READY`` events and
    ``pr_comment_body`` from ``IMPL.ITERATION_REQUESTED`` events so the
    capture-mode prompt sees the highest-signal context for each
    trigger.

    On ``RUN.FAILED`` with ``revision_count > 0`` (cap-hit), enumerates
    the validator artifact S3 keys across every revision round.
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
        "mode": "capture",
        "event_type": envelope.type,
        "project_slug": project_slug,
        "target_repo": target_repo,
        "pr_url": pr_url,
        "issue_url": issue_url,
        "reason": payload.get("reason") or "",
        "verdict": payload.get("verdict"),
        "pr_comment_body": payload.get("pr_comment_body") or payload.get("comment_body") or "",
        "revision_count": revision_count,
        "validation_artifact_keys": validation_artifact_keys(run_id, revision_count),
        "run_id": run_id,
        "correlation_id": str(envelope.correlation_id),
        "actor_id": "retrospector_dispatcher",
    }


VALIDATOR_KINDS = ("reviewer", "tester", "code_critic")


def validation_artifact_keys(run_id: str, revision_count: int) -> list[str]:
    """Enumerate S3 keys for every validator artifact across revisions."""
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


def invoke_runtime(*, payload: dict[str, Any], session_id: str, user_id: str) -> None:
    """Synchronously invoke the Retrospector AgentCore Runtime.

    The Lambda stays fast because the runtime returns
    ``{"status": "dispatched"}`` in ~100ms (the agent runs as a daemon
    thread inside its container).
    """
    body = json.dumps(payload).encode("utf-8")
    agentcore_client().invoke_agent_runtime(
        agentRuntimeArn=retrospector_runtime_arn(),
        runtimeSessionId=session_id,
        runtimeUserId=user_id,
        contentType="application/json",
        accept="application/json",
        payload=body,
        **current_trace_context(),
    )
