"""Pydantic models + Markdown renderer for the Reviewer's impl-PR review.

The Reviewer produces a :class:`Review` whose Markdown rendering lands at:

  s3://{artifacts_bucket}/runs/{run_id}/validation/review-r{N}.md

where ``N`` is the revision number (0 for the first pass, 1+ after each
implementer revision). The rendered Markdown is the canonical artifact;
a single summary comment is attempted on the impl PR via
``repo_helper.comment_pr``.
"""

from __future__ import annotations

from functools import cache
from typing import Annotated, Literal

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape
from pydantic import BaseModel, ConfigDict, Field

from common.validators import NoneSafeList

Severity = Literal["high", "medium", "low"]
Verdict = Literal["approve", "request_changes", "comment"]


class _Frozen(BaseModel):
    """Strict, frozen base for review models."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ReviewSummary(_Frozen):
    """Structured top-of-review summary — bulleted in the rendered output.

    Mirrors the high-quality bug-report pattern: lead with what the diff
    does, then (if requesting changes) the bug, the actual-vs-expected
    behaviour, and the impact. ``issue`` and ``actual_vs_expected`` are
    optional so an ``approve`` review can omit them.
    """

    context: Annotated[str, Field(min_length=1, max_length=1024)]
    issue: Annotated[str, Field(max_length=1024)] | None = None
    actual_vs_expected: Annotated[str, Field(max_length=1024)] | None = None
    impact: Annotated[str, Field(min_length=1, max_length=1024)]


class ReviewComment(_Frozen):
    """One comment the Reviewer would post on the PR.

    ``path``, ``symbol``, and ``line`` together anchor the comment to a
    concrete code location — the dashboard renders ``path:line`` as a
    deep link to the file/diff and uses ``symbol`` as the function /
    class / test name. ``symbol`` and ``line`` are optional because not
    every comment can be pinned to a specific element (e.g., file-level
    notes).

    ``language`` + ``code_excerpt`` render the offending code as a
    fenced block; ``suggested_code`` renders the proposed fix as a
    second fenced block. ``references`` is a free-form list of
    citations — RFC numbers, doc URLs, "see services/X/y.py for the
    established pattern", etc.
    """

    severity: Severity
    path: Annotated[str, Field(min_length=1, max_length=256)]
    symbol: Annotated[str, Field(max_length=128)] | None = None
    line: Annotated[int, Field(ge=1)] | None = None
    description: Annotated[str, Field(min_length=1, max_length=1024)]
    suggestion: Annotated[str, Field(min_length=1, max_length=1024)]
    language: Annotated[str, Field(max_length=32)] | None = None
    code_excerpt: Annotated[str, Field(max_length=4096)] | None = None
    suggested_code: Annotated[str, Field(max_length=4096)] | None = None
    references: Annotated[NoneSafeList[str], Field(max_length=8)] = Field(
        default_factory=list,
    )


class Review(_Frozen):
    """The full code review produced by the Reviewer per impl-PR validation pass."""

    run_id: Annotated[str, Field(min_length=1, max_length=64)]
    verdict: Verdict
    summary: ReviewSummary
    comments: Annotated[NoneSafeList[ReviewComment], Field(max_length=64)] = Field(
        default_factory=list,
    )
    strengths: NoneSafeList[str] = Field(default_factory=list)


def comment_anchor(comment: ReviewComment) -> str:
    """Format ``path[:line] (symbol)`` for human-readable rendering."""
    anchor = f"{comment.path}:{comment.line}" if comment.line is not None else comment.path
    if comment.symbol:
        return f"{anchor} ({comment.symbol})"
    return anchor


def severity_counts(review: Review) -> dict[Severity, int]:
    """Count comments by severity. Missing severities map to zero."""
    counts: dict[Severity, int] = {"high": 0, "medium": 0, "low": 0}
    for comment in review.comments:
        counts[comment.severity] += 1
    return counts


@cache
def template_env() -> Environment:
    """Cached Jinja environment loading templates from ``reviewer/templates/``."""
    return Environment(
        loader=PackageLoader("reviewer", "templates"),
        autoescape=select_autoescape(disabled_extensions=("md", "j2"), default=False),
        undefined=StrictUndefined,
    )


def render_review(review: Review) -> str:
    """Render the review as Markdown — used for both the S3 artifact and PR comment."""
    template = template_env().get_template("review.md.j2")
    body = template.render(
        review=review,
        counts=severity_counts(review),
        anchor=comment_anchor,
    )
    return body.rstrip() + "\n"
