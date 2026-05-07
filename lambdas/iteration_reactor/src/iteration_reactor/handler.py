"""Side-loop reactor for PR iteration triggers.

Two invocation modes share this handler:

  * **Webhook path** — the dashboard's GitHub webhook receiver invokes
    this Lambda synchronously after detecting an iteration-trigger event
    (CI failure, changes_requested review, ``@aidlc-bot``-mention on a
    PR comment / review comment). Input shape is :class:`WebhookTrigger`.
    The handler increments the per-task iteration counter, applies the
    no-progress + max-iterations guards, and dispatches the implementer
    via ``bedrock-agentcore:InvokeAgentRuntime``.

  * **EventBridge path** — an EventBridge rule on
    ``TASK.ITERATION_COMMITTED`` (emitted by the implementer when its
    iteration commit lands) routes the event here. The handler then
    dispatches the Reviewer + Tester against the new commit so their
    advisory output stays current.

The reactor never talks to Step Functions; the SFN's
``WaitForTaskApproval`` token sits unchanged through every iteration
and only releases when the PR finally closes (merge → approve, close
without merge → reject).
"""

from __future__ import annotations

import json
import os
import time
from functools import cache
from typing import TYPE_CHECKING, Any, Literal

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.config import Config
from botocore.exceptions import ClientError, ReadTimeoutError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from common.event_emit import publish
from common.events import (
    EventEnvelope,
    TaskIterationCommitted,
    TaskIterationStarted,
    TaskMaxIterationsReached,
    UntypedEnvelope,
)
from common.ids import CorrelationId, RunId, new_event_id
from common.runtime import (
    CiFailureFeedback,
    FeedbackItem,
    IssueCommentMentionFeedback,
    ReviewChangesRequestedFeedback,
    ReviewCommentMentionFeedback,
)

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

logger = Logger(service="iteration_reactor")
tracer = Tracer(service="iteration_reactor")
metrics = Metrics(namespace="ai-dlc", service="iteration_reactor")

DISPATCH_READ_TIMEOUT_SECONDS = 2.0
DISPATCH_CONNECT_TIMEOUT_SECONDS = 10.0
MAX_ITERATIONS = 3
ITERATION_ROW_TTL_SECONDS = 7 * 24 * 3600

TriggerKind = Literal[
    "ci_failure",
    "review_changes_requested",
    "review_comment_mention",
    "issue_comment_mention",
]


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class WebhookTrigger(BaseModel):
    """Direct-invoke shape from ``services/dashboard/.../webhooks.py``.

    The webhook can't recover the original requestor's Cognito sub from a
    GitHub event, so iteration commits always attribute to the GitHub
    App's installation token (``ai-dlc[bot]``). If you need user-OBO
    attribution on iteration commits, look it up from the runs STATE
    row in a follow-up.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    trigger_kind: TriggerKind
    run_id: str = Field(min_length=1, max_length=64)
    task_id: str = Field(min_length=1, max_length=32)
    correlation_id: str = Field(min_length=1, max_length=64)
    project_slug: str = Field(min_length=1, max_length=64)
    spec_slug: str = Field(min_length=1, max_length=128)
    spec_s3_prefix: str = Field(min_length=1, max_length=512)
    target_repo: str = Field(min_length=3, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")
    pr_url: str = Field(pattern=r"^https://github\.com/.+/pull/\d+$", max_length=512)
    pr_number: int = Field(ge=1)
    # Empty when the upstream GitHub event doesn't carry it (issue-comment
    # webhooks). The implementer reads the actual head_sha after checkout
    # before emitting TASK.ITERATION_COMMITTED, so an empty value here just
    # means "unknown to the webhook". Comment-id-based dedup still works.
    head_sha: str = Field(default="", max_length=40)
    delivery_id: str = Field(min_length=1, max_length=128)
    # Per-trigger payload — shape varies by ``trigger_kind``. Validated
    # downstream by :func:`build_feedback_item`.
    trigger_payload: dict[str, Any]


# ---------------------------------------------------------------------------
# Lambda entrypoint + dispatch
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@tracer.capture_lambda_handler
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: LambdaContext) -> dict[str, Any]:
    """Dispatch on input shape: webhook trigger vs EventBridge event."""
    if isinstance(event, dict) and event.get("detail-type") == "TASK.ITERATION_COMMITTED":
        return handle_iteration_committed(event)
    if isinstance(event, dict) and "trigger_kind" in event:
        try:
            trigger = WebhookTrigger.model_validate(event)
        except ValidationError as exc:
            return error("validation_error", json.loads(exc.json()))
        return handle_webhook_trigger(trigger)
    return error("unknown_event_shape", "expected WebhookTrigger or TASK.ITERATION_COMMITTED")


def handle_webhook_trigger(trigger: WebhookTrigger) -> dict[str, Any]:
    """Increment counter, dispatch implementer (with guards)."""
    state = read_iteration_state(trigger.run_id, trigger.task_id)
    if state is not None and trigger.delivery_id in state["delivery_ids"]:
        logger.info("duplicate delivery — already acted", delivery_id=trigger.delivery_id)
        return ok(skipped="duplicate_delivery")

    iteration_count = state["iteration_count"] if state else 0
    if iteration_count >= MAX_ITERATIONS:
        logger.info("max iterations reached — leaving for human", iteration_count=iteration_count)
        emit_max_iterations_reached(trigger, iteration_count)
        return ok(skipped="max_iterations", iteration_count=iteration_count)

    new_count = iteration_count + 1
    feedback_item = build_feedback_item(trigger)
    record_iteration_started(trigger, new_count)
    post_iteration_comment(trigger, new_count)
    emit_iteration_started(trigger, new_count)
    dispatch_implementer(trigger, feedback_item, iteration_count=new_count)
    metrics.add_metric(name="IterationsDispatched", unit=MetricUnit.Count, value=1)
    return ok(dispatched="implementer", iteration_count=new_count)


def handle_iteration_committed(event: dict[str, Any]) -> dict[str, Any]:
    """Implementer pushed an iteration commit — fan out Reviewer + Tester."""
    detail = event.get("detail")
    if not isinstance(detail, dict):
        return error("validation_error", "EventBridge event missing detail")
    try:
        envelope = UntypedEnvelope.model_validate(detail)
    except ValidationError as exc:
        return error("validation_error", json.loads(exc.json()))
    try:
        committed = TaskIterationCommitted.model_validate(envelope.payload)
    except ValidationError as exc:
        return error("validation_error", json.loads(exc.json()))
    dispatch_reviewer(envelope, committed)
    dispatch_tester(envelope, committed)
    metrics.add_metric(name="AdvisoryDispatched", unit=MetricUnit.Count, value=2)
    return ok(dispatched=["reviewer", "tester"], iteration_count=committed.iteration_count)


# ---------------------------------------------------------------------------
# Feedback construction
# ---------------------------------------------------------------------------


def build_feedback_item(trigger: WebhookTrigger) -> FeedbackItem:
    """Convert the raw webhook trigger payload into a typed FeedbackItem."""
    p = trigger.trigger_payload
    if trigger.trigger_kind == "ci_failure":
        return CiFailureFeedback(
            workflow_name=str(p.get("workflow_name", "(unknown)")),
            conclusion=p.get("conclusion") or "failure",
            head_sha=trigger.head_sha,
            html_url=str(p.get("html_url", "")),
        )
    if trigger.trigger_kind == "review_changes_requested":
        return ReviewChangesRequestedFeedback(
            reviewer=str(p.get("reviewer", "unknown")),
            body=str(p.get("body", "")),
            review_id=int(p.get("review_id", 0)),
        )
    if trigger.trigger_kind == "review_comment_mention":
        # commit_id must be at least 7 chars (FeedbackItem.head_sha rule);
        # GitHub always populates it on review-comment events, but fall
        # back to a placeholder if a malformed payload omits it.
        commit_id = str(p.get("commit_id") or trigger.head_sha or "0000000")
        return ReviewCommentMentionFeedback(
            path=str(p.get("path", "")),
            line=p.get("line"),
            commit_id=commit_id,
            comment_id=int(p.get("comment_id", 0)),
            in_reply_to_id=p.get("in_reply_to_id"),
            body=str(p.get("body", "")),
            commenter=str(p.get("commenter", "unknown")),
        )
    return IssueCommentMentionFeedback(
        comment_id=int(p.get("comment_id", 0)),
        body=str(p.get("body", "")),
        commenter=str(p.get("commenter", "unknown")),
    )


# ---------------------------------------------------------------------------
# DynamoDB iteration-state row
# ---------------------------------------------------------------------------


@cache
def ddb_client() -> DynamoDBClient:
    """Process-cached DynamoDB client."""
    return boto3.client("dynamodb")


def runs_table() -> str:
    """Runs table name (set by Terraform)."""
    return os.environ["AIDLC_RUNS_TABLE"]


def iteration_pk(run_id: str) -> str:
    """Partition key for an iteration row."""
    return f"RUN#{run_id}"


def iteration_sk(task_id: str) -> str:
    """Sort key for an iteration row — one per task."""
    return f"TASK#{task_id}#ITER"


def read_iteration_state(run_id: str, task_id: str) -> dict[str, Any] | None:
    """Return the iteration-state row, or ``None`` if no iteration has started."""
    response = ddb_client().get_item(
        TableName=runs_table(),
        Key={"pk": {"S": iteration_pk(run_id)}, "sk": {"S": iteration_sk(task_id)}},
        ConsistentRead=True,
    )
    item = response.get("Item")
    if item is None:
        return None
    return {
        "iteration_count": int(item.get("iteration_count", {}).get("N", "0")),
        "delivery_ids": set(item.get("delivery_ids", {}).get("SS", [])),
    }


def record_iteration_started(trigger: WebhookTrigger, new_count: int) -> None:
    """Write the new iteration counter + delivery_id to DDB."""
    expires_at = int(time.time()) + ITERATION_ROW_TTL_SECONDS
    ddb_client().update_item(
        TableName=runs_table(),
        Key={
            "pk": {"S": iteration_pk(trigger.run_id)},
            "sk": {"S": iteration_sk(trigger.task_id)},
        },
        UpdateExpression=(
            "SET iteration_count = :n, expires_at = :ttl ADD delivery_ids :did"
        ),
        ExpressionAttributeValues={
            ":n": {"N": str(new_count)},
            ":ttl": {"N": str(expires_at)},
            ":did": {"SS": [trigger.delivery_id]},
        },
    )


# ---------------------------------------------------------------------------
# AgentCore InvokeAgentRuntime dispatch (fire-and-forget)
# ---------------------------------------------------------------------------


@cache
def runtime_client() -> Any:
    """Process-cached bedrock-agentcore data-plane client with a short read timeout."""
    return boto3.client(
        "bedrock-agentcore",
        region_name=os.environ["AWS_REGION"],
        config=Config(
            connect_timeout=DISPATCH_CONNECT_TIMEOUT_SECONDS,
            read_timeout=DISPATCH_READ_TIMEOUT_SECONDS,
            retries={"max_attempts": 1, "mode": "standard"},
        ),
    )


def implementer_runtime_arn() -> str:
    """ARN of the Implementer AgentCore Runtime."""
    return os.environ["AIDLC_IMPLEMENTER_RUNTIME_ARN"]


def reviewer_runtime_arn() -> str:
    """ARN of the Reviewer AgentCore Runtime."""
    return os.environ["AIDLC_REVIEWER_RUNTIME_ARN"]


def tester_runtime_arn() -> str:
    """ARN of the Tester AgentCore Runtime."""
    return os.environ["AIDLC_TESTER_RUNTIME_ARN"]


def fire_and_forget_invoke(
    *,
    runtime_arn: str,
    runtime_session_id: str,
    payload: dict[str, Any],
) -> bool:
    """Fire ``invoke_agent_runtime`` with a 2s read timeout (treat as success).

    Mirrors :mod:`runtime_invoker.handler`'s pattern: by the time the read
    fires, the container has accepted the request and is processing in
    background. The agent emits its own completion event when done — the
    reactor doesn't need (and doesn't get) the synchronous response body.
    """
    try:
        runtime_client().invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            qualifier="DEFAULT",
            runtimeSessionId=runtime_session_id,
            contentType="application/json",
            accept="application/json",
            payload=json.dumps(payload).encode("utf-8"),
        )
    except ReadTimeoutError:
        logger.info("dispatched (read-timeout, container processing)", runtime_arn=runtime_arn)
        return True
    except ClientError as exc:
        logger.exception("dispatch failed", runtime_arn=runtime_arn, error=str(exc))
        return False
    logger.info("dispatched (sub-2s response)", runtime_arn=runtime_arn)
    return True


def dispatch_implementer(
    trigger: WebhookTrigger,
    feedback_item: FeedbackItem,
    *,
    iteration_count: int,
) -> None:
    """Invoke the Implementer agent for an iteration run.

    The payload omits ``requestor_sub`` — webhook-triggered iterations
    can't recover the original submitter's identity, so commits attribute
    to ``ai-dlc[bot]`` via the implementer's installation-token fallback.
    """
    payload = {
        "project_slug": trigger.project_slug,
        "spec_slug": trigger.spec_slug,
        "spec_s3_prefix": trigger.spec_s3_prefix,
        "task_id": trigger.task_id,
        "run_id": trigger.run_id,
        "correlation_id": trigger.correlation_id,
        "actor_id": "iteration_reactor",
        "iteration_count": iteration_count,
        "iteration_feedback": [feedback_item.model_dump(mode="json")],
        "pr_url": trigger.pr_url,
        "target_repo": trigger.target_repo,
    }
    fire_and_forget_invoke(
        runtime_arn=implementer_runtime_arn(),
        runtime_session_id=f"{trigger.run_id}-{trigger.task_id}",
        payload=payload,
    )


def dispatch_reviewer(envelope: UntypedEnvelope, committed: TaskIterationCommitted) -> None:
    """Invoke the Reviewer agent against the iteration's new head_sha."""
    payload = {
        "project_slug": committed.project_slug,
        "spec_slug": committed.spec_slug,
        "spec_s3_prefix": "specs/" + committed.spec_slug + "/",
        "task_id": committed.task_id,
        "pr_url": committed.pr_url,
        "diff_summary": committed.diff_summary,
        "run_id": str(envelope.run_id),
        "correlation_id": str(envelope.correlation_id),
        "actor_id": "iteration_reactor",
    }
    fire_and_forget_invoke(
        runtime_arn=reviewer_runtime_arn(),
        runtime_session_id=f"{envelope.run_id}-{committed.task_id}-reviewer",
        payload=payload,
    )


def dispatch_tester(envelope: UntypedEnvelope, committed: TaskIterationCommitted) -> None:
    """Invoke the Tester agent against the iteration's new head_sha."""
    payload = {
        "project_slug": committed.project_slug,
        "spec_slug": committed.spec_slug,
        "spec_s3_prefix": "specs/" + committed.spec_slug + "/",
        "task_id": committed.task_id,
        "pr_url": committed.pr_url,
        "diff_summary": committed.diff_summary,
        "run_id": str(envelope.run_id),
        "correlation_id": str(envelope.correlation_id),
        "actor_id": "iteration_reactor",
    }
    fire_and_forget_invoke(
        runtime_arn=tester_runtime_arn(),
        runtime_session_id=f"{envelope.run_id}-{committed.task_id}-tester",
        payload=payload,
    )


# ---------------------------------------------------------------------------
# PR comment when an iteration starts (so humans see the agent is working)
# ---------------------------------------------------------------------------


@cache
def lambda_client() -> Any:
    """Process-cached boto3 Lambda client (for invoking ``repo_helper``)."""
    return boto3.client("lambda")


def repo_helper_function_name() -> str | None:
    """Lambda function name for ``repo_helper``; ``None`` when not wired."""
    return os.environ.get("AIDLC_REPO_HELPER_FUNCTION_NAME") or None


def post_iteration_comment(trigger: WebhookTrigger, iteration_count: int) -> None:
    """Drop a comment on the PR so the human knows iteration is in flight.

    Best-effort: a failed comment doesn't abort the iteration. Skipped
    when ``AIDLC_REPO_HELPER_FUNCTION_NAME`` is unset (e.g. in unit tests
    that don't want to reach the Lambda).
    """
    fn = repo_helper_function_name()
    if fn is None:
        return
    body = format_iteration_comment(trigger, iteration_count)
    try:
        lambda_client().invoke(
            FunctionName=fn,
            InvocationType="RequestResponse",
            Payload=json.dumps(
                {
                    "input": {
                        "op": "comment_pr",
                        "repo": trigger.target_repo,
                        "pr_number": trigger.pr_number,
                        "body": body,
                    },
                },
            ).encode("utf-8"),
        )
    except Exception as exc:
        logger.warning(
            "iteration comment failed",
            error=str(exc),
            pr_url=trigger.pr_url,
            iteration_count=iteration_count,
        )


def format_iteration_comment(trigger: WebhookTrigger, iteration_count: int) -> str:
    """Render the in-flight comment body."""
    p = trigger.trigger_payload
    if trigger.trigger_kind == "ci_failure":
        what = f"CI failure on `{p.get('workflow_name', '?')}`"
    elif trigger.trigger_kind == "review_changes_requested":
        what = f"changes requested by @{p.get('reviewer', '?')}"
    elif trigger.trigger_kind == "review_comment_mention":
        what = f"inline review comment from @{p.get('commenter', '?')}"
    else:
        what = f"comment from @{p.get('commenter', '?')}"
    return (
        f"\U0001f440 Working on iteration **{iteration_count}** of {MAX_ITERATIONS} "
        f"({what}). I'll push a fix commit and reply inline if anything needs a written response."
    )


# ---------------------------------------------------------------------------
# EventBridge publish
# ---------------------------------------------------------------------------


def emit_iteration_started(trigger: WebhookTrigger, iteration_count: int) -> None:
    """Emit ``TASK.ITERATION_STARTED`` so the dashboard timeline reflects the dispatch."""
    envelope = EventEnvelope[TaskIterationStarted](
        event_id=new_event_id(),
        type="TASK.ITERATION_STARTED",
        run_id=RunId(trigger.run_id),
        correlation_id=CorrelationId(trigger.correlation_id),
        actor_id="iteration_reactor",
        payload=TaskIterationStarted(
            project_slug=trigger.project_slug,
            spec_slug=trigger.spec_slug,
            task_id=trigger.task_id,
            pr_url=trigger.pr_url,
            iteration_count=iteration_count,
            trigger_kinds=[trigger.trigger_kind],
        ),
    )
    publish(envelope)


def emit_max_iterations_reached(trigger: WebhookTrigger, iteration_count: int) -> None:
    """Emit ``TASK.MAX_ITERATIONS_REACHED`` so reviewers know the loop stopped."""
    envelope = EventEnvelope[TaskMaxIterationsReached](
        event_id=new_event_id(),
        type="TASK.MAX_ITERATIONS_REACHED",
        run_id=RunId(trigger.run_id),
        correlation_id=CorrelationId(trigger.correlation_id),
        actor_id="iteration_reactor",
        payload=TaskMaxIterationsReached(
            project_slug=trigger.project_slug,
            spec_slug=trigger.spec_slug,
            task_id=trigger.task_id,
            pr_url=trigger.pr_url,
            iteration_count=max(iteration_count, 1),
        ),
    )
    publish(envelope)


# ---------------------------------------------------------------------------
# Response envelopes
# ---------------------------------------------------------------------------


def ok(**fields: Any) -> dict[str, Any]:
    """Standard success envelope, mirrors repo_helper's wrap."""
    return {"ok": True, **fields}


def error(kind: str, detail: object) -> dict[str, Any]:
    """Standard error envelope; logged at warn level."""
    logger.warning("rejected", extra={"kind": kind, "detail": detail})
    return {"ok": False, "error": {"kind": kind, "detail": detail}}
