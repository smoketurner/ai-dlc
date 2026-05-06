"""Triage dispatcher Lambda — turns one GitHub issue into a routing decision.

Invocation paths:

* GitHub webhook (``issues.opened``, ``issues.assigned``,
  ``issues.labeled``, ``issue_comment.created`` with ``/aidlc go``) →
  dashboard webhook handler → this Lambda's sync invoke.
* EventBridge schedule (5-min cron backstop) → list ``aidlc:ready``
  issues per registered repo → this Lambda's sync invoke per matching
  issue.

For each invocation: invoke the dedicated :mod:`triage` agent runtime
(Strands + Haiku 4.5) with a typed :class:`TriageInput`, parse the
returned :class:`TriageDecision`, then act on the four-way action
verdict — ``proceed`` (emit ``REQUEST.RECEIVED``), ``ask`` (post the
agent's clarifying questions and wait for a human reply),
``defer`` / ``decline`` (comment + label the issue and stop).

The Lambda is intentionally not invoked by Step Functions and isn't
part of the SDLC state machine — Triage runs *before* a run exists.

The previous implementation embedded the classifier inline as a
Bedrock Converse call with a hand-written system prompt; that
classifier was replaced by the dedicated triage runtime so the
classification logic lives in one place (``agents/triage``) and is
unit-testable with the same harness as every other Strands agent.
"""

from __future__ import annotations

import json
import os
from functools import cache
from typing import TYPE_CHECKING, Any, Literal, cast

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.utilities.parser import ValidationError, parse
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError

from common.events import EventEnvelope, RequestReceived
from common.ids import new_correlation_id, new_event_id, new_run_id
from common.runtime import TriageInput, TriageResult
from common.triage import TriageDecision
from triage_dispatcher import synthesize
from triage_dispatcher.models import TriageRequest

if TYPE_CHECKING:
    from mypy_boto3_events.client import EventBridgeClient
    from mypy_boto3_lambda.client import LambdaClient
    from mypy_boto3_s3.client import S3Client

logger = Logger(service="triage_dispatcher")
tracer = Tracer(service="triage_dispatcher")
metrics = Metrics(namespace="ai-dlc", service="triage_dispatcher")

READY_LABEL = "aidlc:ready"
IN_PROGRESS_LABEL = "aidlc:in-progress"
DEFERRED_LABEL = "aidlc:deferred"
DECLINED_LABEL = "aidlc:declined"
AWAITING_RESPONSE_LABEL = "aidlc:awaiting-response"

# Issue-Type → workflow_kind hint passed through to the agent. The agent
# can override based on body content; this is just the prior.
ISSUE_TYPE_TO_HINT: dict[str, str] = {
    "Bug": "Bug",
    "Feature": "Feature",
    "Task": "Task",
}


@cache
def events_client() -> EventBridgeClient:
    """Process-cached EventBridge client."""
    return boto3.client("events", region_name=os.environ["AWS_REGION"])


@cache
def lambda_client() -> LambdaClient:
    """Process-cached Lambda client (for repo_helper sync invokes)."""
    return boto3.client("lambda", region_name=os.environ["AWS_REGION"])


@cache
def runtime_client() -> Any:
    """Process-cached bedrock-agentcore data-plane client."""
    return boto3.client("bedrock-agentcore", region_name=os.environ["AWS_REGION"])


@cache
def s3_client() -> S3Client:
    """Process-cached S3 client (reads the agent's persisted decision JSON)."""
    return boto3.client("s3")


def bus_name() -> str:
    """Platform EventBridge bus name."""
    return os.environ["AIDLC_BUS_NAME"]


def repo_helper_function() -> str:
    """ARN or name of the repo_helper Lambda this dispatcher invokes."""
    return os.environ["AIDLC_REPO_HELPER_FUNCTION_NAME"]


def triage_runtime_arn() -> str:
    """ARN of the triage AgentCore Runtime."""
    return os.environ["AIDLC_TRIAGE_RUNTIME_ARN"]


def artifacts_bucket() -> str:
    """S3 bucket holding the triage decision JSON."""
    return os.environ["AIDLC_ARTIFACTS_BUCKET"]


@logger.inject_lambda_context(log_event=False)
@tracer.capture_lambda_handler
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: LambdaContext) -> dict[str, Any]:
    """Triage one issue. Returns the decision envelope for caller logging."""
    try:
        req = parse(event=event, model=TriageRequest)
    except ValidationError as exc:
        logger.warning("invalid input", extra={"errors": exc.errors()})
        return {"ok": False, "error": "validation_error"}

    logger.info(
        "triage start",
        extra={"repo": req.repo, "issue": req.issue_number, "labels": req.labels},
    )

    run_id = str(new_run_id())
    correlation_id = str(new_correlation_id())
    payload = build_triage_input(req, run_id=run_id, correlation_id=correlation_id)

    try:
        decision = invoke_triage_runtime(payload, run_id=run_id)
    except (ClientError, ValidationError, json.JSONDecodeError, KeyError) as exc:
        logger.exception("runtime invocation failed", extra={"reason": str(exc)})
        return {"ok": False, "error": "triage_runtime_failed"}

    return apply(req, decision, run_id=run_id, correlation_id=correlation_id)


def build_triage_input(
    req: TriageRequest,
    *,
    run_id: str,
    correlation_id: str,
) -> TriageInput:
    """Translate the dispatcher's webhook input into the agent's contract."""
    issue_type = cast(
        'Literal["Bug", "Feature", "Task", "Other"] | None',
        ISSUE_TYPE_TO_HINT.get(req.issue_type or ""),
    )
    return TriageInput(
        project_slug=project_slug_from_repo(req.repo),
        target_repo=req.repo,
        issue_url=req.issue_url,
        issue_number=req.issue_number,
        issue_title=req.title,
        issue_body=req.body,
        issue_type=issue_type,
        issue_labels=list(req.labels),
        prior_triage_count=req.prior_triage_count,
        prior_human_comments=list(req.prior_human_comments),
        run_id=run_id,
        correlation_id=correlation_id,
        requestor_sub=req.requestor_sub,
    )


def invoke_triage_runtime(payload: TriageInput, *, run_id: str) -> TriageDecision:
    """Synchronously invoke the triage agent and return the parsed decision.

    The agent returns a flattened :class:`TriageResult`; the full
    :class:`TriageDecision` (including the ``ask`` action's clarifying
    questions) is persisted to S3 at ``decision_s3_key``. We fetch it
    from S3 because the agent's response shape only carries the counts
    and flat fields the Step Functions Choice state needs to branch on.
    """
    response = runtime_client().invoke_agent_runtime(
        agentRuntimeArn=triage_runtime_arn(),
        runtimeSessionId=runtime_session_id(run_id),
        contentType="application/json",
        accept="application/json",
        payload=json.dumps(payload.model_dump(mode="json")).encode("utf-8"),
    )
    body = response["response"].read()
    raw = json.loads(body)
    result = TriageResult.model_validate(raw)
    return read_decision(result.decision_s3_key)


def read_decision(s3_key: str) -> TriageDecision:
    """Read the agent's persisted ``TriageDecision`` JSON from S3."""
    obj = s3_client().get_object(Bucket=artifacts_bucket(), Key=s3_key)
    return TriageDecision.model_validate_json(obj["Body"].read())


def runtime_session_id(run_id: str) -> str:
    """Build a 33+ character runtime session id from the run id."""
    return f"triage-session-{run_id}"


def apply(
    req: TriageRequest,
    decision: TriageDecision,
    *,
    run_id: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Carry out the verdict — emit a run, comment + label, or both."""
    if decision.action == "proceed":
        synthetic_slug = maybe_upload_synthetic_spec(req, decision, run_id=run_id)
        emit_request_received(
            req,
            decision,
            run_id=run_id,
            correlation_id=correlation_id,
            synthetic_spec_slug=synthetic_slug,
        )
        post_comment(req, format_proceed_comment(decision, run_id))
        relabel(req, add=[IN_PROGRESS_LABEL])
        return {
            "ok": True,
            "decision": "proceed",
            "run_id": run_id,
            "workflow_kind": decision.workflow_kind,
            "synthetic_spec_slug": synthetic_slug,
        }
    if decision.action == "ask":
        post_comment(req, format_ask_comment(decision))
        relabel(req, add=[AWAITING_RESPONSE_LABEL])
        return {
            "ok": True,
            "decision": "ask",
            "question_count": len(decision.missing_information),
        }
    if decision.action == "defer":
        post_comment(req, format_defer_comment(decision))
        relabel(req, add=[DEFERRED_LABEL])
        return {"ok": True, "decision": "defer"}
    post_comment(req, format_decline_comment(decision))
    relabel(req, add=[DECLINED_LABEL])
    return {"ok": True, "decision": "decline"}


def emit_request_received(
    req: TriageRequest,
    decision: TriageDecision,
    *,
    run_id: str,
    correlation_id: str,
    synthetic_spec_slug: str | None,
) -> None:
    """Publish ``REQUEST.RECEIVED`` for a ``proceed`` verdict."""
    workflow_kind = decision.workflow_kind or "spec_driven"
    envelope = EventEnvelope[RequestReceived](
        event_id=new_event_id(),
        type="REQUEST.RECEIVED",
        run_id=run_id,  # ty: ignore[invalid-argument-type]
        correlation_id=correlation_id,  # ty: ignore[invalid-argument-type]
        actor_id="triage",
        # The agent's structured rationale stays in S3; intent is the
        # issue title (downstream agents can fetch the decision JSON).
        payload=RequestReceived(
            project_slug=project_slug_from_repo(req.repo),
            intent=req.title,
            requestor=req.user or "triage",
            requestor_sub=req.requestor_sub,
            target_repo=req.repo,
            source_issue_url=req.issue_url,
            workflow_kind=workflow_kind,
            synthetic_spec_slug=synthetic_spec_slug,
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
        extra={"run_id": run_id, "issue_url": req.issue_url, "kind": workflow_kind},
    )


def maybe_upload_synthetic_spec(
    req: TriageRequest,
    decision: TriageDecision,
    *,
    run_id: str,
) -> str | None:
    """Render + upload a 1-task synthetic spec for non-``spec_driven`` kinds.

    Returns the spec slug when uploaded, or ``None`` for ``spec_driven``
    (Architect produces the spec at runtime). The slug is the run id —
    keeping the convention global means S3 keys never collide and the
    ASL's ``LoadSyntheticSpec`` Pass state can derive the prefix
    deterministically without round-tripping the slug through the event.
    """
    kind = decision.workflow_kind
    if kind is None or kind == "spec_driven":
        return None
    slug = run_id
    docs = {
        "requirements": synthesize.render_requirements(
            issue_title=req.title,
            issue_body=req.body,
            issue_url=req.issue_url,
        ),
        "design": synthesize.render_design(kind=kind, issue_url=req.issue_url),
        "tasks": synthesize.render_tasks(kind=kind, issue_url=req.issue_url),
    }
    for name, body in docs.items():
        s3_client().put_object(
            Bucket=artifacts_bucket(),
            Key=f"specs/{slug}/{name}.md",
            Body=body.encode("utf-8"),
            ContentType="text/markdown",
        )
    logger.info("synthetic spec uploaded", extra={"run_id": run_id, "kind": kind, "slug": slug})
    return slug


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


def relabel(req: TriageRequest, *, add: list[str]) -> None:
    """Add labels via ``repo_helper.label_issue``.

    The label_issue op is additive; outcome labels are left in place
    after the run terminates so the next webhook delivery's filter
    skips already-handled issues.
    """
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


def format_proceed_comment(decision: TriageDecision, run_id: str) -> str:
    """Comment body for a ``proceed`` decision."""
    return "\n".join(
        [
            "Triage decision: **proceed**",
            "",
            f"Started run `{run_id}` (workflow: `{decision.workflow_kind}`).",
            "",
            decision.rationale,
        ],
    )


def format_ask_comment(decision: TriageDecision) -> str:
    """Comment body for an ``ask`` decision — list the questions inline."""
    parts = [
        "Triage decision: **ask**",
        "",
        decision.rationale,
        "",
        "I need a bit more information before I can act on this issue:",
        "",
    ]
    for ix, item in enumerate(decision.missing_information, start=1):
        parts.append(f"{ix}. **{item.question}**")
        parts.append(f"   _Why:_ {item.why_needed}")
        parts.append("")
    parts.append(
        "Reply on this issue with the answers and I'll re-triage. The "
        f"`{AWAITING_RESPONSE_LABEL}` label is on now; remove it to stop the loop.",
    )
    return "\n".join(parts)


def format_defer_comment(decision: TriageDecision) -> str:
    """Comment body for a ``defer`` decision."""
    return "\n".join(
        [
            "Triage decision: **defer**",
            "",
            decision.rationale,
            "",
            f"Re-add the `{READY_LABEL}` label once unblocked.",
        ],
    )


def format_decline_comment(decision: TriageDecision) -> str:
    """Comment body for a ``decline`` decision."""
    return "\n".join(
        [
            "Triage decision: **decline**",
            "",
            decision.rationale,
        ],
    )
