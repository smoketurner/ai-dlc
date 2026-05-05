"""Triage dispatcher Lambda — turns one GitHub issue into a routing decision.

Invocation paths:

* GitHub webhook (issues.opened, issues.labeled, issue_comment.created with
  ``/aidlc go``) → dashboard webhook handler → this Lambda's sync invoke.
* EventBridge schedule (5-min cron backstop) → list ``aidlc:ready`` issues
  per registered repo → this Lambda's sync invoke per matching issue.

For each invocation: invoke Bedrock Haiku with the one-way-door rubric,
parse a :class:`TriageVerdict`, then either emit ``REQUEST.RECEIVED`` (go),
or post a comment + label change (defer / decline) via ``repo_helper``.

The Lambda is intentionally not invoked by Step Functions and isn't part
of the SDLC state machine — Triage runs *before* a run exists.
"""

from __future__ import annotations

import json
import os
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import ValidationError

from common.events import EventEnvelope, RequestReceived
from common.ids import new_correlation_id, new_event_id, new_run_id
from triage_dispatcher.bedrock import classify
from triage_dispatcher.models import OneWayDoor, TriageRequest, TriageVerdict
from triage_dispatcher.prompts import SYSTEM_PROMPT, render_user_message

if TYPE_CHECKING:
    from mypy_boto3_events.client import EventBridgeClient
    from mypy_boto3_lambda.client import LambdaClient

logger = Logger(service="triage_dispatcher")

READY_LABEL = "aidlc:ready"
IN_PROGRESS_LABEL = "aidlc:in-progress"
DEFERRED_LABEL = "aidlc:deferred"
DECLINED_LABEL = "aidlc:declined"


@cache
def events_client() -> EventBridgeClient:
    """Process-cached EventBridge client."""
    return boto3.client("events", region_name=os.environ["AWS_REGION"])


@cache
def lambda_client() -> LambdaClient:
    """Process-cached Lambda client (for repo_helper sync invokes)."""
    return boto3.client("lambda", region_name=os.environ["AWS_REGION"])


def bus_name() -> str:
    """Platform EventBridge bus name."""
    return os.environ["AIDLC_BUS_NAME"]


def repo_helper_function() -> str:
    """ARN or name of the repo_helper Lambda this dispatcher invokes."""
    return os.environ["AIDLC_REPO_HELPER_FUNCTION_NAME"]


@logger.inject_lambda_context(log_event=False)
def handler(event: dict[str, Any], _context: LambdaContext) -> dict[str, Any]:
    """Triage one issue. Returns the decision envelope for caller logging."""
    try:
        req = TriageRequest.model_validate(event)
    except ValidationError as exc:
        logger.warning("invalid input", extra={"errors": json.loads(exc.json())})
        return {"ok": False, "error": "validation_error"}

    logger.info(
        "triage start",
        extra={"repo": req.repo, "issue": req.issue_number, "labels": req.labels},
    )

    user_message = render_user_message(
        repo=req.repo,
        issue_number=req.issue_number,
        title=req.title,
        body=req.body,
        labels=req.labels,
    )

    try:
        verdict = classify(system_prompt=SYSTEM_PROMPT, user_message=user_message)
    except ValueError as exc:
        logger.exception("classification failed", extra={"reason": str(exc)})
        return {"ok": False, "error": "classification_failed"}

    return apply(req, verdict)


def apply(req: TriageRequest, verdict: TriageVerdict) -> dict[str, Any]:
    """Carry out the verdict: emit a run, comment + label, or both."""
    if verdict.decision == "go":
        run_id = emit_request_received(req, verdict)
        comment_body = format_go_comment(verdict, run_id)
        post_comment(req, comment_body)
        relabel(req, add=[IN_PROGRESS_LABEL], remove=[READY_LABEL])
        return {
            "ok": True,
            "decision": "go",
            "run_id": run_id,
            "one_way_doors": len(verdict.one_way_doors),
        }
    if verdict.decision == "defer":
        post_comment(req, format_defer_comment(verdict))
        relabel(req, add=[DEFERRED_LABEL], remove=[READY_LABEL])
        return {"ok": True, "decision": "defer"}
    post_comment(req, format_decline_comment(verdict))
    relabel(req, add=[DECLINED_LABEL], remove=[READY_LABEL])
    return {"ok": True, "decision": "decline"}


def emit_request_received(req: TriageRequest, verdict: TriageVerdict) -> str:
    """Publish ``REQUEST.RECEIVED`` for a ``go`` verdict and return the run_id."""
    run_id = str(new_run_id())
    correlation_id = str(new_correlation_id())
    envelope = EventEnvelope[RequestReceived](
        event_id=new_event_id(),
        type="REQUEST.RECEIVED",
        run_id=run_id,  # ty: ignore[invalid-argument-type]
        correlation_id=correlation_id,  # ty: ignore[invalid-argument-type]
        actor_id="triage",
        payload=RequestReceived(
            project_slug=project_slug_from_repo(req.repo),
            intent=verdict.intent or req.title,
            requestor=req.user or "triage",
            requestor_sub=req.requestor_sub,
            target_repo=req.repo,
            source_issue_url=req.issue_url,
        ),
    )
    events_client().put_events(
        Entries=[
            {
                "Source": f"ai-dlc.{envelope.actor_id}",
                "DetailType": envelope.type,
                "Detail": envelope.model_dump_json(),
                "EventBusName": bus_name(),
            },
        ],
    )
    logger.info(
        "request received emitted",
        extra={"run_id": run_id, "issue_url": req.issue_url},
    )
    return run_id


def project_slug_from_repo(repo: str) -> str:
    """``owner/name`` → ``owner-name`` (lowercased). Stable across runs."""
    return repo.lower().replace("/", "-")


def post_comment(req: TriageRequest, body: str) -> None:
    """Invoke ``repo_helper.comment_issue`` synchronously."""
    invoke_repo_helper(
        {
            "op": "comment_issue",
            "repo": req.repo,
            "issue_number": req.issue_number,
            "body": body,
            "requestor_sub": req.requestor_sub,
        },
    )


def relabel(req: TriageRequest, *, add: list[str], remove: list[str]) -> None:
    """Add labels and best-effort remove others (remove is informational only).

    The label_issue op is additive; for v1 we only *add* outcome labels and
    leave the trigger label in place. The trigger filter on the webhook +
    cron checks that the outcome labels aren't present, which is sufficient
    for idempotency without a remove call. ``remove`` is here as a forward
    compatibility hint — wire it when we add ``unlabel_issue``.
    """
    del remove
    invoke_repo_helper(
        {
            "op": "label_issue",
            "repo": req.repo,
            "issue_number": req.issue_number,
            "labels": add,
            "requestor_sub": req.requestor_sub,
        },
    )


def invoke_repo_helper(payload: dict[str, Any]) -> dict[str, Any]:
    """Sync-invoke the repo_helper Lambda and return its parsed response."""
    response = lambda_client().invoke(
        FunctionName=repo_helper_function(),
        InvocationType="RequestResponse",
        Payload=json.dumps({"input": payload}).encode("utf-8"),
    )
    raw = response["Payload"].read()
    if not raw:
        return {}
    parsed = json.loads(raw)
    if isinstance(parsed, dict) and not parsed.get("ok", True):
        logger.warning("repo_helper op failed", extra={"response": parsed})
    return parsed if isinstance(parsed, dict) else {}


def format_go_comment(verdict: TriageVerdict, run_id: str) -> str:
    """Comment body for a ``go`` decision."""
    parts = [
        "🤖 Triage decision: **go**",
        "",
        f"Started run `{run_id}`. Reasoning:",
        "",
        verdict.reasoning,
    ]
    if verdict.one_way_doors:
        parts.append("")
        parts.append(format_one_way_doors_section(verdict.one_way_doors))
    return "\n".join(parts)


def format_defer_comment(verdict: TriageVerdict) -> str:
    """Comment body for a ``defer`` decision."""
    return "\n".join(
        [
            "🤖 Triage decision: **defer**",
            "",
            verdict.reasoning,
            "",
            "Re-add the `aidlc:ready` label once unblocked.",
        ],
    )


def format_decline_comment(verdict: TriageVerdict) -> str:
    """Comment body for a ``decline`` decision."""
    return "\n".join(
        [
            "🤖 Triage decision: **decline**",
            "",
            verdict.reasoning,
        ],
    )


def format_one_way_doors_section(doors: list[OneWayDoor]) -> str:
    """Render the one-way-door pre-flag block in the issue comment."""
    lines = ["**One-way door pre-flags** (the architect will revisit these):", ""]
    for door in doors:
        lines.append(f"- _{door.category}_ — {door.summary}")
    return "\n".join(lines)
