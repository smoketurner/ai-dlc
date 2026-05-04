"""Pydantic models + Markdown renderer for the Critic's adversarial review.

The Critic produces a :class:`Critique` whose Markdown rendering lands at:

  s3://{artifacts_bucket}/runs/{run_id}/critique.md

The critique sits alongside the spec bundle and is referenced from the
``CRITIQUE.READY`` event payload via ``critique_s3_key``. The HITL gate at
``WaitForSpecApproval`` includes the critique link so reviewers see it
before approving the spec.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["high", "medium", "low"]


class _Frozen(BaseModel):
    """Strict, frozen base for critique models."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class Issue(_Frozen):
    """One issue the Critic identified in the spec."""

    severity: Severity
    location: Annotated[str, Field(min_length=1, max_length=256)]
    description: Annotated[str, Field(min_length=1, max_length=1024)]
    recommendation: Annotated[str, Field(min_length=1, max_length=1024)]


class Critique(_Frozen):
    """The full adversarial review produced by the Critic per session."""

    spec_slug: Annotated[str, Field(min_length=1, max_length=128)]
    summary: Annotated[str, Field(min_length=1, max_length=2048)]
    issues: Annotated[list[Issue], Field(max_length=64)] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)


def severity_counts(critique: Critique) -> dict[Severity, int]:
    """Count issues by severity. Missing severities map to zero."""
    counts: dict[Severity, int] = {"high": 0, "medium": 0, "low": 0}
    for issue in critique.issues:
        counts[issue.severity] += 1
    return counts


def render_critique(critique: Critique) -> str:
    """Render the critique as a Markdown document."""
    counts = severity_counts(critique)
    lines = [
        f"# Critique — `{critique.spec_slug}`",
        "",
        f"> Issues: **{counts['high']}** high · **{counts['medium']}** medium · "
        f"**{counts['low']}** low",
        "",
        "## Summary",
        "",
        critique.summary,
        "",
    ]
    if critique.issues:
        lines += ["## Issues", ""]
        for ix, issue in enumerate(critique.issues, start=1):
            lines.append(f"### {ix}. [{issue.severity}] {issue.location}")
            lines.append("")
            lines.append(f"**Problem:** {issue.description}")
            lines.append("")
            lines.append(f"**Recommendation:** {issue.recommendation}")
            lines.append("")
    if critique.strengths:
        lines += ["## Strengths", ""]
        lines += [f"- {item}" for item in critique.strengths]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
