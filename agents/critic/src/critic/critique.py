"""Pydantic models + Markdown renderer for the Critic's adversarial review.

The Critic produces a :class:`Critique` whose Markdown rendering lands at:

  s3://{artifacts_bucket}/runs/{run_id}/critique.md

The critique sits alongside the architect's ``plan.md`` and is referenced
from the ``CRITIQUE.READY`` event payload via ``critique_s3_key``. The
implementer reads both the plan and the critique on the first
implementation pass and is instructed to address every high-severity
finding (or document why it deviated).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from common.templating import make_template_env
from common.validators import NoneSafeList

Severity = Literal["high", "medium", "low"]


class _Frozen(BaseModel):
    """Strict, frozen base for critique models."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class Issue(_Frozen):
    """One issue the Critic identified in the architect's plan.

    ``path``, ``symbol``, and ``line`` together anchor the issue to a
    concrete location. For most plan-level findings ``path`` will be
    ``runs/{run_id}/plan.md``; for findings that reference a repo file
    the architect named, ``path`` can be that file. ``symbol`` typically
    holds the plan section header (e.g. ``Approach``, ``Files to modify
    / create``, ``Implementation steps``).
    """

    severity: Severity
    path: Annotated[str, Field(min_length=1, max_length=256)]
    symbol: Annotated[str, Field(max_length=128)] | None = None
    line: Annotated[int, Field(ge=1)] | None = None
    description: Annotated[str, Field(min_length=1, max_length=1024)]
    recommendation: Annotated[str, Field(min_length=1, max_length=1024)]


class Critique(_Frozen):
    """The full adversarial review produced by the Critic per session.

    ``issues`` is required and non-empty: the Critic's job is to find
    at least one thing — a Critique with zero issues is treated as a
    model failure and surfaced to the agent as a Pydantic
    ``ValidationError``, which Strands' structured-output mode lets
    the agent self-correct.
    """

    run_id: Annotated[str, Field(min_length=1, max_length=64)]
    summary: Annotated[str, Field(min_length=1, max_length=2048)]
    issues: Annotated[NoneSafeList[Issue], Field(min_length=1, max_length=64)]
    strengths: NoneSafeList[str] = Field(default_factory=list)


def issue_anchor(issue: Issue) -> str:
    """Format ``path[:line] (symbol)`` for human-readable rendering."""
    anchor = f"{issue.path}:{issue.line}" if issue.line is not None else issue.path
    if issue.symbol:
        return f"{anchor} ({issue.symbol})"
    return anchor


def severity_counts(critique: Critique) -> dict[Severity, int]:
    """Count issues by severity. Missing severities map to zero."""
    counts: dict[Severity, int] = {"high": 0, "medium": 0, "low": 0}
    for issue in critique.issues:
        counts[issue.severity] += 1
    return counts


def render_critique(critique: Critique) -> str:
    """Render the critique as a Markdown document."""
    template = make_template_env(__package__).get_template("critique.md.j2")
    body = template.render(
        critique=critique,
        counts=severity_counts(critique),
        anchor=issue_anchor,
    )
    return body.rstrip() + "\n"
