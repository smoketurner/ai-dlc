"""Pydantic models + Markdown renderer for the Reviewer's task-PR review.

The Reviewer produces a :class:`Review` whose Markdown rendering lands at:

  s3://{artifacts_bucket}/runs/{run_id}/tasks/{task_id}/review.md

Each :class:`ReviewComment` would become a PR comment via
``repo_helper.comment_pr`` once the helper Lambda's network calls land
(Phase 6). For Phase 10 v1 the rendered Markdown is the canonical artifact
and a single summary comment is attempted on the PR.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from common.validators import none_to_empty_list

Severity = Literal["high", "medium", "low"]
Verdict = Literal["approve", "request_changes", "comment"]


class _Frozen(BaseModel):
    """Strict, frozen base for review models."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ReviewComment(_Frozen):
    """One comment the Reviewer would post on the PR."""

    severity: Severity
    location: Annotated[str, Field(min_length=1, max_length=256)]
    description: Annotated[str, Field(min_length=1, max_length=1024)]
    suggestion: Annotated[str, Field(min_length=1, max_length=1024)]


class Review(_Frozen):
    """The full code review produced by the Reviewer per task PR."""

    task_id: Annotated[str, Field(min_length=1, max_length=32)]
    verdict: Verdict
    summary: Annotated[str, Field(min_length=1, max_length=2048)]
    comments: Annotated[
        list[ReviewComment],
        Field(max_length=64),
        BeforeValidator(none_to_empty_list),
    ] = Field(default_factory=list)
    strengths: Annotated[
        list[str],
        BeforeValidator(none_to_empty_list),
    ] = Field(default_factory=list)


def severity_counts(review: Review) -> dict[Severity, int]:
    """Count comments by severity. Missing severities map to zero."""
    counts: dict[Severity, int] = {"high": 0, "medium": 0, "low": 0}
    for comment in review.comments:
        counts[comment.severity] += 1
    return counts


def render_review(review: Review) -> str:
    """Render the review as a Markdown document."""
    counts = severity_counts(review)
    lines = [
        f"# Review — `{review.task_id}`",
        "",
        f"> Verdict: **{review.verdict}** · "
        f"{counts['high']} high · {counts['medium']} medium · {counts['low']} low",
        "",
        "## Summary",
        "",
        review.summary,
        "",
    ]
    if review.comments:
        lines += ["## Comments", ""]
        for ix, comment in enumerate(review.comments, start=1):
            lines.append(f"### {ix}. [{comment.severity}] `{comment.location}`")
            lines.append("")
            lines.append(f"**Issue:** {comment.description}")
            lines.append("")
            lines.append(f"**Suggestion:** {comment.suggestion}")
            lines.append("")
    if review.strengths:
        lines += ["## Strengths", ""]
        lines += [f"- {item}" for item in review.strengths]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
