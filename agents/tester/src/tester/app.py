"""AgentCore Runtime entrypoint for the Tester.

Serves ``POST /invocations`` and ``GET /ping`` on :8080. Step Functions
calls the runtime, which invokes the entrypoint defined here. The
entrypoint:

  1. Validates the input as :class:`TesterInput`.
  2. Asks the Strands agent for a :class:`Report`.
  3. Renders the report as Markdown and uploads it to S3.
  4. Returns a :class:`TesterResult` for the TEST_REPORT.READY event payload.
"""

from __future__ import annotations

from typing import Any

import structlog
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from common.runtime import TesterInput, TesterResult
from tester.agent import analyze_gaps
from tester.report import Report, gap_count, render_report, suggestion_count
from tester.tools import write_report

logger = structlog.get_logger()
app = BedrockAgentCoreApp()


@app.entrypoint
async def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Tester entrypoint. Returns a JSON-serialisable TesterResult."""
    payload = TesterInput.model_validate(event)
    logger.info(
        "tester invoked",
        run_id=payload.run_id,
        task_id=payload.task_id,
        pr_url=payload.pr_url,
    )

    report = analyze_gaps(
        project_slug=payload.project_slug,
        spec_slug=payload.spec_slug,
        task_id=payload.task_id,
        pr_url=payload.pr_url,
        diff_summary=payload.diff_summary,
    )
    upload_report(report, run_id=payload.run_id, task_id=payload.task_id)

    result = TesterResult(
        task_id=report.task_id,
        pr_url=payload.pr_url,
        gap_count=gap_count(report),
        suggested_test_count=suggestion_count(report),
        summary=report.summary[:2048],
        session_id=f"{payload.run_id}-{payload.task_id}-tester",
    )
    logger.info(
        "test report ready",
        run_id=payload.run_id,
        task_id=payload.task_id,
        gap_count=result.gap_count,
        suggested_test_count=result.suggested_test_count,
    )
    return result.model_dump()


def upload_report(report: Report, *, run_id: str, task_id: str) -> None:
    """Render and upload the report Markdown to S3."""
    write_report(run_id=run_id, task_id=task_id, content=render_report(report))


if __name__ == "__main__":
    app.run()
