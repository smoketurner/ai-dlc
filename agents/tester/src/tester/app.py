"""AgentCore Runtime entrypoint for the Tester.

The state-router invokes the runtime once per impl-PR validation pass.
The entrypoint:

  1. Validates the input as :class:`TesterInput`.
  2. Registers an async task with the AgentCore SDK so ``/ping``
     reports ``HealthyBusy`` while the analysis runs.
  3. Spawns a daemon thread that runs the Strands agent, uploads the
     report to S3, posts a summary comment on the PR, and emits
     ``TEST_REPORT.READY``. On exception the thread logs and
     acknowledges the async task — Tester is advisory, so a crash
     doesn't advance any state machine.
  4. Returns ``{"status": "dispatched", ...}`` to the caller in
     ~100ms.
"""

from __future__ import annotations

import json
import os
import re
import threading
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from common.event_emit import publish
from common.events import EventEnvelope, TestReportReady
from common.ids import CorrelationId, RunId, new_event_id
from common.runtime import TesterInput, TesterResult, usage_from_strands
from tester.agent import analyze_gaps, build_agent, model_id
from tester.report import (
    Report,
    ReportSummary,
    gap_count,
    render_report,
    suggestion_count,
)
from tester.tools import write_report

if TYPE_CHECKING:
    from mypy_boto3_lambda.client import LambdaClient

logger = structlog.get_logger()
app = BedrockAgentCoreApp()

PR_URL_PATTERN = re.compile(r"^https://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/pull/(?P<num>\d+)$")


@app.entrypoint
def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Validate the input, kick off background work, return immediately."""
    payload = TesterInput.model_validate(event)
    logger.info(
        "tester invoked",
        run_id=payload.run_id,
        pr_url=payload.pr_url,
        revision_number=payload.revision_number,
    )
    async_task_id = app.add_async_task(
        "tester_run",
        {"run_id": payload.run_id, "revision_number": payload.revision_number},
    )
    threading.Thread(
        target=run_tester,
        args=(payload, async_task_id),
        daemon=True,
    ).start()
    return {
        "status": "dispatched",
        "run_id": payload.run_id,
        "async_task_id": async_task_id,
    }


def run_tester(payload: TesterInput, async_task_id: int) -> None:
    """Body of the tester run — analyzes the impl PR, posts comment, emits event.

    Tester is advisory; an exception is logged and swallowed. The
    run still gates on the reviewer's verdict, so a missing tester
    pass doesn't deadlock — the reviewer simply works without the
    gap analysis as input.
    """
    try:
        agent = build_agent(payload.run_id)
        report = analyze_gaps(
            agent,
            project_slug=payload.project_slug,
            plan_s3_key=payload.plan_s3_key,
            run_id=payload.run_id,
            pr_url=payload.pr_url,
            revision_number=payload.revision_number,
        )
        upload_report(report, run_id=payload.run_id, revision_number=payload.revision_number)
        post_pr_comment(payload=payload, report=report)

        result = TesterResult(
            pr_url=payload.pr_url,
            gap_count=gap_count(report),
            suggested_test_count=suggestion_count(report),
            summary=event_summary(report.summary),
            session_id=f"{payload.run_id}-tester-r{payload.revision_number}",
            **usage_from_strands(agent, model_id=model_id()),
        )
        logger.info(
            "test report ready",
            run_id=payload.run_id,
            gap_count=result.gap_count,
            suggested_test_count=result.suggested_test_count,
        )
        publish_test_report_ready(payload, result)
    except Exception:
        logger.exception(
            "tester run failed",
            run_id=payload.run_id,
        )
    finally:
        app.complete_async_task(async_task_id)


def upload_report(report: Report, *, run_id: str, revision_number: int) -> None:
    """Render and upload the report Markdown to S3."""
    write_report(
        run_id=run_id,
        revision_number=revision_number,
        content=render_report(report),
    )


def event_summary(summary: ReportSummary) -> str:
    """Flatten the structured summary to a single string for the TEST_REPORT.READY event."""
    return (
        f"Context: {summary.context} | Coverage gap: {summary.coverage_gap} | Risk: {summary.risk}"
    )[:2048]


def publish_test_report_ready(payload: TesterInput, result: TesterResult) -> None:
    """Emit TEST_REPORT.READY for the projector + dashboard."""
    envelope = EventEnvelope[TestReportReady](
        event_id=new_event_id(),
        type="TEST_REPORT.READY",
        run_id=RunId(payload.run_id),
        correlation_id=CorrelationId(payload.correlation_id),
        actor_id="tester",
        payload=TestReportReady(
            project_slug=payload.project_slug,
            pr_url=result.pr_url,
            gap_count=result.gap_count,
            suggested_test_count=result.suggested_test_count,
            summary=result.summary,
            session_id=result.session_id,
            token_in=result.token_in,
            token_out=result.token_out,
            cost_usd=result.cost_usd,
            duration_ms=result.duration_ms,
        ),
    )
    publish(envelope)


@cache
def lambda_client() -> LambdaClient:
    """Process-cached boto3 Lambda client for invoking ``repo_helper``."""
    return boto3.client("lambda")


def repo_helper_function_name() -> str | None:
    """Lambda function name for ``repo_helper`` — empty when not wired in this env."""
    return os.environ.get("AIDLC_REPO_HELPER_FUNCTION_NAME") or None


def post_pr_comment(*, payload: TesterInput, report: Report) -> None:
    """Best-effort summary comment on the PR. Never raises — advisory only."""
    fn = repo_helper_function_name()
    if fn is None:
        return
    parsed = PR_URL_PATTERN.match(payload.pr_url)
    if parsed is None:
        logger.warning("could not parse pr_url for comment", pr_url=payload.pr_url)
        return
    body = render_report(report)
    try:
        lambda_client().invoke(
            FunctionName=fn,
            InvocationType="RequestResponse",
            Payload=json.dumps(
                {
                    "input": {
                        "op": "comment_pr",
                        "repo": parsed.group("repo"),
                        "pr_number": int(parsed.group("num")),
                        "body": body,
                        "requestor_sub": payload.requestor_sub,
                    },
                },
            ).encode("utf-8"),
        )
    except Exception as exc:
        logger.warning("comment_pr failed", err=str(exc), pr_url=payload.pr_url)


if __name__ == "__main__":
    app.run()
