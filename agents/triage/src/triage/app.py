"""AgentCore Runtime entrypoint for the Triage agent.

Serves ``POST /invocations`` and ``GET /ping`` on :8080. The state-router
Lambda invokes this runtime when a run reaches ``triaging`` (an
issue-driven trigger arrived via the GitHub webhook). The entrypoint:

  1. Validates the input as :class:`TriageInput`.
  2. Registers an async task with the AgentCore SDK so ``/ping``
     reports ``HealthyBusy`` while the LLM call runs.
  3. Spawns a daemon thread that calls :func:`triage_issue`, uploads
     the decision JSON to S3, emits ``ISSUE.TRIAGED``, and
     acknowledges the async task.
  4. Returns ``{"status": "dispatched", ...}`` to the caller in
     ~100ms so the state-router doesn't sit in a long synchronous
     wait.
"""

from __future__ import annotations

import os
import threading
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from common.event_emit import publish
from common.events import EventEnvelope, IssueTriaged, RunFailed
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
def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Validate the input, kick off background work, return immediately."""
    payload = TriageInput.model_validate(event)
    logger.info(
        "triage invoked",
        run_id=payload.run_id,
        target_repo=payload.target_repo,
        issue_number=payload.issue_number,
        issue_type=payload.issue_type,
        prior_triage_count=payload.prior_triage_count,
    )
    task_id = app.add_async_task("triage_run", {"run_id": payload.run_id})
    threading.Thread(
        target=run_triage,
        args=(payload, task_id),
        daemon=True,
    ).start()
    return {"status": "dispatched", "run_id": payload.run_id, "task_id": task_id}


def run_triage(payload: TriageInput, task_id: int) -> None:
    """Body of the triage run — produces decision, emits event."""
    try:
        decision = triage_issue(payload)
        decision_key = upload_decision(payload.run_id, decision.model_dump_json())

        result = TriageResult(
            decision_s3_key=decision_key,
            action=decision.action,
            rationale=decision.rationale[:2048],
            missing_information_count=len(decision.missing_information),
            confidence=decision.confidence,
            session_id=payload.run_id,
        )
        logger.info(
            "triage decided",
            run_id=payload.run_id,
            action=result.action,
            missing_information=result.missing_information_count,
            confidence=result.confidence,
        )
        publish_issue_triaged(payload, result)
    except Exception as exc:
        logger.exception("triage run failed", run_id=payload.run_id)
        publish_run_failed(payload, exc)
    finally:
        app.complete_async_task(task_id)


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
            decision_s3_key=result.decision_s3_key,
            rationale=result.rationale,
            confidence=result.confidence,
            session_id=result.session_id,
        ),
    )
    publish(envelope)


def publish_run_failed(payload: TriageInput, exc: BaseException) -> None:
    """Emit RUN.FAILED so the projector terminates the run on agent crash."""
    envelope = EventEnvelope[RunFailed](
        event_id=new_event_id(),
        type="RUN.FAILED",
        run_id=RunId(payload.run_id),
        correlation_id=CorrelationId(payload.correlation_id),
        actor_id="triage",
        payload=RunFailed(
            project_slug=payload.project_slug,
            failed_state="triaging",
            error_class=type(exc).__name__,
            error_message=str(exc)[:1024],
            retryable=True,
        ),
    )
    publish(envelope)


if __name__ == "__main__":
    app.run()
