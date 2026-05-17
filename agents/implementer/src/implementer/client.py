"""Drives one Claude Agent SDK invocation for a single impl PR.

Two flows:

* ``mode="implementation"`` (default for the first dispatch per run):
  clone main, create the impl branch ``aidlc/impl/{run_id}``, download
  the architect's ``plan.md`` via the per-agent gateway, run Claude on
  the work, commit, push, open the unified impl PR via
  ``repo_helper.open_pr``, and emit ``IMPL_PR.OPENED``.

* ``mode="revision"``: clone, check out the existing impl branch,
  fetch the previous validator artifacts + any per-revision feedback
  (CI failures, human @aidlc-bot mentions, reviewer changes_requested
  reviews) via the gateway, run Claude on a unified prompt, commit +
  push directly to the impl branch (no PR open), and emit
  ``REVISION.READY``.

All GitHub API operations and S3 reads flow through the per-agent
AgentCore Gateway (MCP). Git operations on the working tree stay on
the container's ``git`` CLI — the agent loop iteratively commits.
"""

from __future__ import annotations

import os
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
import structlog
from claude_agent_sdk import ClaudeSDKClient, ResultMessage
from strands.tools.mcp import MCPClient

from common.gateway_tools import gateway_mcp_client
from common.memory import agent_memory_preamble, agent_skills_preamble
from common.runtime import (
    CiFailureFeedback,
    FeedbackItem,
    ImplementerInput,
    ImplementerResult,
    ImplementerRevisionResult,
    IssueCommentMentionFeedback,
    ReviewChangesRequestedFeedback,
    ReviewCommentMentionFeedback,
)
from common.templating import make_template_env
from implementer.finish import FinishReport, FinishSink
from implementer.options import build_options
from implementer.repo_ops import (
    call_artifact_tool,
    checkout_impl_branch,
    clone_repo,
    commit_changes,
    create_branch,
    fetch_plan,
    has_uncommitted_changes,
    impl_branch_name,
    invoke_repo_helper,
    make_session,
    parse_pr_number,
    post_inline_replies,
    push_branch,
    repo_made_real_changes,
    run_git,
    short_diff_summary,
)

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

logger = structlog.get_logger()


async def execute_implementation(payload: ImplementerInput) -> ImplementerResult:
    """First-pass flow: branch off main, agent edits, push, open PR.

    Reads ``plan.md`` and ``critique.md`` via the gateway into
    ``/workspace/spec/``, runs Claude on the issue, commits + pushes to
    ``aidlc/impl/{run_id}``, then opens the unified impl PR via the
    gateway's ``repo_helper`` target.
    """
    target_repo = resolve_target_repo(payload)
    session = make_session(target_repo=target_repo, requestor_sub=payload.requestor_sub)
    impl_branch = impl_branch_name(payload.run_id)
    logger.info(
        "implementer session opened",
        run_id=payload.run_id,
        target_repo=session.target_repo,
        impl_branch=impl_branch,
        on_behalf_of_user=session.on_behalf_of_user,
    )

    clone_repo(session)
    create_branch(impl_branch)

    with gateway_mcp_client() as mcp_client:  # ty: ignore[invalid-context-manager]
        fetch_plan(mcp_client, plan_s3_key=payload.plan_s3_key)

        user_prompt = compose_implementation_prompt(payload)
        report, usage = await drive_agent(user_prompt, run_id=payload.run_id)

        if report is not None and report.status == "blocked":
            msg = f"implementer blocked: {report.blocked_reason or 'agent reported blocked'}"
            raise RuntimeError(msg)
        if not repo_made_real_changes():
            msg = "implementer produced no diff — nothing to PR"
            raise RuntimeError(msg)

        commit_message = build_commit_message(payload.run_id, report=report)
        if has_uncommitted_changes():
            commit_changes(commit_message)
        push_branch(impl_branch)

        pr_url = open_impl_pr(
            mcp_client,
            payload,
            session_target_repo=session.target_repo,
            impl_branch=impl_branch,
            report=report,
        )

    return ImplementerResult(
        pr_url=pr_url,
        diff_summary=short_diff_summary()[:4096],
        session_id=payload.run_id,
        **usage,
    )


async def execute_revision(payload: ImplementerInput) -> ImplementerRevisionResult:
    """Revision flow: check out impl branch, apply aggregated feedback.

    Runs after validator feedback / CI failure / human mention triggered
    a revision pass. The implementer clones the repo, checks out the
    run's impl branch directly (no task branch — fixes land as commits
    on the impl branch itself), reads the prior-pass validation
    artifacts and the per-revision feedback items, composes a unified
    prompt, drives the agent, commits + pushes. The runtime emits
    ``REVISION.READY`` and the state-router fires the validators again
    on the updated diff.
    """
    target_repo = resolve_target_repo(payload)
    session = make_session(target_repo=target_repo, requestor_sub=payload.requestor_sub)
    impl_branch = impl_branch_name(payload.run_id)
    revision_number = max(payload.revision_number, 1)
    prior_revision = revision_number - 1
    logger.info(
        "implementer revision session opened",
        run_id=payload.run_id,
        revision_number=revision_number,
        target_repo=session.target_repo,
        impl_branch=impl_branch,
    )

    clone_repo(session)
    checkout_impl_branch(impl_branch)

    with gateway_mcp_client() as mcp_client:  # ty: ignore[invalid-context-manager]
        if payload.plan_s3_key:
            fetch_plan(mcp_client, plan_s3_key=payload.plan_s3_key)

        inputs = fetch_revision_inputs(
            mcp_client,
            run_id=payload.run_id,
            revision_number=prior_revision,
        )
        user_prompt = compose_revision_prompt(
            payload,
            revision_number=revision_number,
            inputs=inputs,
        )
        report, usage = await drive_agent(user_prompt, run_id=payload.run_id)

        if has_uncommitted_changes():
            commit_changes(
                f"revision r{revision_number}: address aggregated feedback",
            )
        push_branch(impl_branch)

        if report is not None and report.inline_replies and payload.pr_url is not None:
            post_inline_replies(
                mcp_client,
                repo=target_repo,
                pr_number=parse_pr_number(payload.pr_url),
                requestor_sub=payload.requestor_sub,
                replies=[(r.comment_id, r.body) for r in report.inline_replies],
            )

    pr_url = payload.pr_url or ""
    return ImplementerRevisionResult(
        pr_url=pr_url,
        diff_summary=short_diff_summary()[:4096],
        revision_number=revision_number,
        session_id=f"{payload.run_id}-revision-r{revision_number}",
        **usage,
    )


def fetch_revision_inputs(
    mcp_client: MCPClient,
    *,
    run_id: str,
    revision_number: int,
) -> dict[str, str]:
    """Read the three validator artifacts + any per-revision context via the gateway.

    Returns a dict with keys ``review`` / ``test_report`` / ``critique``
    (the three validator outputs from the prior pass) plus ``mention``
    and ``checks`` if per-revision context exists. Each key is
    best-effort — a missing artifact maps to an empty string rather
    than raising. The revision can still proceed with whichever
    sources are available.
    """
    inputs: dict[str, str] = {}
    sources = (
        ("review", f"runs/{run_id}/validation/review-r{revision_number}.md"),
        ("test_report", f"runs/{run_id}/validation/test_report-r{revision_number}.md"),
        ("critique", f"runs/{run_id}/validation/critique-r{revision_number}.md"),
        ("mention", f"runs/{run_id}/revision/r{revision_number + 1}-mention.md"),
        ("checks", f"runs/{run_id}/revision/r{revision_number + 1}-checks.md"),
    )
    for name, key in sources:
        try:
            envelope = call_artifact_tool(mcp_client, op="get_artifact", key=key)
        except Exception:
            inputs[name] = ""
            continue
        inputs[name] = str(envelope.get("result", {}).get("content", ""))
    return inputs


def compose_revision_prompt(
    payload: ImplementerInput,
    *,
    revision_number: int,
    inputs: dict[str, str],
) -> str:
    """Compose the user-message prompt for a revision pass."""
    parts = [
        agent_memory_preamble(project_slug=payload.project_slug, query=payload.run_id),
        agent_skills_preamble(),
        f"Project: {payload.project_slug}",
        f"Run id: {payload.run_id}",
        f"Impl PR: {payload.pr_url}",
        f"Revision number: {revision_number}",
        "",
        "You are working **directly on the impl branch** — no task branch. "
        "Apply the aggregated feedback below as fix commits on the impl "
        "branch. Keep changes minimal: address each finding precisely, "
        "no incidental refactors. After the fixes land, validators "
        "re-run on the integrated diff; if they still request changes "
        "the loop continues (capped at 3 automated revisions).",
        "",
        "## Reviewer findings (prior pass)",
        inputs.get("review", "") or "(none)",
        "",
        "## Tester findings (prior pass)",
        inputs.get("test_report", "") or "(none)",
        "",
        "## Code-critic findings (prior pass)",
        inputs.get("critique", "") or "(none)",
    ]
    if inputs.get("mention"):
        parts += ["", "## Human @aidlc-bot mention", inputs["mention"]]
    if inputs.get("checks"):
        parts += ["", "## CI failure context", inputs["checks"]]
    if payload.revision_feedback:
        parts += ["", "## Per-revision feedback items"]
        for item in payload.revision_feedback:
            parts.append(format_feedback_item(item))
    return "\n".join(parts)


async def drive_agent(
    user_prompt: str,
    *,
    run_id: str,
) -> tuple[FinishReport | None, dict[str, Any]]:
    """Run one ClaudeSDKClient session for the main implementer agent."""
    sink = FinishSink()
    options = build_options(run_id, finish_sink=sink)
    usage: dict[str, Any] = {"token_in": 0, "token_out": 0, "cost_usd": 0.0, "duration_ms": 0}
    async with ClaudeSDKClient(options=options) as client:
        await client.query(user_prompt)
        async for msg in client.receive_response():
            if isinstance(msg, ResultMessage):
                usage = extract_usage(msg)
                logger.info(
                    "session done",
                    session_id=msg.session_id,
                    cost_usd=usage["cost_usd"],
                    token_in=usage["token_in"],
                    token_out=usage["token_out"],
                    duration_ms=usage["duration_ms"],
                )
    return sink.report, usage


def extract_usage(msg: ResultMessage) -> dict[str, Any]:
    """Pull the four usage fields off a Claude Agent SDK ResultMessage."""
    raw = msg.usage or {}
    return {
        "token_in": int(raw.get("input_tokens", 0) or 0),
        "token_out": int(raw.get("output_tokens", 0) or 0),
        "cost_usd": float(msg.total_cost_usd or 0.0),
        "duration_ms": int(msg.duration_ms or 0),
    }


def resolve_target_repo(payload: ImplementerInput) -> str:
    """Pick the repo this run targets. Required field on ImplementerInput."""
    if payload.target_repo:
        return payload.target_repo
    msg = "ImplementerInput.target_repo is required but missing"
    raise RuntimeError(msg)


def compose_implementation_prompt(payload: ImplementerInput) -> str:
    """Compose the user message handed to Claude for the first-pass agent run."""
    parts = [
        agent_memory_preamble(project_slug=payload.project_slug, query=payload.run_id),
        agent_skills_preamble(),
        f"Project: {payload.project_slug}  (repo at /workspace/repo/)",
        f"Run id: {payload.run_id}",
    ]
    if payload.source_issue_url:
        parts.append(f"GitHub issue: {payload.source_issue_url}")
    if payload.plan_s3_key:
        parts.append(f"Plan S3 key: {payload.plan_s3_key}")
    parts += [
        "",
        "Read /workspace/spec/plan.md before you start. Treat its "
        "``Implementation steps`` section as your internal task list "
        "(use TodoWrite to track them). The plan's ``Assumptions`` "
        "section lists the architect's load-bearing judgment calls — "
        "if any feels wrong while implementing, surface it in your "
        "`finish` summary rather than silently working around it.",
        "",
        "Make the smallest set of edits that addresses the issue. Run "
        "lint/format/type/test before you stop. When done, call ``finish`` "
        "with a one-paragraph summary; the platform opens the PR.",
    ]
    return "\n".join(parts)


def build_commit_message(run_id: str, *, report: FinishReport | None) -> str:
    """One-line commit subject for the impl branch's commit."""
    if report and report.summary:
        first_line = report.summary.strip().splitlines()[0]
        return first_line[:80] or f"impl: run {run_id}"
    return f"impl: run {run_id}"


def open_impl_pr(
    mcp_client: MCPClient,
    payload: ImplementerInput,
    *,
    session_target_repo: str,
    impl_branch: str,
    report: FinishReport | None,
) -> str:
    """Open the unified impl PR via the gateway-routed ``repo_helper.open_pr``."""
    body = render_pr_body(
        report=report,
        run_id=payload.run_id,
        source_issue_url=payload.source_issue_url,
        source_issue_title=payload.source_issue_title,
        intent=payload.intent,
    )
    title = pr_title(
        report=report,
        source_issue_title=payload.source_issue_title,
        intent=payload.intent,
    )
    result = invoke_repo_helper(
        mcp_client,
        op="open_pr",
        requestor_sub=payload.requestor_sub,
        repo=session_target_repo,
        head=impl_branch,
        base="main",
        title=title,
        body=body,
    )
    pr_url = str(result.get("pr_url") or "")
    if not pr_url:
        msg = f"repo_helper.open_pr returned no pr_url: {result!r}"
        raise RuntimeError(msg)
    return pr_url


_TITLE_MAX = 200


def pr_title(
    *,
    report: FinishReport | None,
    source_issue_title: str | None,
    intent: str | None,
) -> str:
    """Build the PR title with no run-UUID fallback.

    Priority: source issue title → first line of the agent's finish summary →
    first line of the original intent. Each candidate is stripped, truncated
    to ``_TITLE_MAX`` chars, and the first non-empty one wins.
    """
    candidates: list[str | None] = [
        source_issue_title,
        report.summary if report and report.summary else None,
        intent,
    ]
    for candidate in candidates:
        if not candidate:
            continue
        first = candidate.strip().splitlines()[0].strip()
        if first:
            return first[:_TITLE_MAX]
    return "ai-dlc: automated changes"


def render_pr_body(
    *,
    report: FinishReport | None,
    run_id: str,
    source_issue_url: str | None,
    source_issue_title: str | None,
    intent: str | None,
) -> str:
    """Render the PR body from the finish report + run metadata."""
    template = make_template_env(__package__).get_template("pr_body.md.j2")
    summary = pr_body_summary(
        report=report,
        source_issue_title=source_issue_title,
        intent=intent,
    )
    body = template.render(
        summary=summary,
        report=report,
        run_id=run_id,
        source_issue_url=source_issue_url,
    )
    return body.rstrip() + "\n"


def pr_body_summary(
    *,
    report: FinishReport | None,
    source_issue_title: str | None,
    intent: str | None,
) -> str | None:
    """Pick the best available summary string for the PR body.

    Prefers the agent's own finish-report summary (a paragraph describing
    what the PR does), then the issue title, then the original intent.
    Returns ``None`` if nothing is available so the template can omit
    the section.
    """
    for candidate in (
        report.summary if report and report.summary else None,
        source_issue_title,
        intent,
    ):
        if not candidate:
            continue
        text = candidate.strip()
        if text:
            return text
    return None


def format_feedback_item(item: FeedbackItem) -> str:
    """Render one ``FeedbackItem`` into the revision prompt as a bullet."""
    if isinstance(item, CiFailureFeedback):
        return (
            f"- **CI failure** in workflow `{item.workflow_name}` "
            f"(conclusion: `{item.conclusion}`, logs: {item.html_url})"
        )
    if isinstance(item, ReviewChangesRequestedFeedback):
        body = item.body.strip() or "(no review body)"
        return f"- **Review requested changes** by @{item.reviewer}: {body}"
    if isinstance(item, ReviewCommentMentionFeedback):
        loc = f"`{item.path}`" + (f":{item.line}" if item.line else "")
        return (
            f"- **Inline comment** at {loc} from @{item.commenter} "
            f"(comment_id={item.comment_id}): {item.body.strip()}"
        )
    if isinstance(item, IssueCommentMentionFeedback):
        return (
            f"- **PR comment** from @{item.commenter} "
            f"(comment_id={item.comment_id}): {item.body.strip()}"
        )
    msg = f"unknown feedback kind: {item!r}"
    raise TypeError(msg)


def any_ci_failure_feedback(feedback: list[FeedbackItem] | None) -> bool:
    """``True`` when at least one item in ``feedback`` is a CI failure."""
    if not feedback:
        return False
    return any(isinstance(item, CiFailureFeedback) for item in feedback)


# ---------------------------------------------------------------------------
# Cancellation check — read STATE row, return True if cancelled/failed
# ---------------------------------------------------------------------------


@cache
def ddb_client() -> DynamoDBClient:
    """Process-cached DDB client (cancellation check)."""
    return boto3.client("dynamodb")


def runs_table_name() -> str | None:
    """Runs table name, ``None`` when not wired (local dev)."""
    return os.environ.get("AIDLC_RUNS_TABLE") or None


def run_cancelled(run_id: str) -> bool:
    """``True`` when the run's STATE row is in ``cancelled`` or ``failed``.

    Skips the check when ``AIDLC_RUNS_TABLE`` is unset (local dev /
    tests). A read failure is treated as not-cancelled — fail-open is
    safer here than blocking the PR open on a transient DDB error.
    """
    table = runs_table_name()
    if table is None:
        return False
    try:
        response = ddb_client().get_item(
            TableName=table,
            Key={"pk": {"S": f"RUN#{run_id}"}, "sk": {"S": "STATE"}},
            ProjectionExpression="current_state",
        )
    except Exception as exc:
        logger.warning("run_cancelled lookup failed", run_id=run_id, error=str(exc))
        return False
    state = (response.get("Item") or {}).get("current_state", {}).get("S", "")
    return state in {"cancelled", "failed"}


# Re-exported so app.py can call run_git directly if needed (kept for
# future use; not currently wired).
__all__ = [
    "any_ci_failure_feedback",
    "build_commit_message",
    "compose_implementation_prompt",
    "compose_revision_prompt",
    "drive_agent",
    "execute_implementation",
    "execute_revision",
    "extract_usage",
    "fetch_revision_inputs",
    "format_feedback_item",
    "open_impl_pr",
    "pr_title",
    "render_pr_body",
    "resolve_target_repo",
    "run_cancelled",
    "run_git",
]
