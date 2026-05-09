"""AgentCore Runtime entrypoint for the Retrospector.

The dispatcher Lambda invokes this runtime once per terminal event
(``SPEC.APPROVED`` / ``SPEC.REJECTED`` / ``TASK.APPROVED`` /
``TASK.REJECTED`` / ``RUN.CANCEL_REQUESTED``). The entrypoint:

  1. Validates the input as :class:`RetrospectorInput`.
  2. Registers an async task with the AgentCore SDK so ``/ping``
     reports ``HealthyBusy`` while the synthesis runs.
  3. Spawns a daemon thread that asks the Strands agent for a
     :class:`RetrospectiveDecision`. If the decision proposes a
     MEMORY.md addition, opens a PR via ``repo_helper`` that appends
     the addition to ``docs/MEMORY.md`` on a fresh branch.
  4. Returns ``{"status": "dispatched", ...}`` to the caller in
     ~100ms.

Retrospectives never advance the run state machine — they're a
side-channel learner. Failures are logged and swallowed; the system
continues to function without retrospectives if the agent or
``repo_helper`` is unavailable.
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

from common.memory_md import MemoryDoc, Section, parse, render
from common.runtime import RetrospectorInput
from retrospector.agent import retrospect
from retrospector.decision import RetrospectiveDecision

if TYPE_CHECKING:
    from mypy_boto3_lambda.client import LambdaClient
    from mypy_boto3_s3.client import S3Client

logger = structlog.get_logger()
app = BedrockAgentCoreApp()

MEMORY_MD_PATH = "docs/MEMORY.md"
RUN_ID_BRANCH_RE = re.compile(r"[^a-z0-9-]+")


@cache
def lambda_client() -> LambdaClient:
    """Process-cached boto3 Lambda client (used for repo_helper invocation)."""
    return boto3.client("lambda")


@cache
def s3_client() -> S3Client:
    """Process-cached boto3 S3 client (reads the per-project MEMORY.md mirror)."""
    return boto3.client("s3")


def repo_helper_function_name() -> str:
    """Lambda function name of the ``repo_helper`` tool."""
    return os.environ["AIDLC_REPO_HELPER_FUNCTION_NAME"]


def memory_md_bucket() -> str:
    """S3 bucket name where the architect mirrors the per-project ``MEMORY.md``."""
    return os.environ["AIDLC_MEMORY_MD_BUCKET"]


@app.entrypoint
def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Validate the input, kick off background work, return immediately."""
    payload = RetrospectorInput.model_validate(event)
    logger.info(
        "retrospector invoked",
        run_id=payload.run_id,
        event_type=payload.event_type,
        target_repo=payload.target_repo,
    )
    task_id = app.add_async_task("retrospector_run", {"run_id": payload.run_id})
    threading.Thread(
        target=run_retrospective,
        args=(payload, task_id),
        daemon=True,
    ).start()
    return {"status": "dispatched", "run_id": payload.run_id, "task_id": task_id}


def run_retrospective(payload: RetrospectorInput, async_task_id: int) -> None:
    """Body of the retrospective — ask the agent, optionally open a MEMORY.md PR."""
    try:
        decision = retrospect(
            event_type=payload.event_type,
            project_slug=payload.project_slug,
            target_repo=payload.target_repo,
            run_id=payload.run_id,
            pr_url=payload.pr_url or None,
            issue_url=payload.issue_url or None,
            spec_slug=payload.spec_slug or None,
            task_id=payload.task_id or None,
            reviewer=payload.reviewer or None,
            reason=payload.reason or None,
        )
        if not decision.has_lesson:
            logger.info(
                "retrospective: no lesson",
                run_id=payload.run_id,
                event_type=payload.event_type,
                rationale=decision.rationale[:200],
            )
            return
        pr_url = open_memory_md_pr(payload=payload, decision=decision)
        logger.info(
            "retrospective: lesson recorded",
            run_id=payload.run_id,
            event_type=payload.event_type,
            confidence=decision.confidence,
            pr_url=pr_url,
        )
    except Exception:
        logger.exception("retrospective failed", run_id=payload.run_id)
    finally:
        app.complete_async_task(async_task_id)


def open_memory_md_pr(
    *,
    payload: RetrospectorInput,
    decision: RetrospectiveDecision,
) -> str:
    """Append ``decision.memory_md_addition`` to docs/MEMORY.md and open a PR.

    Reads the current MEMORY.md from the architect's S3 mirror, parses
    it against the strict six-section schema, appends the agent's
    addition under ``decision.section``, and renders the canonical
    Markdown back. The result is committed to a fresh branch and a PR
    is opened against ``main``.

    The S3 mirror lags ``main`` by one architect-sync cycle. A
    same-day duplicate addition is possible; the human reviewer
    catches it on the PR. Future work can swap the S3 read for a
    direct ``repo_helper.get_file`` once that op exists.

    Returns the PR URL.
    """
    if decision.section is None:
        msg = "decision.section must be set when opening a MEMORY.md PR"
        raise ValueError(msg)
    branch = branch_name(run_id=payload.run_id)
    invoke_repo_helper(
        op="create_branch",
        repo=payload.target_repo,
        branch=branch,
        base="main",
    )
    existing = fetch_memory_md(project_slug=payload.project_slug)
    new_content = render_memory_md_patch(
        existing=existing,
        section=decision.section,
        addition=decision.memory_md_addition,
    )
    invoke_repo_helper(
        op="commit_files",
        repo=payload.target_repo,
        branch=branch,
        message=render_commit_message(decision),
        files=[{"path": MEMORY_MD_PATH, "content": new_content}],
    )
    out = invoke_repo_helper(
        op="open_pr",
        repo=payload.target_repo,
        base="main",
        head=branch,
        title=render_pr_title(decision),
        body=render_pr_body(payload=payload, decision=decision),
    )
    pr_url = out.get("result", {}).get("pr_url")
    if not isinstance(pr_url, str):
        msg = f"open_pr did not return a pr_url: {out!r}"
        raise TypeError(msg)
    return pr_url


def fetch_memory_md(*, project_slug: str) -> str:
    """Read the current ``docs/MEMORY.md`` from the architect's S3 mirror.

    Returns the empty string when no mirror exists (e.g., the project
    has never had an architect run). In that case
    :func:`render_memory_md_patch` produces a fresh canonical body
    from the empty :class:`MemoryDoc` default.
    """
    key = f"projects/{project_slug}/MEMORY.md"
    try:
        obj = s3_client().get_object(Bucket=memory_md_bucket(), Key=key)
    except Exception:
        return ""
    return obj["Body"].read().decode("utf-8")


def render_memory_md_patch(*, existing: str, section: Section, addition: str) -> str:
    """Produce the new MEMORY.md content by appending under ``section``.

    Parses the existing body via :func:`common.memory_md.parse`,
    appends ``addition`` to the named section, and re-renders. When
    ``existing`` is empty (no mirror yet), starts from the default
    :class:`MemoryDoc` so the resulting file has all six canonical
    headers and the new bullet under the right one.
    """
    doc = parse(existing) if existing.strip() else MemoryDoc()
    return render(doc.with_appended(section, addition.strip()))


def render_commit_message(decision: RetrospectiveDecision) -> str:
    """Single-line commit message — uses the lesson summary."""
    summary = decision.lesson_summary.strip().splitlines()[0][:72]
    return f"retrospective: {summary}"


def render_pr_title(decision: RetrospectiveDecision) -> str:
    """PR title — ≤72 chars, leads with the agent identifier."""
    summary = decision.lesson_summary.strip().splitlines()[0]
    title = f"retrospective: {summary}"
    return title[:72]


def render_pr_body(
    *,
    payload: RetrospectorInput,
    decision: RetrospectiveDecision,
) -> str:
    """PR body — quotes the agent's rationale and links the source event."""
    parts = [
        f"Recorded a lesson from {payload.event_type} on this run.",
        "",
        "**Lesson:**",
        decision.lesson_summary,
        "",
        "**Rationale:**",
        decision.rationale,
        "",
        f"Confidence: {decision.confidence:.2f}",
    ]
    if payload.pr_url:
        parts.extend(["", f"Source PR: {payload.pr_url}"])
    if payload.issue_url:
        parts.extend(["", f"Source issue: {payload.issue_url}"])
    return "\n".join(parts) + "\n"


def branch_name(*, run_id: str) -> str:
    """Deterministic branch name per run — keeps re-fires idempotent on the remote."""
    safe = RUN_ID_BRANCH_RE.sub("-", run_id.lower())
    return f"retrospective/{safe}"


def invoke_repo_helper(*, op: str, **fields: Any) -> dict[str, Any]:
    """Invoke ``repo_helper`` with one op + raise on the standard envelope."""
    response = lambda_client().invoke(
        FunctionName=repo_helper_function_name(),
        InvocationType="RequestResponse",
        Payload=json.dumps({"input": {"op": op, **fields}}).encode("utf-8"),
    )
    body = json.loads(response["Payload"].read())
    if not body.get("ok"):
        msg = f"repo_helper.{op} failed: {body!r}"
        raise RuntimeError(msg)
    return body


if __name__ == "__main__":
    app.run()
