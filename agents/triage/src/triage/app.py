"""AgentCore Runtime entrypoint for the Triage agent.

Serves ``POST /invocations`` and ``GET /ping`` on :8080. The webhook
handler (or the existing triage_dispatcher Lambda once it's rewired to
call this runtime) sends a ``TriageInput`` body. The entrypoint:

  1. Validates the input as :class:`TriageInput`.
  2. Calls :func:`triage_issue` to get a :class:`TriageDecision`.
  3. Uploads the decision as JSON to
     ``s3://{artifacts_bucket}/runs/{run_id}/triage.json`` so the
     dashboard and downstream Lambdas can read the full structured
     output.
  4. Returns a :class:`TriageResult` carrying the flattened fields the
     Step Functions ``Choice`` state branches on (``action``,
     ``workflow_kind``).
"""

from __future__ import annotations

import os
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from common.event_emit import publish
from common.events import EventEnvelope, IssueTriaged
from common.ids import CorrelationId, RunId, new_event_id
from common.runtime import TriageInput, TriageResult
from triage.agent import triage_issue

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

logger = structlog.get_logger()
app = BedrockAgentCoreApp()


@cache
def s3_client() -> S3Client:
    """Process-cached boto3 S3 client."""
    return boto3.client("s3")


def artifacts_bucket() -> str:
    """Bucket holding run artifacts (specs, ADRs, triage decisions)."""
    return os.environ["AIDLC_ARTIFACTS_BUCKET"]


def triage_decision_s3_key(run_id: str) -> str:
    """S3 key under the artifacts bucket for a run's triage decision."""
    return f"runs/{run_id}/triage.json"


def upload_decision(run_id: str, decision_json: str) -> str:
    """Upload the triage decision JSON to S3 and return its key."""
    bucket = artifacts_bucket()
    key = triage_decision_s3_key(run_id)
    s3_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=decision_json.encode("utf-8"),
        ContentType="application/json",
    )
    return key


@app.entrypoint
async def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Triage entrypoint. Returns a JSON-serialisable TriageResult."""
    payload = TriageInput.model_validate(event)
    logger.info(
        "triage invoked",
        run_id=payload.run_id,
        target_repo=payload.target_repo,
        issue_number=payload.issue_number,
        issue_type=payload.issue_type,
        prior_triage_count=payload.prior_triage_count,
    )

    decision = triage_issue(payload)
    decision_key = upload_decision(payload.run_id, decision.model_dump_json())

    result = TriageResult(
        decision_s3_key=decision_key,
        action=decision.action,
        workflow_kind=decision.workflow_kind,
        rationale=decision.rationale[:2048],
        missing_information_count=len(decision.missing_information),
        confidence=decision.confidence,
        session_id=payload.run_id,
    )
    logger.info(
        "triage decided",
        run_id=payload.run_id,
        action=result.action,
        workflow_kind=result.workflow_kind,
        missing_information=result.missing_information_count,
        confidence=result.confidence,
    )
    publish_issue_triaged(payload, result)
    return result.model_dump()


def publish_issue_triaged(payload: TriageInput, result: TriageResult) -> None:
    """Emit ISSUE.TRIAGED so the projector advances the run to ``triage_decided``."""
    envelope = EventEnvelope[IssueTriaged](
        event_id=new_event_id(),
        type="ISSUE.TRIAGED",
        run_id=RunId(payload.run_id),
        correlation_id=CorrelationId(payload.correlation_id),
        actor_id="triage",
        payload=IssueTriaged(
            project_slug=payload.project_slug,
            target_repo=payload.target_repo,
            issue_url=payload.issue_url,
            issue_number=payload.issue_number,
            action=result.action,
            workflow_kind=result.workflow_kind,
            decision_s3_key=result.decision_s3_key,
            rationale=result.rationale,
            confidence=result.confidence,
            session_id=result.session_id,
        ),
    )
    publish(envelope)


if __name__ == "__main__":
    app.run()
