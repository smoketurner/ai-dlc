"""AgentCore Runtime entrypoint for the Tester.

Serves ``POST /invocations`` and ``GET /ping`` on :8080. Step Functions
calls the runtime, which invokes the entrypoint defined here. The
entrypoint:

  1. Validates the input as :class:`TesterInput`.
  2. Asks the Strands agent for a :class:`Report`.
  3. Renders the report as Markdown and uploads it to S3.
  4. Posts a summary comment on the PR via ``repo_helper.comment_pr``,
     forwarding ``requestor_sub`` so the comment attributes to the
     requestor when their GitHub identity is linked.
  5. Returns a :class:`TesterResult` for the TEST_REPORT.READY event payload.
"""

from __future__ import annotations

import json
import os
import re
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from common.event_emit import publish
from common.events import EventEnvelope, TestReportReady
from common.ids import CorrelationId, RunId, new_event_id
from common.runtime import TesterInput, TesterResult, usage_from_strands
from common.task_token import heartbeat_loop, send_task_failure, send_task_success
from tester.agent import analyze_gaps, build_agent, model_id
from tester.report import Report, gap_count, render_report, suggestion_count
from tester.tools import write_report

if TYPE_CHECKING:
    from mypy_boto3_lambda.client import LambdaClient

logger = structlog.get_logger()
app = BedrockAgentCoreApp()

PR_URL_PATTERN = re.compile(r"^https://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/pull/(?P<num>\d+)$")


@app.entrypoint
async def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Tester entrypoint. Returns a JSON-serialisable TesterResult.

    See :mod:`common.task_token` for the ``task_token``/SendTaskSuccess
    callback pattern.
    """
    payload = TesterInput.model_validate(event)
    logger.info(
        "tester invoked",
        run_id=payload.run_id,
        task_id=payload.task_id,
        pr_url=payload.pr_url,
        async_token=payload.task_token is not None,
    )

    agent = build_agent(payload.run_id)
    try:
        with heartbeat_loop(payload.task_token):
            report = analyze_gaps(
                agent,
                project_slug=payload.project_slug,
                spec_slug=payload.spec_slug,
                task_id=payload.task_id,
                pr_url=payload.pr_url,
                diff_summary=payload.diff_summary,
            )
            upload_report(report, run_id=payload.run_id, task_id=payload.task_id)
            post_pr_comment(payload=payload, report=report)
    except BaseException as exc:
        if payload.task_token is not None:
            send_task_failure(task_token=payload.task_token, exc=exc)
            return {"task_token_dispatched": True, "ok": False}
        raise

    result = TesterResult(
        task_id=report.task_id,
        pr_url=payload.pr_url,
        gap_count=gap_count(report),
        suggested_test_count=suggestion_count(report),
        summary=report.summary[:2048],
        session_id=f"{payload.run_id}-{payload.task_id}-tester",
        **usage_from_strands(agent, model_id=model_id()),
    )
    logger.info(
        "test report ready",
        run_id=payload.run_id,
        task_id=payload.task_id,
        gap_count=result.gap_count,
        suggested_test_count=result.suggested_test_count,
    )
    output = result.model_dump()
    if payload.task_token is not None:
        send_task_success(task_token=payload.task_token, output=output)
        return {"task_token_dispatched": True, "ok": True}
    publish_test_report_ready(payload, result)
    return output


def upload_report(report: Report, *, run_id: str, task_id: str) -> None:
    """Render and upload the report Markdown to S3."""
    write_report(run_id=run_id, task_id=task_id, content=render_report(report))


def publish_test_report_ready(payload: TesterInput, result: TesterResult) -> None:
    """Emit TEST_REPORT.READY ourselves when invoked without a SF task_token.

    On the SFN-driven path the ``PublishTestReportReady`` ASL state emits
    this event after SendTaskSuccess. On the iteration_reactor path there's
    no SFN, so the agent must publish so the dashboard timeline + memory
    projector see the iteration's tester output.
    """
    envelope = EventEnvelope[TestReportReady](
        event_id=new_event_id(),
        type="TEST_REPORT.READY",
        run_id=RunId(payload.run_id),
        correlation_id=CorrelationId(payload.correlation_id),
        actor_id="tester",
        payload=TestReportReady(
            project_slug=payload.project_slug,
            spec_slug=payload.spec_slug,
            task_id=result.task_id,
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
    body = format_comment(report)
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


def format_comment(report: Report) -> str:
    """Render the Tester's PR comment body."""
    header = (
        f"### ai-dlc tester — **{gap_count(report)}** test gap(s) · "
        f"**{suggestion_count(report)}** suggested test(s)"
    )
    return f"{header}\n\n{report.summary}\n"


if __name__ == "__main__":
    app.run()
