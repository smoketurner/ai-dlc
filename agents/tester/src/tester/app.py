"""AgentCore Runtime entrypoint for the Tester.

Validates :class:`TesterInput`, dispatches the agent loop on a daemon
thread (under a copied :mod:`contextvars` context — see
:func:`common.gateway_tools.fetch_gateway_token`), and returns
``{"status": "dispatched", ...}`` so the state-router gets a fast
response. The daemon runs the agent, uploads the report via the
gateway, posts a summary comment on the PR, and emits
``TEST_REPORT.READY``. Tester is advisory; exceptions are logged and
swallowed.
"""

from __future__ import annotations

import contextvars
import re
import threading
from typing import Any

import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands.tools.mcp import MCPClient

from common.event_emit import publish
from common.events import EventEnvelope, TestReportReady
from common.gateway_tools import (
    ARTIFACT_TOOL,
    REPO_HELPER,
    call_gateway_tool,
    gateway_mcp_client,
)
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
from tester.tools import report_s3_key

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
    ctx = contextvars.copy_context()
    threading.Thread(
        target=ctx.run,
        args=(run_tester, payload, async_task_id),
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
        with gateway_mcp_client() as mcp_client:  # ty: ignore[invalid-context-manager]
            agent = build_agent(payload.run_id, mcp_client=mcp_client)
            report = analyze_gaps(
                agent,
                project_slug=payload.project_slug,
                plan_s3_key=payload.plan_s3_key,
                run_id=payload.run_id,
                pr_url=payload.pr_url,
                revision_number=payload.revision_number,
            )
            upload_report(
                mcp_client,
                report,
                run_id=payload.run_id,
                revision_number=payload.revision_number,
            )
            post_pr_comment(mcp_client, payload=payload, report=report)

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


def upload_report(
    mcp_client: MCPClient,
    report: Report,
    *,
    run_id: str,
    revision_number: int,
) -> None:
    """Render and upload the report Markdown via the artifact_tool gateway target."""
    call_gateway_tool(
        mcp_client,
        name=ARTIFACT_TOOL,
        arguments={
            "op": "put_artifact",
            "key": report_s3_key(run_id=run_id, revision_number=revision_number),
            "content": render_report(report),
        },
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


def post_pr_comment(
    mcp_client: MCPClient,
    *,
    payload: TesterInput,
    report: Report,
) -> None:
    """Best-effort summary comment on the PR via the repo_helper gateway target."""
    parsed = PR_URL_PATTERN.match(payload.pr_url)
    if parsed is None:
        logger.warning("could not parse pr_url for comment", pr_url=payload.pr_url)
        return
    body = render_report(report)
    try:
        call_gateway_tool(
            mcp_client,
            name=REPO_HELPER,
            arguments={
                "op": "comment_pr",
                "repo": parsed.group("repo"),
                "pr_number": int(parsed.group("num")),
                "body": body,
                "requestor_sub": payload.requestor_sub,
            },
        )
    except Exception as exc:
        logger.warning("comment_pr failed", err=str(exc), pr_url=payload.pr_url)


if __name__ == "__main__":
    app.run()
