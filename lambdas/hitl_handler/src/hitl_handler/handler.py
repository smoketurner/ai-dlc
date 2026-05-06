"""HITL handler — bridges Step Functions task tokens to human approval.

Two operations dispatched on ``input.op``:

* ``REQUEST_APPROVAL`` — Step Functions invokes via ``.waitForTaskToken``.
  We persist the ``task_token`` in the approvals table keyed by
  ``(run_id, gate_kind, gate_ref)`` and post a comment on the relevant
  GitHub PR via ``repo_helper``.

* ``DECIDE`` — Invoked by API Gateway's ``POST /v1/runs/{id}/decide`` (and
  the GitHub webhook entrypoint). Looks up the stored task_token by gate
  reference and calls ``SendTaskSuccess`` or ``SendTaskFailure``.

Gate references:
  * ``spec``         — the SPEC.READY → SPEC.APPROVED gate; one per run.
  * ``task:T-NNN``   — the per-task TASK.READY → TASK.APPROVED gate.
"""

from __future__ import annotations

import json
import os
from functools import cache
from typing import TYPE_CHECKING, Any, Literal

import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_stepfunctions.client import SFNClient

logger = Logger(service="hitl_handler")


class BaseOp(BaseModel):
    """Common configuration for every input model."""

    model_config = ConfigDict(extra="forbid", strict=True)


class RequestApprovalInput(BaseOp):
    """Persist a Step Functions task_token waiting for human approval.

    ``eval_mode=true`` short-circuits the HITL gate by immediately calling
    ``SendTaskSuccess`` with an auto-approval payload. The eval runner sets
    this so its synthetic SDLC runs don't block forever on human review.
    Real (production) runs always have ``eval_mode=false``.
    """

    op: Literal["REQUEST_APPROVAL"]
    task_token: str = Field(min_length=1)
    run_id: str = Field(min_length=1, max_length=128)
    project_slug: str = Field(min_length=1, max_length=64)
    gate_ref: str = Field(min_length=1, max_length=64)
    pr_url: str | None = None
    summary: str = Field(max_length=4096)
    eval_mode: bool = False
    # Spec gate: S3 key for the Critic's full critique markdown (rendered
    # in the dashboard). Task gates: aggregate counts the Reviewer + Tester
    # produced. All optional — REQUEST_APPROVAL is shared across gates and
    # only the originating gate populates the relevant subset.
    critique_s3_key: str | None = Field(default=None, max_length=512)
    review_verdict: str | None = Field(default=None, max_length=64)
    review_comment_count: int | None = Field(default=None, ge=0)
    test_gap_count: int | None = Field(default=None, ge=0)


class DecideInput(BaseOp):
    """Resolve a pending gate as approved or rejected."""

    op: Literal["DECIDE"]
    run_id: str = Field(min_length=1, max_length=128)
    gate_ref: str = Field(min_length=1, max_length=64)
    decision: Literal["approve", "reject"]
    reviewer: str = Field(min_length=1, max_length=128)
    reason: str | None = None


@cache
def ddb() -> DynamoDBClient:
    """Process-cached DynamoDB client."""
    return boto3.client("dynamodb")


@cache
def sfn() -> SFNClient:
    """Process-cached Step Functions client."""
    return boto3.client("stepfunctions")


def approvals_table() -> str:
    """DynamoDB table holding pending approvals."""
    return os.environ["AIDLC_APPROVALS_TABLE"]


def store_token(req: RequestApprovalInput) -> None:
    """Persist the task_token + gate metadata for later DECIDE lookup."""
    ddb().put_item(
        TableName=approvals_table(),
        Item={
            "pk": {"S": f"RUN#{req.run_id}"},
            "sk": {"S": f"GATE#{req.gate_ref}"},
            "gsi1pk": {"S": f"PROJECT#{req.project_slug}"},
            "gsi1sk": {"S": f"RUN#{req.run_id}#GATE#{req.gate_ref}"},
            "task_token": {"S": req.task_token},
            "pr_url": {"S": req.pr_url or ""},
            "summary": {"S": req.summary},
            "status": {"S": "PENDING"},
        },
    )


def fetch_token(run_id: str, gate_ref: str) -> str | None:
    """Fetch the task_token previously stored for ``(run_id, gate_ref)``."""
    resp = ddb().get_item(
        TableName=approvals_table(),
        Key={"pk": {"S": f"RUN#{run_id}"}, "sk": {"S": f"GATE#{gate_ref}"}},
        ProjectionExpression="task_token, #s",
        ExpressionAttributeNames={"#s": "status"},
    )
    item = resp.get("Item")
    if item is None:
        return None
    if item.get("status", {}).get("S") != "PENDING":
        return None
    return item["task_token"]["S"]


def mark_resolved(run_id: str, gate_ref: str, decision: str, reviewer: str) -> None:
    """Flip the approval row's status from PENDING to the decision outcome."""
    ddb().update_item(
        TableName=approvals_table(),
        Key={"pk": {"S": f"RUN#{run_id}"}, "sk": {"S": f"GATE#{gate_ref}"}},
        UpdateExpression="SET #s = :s, reviewer = :r",
        ConditionExpression="#s = :pending",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": {"S": decision.upper()},
            ":r": {"S": reviewer},
            ":pending": {"S": "PENDING"},
        },
    )


def request_approval(req: RequestApprovalInput) -> dict[str, Any]:
    """Persist token; auto-approve and short-circuit if running in eval mode."""
    store_token(req)
    if req.eval_mode:
        sfn().send_task_success(
            taskToken=req.task_token,
            output=json.dumps(
                {
                    "run_id": req.run_id,
                    "gate_ref": req.gate_ref,
                    "reviewer": "eval-runner",
                    "decision": "approve",
                    "reason": "eval_mode auto-approval",
                },
            ),
        )
        mark_resolved(req.run_id, req.gate_ref, "approve", "eval-runner")
        logger.info(
            "approval gate auto-approved (eval_mode)",
            extra={"run_id": req.run_id, "gate_ref": req.gate_ref},
        )
        return {"ok": True, "gate_ref": req.gate_ref, "auto_approved": True}
    logger.info(
        "approval gate opened",
        extra={"run_id": req.run_id, "gate_ref": req.gate_ref, "pr_url": req.pr_url},
    )
    return {"ok": True, "gate_ref": req.gate_ref}


def decide(req: DecideInput) -> dict[str, Any]:
    """Resolve a pending gate by sending the task_token to Step Functions."""
    token = fetch_token(req.run_id, req.gate_ref)
    if token is None:
        return error(
            "not_found", f"no pending gate for run_id={req.run_id} gate_ref={req.gate_ref}"
        )
    output = {
        "run_id": req.run_id,
        "gate_ref": req.gate_ref,
        "reviewer": req.reviewer,
        "decision": req.decision,
        "reason": req.reason,
    }
    if req.decision == "approve":
        sfn().send_task_success(taskToken=token, output=json.dumps(output))
    else:
        sfn().send_task_failure(
            taskToken=token,
            error="ApprovalRejected",
            cause=req.reason or "rejected without reason",
        )
    mark_resolved(req.run_id, req.gate_ref, req.decision, req.reviewer)
    logger.info(
        "approval gate resolved",
        extra={"run_id": req.run_id, "gate_ref": req.gate_ref, "decision": req.decision},
    )
    return {"ok": True, "decision": req.decision}


DISPATCH: dict[str, tuple[type[BaseOp], Any]] = {
    "REQUEST_APPROVAL": (RequestApprovalInput, request_approval),
    "DECIDE": (DecideInput, decide),
}


@logger.inject_lambda_context(log_event=False)
def handler(event: dict[str, Any], _context: LambdaContext) -> dict[str, Any]:
    """Lambda entrypoint dispatching on ``op``."""
    if not isinstance(event, dict):
        return error("invalid_event", "expected JSON object")
    op = event.get("op")
    if op not in DISPATCH:
        return error("unknown_op", f"op must be one of {sorted(DISPATCH)}, got {op!r}")
    model_cls, fn = DISPATCH[op]
    try:
        req = model_cls.model_validate(event)
    except ValidationError as exc:
        return error("validation_error", json.loads(exc.json()))
    return fn(req)


def error(kind: str, detail: object) -> dict[str, Any]:
    """Log a rejection and return the standard error envelope."""
    logger.warning("op rejected", extra={"kind": kind, "detail": detail})
    return {"ok": False, "error": {"kind": kind, "detail": detail}}
